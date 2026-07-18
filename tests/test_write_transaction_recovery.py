"""应用层文件写事务恢复测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Self, cast

import pytest

from app.application.handler import TranslationHandler
from app.application.write_transaction import DurableFileWriteTransaction, PlannedFileWrite
from app.llm import LLMHandler
from app.persistence import GameRegistry, RecoveryRequiredError
from app.persistence.records import (
    WriteTransactionFileRecord,
    WriteTransactionPayload,
    WriteTransactionRecord,
)


class FakeRecoverySession:
    """记录恢复状态变化的最小游戏会话。"""

    def __init__(
        self,
        *,
        game_path: Path,
        content_root: Path,
        record: WriteTransactionRecord,
    ) -> None:
        self.game_path: Path = game_path
        self.content_root: Path = content_root
        self.record: WriteTransactionRecord = record
        self.block_terminal_state: bool = False
        self.terminal_state_started: asyncio.Event = asyncio.Event()
        self.terminal_state_release: asyncio.Event = asyncio.Event()
        self.lease_call_count: int = 0

    def acquire_mutation_lease(self) -> None:
        self.lease_call_count += 1

    async def _wait_before_terminal_state(self) -> None:
        if not self.block_terminal_state:
            return
        _ = self.terminal_state_started.set()
        _ = await self.terminal_state_release.wait()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type
        _ = exc
        _ = traceback

    async def read_unfinished_write_transactions(self) -> list[WriteTransactionRecord]:
        if self.record.state in {"preparing", "prepared", "committed", "recovery_required"}:
            return [self.record]
        return []

    async def mark_write_transaction_finalized(self, transaction_id: str) -> None:
        assert transaction_id == self.record.transaction_id
        await self._wait_before_terminal_state()
        self.record.state = "finalized"

    async def mark_write_transaction_rolled_back(
        self,
        transaction_id: str,
        error: str = "",
    ) -> None:
        assert transaction_id == self.record.transaction_id
        await self._wait_before_terminal_state()
        self.record.state = "rolled_back"
        self.record.error = error

    async def mark_write_transaction_recovery_required(
        self,
        transaction_id: str,
        error: str,
    ) -> None:
        assert transaction_id == self.record.transaction_id
        self.record.state = "recovery_required"
        self.record.error = error


class FakeRecoveryRegistry:
    """只返回一个恢复测试会话。"""

    def __init__(self, session: FakeRecoverySession) -> None:
        self.session: FakeRecoverySession = session

    async def open_game(self, game_title: str) -> FakeRecoverySession:
        assert game_title == "demo"
        return self.session

    async def open_game_with_mutation_lease(self, game_title: str) -> FakeRecoverySession:
        assert game_title == "demo"
        self.session.acquire_mutation_lease()
        return self.session


def build_handler(session: FakeRecoverySession) -> TranslationHandler:
    """构造只用于恢复入口的 handler。"""
    registry = cast(GameRegistry, cast(object, FakeRecoveryRegistry(session)))
    llm_handler = cast(LLMHandler, object())
    return TranslationHandler(registry, llm_handler)


def build_recovery_fixture(
    tmp_path: Path,
    *,
    database_state: str,
) -> tuple[FakeRecoverySession, Path]:
    """创建已经替换目标文件、等待恢复的事务。"""
    game_path = tmp_path / "game"
    content_root = game_path / "www"
    content_root.mkdir(parents=True)
    target_path = content_root / "data" / "Actors.json"
    target_path.parent.mkdir()
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txhandler",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    manifest = transaction.export_manifest()
    transaction.replace_targets()
    payload = WriteTransactionPayload(
        version=1,
        database_committed=database_state == "committed",
        files=tuple(
            WriteTransactionFileRecord(
                target_relative_path=entry.target_relative_path,
                staged_relative_path=entry.staged_relative_path,
                backup_relative_path=entry.backup_relative_path,
                existed_before=entry.existed_before,
                original_sha256=entry.original_sha256,
                target_sha256=entry.target_sha256,
            )
            for entry in manifest
        ),
    )
    record = WriteTransactionRecord(
        transaction_id=transaction.transaction_id,
        operation="write_back",
        game_path=game_path,
        state=database_state,
        journal_path=transaction.journal_path,
        payload=payload,
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
        error="",
    )
    return (
        FakeRecoverySession(
            game_path=game_path,
            content_root=content_root,
            record=record,
        ),
        target_path,
    )


@pytest.mark.asyncio
async def test_handler_recovery_rolls_back_files_when_database_is_not_committed(
    tmp_path: Path,
) -> None:
    """数据库仍为 prepared 时必须恢复旧文件。"""
    session, target_path = build_recovery_fixture(tmp_path, database_state="prepared")

    summary = await build_handler(session).recover_write_transaction("demo")

    assert summary.final_state == "rolled_back"
    assert summary.restored_file_count == 1
    assert target_path.read_bytes() == b"old"
    assert session.record.state == "rolled_back"
    assert not session.record.journal_path.exists()


@pytest.mark.asyncio
async def test_handler_recovery_finalizes_new_files_when_database_is_committed(
    tmp_path: Path,
) -> None:
    """数据库已 committed 时不得把新文件回滚。"""
    session, target_path = build_recovery_fixture(tmp_path, database_state="committed")

    summary = await build_handler(session).recover_write_transaction("demo")

    assert summary.final_state == "finalized"
    assert summary.finalized_committed_file_count == 1
    assert target_path.read_bytes() == b"new"
    assert session.record.state == "finalized"
    assert not session.record.journal_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_state", "expected_final_state", "expected_content"),
    [
        ("prepared", "rolled_back", b"old"),
        ("committed", "finalized", b"new"),
    ],
)
async def test_handler_recovery_cancellation_waits_for_database_terminal_state(
    tmp_path: Path,
    database_state: str,
    expected_final_state: str,
    expected_content: bytes,
) -> None:
    """恢复文件后即使收到取消，也必须等数据库终态真正保存。"""
    session, target_path = build_recovery_fixture(tmp_path, database_state=database_state)
    session.block_terminal_state = True
    recovery_task = asyncio.create_task(build_handler(session).recover_write_transaction("demo"))

    _ = await session.terminal_state_started.wait()
    _ = recovery_task.cancel()
    _ = session.terminal_state_release.set()

    with pytest.raises(asyncio.CancelledError):
        await recovery_task
    assert target_path.read_bytes() == expected_content
    assert session.record.state == expected_final_state
    assert not session.record.journal_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_state", "expected_final_state"),
    [("prepared", "rolled_back"), ("committed", "finalized")],
)
async def test_handler_recovery_finishes_database_state_when_journal_was_already_cleaned(
    tmp_path: Path,
    database_state: str,
    expected_final_state: str,
) -> None:
    """文件清理后、数据库收尾前崩溃时，重试应幂等完成状态转换。"""
    session, target_path = build_recovery_fixture(tmp_path, database_state=database_state)
    session.record.journal_path.unlink()
    if database_state == "committed":
        for artifact_path in session.content_root.rglob("*att-mz-write-*"):
            if artifact_path.is_file():
                artifact_path.unlink()

    summary = await build_handler(session).recover_write_transaction("demo")

    assert summary.final_state == expected_final_state
    assert session.record.state == expected_final_state
    expected_content = b"new" if database_state == "committed" else b"old"
    assert target_path.read_bytes() == expected_content


@pytest.mark.asyncio
async def test_handler_recovery_refuses_corrupt_journal_without_database_payload(tmp_path: Path) -> None:
    """数据库没有清单且 journal 不可验证时，必须保留现场并要求人工恢复。"""
    game_path = tmp_path / "game"
    content_root = game_path / "www"
    content_root.mkdir(parents=True)
    transaction_id = "txmissingpayload"
    journal_path = content_root / ".att-mz-write-transactions" / f"{transaction_id}.json"
    journal_path.parent.mkdir()
    _ = journal_path.write_text("{}", encoding="utf-8")
    session = FakeRecoverySession(
        game_path=game_path,
        content_root=content_root,
        record=WriteTransactionRecord(
            transaction_id=transaction_id,
            operation="write_back",
            game_path=game_path,
            state="preparing",
            journal_path=journal_path,
            payload=None,
            created_at="2026-07-18T00:00:00+00:00",
            updated_at="2026-07-18T00:00:00+00:00",
            error="",
        ),
    )

    with pytest.raises(RecoveryRequiredError, match="恢复失败") as raised:
        _ = await build_handler(session).recover_write_transaction("demo")

    assert raised.value.code == "recovery_required"
    assert raised.value.details["transaction_id"] == transaction_id
    assert session.record.state == "recovery_required"
    assert journal_path.exists()


@pytest.mark.asyncio
async def test_handler_recovery_preserves_external_changes_and_keeps_modifications_blocked(
    tmp_path: Path,
) -> None:
    """目标被外部改写时不得覆盖现场，并把事务保持为 recovery_required。"""
    session, target_path = build_recovery_fixture(tmp_path, database_state="prepared")
    _ = target_path.write_bytes(b"external")

    with pytest.raises(RecoveryRequiredError, match="保持阻断状态") as raised:
        _ = await build_handler(session).recover_write_transaction("demo")

    assert raised.value.code == "recovery_required"
    assert raised.value.details["transaction_id"] == session.record.transaction_id
    assert target_path.read_bytes() == b"external"
    assert session.record.state == "recovery_required"
    assert session.record.journal_path.exists()


@pytest.mark.asyncio
async def test_committed_recovery_without_journal_verifies_database_target_hash(
    tmp_path: Path,
) -> None:
    """已提交事务即使 journal 消失，也必须用 DB payload 发现外部改写。"""
    session, target_path = build_recovery_fixture(tmp_path, database_state="committed")
    session.record.journal_path.unlink()
    _ = target_path.write_bytes(b"external-after-commit")

    with pytest.raises(RecoveryRequiredError, match="保持阻断状态") as raised:
        _ = await build_handler(session).recover_write_transaction("demo")

    assert raised.value.code == "recovery_required"
    assert raised.value.details["transaction_id"] == session.record.transaction_id
    assert target_path.read_bytes() == b"external-after-commit"
    assert session.record.state == "recovery_required"
    assert not session.record.journal_path.exists()
