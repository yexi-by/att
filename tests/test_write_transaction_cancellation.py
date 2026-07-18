"""写回事务取消与提交竞态测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast, final

import pytest

from app.application.handler import TranslationHandler, WriteRuntimeMode
from app.application.write_transaction import DurableFileWriteTransaction
from app.config.schemas import Setting, TextRulesSetting, WriteBackSetting
from app.llm import LLMHandler
from app.native_write_plan import NativePlannedFile, NativeWriteBackPlan, NativeWriteBackSummary
from app.persistence import GameRegistry, RecoveryRequiredError, TargetGameSession
from app.persistence.records import WriteTransactionPayload, WriteTransactionRecord
from app.rmmz.text_rules import TextRules


@final
class _CancellationWriteSession:
    """可在写事务各 await 边界挂起的最小会话。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        block_create: bool = False,
        block_first_prepare: bool = False,
        block_rollback: bool = False,
        block_commit_after_persist: bool = False,
        block_finalize: bool = False,
    ) -> None:
        self.game_path = tmp_path / "game"
        self.content_root = self.game_path
        self.db_path = tmp_path / "game.db"
        self.content_root.mkdir(parents=True)
        self.target_path = self.content_root / "data" / "System.json"
        self.target_path.parent.mkdir()
        _ = self.target_path.write_bytes(b"old")
        self.record: WriteTransactionRecord | None = None
        self.block_create = block_create
        self.block_first_prepare = block_first_prepare
        self.block_rollback = block_rollback
        self.block_commit_after_persist = block_commit_after_persist
        self.block_finalize = block_finalize
        self.create_started = asyncio.Event()
        self.prepare_started = asyncio.Event()
        self.rollback_started = asyncio.Event()
        self.rollback_release = asyncio.Event()
        self.commit_persisted = asyncio.Event()
        self.finalize_started = asyncio.Event()
        self.finalize_release = asyncio.Event()
        self.prepare_call_count = 0

    async def create_write_transaction(self, record: WriteTransactionRecord) -> None:
        self.record = record
        if self.block_create:
            _ = self.create_started.set()
            _ = await asyncio.Event().wait()

    async def read_write_transaction(self, transaction_id: str) -> WriteTransactionRecord | None:
        if self.record is None or self.record.transaction_id != transaction_id:
            return None
        return self.record

    async def mark_write_transaction_prepared(
        self,
        transaction_id: str,
        payload: WriteTransactionPayload,
    ) -> None:
        assert self.record is not None
        assert self.record.transaction_id == transaction_id
        self.prepare_call_count += 1
        if self.block_first_prepare and self.prepare_call_count == 1:
            _ = self.prepare_started.set()
            _ = await asyncio.Event().wait()
        self.record.state = "prepared"
        self.record.payload = payload

    async def finalize_write_transaction_commit(
        self,
        *,
        transaction_id: str,
        runtime_maps: object,
        font_records: object,
    ) -> None:
        _ = runtime_maps
        _ = font_records
        assert self.record is not None
        assert self.record.transaction_id == transaction_id
        assert self.record.payload is not None
        self.record.payload = WriteTransactionPayload(
            version=self.record.payload.version,
            database_committed=True,
            files=self.record.payload.files,
        )
        self.record.state = "committed"
        if self.block_commit_after_persist:
            _ = self.commit_persisted.set()
            _ = await asyncio.Event().wait()

    async def mark_write_transaction_finalized(self, transaction_id: str) -> None:
        assert self.record is not None
        assert self.record.transaction_id == transaction_id
        if self.block_finalize:
            _ = self.finalize_started.set()
            _ = await self.finalize_release.wait()
        self.record.state = "finalized"

    async def mark_write_transaction_rolled_back(
        self,
        transaction_id: str,
        error: str = "",
    ) -> None:
        assert self.record is not None
        assert self.record.transaction_id == transaction_id
        _ = self.rollback_started.set()
        if self.block_rollback:
            _ = await self.rollback_release.wait()
        self.record.state = "rolled_back"
        self.record.error = error

    async def mark_write_transaction_recovery_required(
        self,
        transaction_id: str,
        error: str,
    ) -> None:
        assert self.record is not None
        assert self.record.transaction_id == transaction_id
        self.record.state = "recovery_required"
        self.record.error = error


def _patch_native_write_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    session: _CancellationWriteSession,
    *,
    mode: WriteRuntimeMode,
    include_file: bool = True,
) -> None:
    """为取消测试固定最小 native 计划和写后审计。"""

    def fake_build_setting_payload(
        _handler: TranslationHandler,
        **_kwargs: object,
    ) -> tuple[dict[str, object], None, list[str]]:
        return {}, None, []

    def fake_build_native_write_back_plan(**kwargs: object) -> NativeWriteBackPlan:
        assert kwargs["mode"] == mode
        files = (
            [
                NativePlannedFile(
                    target_path=session.target_path,
                    relative_path="data/System.json",
                    content="new",
                )
            ]
            if include_file
            else []
        )
        return NativeWriteBackPlan(
            files=files,
            plugin_source_runtime_write_maps=[],
            font_replacement_records=[],
            summary=NativeWriteBackSummary(
                data_item_count=int(include_file),
                plugin_item_count=0,
                terminology_written_count=0,
                target_font_name=None,
                source_font_count=0,
                replaced_font_reference_count=0,
                font_copied=False,
                planned_file_count=len(files),
                skipped_file_count=0,
            ),
            timings_ms={"total": 1},
        )

    async def fake_load_active_runtime_game_data(_game_path: Path) -> object:
        return object()

    def fake_audit(_handler: TranslationHandler, **_kwargs: object) -> None:
        return

    monkeypatch.setattr(
        TranslationHandler,
        "_build_native_write_back_setting_payload",
        fake_build_setting_payload,
    )
    monkeypatch.setattr(
        TranslationHandler,
        "_assert_post_write_active_runtime_audit_passed",
        fake_audit,
    )
    monkeypatch.setattr(
        "app.application.handler.build_native_write_back_plan",
        fake_build_native_write_back_plan,
    )
    monkeypatch.setattr(
        "app.application.handler.load_active_runtime_game_data",
        fake_load_active_runtime_game_data,
    )


async def _run_native_write(
    session: _CancellationWriteSession,
    *,
    mode: WriteRuntimeMode,
) -> None:
    handler = TranslationHandler(GameRegistry(session.db_path.parent / "db"), LLMHandler())
    try:
        _ = await handler.write_runtime_files_with_native_plan(
            session=cast(TargetGameSession, cast(object, session)),
            game_title="demo",
            callbacks=(lambda _current, _total: None, lambda _count: None),
            setting=cast(
                Setting,
                cast(
                    object,
                    SimpleNamespace(
                        text_rules=TextRulesSetting(),
                        write_back=WriteBackSetting(),
                    ),
                ),
            ),
            text_rules=TextRules.from_setting(TextRulesSetting()),
            mode=mode,
            writable_location_paths=[],
            confirm_font_overwrite=False,
            success_phase="测试写回完成",
        )
    finally:
        await handler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["write_back", "rebuild_active_runtime", "write_terminology"])
async def test_cancellation_between_create_and_prepared_rolls_back_every_native_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: WriteRuntimeMode,
) -> None:
    """三种 native 写回在清单入库前取消都必须恢复并结束事务。"""
    session = _CancellationWriteSession(
        tmp_path,
        block_first_prepare=True,
        block_rollback=True,
    )
    _patch_native_write_dependencies(monkeypatch, session, mode=mode)
    write_task = asyncio.create_task(_run_native_write(session, mode=mode))

    _ = await session.prepare_started.wait()
    _ = write_task.cancel()
    _ = await session.rollback_started.wait()
    _ = write_task.cancel()
    _ = session.rollback_release.set()

    with pytest.raises(asyncio.CancelledError):
        await write_task
    assert session.target_path.read_bytes() == b"old"
    assert session.record is not None
    assert session.record.state == "rolled_back"
    assert session.record.payload is not None
    assert not list(session.content_root.rglob("*.att-mz-write-*"))


@pytest.mark.asyncio
async def test_cancellation_after_preparing_insert_does_not_leave_permanent_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建记录已落盘但还没开始暂存时取消，preparing 必须转为 rolled_back。"""
    session = _CancellationWriteSession(tmp_path, block_create=True)
    _patch_native_write_dependencies(monkeypatch, session, mode="write_back")
    write_task = asyncio.create_task(_run_native_write(session, mode="write_back"))

    _ = await session.create_started.wait()
    _ = write_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await write_task
    assert session.target_path.read_bytes() == b"old"
    assert session.record is not None
    assert session.record.state == "rolled_back"
    assert session.record.payload is None
    assert not list(session.content_root.rglob("*.att-mz-write-*"))


@pytest.mark.asyncio
async def test_cancellation_of_diagnostics_only_write_rolls_back_empty_preparing_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有文件差异的诊断事务取消后也不得留下 preparing。"""
    session = _CancellationWriteSession(tmp_path, block_first_prepare=True)
    _patch_native_write_dependencies(
        monkeypatch,
        session,
        mode="rebuild_active_runtime",
        include_file=False,
    )
    write_task = asyncio.create_task(_run_native_write(session, mode="rebuild_active_runtime"))

    _ = await session.prepare_started.wait()
    _ = write_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await write_task
    assert session.record is not None
    assert session.record.state == "rolled_back"
    assert session.record.payload is None


@pytest.mark.asyncio
async def test_cancellation_after_database_commit_waits_for_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库提交点之后的取消必须完成清理和 finalized，不能孤立后台任务。"""
    session = _CancellationWriteSession(tmp_path, block_finalize=True)
    _patch_native_write_dependencies(monkeypatch, session, mode="write_back")
    write_task = asyncio.create_task(_run_native_write(session, mode="write_back"))

    _ = await session.finalize_started.wait()
    _ = write_task.cancel()
    _ = session.finalize_release.set()

    with pytest.raises(asyncio.CancelledError):
        await write_task
    assert session.target_path.read_bytes() == b"new"
    assert session.record is not None
    assert session.record.state == "finalized"
    assert session.record.payload is not None
    assert session.record.payload.database_committed
    assert not list(session.content_root.rglob("*.att-mz-write-*"))


@pytest.mark.asyncio
async def test_cancellation_racing_database_commit_finishes_committed_state_instead_of_rolling_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交 await 被取消时若数据库已 committed，必须保留新文件并完成收尾。"""
    session = _CancellationWriteSession(tmp_path, block_commit_after_persist=True)
    _patch_native_write_dependencies(monkeypatch, session, mode="write_back")
    write_task = asyncio.create_task(_run_native_write(session, mode="write_back"))

    _ = await session.commit_persisted.wait()
    _ = write_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await write_task
    assert session.target_path.read_bytes() == b"new"
    assert session.record is not None
    assert session.record.state == "finalized"
    assert session.record.payload is not None
    assert session.record.payload.database_committed
    assert not list(session.content_root.rglob("*.att-mz-write-*"))


@pytest.mark.asyncio
async def test_cancelled_rollback_cleanup_failure_keeps_recoverable_database_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消后的自动清理若失败，必须保留可独立恢复的数据库清单。"""
    session = _CancellationWriteSession(tmp_path, block_first_prepare=True)
    _patch_native_write_dependencies(monkeypatch, session, mode="write_back")

    def fail_cleanup(_transaction: DurableFileWriteTransaction) -> None:
        raise OSError("注入回滚产物清理失败")

    monkeypatch.setattr(
        DurableFileWriteTransaction,
        "finalize_rolled_back_cleanup",
        fail_cleanup,
    )
    write_task = asyncio.create_task(_run_native_write(session, mode="write_back"))

    _ = await session.prepare_started.wait()
    _ = write_task.cancel()

    with pytest.raises(RecoveryRequiredError, match="自动恢复未完成"):
        await write_task
    assert session.target_path.read_bytes() == b"old"
    assert session.record is not None
    assert session.record.state == "recovery_required"
    assert session.record.payload is not None
    assert not session.record.payload.database_committed
    assert len(session.record.payload.files) == 1
    assert session.record.journal_path.is_file()
