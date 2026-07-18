"""翻译运行终态 SQLite 双重失败后的确定性恢复日志。"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

import aiosqlite

from app.rmmz.schema import LlmFailureRecord, TranslationRunRecord

from .errors import TranslationRunRecoveryRequiredError
from .session_utils import current_timestamp_text
from .sql import (
    DELETE_LLM_FAILURES_BY_RUN,
    SELECT_LLM_FAILURES_BY_RUN,
    SELECT_TRANSLATION_RUN,
    TRANSLATION_RUNS_TABLE_NAME,
)

_RECOVERY_FORMAT = "att-mz.translation-run-terminal-recovery"
_RECOVERY_VERSION = 2
_RECOVERY_SUFFIX = ".translation-run-recovery.json"
_MAX_RECOVERY_BYTES = 64 * 1024
_MAX_IDENTITY_TEXT_BYTES = 512
_MAX_FALLBACK_REASON_BYTES = 8192
_MAX_FALLBACK_ERROR_BYTES = 512
_DEFAULT_ERROR_SUMMARY_CHARS = 2048
_SQLITE_MAX_INT = (1 << 63) - 1
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_TERMINAL_STATUSES = {"completed", "blocked", "cancelled", "failed", "stopped"}
_RUN_FIELD_NAMES = frozenset(TranslationRunRecord.model_fields)
_STABLE_RUN_FIELDS = (
    "run_id",
    "status",
    "total_extracted",
    "pending_count",
    "deduplicated_count",
    "batch_count",
    "success_count",
    "quality_error_count",
    "llm_failure_count",
    "physical_request_count",
    "retry_request_count",
    "started_at",
    "finished_at",
    "stop_reason",
    "last_error",
)
_TEXT_RUN_FIELDS = frozenset({"run_id", "started_at", "finished_at", "stop_reason", "last_error"})
_COUNT_FIELDS = (
    "total_extracted",
    "pending_count",
    "deduplicated_count",
    "batch_count",
    "success_count",
    "quality_error_count",
    "llm_failure_count",
    "physical_request_count",
    "retry_request_count",
)
_PERSISTED_RESULT_COUNT_FIELDS = (
    "batch_count",
    "success_count",
    "quality_error_count",
)
_REQUEST_COUNT_FIELDS = (
    "physical_request_count",
    "retry_request_count",
)
_IMMUTABLE_FIELDS = (
    "run_id",
    "total_extracted",
    "pending_count",
    "deduplicated_count",
    "started_at",
)
_RECOVERY_KEYS = {
    "format",
    "version",
    "database_file",
    "game_id",
    "run_id",
    "expected_started_at",
    "created_at",
    "attempted_snapshot",
    "attempted_fingerprint",
    "attempted_failure_snapshot",
    "attempted_failure_fingerprint",
    "fallback_record",
    "fallback_fingerprint",
    "checksum_sha256",
}
_TEXT_MATERIAL_KEYS = {"chars", "utf8_bytes", "sha256"}
_FAILURE_FIELD_NAMES = frozenset(LlmFailureRecord.model_fields)
_FAILURE_TEXT_FIELDS = frozenset({"run_id", "error_type", "error_message", "created_at"})
_FAILURE_SNAPSHOT_FIELDS = (
    "run_id",
    "category",
    "error_type",
    "error_message",
    "retryable",
    "attempt_count",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class _FileEvidence:
    """一次受限读取绑定的文件身份和内容摘要。"""

    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TranslationRunRecoveryRecord:
    """绑定数据库、原终态尝试和唯一 fallback 的恢复意图。"""

    database_file: str
    game_id: str
    run_id: str
    expected_started_at: str
    created_at: str
    attempted_snapshot: dict[str, object]
    attempted_fingerprint: str
    attempted_failure_snapshot: dict[str, object] | None
    attempted_failure_fingerprint: str | None
    fallback_record: TranslationRunRecord
    fallback_fingerprint: str

    def payload_without_checksum(self) -> dict[str, object]:
        """返回参与校验和计算的严格版本化载荷。"""
        return {
            "format": _RECOVERY_FORMAT,
            "version": _RECOVERY_VERSION,
            "database_file": self.database_file,
            "game_id": self.game_id,
            "run_id": self.run_id,
            "expected_started_at": self.expected_started_at,
            "created_at": self.created_at,
            "attempted_snapshot": self.attempted_snapshot,
            "attempted_fingerprint": self.attempted_fingerprint,
            "attempted_failure_snapshot": self.attempted_failure_snapshot,
            "attempted_failure_fingerprint": self.attempted_failure_fingerprint,
            "fallback_record": self.fallback_record.model_dump(mode="json"),
            "fallback_fingerprint": self.fallback_fingerprint,
        }

    def to_bytes(self) -> bytes:
        """序列化为带内容校验和的规范 JSON。"""
        payload = self.payload_without_checksum()
        payload["checksum_sha256"] = _payload_checksum(payload)
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def translation_run_recovery_path(db_path: Path) -> Path:
    """返回与数据库同目录、但不会被当作数据库扫描的恢复日志路径。"""
    return db_path.with_name(f"{db_path.name}{_RECOVERY_SUFFIX}")


def has_translation_run_recovery(db_path: Path) -> bool:
    """判断数据库是否存在待协调的翻译终态恢复日志。"""
    path = translation_run_recovery_path(db_path)
    try:
        return _path_entry_exists(path)
    except OSError as error:
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason=f"无法检查翻译运行恢复日志：{type(error).__name__}: {error}",
        ) from error


def translation_run_stable_fingerprint(record: TranslationRunRecord) -> str:
    """计算忽略数据库自管 ``updated_at`` 的完整稳定运行指纹。"""
    snapshot = _stable_run_snapshot(record)
    return _snapshot_fingerprint(snapshot)


def llm_failure_stable_fingerprint(record: LlmFailureRecord) -> str:
    """计算模型故障行的完整稳定指纹。"""
    return _snapshot_fingerprint(_stable_failure_snapshot(record))


def build_bounded_persistence_failure_text(
    *errors: BaseException,
    max_chars: int = _DEFAULT_ERROR_SUMMARY_CHARS,
) -> str:
    """把持久化异常压缩为可入库、可写恢复日志的有界摘要。"""
    if not errors:
        raise ValueError("至少需要一个持久化异常")
    if isinstance(max_chars, bool) or max_chars < 256:
        raise ValueError("max_chars 必须是大于等于 256 的整数")
    parts: list[str] = []
    for error in errors:
        try:
            detail = str(error)
        except BaseException as stringify_error:
            detail = f"<异常文本不可读取: {type(stringify_error).__name__}>"
        parts.append(f"{type(error).__name__}: {detail}")
    full_text = "保存翻译运行终态失败: " + "；".join(parts)
    if len(full_text) <= max_chars:
        return full_text
    digest = hashlib.sha256(full_text.encode("utf-8", errors="replace")).hexdigest()
    suffix = f"…[完整错误 sha256={digest}]"
    prefix_length = max(max_chars - len(suffix), 1)
    return full_text[:prefix_length] + suffix


async def write_translation_run_recovery(
    *,
    db_path: Path,
    game_id: str,
    attempted_record: TranslationRunRecord,
    attempted_failure: LlmFailureRecord | None = None,
    fallback_record: TranslationRunRecord,
) -> Path:
    """以原子 no-clobber 方式写入一次性恢复日志。"""
    record = TranslationRunRecoveryRecord(
        database_file=db_path.name,
        game_id=game_id,
        run_id=attempted_record.run_id,
        expected_started_at=attempted_record.started_at,
        created_at=current_timestamp_text(),
        attempted_snapshot=_stable_run_snapshot(attempted_record),
        attempted_fingerprint=translation_run_stable_fingerprint(attempted_record),
        attempted_failure_snapshot=(None if attempted_failure is None else _stable_failure_snapshot(attempted_failure)),
        attempted_failure_fingerprint=(
            None if attempted_failure is None else llm_failure_stable_fingerprint(attempted_failure)
        ),
        fallback_record=fallback_record,
        fallback_fingerprint=translation_run_stable_fingerprint(fallback_record),
    )
    path = translation_run_recovery_path(db_path)
    try:
        _validate_recovery_record(record)
        content = record.to_bytes()
        if not content or len(content) > _MAX_RECOVERY_BYTES:
            raise ValueError(f"恢复日志序列化大小超出限制: {len(content)}")
        parsed = _parse_recovery_bytes(content)
        _validate_recovery_record(parsed)
        await asyncio.to_thread(_write_recovery_bytes_atomically, path, content)
    except TranslationRunRecoveryRequiredError:
        raise
    except BaseException as error:
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason=f"恢复日志无法安全写入：{type(error).__name__}: {error}",
            run_id=attempted_record.run_id,
        ) from error
    return path


async def reconcile_translation_run_recovery(
    *,
    connection: aiosqlite.Connection,
    db_path: Path,
    game_id: str,
) -> bool:
    """精确协调 sidecar 指定的 attempted/fallback 终态。"""
    path = translation_run_recovery_path(db_path)
    if not _path_entry_exists(path):
        return False
    try:
        record, evidence = await asyncio.to_thread(_read_recovery_record, path)
    except TranslationRunRecoveryRequiredError:
        raise
    except BaseException as error:
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason=f"恢复日志不可读取：{type(error).__name__}: {error}",
        ) from error
    if record.database_file != db_path.name:
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason="恢复日志绑定的数据库文件名与当前数据库不一致",
            run_id=record.run_id,
        )
    if record.game_id != game_id:
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason="恢复日志绑定的 game_id 与当前数据库不一致，数据库可能已被外部替换",
            run_id=record.run_id,
        )

    try:
        await connection.rollback()
        _ = await connection.execute("BEGIN IMMEDIATE")
        async with connection.execute(SELECT_TRANSLATION_RUN, (record.run_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise _recovery_error(
                db_path=db_path,
                path=path,
                reason="恢复日志对应的翻译运行不存在，数据库可能已被外部修改",
                run_id=record.run_id,
            )
        current = _decode_translation_run(row, db_path)
        if current.started_at != record.expected_started_at:
            raise _recovery_error(
                db_path=db_path,
                path=path,
                reason="翻译运行 started_at 与恢复日志不一致，拒绝覆盖外部修改",
                run_id=record.run_id,
            )
        failures = await _read_llm_failures(connection, record.run_id, db_path)
        current_fingerprint = translation_run_stable_fingerprint(current)
        if current_fingerprint == record.attempted_fingerprint:
            if not _attempted_failures_match(failures=failures, recovery=record):
                raise _recovery_error(
                    db_path=db_path,
                    path=path,
                    reason="已提交 attempted 终态的模型故障内容与恢复指纹不一致",
                    run_id=record.run_id,
                )
        elif current_fingerprint == record.fallback_fingerprint:
            if failures:
                raise _recovery_error(
                    db_path=db_path,
                    path=path,
                    reason="已提交 fallback 终态仍含模型故障行",
                    run_id=record.run_id,
                )
        elif current.status == "running":
            _assert_running_can_apply_fallback(
                current=current,
                failure_count=len(failures),
                recovery=record,
                db_path=db_path,
                path=path,
            )
            _ = await connection.execute(DELETE_LLM_FAILURES_BY_RUN, (record.run_id,))
            fallback = record.fallback_record
            update_cursor = await connection.execute(
                f"""
                UPDATE [{TRANSLATION_RUNS_TABLE_NAME}]
                SET status = ?,
                    total_extracted = ?,
                    pending_count = ?,
                    deduplicated_count = ?,
                    batch_count = ?,
                    success_count = ?,
                    quality_error_count = ?,
                    llm_failure_count = ?,
                    physical_request_count = ?,
                    retry_request_count = ?,
                    updated_at = ?,
                    finished_at = ?,
                    stop_reason = ?,
                    last_error = ?
                WHERE run_id = ? AND status = 'running' AND started_at = ?
                """,
                (
                    fallback.status,
                    fallback.total_extracted,
                    fallback.pending_count,
                    fallback.deduplicated_count,
                    fallback.batch_count,
                    fallback.success_count,
                    fallback.quality_error_count,
                    fallback.llm_failure_count,
                    fallback.physical_request_count,
                    fallback.retry_request_count,
                    current_timestamp_text(),
                    fallback.finished_at,
                    fallback.stop_reason,
                    fallback.last_error,
                    fallback.run_id,
                    fallback.started_at,
                ),
            )
            if update_cursor.rowcount != 1:
                raise _recovery_error(
                    db_path=db_path,
                    path=path,
                    reason="翻译运行在恢复事务中发生变化，拒绝覆盖",
                    run_id=record.run_id,
                )
        else:
            raise _recovery_error(
                db_path=db_path,
                path=path,
                reason="数据库终态既不匹配 attempted，也不匹配唯一 fallback",
                run_id=record.run_id,
            )
        await connection.commit()
    except BaseException as error:
        try:
            await connection.rollback()
        except BaseException as rollback_error:
            error.add_note(f"翻译运行恢复回滚也失败：{type(rollback_error).__name__}: {rollback_error}")
        if isinstance(error, TranslationRunRecoveryRequiredError):
            raise
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason=f"恢复事务失败：{type(error).__name__}: {error}",
            run_id=record.run_id,
        ) from error

    try:
        await asyncio.to_thread(_archive_recovery_if_unchanged, path, evidence)
    except BaseException as error:
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason=f"数据库已恢复，但恢复日志无法安全删除：{type(error).__name__}: {error}",
            run_id=record.run_id,
        ) from error
    return True


def _validate_recovery_record(record: TranslationRunRecoveryRecord) -> None:
    _validate_safe_basename(record.database_file)
    for field_name, value in (
        ("game_id", record.game_id),
        ("run_id", record.run_id),
        ("expected_started_at", record.expected_started_at),
        ("created_at", record.created_at),
    ):
        _validate_bounded_text(value, field_name, _MAX_IDENTITY_TEXT_BYTES, allow_empty=False)
    _validate_fingerprint(record.attempted_fingerprint, "attempted_fingerprint")
    _validate_fingerprint(record.fallback_fingerprint, "fallback_fingerprint")
    _validate_stable_snapshot(record.attempted_snapshot)
    if _snapshot_fingerprint(record.attempted_snapshot) != record.attempted_fingerprint:
        raise ValueError("attempted_snapshot 与 attempted_fingerprint 不一致")
    attempted_failure_count = _snapshot_int(record.attempted_snapshot, "llm_failure_count")
    if attempted_failure_count == 0:
        if record.attempted_failure_snapshot is not None or record.attempted_failure_fingerprint is not None:
            raise ValueError("无模型故障的 attempted 不得包含故障指纹")
    elif attempted_failure_count == 1:
        if record.attempted_failure_snapshot is None or record.attempted_failure_fingerprint is None:
            raise ValueError("含模型故障的 attempted 缺少完整故障指纹")
        _validate_failure_snapshot(record.attempted_failure_snapshot)
        if _snapshot_fingerprint(record.attempted_failure_snapshot) != record.attempted_failure_fingerprint:
            raise ValueError("attempted_failure_snapshot 与指纹不一致")
        failure_run_id = cast(dict[str, object], record.attempted_failure_snapshot["run_id"])
        if failure_run_id != record.attempted_snapshot["run_id"]:
            raise ValueError("attempted 模型故障与翻译运行 run_id 不一致")
    else:
        raise ValueError("attempted llm_failure_count 只能是 0 或 1")
    fallback = record.fallback_record
    _validate_terminal_record(fallback, label="fallback")
    if fallback.status != "failed" or fallback.last_error != "persistence_failed":
        raise ValueError("fallback 必须是 persistence_failed 终态")
    if fallback.llm_failure_count != 0:
        raise ValueError("fallback 不得承诺未原子保存的模型故障记录")
    if translation_run_stable_fingerprint(fallback) != record.fallback_fingerprint:
        raise ValueError("fallback_record 与 fallback_fingerprint 不一致")
    if fallback.run_id != record.run_id or fallback.started_at != record.expected_started_at:
        raise ValueError("fallback 身份与恢复日志不一致")
    fallback_snapshot = _stable_run_snapshot(fallback)
    for field in _IMMUTABLE_FIELDS:
        if fallback_snapshot[field] != record.attempted_snapshot[field]:
            raise ValueError(f"fallback 不可变字段 {field} 与 attempted 不一致")
    for field in _PERSISTED_RESULT_COUNT_FIELDS:
        if _snapshot_int(fallback_snapshot, field) > _snapshot_int(record.attempted_snapshot, field):
            raise ValueError(f"fallback 计数字段 {field} 超过 attempted")
    for field in _REQUEST_COUNT_FIELDS:
        if fallback_snapshot[field] != record.attempted_snapshot[field]:
            raise ValueError(f"fallback 请求计数字段 {field} 与 attempted 不一致")
    attempted_status = cast(str, record.attempted_snapshot["status"])
    if attempted_status not in _TERMINAL_STATUSES:
        raise ValueError("attempted 状态不是合法终态")
    attempted_finished_at = record.attempted_snapshot["finished_at"]
    if attempted_finished_at is None:
        raise ValueError("attempted 终态缺少 finished_at")
    _validate_bounded_text(fallback.stop_reason, "fallback.stop_reason", _MAX_FALLBACK_REASON_BYTES)
    _validate_bounded_text(fallback.last_error, "fallback.last_error", _MAX_FALLBACK_ERROR_BYTES)


def _validate_terminal_record(record: TranslationRunRecord, *, label: str) -> None:
    if record.status not in _TERMINAL_STATUSES or record.finished_at is None or not record.finished_at:
        raise ValueError(f"{label} 不是完整终态")
    _validate_run_counts(record, label=label)
    for field_name, value in (
        (f"{label}.run_id", record.run_id),
        (f"{label}.started_at", record.started_at),
        (f"{label}.updated_at", record.updated_at),
        (f"{label}.finished_at", record.finished_at),
    ):
        _validate_bounded_text(value, field_name, _MAX_IDENTITY_TEXT_BYTES, allow_empty=False)


def _validate_run_counts(record: TranslationRunRecord, *, label: str) -> None:
    values = (
        ("total_extracted", record.total_extracted),
        ("pending_count", record.pending_count),
        ("deduplicated_count", record.deduplicated_count),
        ("batch_count", record.batch_count),
        ("success_count", record.success_count),
        ("quality_error_count", record.quality_error_count),
        ("llm_failure_count", record.llm_failure_count),
        ("physical_request_count", record.physical_request_count),
        ("retry_request_count", record.retry_request_count),
    )
    for field, value in values:
        if isinstance(value, bool) or value < 0 or value > _SQLITE_MAX_INT:
            raise ValueError(f"{label}.{field} 必须是 SQLite 有符号整数范围内的非负整数")
    if record.retry_request_count > record.physical_request_count:
        raise ValueError(f"{label}.retry_request_count 不能超过 physical_request_count")


def _stable_run_snapshot(record: TranslationRunRecord) -> dict[str, object]:
    return {
        "run_id": _text_material(record.run_id),
        "status": record.status,
        "total_extracted": record.total_extracted,
        "pending_count": record.pending_count,
        "deduplicated_count": record.deduplicated_count,
        "batch_count": record.batch_count,
        "success_count": record.success_count,
        "quality_error_count": record.quality_error_count,
        "llm_failure_count": record.llm_failure_count,
        "physical_request_count": record.physical_request_count,
        "retry_request_count": record.retry_request_count,
        "started_at": _text_material(record.started_at),
        "finished_at": None if record.finished_at is None else _text_material(record.finished_at),
        "stop_reason": _text_material(record.stop_reason),
        "last_error": _text_material(record.last_error),
    }


def _stable_failure_snapshot(record: LlmFailureRecord) -> dict[str, object]:
    return {
        "run_id": _text_material(record.run_id),
        "category": record.category,
        "error_type": _text_material(record.error_type),
        "error_message": _text_material(record.error_message),
        "retryable": record.retryable,
        "attempt_count": record.attempt_count,
        "created_at": _text_material(record.created_at),
    }


def _text_material(value: str) -> dict[str, object]:
    encoded = value.encode("utf-8")
    return {
        "chars": len(value),
        "utf8_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _snapshot_fingerprint(snapshot: Mapping[str, object]) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_stable_snapshot(snapshot: Mapping[str, object]) -> None:
    if set(snapshot) != set(_STABLE_RUN_FIELDS):
        raise ValueError("stable snapshot 字段集合不正确")
    status = snapshot["status"]
    if not isinstance(status, str) or status not in _TERMINAL_STATUSES | {"running"}:
        raise ValueError("stable snapshot status 无效")
    for field in _COUNT_FIELDS:
        value = snapshot[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _SQLITE_MAX_INT:
            raise ValueError(f"stable snapshot 计数 {field} 无效")
    if _snapshot_int(snapshot, "retry_request_count") > _snapshot_int(snapshot, "physical_request_count"):
        raise ValueError("stable snapshot 重试请求数超过物理请求数")
    for field in _TEXT_RUN_FIELDS:
        value = snapshot[field]
        if field == "finished_at" and value is None:
            continue
        _validate_text_material(value, field)


def _validate_failure_snapshot(snapshot: Mapping[str, object]) -> None:
    if set(snapshot) != set(_FAILURE_SNAPSHOT_FIELDS):
        raise ValueError("attempted 模型故障 snapshot 字段集合不正确")
    category = snapshot["category"]
    if category not in {"rate_limit", "timeout", "connection", "server", "conflict", "fatal", "unknown"}:
        raise ValueError("attempted 模型故障 category 无效")
    retryable = snapshot["retryable"]
    if not isinstance(retryable, bool):
        raise ValueError("attempted 模型故障 retryable 无效")
    attempt_count = snapshot["attempt_count"]
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
        or attempt_count > _SQLITE_MAX_INT
    ):
        raise ValueError("attempted 模型故障 attempt_count 无效")
    for field in _FAILURE_TEXT_FIELDS:
        _validate_text_material(snapshot[field], f"attempted_failure.{field}")


def _validate_text_material(value: object, field: str) -> None:
    material = _object_mapping(value, label=f"stable snapshot 文本字段 {field}")
    if set(material) != _TEXT_MATERIAL_KEYS:
        raise ValueError(f"stable snapshot 文本字段 {field} 无效")
    for count_field in ("chars", "utf8_bytes"):
        count = material[count_field]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"stable snapshot 文本字段 {field}.{count_field} 无效")
    _validate_fingerprint(material["sha256"], f"{field}.sha256")


def _snapshot_int(snapshot: Mapping[str, object], field: str) -> int:
    return cast(int, snapshot[field])


def _validate_fingerprint(value: object, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} 不是小写 SHA-256")


def _validate_bounded_text(
    value: str,
    field: str,
    max_bytes: int,
    *,
    allow_empty: bool = True,
) -> None:
    if not allow_empty and not value:
        raise ValueError(f"{field} 必须是{'可为空' if allow_empty else '非空'}字符串")
    byte_count = len(value.encode("utf-8"))
    if byte_count > max_bytes:
        raise ValueError(f"{field} 超过 {max_bytes} 字节限制")


def _validate_safe_basename(value: str) -> None:
    _validate_bounded_text(value, "database_file", _MAX_IDENTITY_TEXT_BYTES, allow_empty=False)
    if value in {".", ".."} or "/" in value or "\\" in value or Path(value).name != value:
        raise ValueError("database_file 不是安全的单一文件名")


def _parse_recovery_bytes(content: bytes) -> TranslationRunRecoveryRecord:
    if not content or len(content) > _MAX_RECOVERY_BYTES:
        raise ValueError(f"恢复日志大小无效: {len(content)}")
    try:
        value = cast(object, json.loads(content.decode("utf-8"), object_pairs_hook=_strict_object))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"恢复日志不是合法的严格 JSON：{error}") from error
    payload = _object_mapping(value, label="恢复日志根节点")
    if set(payload) != _RECOVERY_KEYS:
        raise ValueError("恢复日志字段集合不符合版本 2 契约")
    version = payload["version"]
    if (
        payload["format"] != _RECOVERY_FORMAT
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version != _RECOVERY_VERSION
    ):
        raise ValueError("恢复日志格式或版本不受支持")
    checksum = _required_text(payload, "checksum_sha256")
    unsigned_payload = dict(payload)
    del unsigned_payload["checksum_sha256"]
    if checksum != _payload_checksum(unsigned_payload):
        raise ValueError("恢复日志校验和不匹配")
    attempted_snapshot = _object_mapping(payload["attempted_snapshot"], label="attempted_snapshot")
    attempted_failure_value = payload["attempted_failure_snapshot"]
    attempted_failure_snapshot = (
        None
        if attempted_failure_value is None
        else _object_mapping(attempted_failure_value, label="attempted_failure_snapshot")
    )
    attempted_failure_fingerprint_value = payload["attempted_failure_fingerprint"]
    if attempted_failure_fingerprint_value is not None and not isinstance(attempted_failure_fingerprint_value, str):
        raise ValueError("attempted_failure_fingerprint 必须是字符串或 null")
    fallback_value = _object_mapping(payload["fallback_record"], label="fallback_record")
    if set(fallback_value) != set(_RUN_FIELD_NAMES):
        raise ValueError("fallback_record 字段集合不符合 TranslationRunRecord 契约")
    fallback_record = TranslationRunRecord.model_validate(fallback_value, strict=True)
    record = TranslationRunRecoveryRecord(
        database_file=_required_text(payload, "database_file"),
        game_id=_required_text(payload, "game_id"),
        run_id=_required_text(payload, "run_id"),
        expected_started_at=_required_text(payload, "expected_started_at"),
        created_at=_required_text(payload, "created_at"),
        attempted_snapshot=attempted_snapshot,
        attempted_fingerprint=_required_text(payload, "attempted_fingerprint"),
        attempted_failure_snapshot=attempted_failure_snapshot,
        attempted_failure_fingerprint=attempted_failure_fingerprint_value,
        fallback_record=fallback_record,
        fallback_fingerprint=_required_text(payload, "fallback_fingerprint"),
    )
    _validate_recovery_record(record)
    return record


def _payload_checksum(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _required_text(payload: Mapping[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str) or not value:
        raise ValueError(f"恢复日志字段 {field} 必须是非空字符串")
    return value


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 字段: {key}")
        result[key] = value
    return result


def _object_mapping(value: object, *, label: str) -> dict[str, object]:
    """把 JSON 动态对象收窄为严格字符串键映射。"""
    if not isinstance(value, dict):
        raise ValueError(f"{label} 不是对象")
    raw_mapping = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} 含有非字符串字段名")
        result[key] = item
    return result


def _write_recovery_bytes_atomically(path: Path, content: bytes) -> None:
    if not path.parent.is_dir():
        raise FileNotFoundError(f"恢复日志父目录不存在: {path.parent}")
    if _path_entry_exists(path):
        raise FileExistsError(f"已存在待恢复日志，拒绝覆盖: {path}")
    if os.name != "nt":
        # v0.1.15 只发布 Windows；POSIX 使用 O_EXCL 直接落正式路径，
        # 崩溃时宁可留下不可解析证据并阻断，也不使用 link→unlink 双链接窗口。
        _write_exclusive_file(path, content)
        installed_record, installed_evidence = _read_recovery_record(path)
        if installed_record.to_bytes() != content or installed_evidence.sha256 != hashlib.sha256(content).hexdigest():
            raise OSError("恢复日志写入后回读不一致")
        _fsync_directory_best_effort(path.parent)
        return

    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _write_exclusive_file(temporary_path, content)
    temporary_record, temporary_evidence = _read_recovery_record(temporary_path)
    if temporary_record.to_bytes() != content or temporary_evidence.sha256 != hashlib.sha256(content).hexdigest():
        raise OSError("恢复日志临时文件回读不一致")
    _rename_no_replace(temporary_path, path)
    installed_record, installed_evidence = _read_recovery_record(path)
    if installed_record.to_bytes() != content or installed_evidence.sha256 != temporary_evidence.sha256:
        raise OSError("恢复日志安装后回读不一致")
    _fsync_directory_best_effort(path.parent)


def _write_exclusive_file(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            _write_all(stream, content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_all(stream: BinaryIO, content: bytes) -> None:
    written = stream.write(content)
    if written != len(content):
        raise OSError(f"恢复日志写入不完整: {written}/{len(content)}")


def _read_recovery_record(path: Path) -> tuple[TranslationRunRecoveryRecord, _FileEvidence]:
    content, evidence = _read_bounded_regular_file(path)
    return _parse_recovery_bytes(content), evidence


def _read_bounded_regular_file(path: Path) -> tuple[bytes, _FileEvidence]:
    before = path.lstat()
    _assert_safe_regular_file(before)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if isinstance(no_follow, int):
        flags |= no_follow
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _assert_safe_regular_file(opened)
        _assert_same_file_identity(before, opened)
        chunks: list[bytes] = []
        remaining = _MAX_RECOVERY_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > _MAX_RECOVERY_BYTES:
            raise ValueError("恢复日志超过 64 KiB 限制")
        after = os.fstat(descriptor)
        _assert_safe_regular_file(after)
        _assert_unchanged_stat(opened, after)
    finally:
        os.close(descriptor)
    current = path.lstat()
    _assert_safe_regular_file(current)
    _assert_unchanged_stat(opened, current)
    if len(content) != current.st_size:
        raise RuntimeError("恢复日志读取期间大小发生变化")
    return content, _file_evidence(current, content)


def _assert_safe_regular_file(file_stat: os.stat_result) -> None:
    attributes = cast(int, getattr(file_stat, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RuntimeError("恢复日志不是单链接、非 reparse point 的普通文件")
    if file_stat.st_size <= 0 or file_stat.st_size > _MAX_RECOVERY_BYTES:
        raise ValueError(f"恢复日志大小无效: {file_stat.st_size}")


def _assert_same_file_identity(left: os.stat_result, right: os.stat_result) -> None:
    if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
        raise RuntimeError("恢复日志文件身份在打开期间发生变化")


def _assert_unchanged_stat(left: os.stat_result, right: os.stat_result) -> None:
    _assert_same_file_identity(left, right)
    if left.st_size != right.st_size or left.st_mtime_ns != right.st_mtime_ns:
        raise RuntimeError("恢复日志内容或属性在操作期间发生变化")


def _file_evidence(file_stat: os.stat_result, content: bytes) -> _FileEvidence:
    return _FileEvidence(
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        modified_ns=file_stat.st_mtime_ns,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _archive_recovery_if_unchanged(path: Path, expected: _FileEvidence) -> None:
    """把已协调证据移出活动路径；不再按路径删除，避免 unlink 竞态。"""
    content, current = _read_bounded_regular_file(path)
    if current != expected or hashlib.sha256(content).hexdigest() != expected.sha256:
        raise RuntimeError("恢复日志在数据库提交后发生变化，拒绝删除")

    quarantine_path = path.with_name(f".{path.name}.{uuid4().hex}.resolved")
    if _path_entry_exists(quarantine_path):
        raise FileExistsError(f"恢复日志隔离路径意外存在: {quarantine_path}")
    _rename_no_replace(path, quarantine_path)
    try:
        moved_content, moved = _read_bounded_regular_file(quarantine_path)
    except BaseException:
        _restore_quarantined_evidence(path=path, quarantine_path=quarantine_path)
        raise
    if moved != expected or hashlib.sha256(moved_content).hexdigest() != expected.sha256:
        _restore_quarantined_evidence(path=path, quarantine_path=quarantine_path)
        raise RuntimeError("恢复日志在归档时被替换，已保留证据并拒绝完成")
    if _path_entry_exists(path):
        raise RuntimeError("恢复日志归档后出现新的同名证据，拒绝报告清理成功")
    _fsync_directory_best_effort(path.parent)


def _restore_quarantined_evidence(*, path: Path, quarantine_path: Path) -> None:
    """隔离后的文件身份不符时，尽力把证据恢复到原路径。"""
    if not _path_entry_exists(quarantine_path) or _path_entry_exists(path):
        return
    try:
        _rename_no_replace(quarantine_path, path)
    except OSError:
        # 恢复失败时保留随机名证据，不再执行任何删除。
        return


def _rename_no_replace(source: Path, target: Path) -> None:
    """以平台原子 no-replace 语义移动单个证据文件。"""
    if os.name == "nt":
        os.rename(source, target)
        return
    if os.name != "posix":
        raise OSError(errno.ENOTSUP, "当前平台不支持原子 no-replace rename", str(target))
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = library.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "当前 libc 不提供 renameat2", str(target)) from error
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = cast(
        int,
        renameat2(
            at_fdcwd,
            os.fsencode(source),
            at_fdcwd,
            os.fsencode(target),
            rename_noreplace,
        ),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(target))


def _decode_translation_run(row: aiosqlite.Row, db_path: Path) -> TranslationRunRecord:
    values = {field: cast(object, row[field]) for field in TranslationRunRecord.model_fields}
    try:
        record = TranslationRunRecord.model_validate(values, strict=True)
    except Exception as error:
        raise RuntimeError(f"数据库翻译运行记录不符合当前契约: {db_path}") from error
    _validate_run_counts(record, label="database")
    return record


async def _read_llm_failures(
    connection: aiosqlite.Connection,
    run_id: str,
    db_path: Path,
) -> tuple[LlmFailureRecord, ...]:
    async with connection.execute(SELECT_LLM_FAILURES_BY_RUN, (run_id,)) as cursor:
        rows = await cursor.fetchall()
    failures: list[LlmFailureRecord] = []
    for row in rows:
        retryable_value = cast(object, row["retryable"])
        if not isinstance(retryable_value, int) or isinstance(retryable_value, bool) or retryable_value not in {0, 1}:
            raise RuntimeError(f"数据库模型故障 retryable 字段无效: {db_path}")
        values = {field: cast(object, row[field]) for field in _FAILURE_FIELD_NAMES}
        values["retryable"] = bool(retryable_value)
        try:
            failures.append(LlmFailureRecord.model_validate(values, strict=True))
        except Exception as error:
            raise RuntimeError(f"数据库模型故障记录不符合当前契约: {db_path}") from error
    return tuple(failures)


def _attempted_failures_match(
    *,
    failures: tuple[LlmFailureRecord, ...],
    recovery: TranslationRunRecoveryRecord,
) -> bool:
    expected_fingerprint = recovery.attempted_failure_fingerprint
    if expected_fingerprint is None:
        return not failures
    return len(failures) == 1 and llm_failure_stable_fingerprint(failures[0]) == expected_fingerprint


def _assert_running_can_apply_fallback(
    *,
    current: TranslationRunRecord,
    failure_count: int,
    recovery: TranslationRunRecoveryRecord,
    db_path: Path,
    path: Path,
) -> None:
    try:
        _validate_run_counts(current, label="running")
        if (
            current.finished_at is not None
            or current.llm_failure_count != 0
            or current.stop_reason
            or current.last_error
            or failure_count != 0
        ):
            raise ValueError("running 记录含有终态字段或模型故障行")
        current_snapshot = _stable_run_snapshot(current)
        for field in _IMMUTABLE_FIELDS:
            if current_snapshot[field] != recovery.attempted_snapshot[field]:
                raise ValueError(f"running 不可变字段 {field} 与恢复意图不一致")
        fallback = recovery.fallback_record
        if current.batch_count > fallback.batch_count:
            raise ValueError("fallback 会降低已确认的 batch_count")
        for field in ("success_count", "quality_error_count"):
            if getattr(current, field) != getattr(fallback, field):
                raise ValueError(f"fallback 不能改变缺少对应持久化行的计数 {field}")
        if current.physical_request_count > fallback.physical_request_count:
            raise ValueError("fallback 会降低已提交 physical_request_count")
        if current.retry_request_count > fallback.retry_request_count:
            raise ValueError("fallback 会降低已提交 retry_request_count")
        physical_delta = fallback.physical_request_count - current.physical_request_count
        retry_delta = fallback.retry_request_count - current.retry_request_count
        if retry_delta > physical_delta:
            raise ValueError("fallback 的新增重试请求数超过新增物理请求数")
    except ValueError as error:
        raise _recovery_error(
            db_path=db_path,
            path=path,
            reason=str(error),
            run_id=recovery.run_id,
        ) from error


def _fsync_directory_best_effort(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _path_entry_exists(path: Path) -> bool:
    """把普通文件、链接和损坏链接都视为已存在。"""
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise
    return True


def _recovery_error(
    *,
    db_path: Path,
    path: Path,
    reason: str,
    run_id: str | None = None,
) -> TranslationRunRecoveryRequiredError:
    return TranslationRunRecoveryRequiredError(
        db_path=db_path,
        recovery_path=path,
        reason=reason,
        run_id=run_id,
    )


__all__ = [
    "build_bounded_persistence_failure_text",
    "has_translation_run_recovery",
    "reconcile_translation_run_recovery",
    "translation_run_recovery_path",
    "translation_run_stable_fingerprint",
    "write_translation_run_recovery",
]
