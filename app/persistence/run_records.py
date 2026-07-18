"""正文翻译运行状态和检查问题记录会话能力。"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

import aiosqlite

from app.rmmz.schema import LlmFailureRecord, TranslationErrorItem, TranslationItem, TranslationRunRecord

from .errors import TranslationRunStateConflictError
from .records import TranslationReuseContext
from .rows import decode_string_list, row_int, row_item_type, row_optional_str, row_str
from .session_base import SessionMixinBase
from .session_utils import (
    current_timestamp_text,
    parse_error_type,
    parse_llm_failure_category,
    parse_translation_run_status,
)
from .sql import (
    DELETE_ALL_TRANSLATION_QUALITY_ERRORS,
    DELETE_LLM_FAILURES_BY_RUN,
    INSERT_LLM_FAILURE,
    INSERT_TRANSLATION,
    INSERT_TRANSLATION_QUALITY_ERROR,
    INSERT_TRANSLATION_RUN,
    SELECT_LATEST_TRANSLATION_RUN,
    SELECT_LLM_FAILURES_BY_RUN,
    SELECT_RUNNING_TRANSLATION_RUNS,
    SELECT_TRANSLATION_QUALITY_ERRORS_BY_RUN,
    SELECT_TRANSLATION_RUN,
    TRANSLATION_QUALITY_ERRORS_TABLE_NAME,
    TRANSLATION_TABLE_NAME,
    UPDATE_TRANSLATION_RUN_CAS,
)
from .translation_records import serialize_translation_item
from .translation_run_recovery import translation_run_stable_fingerprint, write_translation_run_recovery


class RunRecordSessionMixin(SessionMixinBase):
    """负责翻译运行状态、模型故障和检查问题记录。"""

    active_translation_run_id: str | None

    async def start_translation_run(
        self,
        *,
        total_extracted: int,
        pending_count: int,
        deduplicated_count: int,
        batch_count: int,
    ) -> TranslationRunRecord:
        """创建新的正文翻译运行状态。"""
        async with self.translation_run_write_operation():
            _ = await self.reconcile_translation_run_recovery()
            if self.active_translation_run_id is not None:
                raise TranslationRunStateConflictError(
                    "当前会话已经启动了正文翻译运行，不能重复启动",
                    run_id=self.active_translation_run_id,
                    reason="active_run_already_started",
                )
            now = current_timestamp_text()
            run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
            record = TranslationRunRecord(
                run_id=run_id,
                status="running",
                total_extracted=total_extracted,
                pending_count=pending_count,
                deduplicated_count=deduplicated_count,
                batch_count=batch_count,
                success_count=0,
                quality_error_count=0,
                llm_failure_count=0,
                physical_request_count=0,
                retry_request_count=0,
                started_at=now,
                updated_at=now,
                finished_at=None,
                stop_reason="",
                last_error="",
            )
            commit_attempted = False
            try:
                _ = await self.connection.execute("BEGIN IMMEDIATE")
                async with self.connection.execute(SELECT_RUNNING_TRANSLATION_RUNS) as cursor:
                    running_rows = list(await cursor.fetchall())
                if running_rows:
                    running_id = row_str(running_rows[0], "run_id", self.db_path)
                    raise TranslationRunStateConflictError(
                        "数据库仍存在未收束的正文翻译运行，拒绝创建第二个 running",
                        run_id=running_id,
                        reason="unfinished_run_not_reconciled",
                    )
                _ = await self.connection.execute(DELETE_ALL_TRANSLATION_QUALITY_ERRORS)
                _ = await self.connection.execute(
                    INSERT_TRANSLATION_RUN,
                    _translation_run_values(record, updated_at=now),
                )
                commit_attempted = True
                await self.connection.commit()
            except BaseException as error:
                try:
                    await self.connection.rollback()
                except BaseException as rollback_error:
                    error.add_note(
                        f"正文翻译运行启动失败后，SQLite 回滚也失败：{type(rollback_error).__name__}: {rollback_error}"
                    )
                if commit_attempted:
                    try:
                        committed = await self._read_translation_run_in_transaction(run_id)
                    except BaseException as readback_error:
                        error.add_note(
                            f"正文翻译运行启动提交确认也失败：{type(readback_error).__name__}: {readback_error}"
                        )
                    else:
                        if _translation_run_matches(committed, record):
                            self.active_translation_run_id = run_id
                            if isinstance(error, Exception):
                                return record
                raise
            self.active_translation_run_id = run_id
            return record

    async def write_translation_run(self, record: TranslationRunRecord) -> None:
        """只更新已存在且身份匹配的 run；禁止 UPSERT 创建或复活。"""
        async with self.translation_run_write_operation():
            commit_attempted = False
            try:
                _ = await self.connection.execute("BEGIN IMMEDIATE")
                current = await self._read_translation_run_in_transaction(record.run_id)
                if current is None:
                    raise TranslationRunStateConflictError(
                        "要更新的正文翻译运行不存在",
                        run_id=record.run_id,
                        reason="run_not_found",
                    )
                _assert_same_run_identity(current=current, requested=record)
                _assert_snapshot_transition_allowed(current=current, requested=record)
                cursor = await self.connection.execute(
                    UPDATE_TRANSLATION_RUN_CAS,
                    _translation_run_update_values(
                        record,
                        updated_at=current_timestamp_text(),
                        expected_status=current.status,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TranslationRunStateConflictError(
                        "正文翻译运行在更新期间发生变化",
                        run_id=record.run_id,
                        reason="run_compare_and_swap_failed",
                    )
                commit_attempted = True
                await self.connection.commit()
            except BaseException as error:
                try:
                    await self.connection.rollback()
                except BaseException as rollback_error:
                    error.add_note(
                        f"正文翻译运行更新失败后，SQLite 回滚也失败：{type(rollback_error).__name__}: {rollback_error}"
                    )
                if commit_attempted:
                    try:
                        committed = await self._read_translation_run_in_transaction(record.run_id)
                    except BaseException as readback_error:
                        error.add_note(
                            f"正文翻译运行更新提交确认也失败：{type(readback_error).__name__}: {readback_error}"
                        )
                    else:
                        if _translation_run_matches(committed, record) and isinstance(error, Exception):
                            if record.status != "running" and self.active_translation_run_id == record.run_id:
                                self.active_translation_run_id = None
                            return
                raise
            if record.status != "running" and self.active_translation_run_id == record.run_id:
                self.active_translation_run_id = None

    async def persist_translation_run_terminal(
        self,
        record: TranslationRunRecord,
        llm_failure: LlmFailureRecord | None = None,
    ) -> None:
        """在同一事务中写入可选模型故障与翻译运行终态。"""
        if record.status == "running":
            raise ValueError("终态持久化不接受 running 状态")
        if record.finished_at is None:
            raise ValueError("终态持久化必须提供 finished_at")
        expected_failure_count = 0 if llm_failure is None else 1
        if record.llm_failure_count != expected_failure_count:
            raise ValueError("终态 llm_failure_count 与模型故障记录不一致")
        if llm_failure is not None and llm_failure.run_id != record.run_id:
            raise ValueError("模型故障记录与翻译运行 run_id 不一致")

        async with self.translation_run_write_operation():
            _ = await self.reconcile_translation_run_recovery()
            try:
                _ = await self.connection.execute("BEGIN IMMEDIATE")
                current = await self._read_translation_run_in_transaction(record.run_id)
                if current is None:
                    raise TranslationRunStateConflictError(
                        "要结束的正文翻译运行不存在",
                        run_id=record.run_id,
                        reason="run_not_found",
                    )
                _assert_same_run_identity(current=current, requested=record)
                if current.status != "running":
                    raise TranslationRunStateConflictError(
                        "正文翻译运行已经结束，拒绝覆盖既有终态",
                        run_id=record.run_id,
                        reason="terminal_run_cannot_transition_again",
                    )
                _assert_terminal_counts_not_stale(current=current, requested=record)
                _ = await self.connection.execute(
                    DELETE_LLM_FAILURES_BY_RUN,
                    (record.run_id,),
                )
                if llm_failure is not None:
                    _ = await self.connection.execute(
                        INSERT_LLM_FAILURE,
                        _llm_failure_values(llm_failure),
                    )
                cursor = await self.connection.execute(
                    UPDATE_TRANSLATION_RUN_CAS,
                    _translation_run_update_values(
                        record,
                        updated_at=current_timestamp_text(),
                        expected_status="running",
                    ),
                )
                if cursor.rowcount != 1:
                    raise TranslationRunStateConflictError(
                        "正文翻译运行在终态提交期间发生变化",
                        run_id=record.run_id,
                        reason="run_compare_and_swap_failed",
                    )
                await self.connection.commit()
            except BaseException as error:
                try:
                    await self.connection.rollback()
                except BaseException as rollback_error:
                    error.add_note(
                        f"正文翻译终态提交失败后，SQLite 回滚也失败：{type(rollback_error).__name__}: {rollback_error}"
                    )
                raise
            if self.active_translation_run_id == record.run_id:
                self.active_translation_run_id = None

    async def persist_translation_batch(
        self,
        run_record: TranslationRunRecord,
        success_items: Sequence[TranslationItem],
        error_items: Sequence[TranslationErrorItem],
        reuse_contexts_by_path: Mapping[str, TranslationReuseContext] | None = None,
        *,
        physical_request_count_delta: int = 0,
        retry_request_count_delta: int = 0,
    ) -> None:
        """在同一事务中保存批次译文、检查问题和运行计数。"""
        for field_name, value in (
            ("physical_request_count_delta", physical_request_count_delta),
            ("retry_request_count_delta", retry_request_count_delta),
        ):
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} 必须是大于等于 0 的整数")
        if retry_request_count_delta > physical_request_count_delta:
            raise ValueError("retry_request_count_delta 不能大于 physical_request_count_delta")
        contexts_by_path = reuse_contexts_by_path or {}
        success_paths = {item.location_path for item in success_items}
        unknown_context_paths = sorted(set(contexts_by_path) - success_paths)
        if unknown_context_paths:
            raise ValueError(f"复用上下文包含本批次之外的位置: {unknown_context_paths[0]}")
        async with self.translation_run_write_operation():
            commit_attempted = False
            persisted_run: TranslationRunRecord | None = None
            try:
                _ = await self.connection.execute("BEGIN IMMEDIATE")
                current = await self._read_translation_run_in_transaction(run_record.run_id)
                if current is None:
                    raise TranslationRunStateConflictError(
                        "正文翻译运行不存在",
                        run_id=run_record.run_id,
                        reason="run_not_found",
                    )
                _assert_same_run_identity(current=current, requested=run_record)
                if current.status != "running" or run_record.status != "running":
                    raise TranslationRunStateConflictError(
                        "只有 running 状态的正文翻译运行可以保存批次",
                        run_id=run_record.run_id,
                        reason="batch_requires_running_run",
                    )
                _assert_batch_counts_not_stale(current=current, requested=run_record)
                persisted_run = run_record.model_copy(
                    update={
                        "physical_request_count": current.physical_request_count + physical_request_count_delta,
                        "retry_request_count": current.retry_request_count + retry_request_count_delta,
                    }
                )
                if success_items:
                    _ = await self.connection.executemany(
                        INSERT_TRANSLATION,
                        [
                            serialize_translation_item(
                                item,
                                contexts_by_path.get(item.location_path),
                            )
                            for item in success_items
                        ],
                    )
                if error_items:
                    _ = await self.connection.executemany(
                        INSERT_TRANSLATION_QUALITY_ERROR,
                        [
                            (
                                run_record.run_id,
                                item.location_path,
                                item.item_type,
                                item.role,
                                json.dumps(item.original_lines, ensure_ascii=False),
                                json.dumps(item.translation_lines, ensure_ascii=False),
                                item.error_type,
                                json.dumps(item.error_detail, ensure_ascii=False),
                                item.model_response,
                            )
                            for item in error_items
                        ],
                    )
                cursor = await self.connection.execute(
                    UPDATE_TRANSLATION_RUN_CAS,
                    _translation_run_update_values(
                        persisted_run,
                        updated_at=current_timestamp_text(),
                        expected_status="running",
                    ),
                )
                if cursor.rowcount != 1:
                    raise TranslationRunStateConflictError(
                        "正文翻译运行在批次提交期间发生变化",
                        run_id=run_record.run_id,
                        reason="run_compare_and_swap_failed",
                    )
                commit_attempted = True
                await self.connection.commit()
            except BaseException as error:
                try:
                    await self.connection.rollback()
                except BaseException as rollback_error:
                    error.add_note(
                        f"正文翻译批次提交失败后，SQLite 回滚也失败：{type(rollback_error).__name__}: {rollback_error}"
                    )
                if commit_attempted and persisted_run is not None:
                    try:
                        committed_run = await self._read_translation_run_in_transaction(persisted_run.run_id)
                        committed = _translation_run_matches(
                            committed_run,
                            persisted_run,
                        ) and await _batch_rows_match(
                            connection=self.connection,
                            run_id=persisted_run.run_id,
                            success_items=success_items,
                            error_items=error_items,
                            contexts_by_path=contexts_by_path,
                        )
                    except BaseException as readback_error:
                        error.add_note(f"正文翻译批次提交确认也失败：{type(readback_error).__name__}: {readback_error}")
                    else:
                        if committed and isinstance(error, Exception):
                            return
                raise

    async def read_latest_translation_run(self) -> TranslationRunRecord | None:
        """读取最新正文翻译运行状态。"""
        _ = await self.reconcile_translation_run_recovery()
        async with self.connection.execute(SELECT_LATEST_TRANSLATION_RUN) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._decode_translation_run(row)

    async def reconcile_interrupted_translation_runs(self) -> int:
        """在独占命令租约内把崩溃遗留的 running 明确收束为 failed。"""
        async with self.translation_run_write_operation():
            now = current_timestamp_text()
            reconciled = 0
            try:
                _ = await self.connection.execute("BEGIN IMMEDIATE")
                async with self.connection.execute(SELECT_RUNNING_TRANSLATION_RUNS) as cursor:
                    rows = await cursor.fetchall()
                for row in rows:
                    current = self._decode_translation_run(row)
                    await _assert_running_record_consistent(
                        connection=self.connection,
                        record=current,
                        db_path=self.db_path,
                    )
                    interrupted = current.model_copy(
                        update={
                            "status": "failed",
                            "llm_failure_count": 0,
                            "finished_at": now,
                            "stop_reason": "检测到上次正文翻译进程未正常结束，已在新修改命令开始前标记失败",
                            "last_error": "process_interrupted",
                        }
                    )
                    update_cursor = await self.connection.execute(
                        UPDATE_TRANSLATION_RUN_CAS,
                        _translation_run_update_values(
                            interrupted,
                            updated_at=now,
                            expected_status="running",
                        ),
                    )
                    if update_cursor.rowcount != 1:
                        raise TranslationRunStateConflictError(
                            "崩溃遗留的正文翻译运行在收束期间发生变化",
                            run_id=current.run_id,
                            reason="run_compare_and_swap_failed",
                        )
                    reconciled += 1
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise
            if self.active_translation_run_id is not None and rows:
                running_ids = {row_str(row, "run_id", self.db_path) for row in rows}
                if self.active_translation_run_id in running_ids:
                    self.active_translation_run_id = None
            return reconciled

    async def read_translation_run(self, run_id: str) -> TranslationRunRecord | None:
        """按运行 ID 读取正文翻译状态。"""
        _ = await self.reconcile_translation_run_recovery()
        async with self.connection.execute(SELECT_TRANSLATION_RUN, (run_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._decode_translation_run(row)

    async def read_translation_terminal_snapshot(
        self,
        run_id: str,
    ) -> tuple[TranslationRunRecord | None, tuple[LlmFailureRecord, ...]]:
        """在命令级修改租约内读取一次终态提交确认快照。"""
        async with self.translation_run_write_operation():
            record = await self._read_translation_run_in_transaction(run_id)
            failures = tuple(await self._read_llm_failures_in_transaction(run_id))
        return record, failures

    async def read_llm_failures(self, run_id: str) -> list[LlmFailureRecord]:
        """读取指定运行的模型故障记录。"""
        return await self._read_llm_failures_in_transaction(run_id)

    async def _read_llm_failures_in_transaction(self, run_id: str) -> list[LlmFailureRecord]:
        """复用当前连接读取模型故障，不自行改变事务边界。"""
        async with self.connection.execute(SELECT_LLM_FAILURES_BY_RUN, (run_id,)) as cursor:
            rows = await cursor.fetchall()
        return [
            LlmFailureRecord(
                run_id=row_str(row, "run_id", self.db_path),
                category=parse_llm_failure_category(row_str(row, "category", self.db_path), self.db_path),
                error_type=row_str(row, "error_type", self.db_path),
                error_message=row_str(row, "error_message", self.db_path),
                retryable=row_int(row, "retryable", self.db_path) == 1,
                attempt_count=row_int(row, "attempt_count", self.db_path),
                created_at=row_str(row, "created_at", self.db_path),
            )
            for row in rows
        ]

    async def write_translation_quality_errors(
        self,
        run_id: str,
        items: list[TranslationErrorItem],
    ) -> None:
        """写入没通过项目检查的最终译文。"""
        if items:
            serialized_items = [
                (
                    run_id,
                    error_item.location_path,
                    error_item.item_type,
                    error_item.role,
                    json.dumps(error_item.original_lines, ensure_ascii=False),
                    json.dumps(error_item.translation_lines, ensure_ascii=False),
                    error_item.error_type,
                    json.dumps(error_item.error_detail, ensure_ascii=False),
                    error_item.model_response,
                )
                for error_item in items
            ]
            _ = await self.connection.executemany(
                INSERT_TRANSLATION_QUALITY_ERROR,
                serialized_items,
            )
        await self.connection.commit()

    async def read_translation_quality_errors(self, run_id: str) -> list[TranslationErrorItem]:
        """读取指定运行中没通过项目检查的最终译文。"""
        async with self.connection.execute(SELECT_TRANSLATION_QUALITY_ERRORS_BY_RUN, (run_id,)) as cursor:
            rows = await cursor.fetchall()
        return [
            TranslationErrorItem(
                location_path=row_str(row, "location_path", self.db_path),
                item_type=row_item_type(row, "item_type", self.db_path),
                role=row_optional_str(row, "role", self.db_path),
                original_lines=decode_string_list(row_str(row, "original_lines", self.db_path), "original_lines"),
                translation_lines=decode_string_list(
                    row_str(row, "translation_lines", self.db_path),
                    "translation_lines",
                ),
                error_type=parse_error_type(row_str(row, "error_type", self.db_path), self.db_path),
                error_detail=decode_string_list(row_str(row, "error_detail", self.db_path), "error_detail"),
                model_response=row_str(row, "model_response", self.db_path),
            )
            for row in rows
        ]

    async def delete_translation_quality_errors_by_paths(self, location_paths: set[str]) -> int:
        """按文本内部位置清理已经修好的译文检查失败明细。"""
        if not location_paths:
            return 0
        sorted_paths = sorted(location_paths)
        placeholders = ", ".join("?" for _ in sorted_paths)
        cursor = await self.connection.execute(
            f"""
--sql
                DELETE FROM [{TRANSLATION_QUALITY_ERRORS_TABLE_NAME}]
                WHERE location_path IN ({placeholders})
            """,
            tuple(sorted_paths),
        )
        await self.connection.commit()
        return max(cursor.rowcount, 0)

    async def write_translation_run_recovery(
        self,
        *,
        attempted_record: TranslationRunRecord,
        attempted_failure: LlmFailureRecord | None = None,
        fallback_record: TranslationRunRecord,
    ) -> Path:
        """在 SQLite 终态连续失败后写入同目录的一次性恢复日志。"""
        async with self.translation_run_write_operation():
            return await write_translation_run_recovery(
                db_path=self.db_path,
                game_id=self.game_id,
                attempted_record=attempted_record,
                attempted_failure=attempted_failure,
                fallback_record=fallback_record,
            )

    async def _read_translation_run_in_transaction(self, run_id: str) -> TranslationRunRecord | None:
        async with self.connection.execute(SELECT_TRANSLATION_RUN, (run_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._decode_translation_run(row)

    def _decode_translation_run(self, row: aiosqlite.Row) -> TranslationRunRecord:
        """把 SQLite 行转换成正文翻译运行状态。"""
        return TranslationRunRecord(
            run_id=row_str(row, "run_id", self.db_path),
            status=parse_translation_run_status(row_str(row, "status", self.db_path), self.db_path),
            total_extracted=row_int(row, "total_extracted", self.db_path),
            pending_count=row_int(row, "pending_count", self.db_path),
            deduplicated_count=row_int(row, "deduplicated_count", self.db_path),
            batch_count=row_int(row, "batch_count", self.db_path),
            success_count=row_int(row, "success_count", self.db_path),
            quality_error_count=row_int(row, "quality_error_count", self.db_path),
            llm_failure_count=row_int(row, "llm_failure_count", self.db_path),
            physical_request_count=row_int(row, "physical_request_count", self.db_path),
            retry_request_count=row_int(row, "retry_request_count", self.db_path),
            started_at=row_str(row, "started_at", self.db_path),
            updated_at=row_str(row, "updated_at", self.db_path),
            finished_at=row_optional_str(row, "finished_at", self.db_path),
            stop_reason=row_str(row, "stop_reason", self.db_path),
            last_error=row_str(row, "last_error", self.db_path),
        )


def _translation_run_values(
    record: TranslationRunRecord,
    *,
    updated_at: str,
) -> tuple[object, ...]:
    """按运行记录列顺序序列化状态。"""
    return (
        record.run_id,
        record.status,
        record.total_extracted,
        record.pending_count,
        record.deduplicated_count,
        record.batch_count,
        record.success_count,
        record.quality_error_count,
        record.llm_failure_count,
        record.physical_request_count,
        record.retry_request_count,
        record.started_at,
        updated_at,
        record.finished_at,
        record.stop_reason,
        record.last_error,
    )


def _translation_run_update_values(
    record: TranslationRunRecord,
    *,
    updated_at: str,
    expected_status: str,
) -> tuple[object, ...]:
    """按 CAS UPDATE 列顺序序列化运行状态和预期旧状态。"""
    values = _translation_run_values(record, updated_at=updated_at)
    return (*values[1:], record.run_id, expected_status, record.started_at)


def _assert_same_run_identity(*, current: TranslationRunRecord, requested: TranslationRunRecord) -> None:
    if current.run_id != requested.run_id or current.started_at != requested.started_at:
        raise TranslationRunStateConflictError(
            "正文翻译运行身份与当前数据库记录不一致",
            run_id=requested.run_id,
            reason="run_identity_mismatch",
        )
    for field in ("total_extracted", "pending_count", "deduplicated_count"):
        if getattr(current, field) != getattr(requested, field):
            raise TranslationRunStateConflictError(
                f"正文翻译运行不可变计数 {field} 与当前数据库记录不一致",
                run_id=requested.run_id,
                reason="run_immutable_counts_mismatch",
            )


def _assert_snapshot_transition_allowed(*, current: TranslationRunRecord, requested: TranslationRunRecord) -> None:
    if requested.status == "running":
        if current.status != "running" or requested.finished_at is not None:
            raise TranslationRunStateConflictError(
                "已结束的正文翻译运行不能恢复为 running",
                run_id=requested.run_id,
                reason="terminal_run_cannot_be_revived",
            )
        _assert_batch_counts_not_stale(current=current, requested=requested)
        return
    if requested.status != "completed" or not requested.finished_at:
        raise TranslationRunStateConflictError(
            "普通状态更新只允许 running 快照或手动修复为 completed",
            run_id=requested.run_id,
            reason="unsupported_run_snapshot_transition",
        )
    if current.status == "running":
        raise TranslationRunStateConflictError(
            "running 运行必须通过原子终态接口结束",
            run_id=requested.run_id,
            reason="terminal_transition_requires_atomic_api",
        )
    _assert_terminal_counts_not_stale(current=current, requested=requested)


def _assert_batch_counts_not_stale(*, current: TranslationRunRecord, requested: TranslationRunRecord) -> None:
    if requested.llm_failure_count != 0 or requested.finished_at is not None:
        raise TranslationRunStateConflictError(
            "running 批次快照包含终态字段",
            run_id=requested.run_id,
            reason="invalid_running_snapshot",
        )
    for field in ("batch_count", "success_count", "quality_error_count"):
        if getattr(requested, field) < getattr(current, field):
            raise TranslationRunStateConflictError(
                f"批次快照计数 {field} 早于数据库当前值",
                run_id=requested.run_id,
                reason="stale_run_counts",
            )


def _assert_terminal_counts_not_stale(*, current: TranslationRunRecord, requested: TranslationRunRecord) -> None:
    for field in (
        "batch_count",
        "success_count",
        "quality_error_count",
        "physical_request_count",
        "retry_request_count",
    ):
        if getattr(requested, field) < getattr(current, field):
            raise TranslationRunStateConflictError(
                f"终态计数 {field} 早于数据库当前值",
                run_id=requested.run_id,
                reason="stale_run_counts",
            )


async def _assert_running_record_consistent(
    *,
    connection: aiosqlite.Connection,
    record: TranslationRunRecord,
    db_path: Path,
) -> None:
    if record.finished_at is not None or record.llm_failure_count != 0:
        raise TranslationRunStateConflictError(
            "running 翻译运行含有终态字段或模型故障计数，拒绝自动收束",
            run_id=record.run_id,
            reason="invalid_running_record",
        )
    for field in (
        "total_extracted",
        "pending_count",
        "deduplicated_count",
        "batch_count",
        "success_count",
        "quality_error_count",
        "physical_request_count",
        "retry_request_count",
    ):
        if getattr(record, field) < 0:
            raise TranslationRunStateConflictError(
                f"running 翻译运行计数 {field} 为负数，拒绝自动收束",
                run_id=record.run_id,
                reason="invalid_running_counts",
            )
    if record.retry_request_count > record.physical_request_count:
        raise TranslationRunStateConflictError(
            "running 翻译运行重试请求数超过物理请求数，拒绝自动收束",
            run_id=record.run_id,
            reason="invalid_running_counts",
        )
    async with connection.execute(
        "SELECT COUNT(*) AS failure_count FROM llm_failures WHERE run_id = ?",
        (record.run_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row_int(row, "failure_count", db_path) != 0:
        raise TranslationRunStateConflictError(
            "running 翻译运行存在模型故障行，拒绝自动收束",
            run_id=record.run_id,
            reason="invalid_running_failure_rows",
        )


def _llm_failure_values(failure: LlmFailureRecord) -> tuple[object, ...]:
    """序列化运行级模型故障。"""
    return (
        failure.run_id,
        failure.category,
        failure.error_type,
        failure.error_message,
        1 if failure.retryable else 0,
        failure.attempt_count,
        failure.created_at,
    )


def _translation_run_matches(
    actual: TranslationRunRecord | None,
    expected: TranslationRunRecord,
) -> bool:
    return actual is not None and translation_run_stable_fingerprint(actual) == translation_run_stable_fingerprint(
        expected
    )


async def _batch_rows_match(
    *,
    connection: aiosqlite.Connection,
    run_id: str,
    success_items: Sequence[TranslationItem],
    error_items: Sequence[TranslationErrorItem],
    contexts_by_path: Mapping[str, TranslationReuseContext],
) -> bool:
    """提交确认丢失时，逐行确认批次的全部原子写入结果。"""
    translation_columns = (
        "location_path",
        "item_type",
        "role",
        "original_lines",
        "source_line_paths",
        "translation_lines",
        "context_key_json",
        "context_key_hash",
        "source_fingerprint",
        "rule_fingerprint",
        "terminology_fingerprint",
        "language_fingerprint",
        "prompt_protocol_version",
    )
    for item in success_items:
        expected = serialize_translation_item(item, contexts_by_path.get(item.location_path))
        async with connection.execute(
            f"SELECT {', '.join(translation_columns)} FROM [{TRANSLATION_TABLE_NAME}] WHERE location_path = ?",
            (item.location_path,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or tuple(cast(object, row[column]) for column in translation_columns) != expected:
            return False

    quality_columns = (
        "run_id",
        "location_path",
        "item_type",
        "role",
        "original_lines",
        "translation_lines",
        "error_type",
        "error_detail",
        "model_response",
    )
    for item in error_items:
        expected = (
            run_id,
            item.location_path,
            item.item_type,
            item.role,
            json.dumps(item.original_lines, ensure_ascii=False),
            json.dumps(item.translation_lines, ensure_ascii=False),
            item.error_type,
            json.dumps(item.error_detail, ensure_ascii=False),
            item.model_response,
        )
        async with connection.execute(
            f"SELECT {', '.join(quality_columns)} FROM [{TRANSLATION_QUALITY_ERRORS_TABLE_NAME}] WHERE run_id = ? AND location_path = ?",
            (run_id, item.location_path),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or tuple(cast(object, row[column]) for column in quality_columns) != expected:
            return False
    return True
