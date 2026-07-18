"""文件写事务的持久化与跨记录原子提交。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

import aiosqlite

from app.rmmz.schema import FontReplacementRecord, PluginSourceRuntimeWriteMapRecord

from .errors import RecoveryRequiredError
from .records import WriteTransactionFileRecord, WriteTransactionPayload, WriteTransactionRecord
from .rows import row_optional_str, row_str
from .session_base import SessionMixinBase
from .session_utils import current_timestamp_text
from .sql import (
    DELETE_ALL_FONT_REPLACEMENT_RECORDS,
    DELETE_ALL_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS,
    INSERT_FONT_REPLACEMENT_RECORD,
    INSERT_PLUGIN_SOURCE_RUNTIME_WRITE_MAP,
    INSERT_WRITE_TRANSACTION,
    SELECT_UNFINISHED_WRITE_TRANSACTION,
    SELECT_WRITE_TRANSACTION,
    UPDATE_WRITE_TRANSACTION_COMMITTED,
    UPDATE_WRITE_TRANSACTION_PREPARED,
)

type PersistedWriteTransactionState = Literal[
    "preparing",
    "prepared",
    "committed",
    "finalized",
    "rolled_back",
    "recovery_required",
]

UNFINISHED_WRITE_TRANSACTION_STATES: frozenset[PersistedWriteTransactionState] = frozenset(
    {"preparing", "prepared", "committed", "recovery_required"}
)


class WriteTransactionRecordSessionMixin(SessionMixinBase):
    """维护写事务，并在单个 SQLite 事务中提交其诊断记录。"""

    async def create_write_transaction(
        self,
        record: WriteTransactionRecord,
    ) -> None:
        """在文件暂存开始前保存 preparing 事务。"""
        if record.state != "preparing" or record.payload is not None:
            raise ValueError("新建写事务必须是 payload 为空的 preparing 状态")
        try:
            _ = await self.connection.execute(
                INSERT_WRITE_TRANSACTION,
                (
                    record.transaction_id,
                    record.operation,
                    str(record.game_path),
                    record.state,
                    str(record.journal_path),
                    None,
                    record.created_at,
                    record.updated_at,
                    record.error,
                ),
            )
            await self.connection.commit()
        except aiosqlite.IntegrityError as error:
            await self.connection.rollback()
            unfinished = await self.read_unfinished_write_transactions()
            if unfinished:
                existing = unfinished[0]
                raise _unfinished_write_transaction_error(existing) from error
            raise
        except BaseException:
            await self.connection.rollback()
            raise

    async def mark_write_transaction_prepared(
        self,
        transaction_id: str,
        payload: WriteTransactionPayload,
    ) -> None:
        """在全部暂存完成后原子保存恢复清单并进入 prepared。"""
        if payload.database_committed:
            raise ValueError("prepared 写事务的 database_committed 必须为 false")
        payload_json = _write_transaction_payload_json(payload)
        try:
            cursor = await self.connection.execute(
                UPDATE_WRITE_TRANSACTION_PREPARED,
                (payload_json, current_timestamp_text(), transaction_id),
            )
            if cursor.rowcount != 1:
                latest = await self.read_write_transaction(transaction_id)
                latest_state = latest.state if latest is not None else "missing"
                raise RuntimeError(f"写事务 {transaction_id} 状态为 {latest_state}，不能转换到 prepared")
            await self.connection.commit()
        except BaseException:
            await self.connection.rollback()
            raise

    async def read_write_transaction(self, transaction_id: str) -> WriteTransactionRecord | None:
        """按标识读取写事务。"""
        async with self.connection.execute(SELECT_WRITE_TRANSACTION, (transaction_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _write_transaction_from_row(row=row, db_path=self.db_path)

    async def read_unfinished_write_transactions(self) -> list[WriteTransactionRecord]:
        """读取会阻止后续修改命令的写事务。"""
        record = await read_unfinished_write_transaction(
            connection=self.connection,
            db_path=self.db_path,
        )
        return [] if record is None else [record]

    async def assert_no_unfinished_write_transaction(self) -> None:
        """阻止在未完成写事务之上继续修改游戏文件。"""
        unfinished = await self.read_unfinished_write_transactions()
        if not unfinished:
            return
        raise _unfinished_write_transaction_error(unfinished[0])

    async def mark_write_transaction_recovery_required(self, transaction_id: str, error: str) -> None:
        """记录自动回滚未完成，需要显式恢复。"""
        await self._transition_write_transaction(
            transaction_id=transaction_id,
            allowed_states={"preparing", "prepared", "committed", "recovery_required"},
            target_state="recovery_required",
            error=error,
        )

    async def mark_write_transaction_rolled_back(self, transaction_id: str, error: str = "") -> None:
        """记录文件已恢复到事务前状态。"""
        record = await self.read_write_transaction(transaction_id)
        if record is not None and record.payload is not None and record.payload.database_committed:
            raise RuntimeError(f"已提交写事务 {transaction_id} 不允许标记为 rolled_back")
        await self._transition_write_transaction(
            transaction_id=transaction_id,
            allowed_states={"preparing", "prepared", "recovery_required"},
            target_state="rolled_back",
            error=error,
        )

    async def mark_write_transaction_finalized(self, transaction_id: str) -> None:
        """记录已提交事务的 journal 和备份均已清理。"""
        record = await self.read_write_transaction(transaction_id)
        if record is None or record.payload is None or not record.payload.database_committed:
            raise RuntimeError(f"写事务 {transaction_id} 缺少可信的已提交 payload")
        await self._transition_write_transaction(
            transaction_id=transaction_id,
            allowed_states={"committed", "recovery_required"},
            target_state="finalized",
            error="",
        )

    async def finalize_write_transaction_commit(
        self,
        *,
        transaction_id: str,
        runtime_maps: Sequence[PluginSourceRuntimeWriteMapRecord] | None,
        font_records: Sequence[FontReplacementRecord] | None,
    ) -> None:
        """把 runtime map、字体记录和 committed 状态在同一事务中落盘。"""
        _ = await self.connection.execute("BEGIN IMMEDIATE")
        try:
            record = await self.read_write_transaction(transaction_id)
            if record is None:
                raise RuntimeError(f"写事务不存在: {transaction_id}")
            if record.state != "prepared":
                raise RuntimeError(f"写事务 {transaction_id} 状态 {record.state} 不允许提交")
            payload = record.payload
            if payload is None or payload.database_committed:
                raise RuntimeError(f"写事务 {transaction_id} 缺少可信的 prepared payload")

            if runtime_maps is not None:
                _ = await self.connection.execute(DELETE_ALL_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS)
                if runtime_maps:
                    _ = await self.connection.executemany(
                        INSERT_PLUGIN_SOURCE_RUNTIME_WRITE_MAP,
                        [_runtime_write_map_values(record) for record in runtime_maps],
                    )
            if font_records is not None:
                _ = await self.connection.execute(DELETE_ALL_FONT_REPLACEMENT_RECORDS)
                if font_records:
                    _ = await self.connection.executemany(
                        INSERT_FONT_REPLACEMENT_RECORD,
                        [_font_replacement_values(record) for record in font_records],
                    )
            committed_payload = replace(payload, database_committed=True)
            cursor = await self.connection.execute(
                UPDATE_WRITE_TRANSACTION_COMMITTED,
                (
                    _write_transaction_payload_json(committed_payload),
                    current_timestamp_text(),
                    transaction_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"写事务状态提交失败: {transaction_id}")
            await self.connection.commit()
        except BaseException:
            await self.connection.rollback()
            raise

    async def _transition_write_transaction(
        self,
        *,
        transaction_id: str,
        allowed_states: set[PersistedWriteTransactionState],
        target_state: PersistedWriteTransactionState,
        error: str,
    ) -> None:
        record = await self.read_write_transaction(transaction_id)
        if record is None:
            raise RuntimeError(f"写事务不存在: {transaction_id}")
        current_state = _parse_persisted_state(record.state)
        if current_state not in allowed_states:
            raise RuntimeError(f"写事务 {transaction_id} 不能从 {current_state} 转换到 {target_state}")
        ordered_allowed_states = sorted(allowed_states)
        allowed_placeholders = ", ".join("?" for _state in ordered_allowed_states)
        try:
            cursor = await self.connection.execute(
                f"""
                UPDATE write_transactions
                SET state = ?, updated_at = ?, error = ?
                WHERE transaction_id = ?
                  AND state IN ({allowed_placeholders})
                """,
                (
                    target_state,
                    current_timestamp_text(),
                    error,
                    transaction_id,
                    *ordered_allowed_states,
                ),
            )
        except BaseException:
            await self.connection.rollback()
            raise
        if cursor.rowcount != 1:
            await self.connection.rollback()
            latest = await self.read_write_transaction(transaction_id)
            latest_state = latest.state if latest is not None else "missing"
            raise RuntimeError(f"写事务 {transaction_id} 状态已并发变更为 {latest_state}，不能转换到 {target_state}")
        try:
            await self.connection.commit()
        except BaseException:
            await self.connection.rollback()
            raise


WriteTransactionSessionMixin = WriteTransactionRecordSessionMixin


async def read_unfinished_write_transaction(
    *,
    connection: aiosqlite.Connection,
    db_path: Path,
) -> WriteTransactionRecord | None:
    """从任意已校验连接读取唯一未完成写事务。"""
    async with connection.execute(SELECT_UNFINISHED_WRITE_TRANSACTION) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return _write_transaction_from_row(row=row, db_path=db_path)


async def assert_connection_has_no_unfinished_write_transaction(
    *,
    connection: aiosqlite.Connection,
    db_path: Path,
) -> None:
    """在注册等尚未建立 TargetGameSession 的入口复用写事务阻断。"""
    record = await read_unfinished_write_transaction(connection=connection, db_path=db_path)
    if record is not None:
        raise _unfinished_write_transaction_error(record)


def _unfinished_write_transaction_error(record: WriteTransactionRecord) -> RecoveryRequiredError:
    """把所有修改入口的事务阻断统一成稳定业务错误。"""
    return RecoveryRequiredError(
        (f"当前游戏存在未完成写事务 {record.transaction_id}（{record.state}），请先执行 recover-write-transaction"),
        transaction_id=record.transaction_id,
        state=record.state,
        details={
            "operation": record.operation,
            "game_path": str(record.game_path),
        },
    )


def _write_transaction_from_row(*, row: aiosqlite.Row, db_path: Path) -> WriteTransactionRecord:
    state = _parse_persisted_state(row_str(row, "state", db_path))
    payload_json = row_optional_str(row, "payload_json", db_path)
    payload = None if payload_json is None else _parse_write_transaction_payload(payload_json, db_path)
    if state == "preparing" and payload is not None:
        raise RuntimeError(f"preparing 写事务不得包含 payload: {db_path}")
    if state == "prepared" and (payload is None or payload.database_committed):
        raise RuntimeError(f"prepared 写事务缺少未提交 payload: {db_path}")
    if state in {"committed", "finalized"} and (payload is None or not payload.database_committed):
        raise RuntimeError(f"{state} 写事务缺少已提交 payload: {db_path}")
    return WriteTransactionRecord(
        transaction_id=row_str(row, "transaction_id", db_path),
        operation=row_str(row, "operation", db_path),
        game_path=Path(row_str(row, "game_path", db_path)).resolve(),
        state=state,
        journal_path=Path(row_str(row, "journal_path", db_path)).resolve(),
        payload=payload,
        created_at=row_str(row, "created_at", db_path),
        updated_at=row_str(row, "updated_at", db_path),
        error=row_str(row, "error", db_path),
    )


def _parse_persisted_state(value: str) -> PersistedWriteTransactionState:
    if value in {
        "preparing",
        "prepared",
        "committed",
        "finalized",
        "rolled_back",
        "recovery_required",
    }:
        return cast(PersistedWriteTransactionState, value)
    raise RuntimeError(f"数据库包含未知写事务状态: {value}")


def _write_transaction_payload_json(payload: WriteTransactionPayload) -> str:
    return json.dumps(
        {
            "version": payload.version,
            "database_committed": payload.database_committed,
            "files": [
                {
                    "target_relative_path": file_record.target_relative_path,
                    "staged_relative_path": file_record.staged_relative_path,
                    "backup_relative_path": file_record.backup_relative_path,
                    "existed_before": file_record.existed_before,
                    "original_sha256": file_record.original_sha256,
                    "target_sha256": file_record.target_sha256,
                }
                for file_record in payload.files
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_write_transaction_payload(raw_json: str, db_path: Path) -> WriteTransactionPayload:
    try:
        raw_payload = cast(object, json.loads(raw_json))
        payload = _strict_json_object(
            raw_payload,
            expected_keys={"version", "database_committed", "files"},
            field_name="write_transactions.payload_json",
        )
        raw_version = payload["version"]
        raw_database_committed = payload["database_committed"]
        raw_files = payload["files"]
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise TypeError("version 必须是整数")
        if raw_version != 1:
            raise ValueError("version 必须为 1")
        if not isinstance(raw_database_committed, bool):
            raise TypeError("database_committed 必须是布尔值")
        if not isinstance(raw_files, list):
            raise TypeError("files 必须是数组")
        file_records = tuple(
            _parse_write_transaction_file(raw_file, index)
            for index, raw_file in enumerate(cast(list[object], raw_files))
        )
        return WriteTransactionPayload(
            version=raw_version,
            database_committed=raw_database_committed,
            files=file_records,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"write_transactions.payload_json 非法: {db_path}；{error}") from error


def _parse_write_transaction_file(raw_file: object, index: int) -> WriteTransactionFileRecord:
    field_name = f"write_transactions.payload_json.files[{index}]"
    payload = _strict_json_object(
        raw_file,
        expected_keys={
            "target_relative_path",
            "staged_relative_path",
            "backup_relative_path",
            "existed_before",
            "original_sha256",
            "target_sha256",
        },
        field_name=field_name,
    )
    existed_before = payload["existed_before"]
    if not isinstance(existed_before, bool):
        raise TypeError(f"{field_name}.existed_before 必须是布尔值")
    return WriteTransactionFileRecord(
        target_relative_path=_required_string(payload["target_relative_path"], f"{field_name}.target_relative_path"),
        staged_relative_path=_required_string(payload["staged_relative_path"], f"{field_name}.staged_relative_path"),
        backup_relative_path=_optional_string(payload["backup_relative_path"], f"{field_name}.backup_relative_path"),
        existed_before=existed_before,
        original_sha256=_optional_string(payload["original_sha256"], f"{field_name}.original_sha256"),
        target_sha256=_required_string(payload["target_sha256"], f"{field_name}.target_sha256"),
    )


def _strict_json_object(
    value: object,
    *,
    expected_keys: set[str],
    field_name: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} 必须是对象")
    raw_object = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw_object):
        raise TypeError(f"{field_name} 的键必须是字符串")
    result = cast(dict[str, object], raw_object)
    if set(result) != expected_keys:
        raise ValueError(f"{field_name} 字段必须严格为 {sorted(expected_keys)}")
    return result


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _runtime_write_map_values(record: PluginSourceRuntimeWriteMapRecord) -> tuple[object, ...]:
    return (
        record.location_path,
        record.mapping_kind,
        record.source_file_name,
        record.source_selector,
        record.source_file_hash,
        record.source_text_hash,
        record.translation_lines_hash,
        record.runtime_file_name,
        record.runtime_selector,
        record.runtime_file_hash,
        record.runtime_text_hash,
        record.runtime_line,
        record.created_at,
    )


def _font_replacement_values(record: FontReplacementRecord) -> tuple[object, ...]:
    return (
        record.file_name,
        record.value_path,
        record.original_text,
        record.replaced_text,
        record.replacement_font_name,
    )


__all__ = [
    "PersistedWriteTransactionState",
    "UNFINISHED_WRITE_TRANSACTION_STATES",
    "WriteTransactionRecordSessionMixin",
    "WriteTransactionSessionMixin",
    "assert_connection_has_no_unfinished_write_transaction",
    "read_unfinished_write_transaction",
]
