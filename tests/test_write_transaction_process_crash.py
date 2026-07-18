"""preparing 写事务的进程崩溃恢复测试。"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

import pytest

from app.application.handler import TranslationHandler
from app.application.summaries import WriteTransactionRecoverySummary
from app.application.write_transaction import (
    DurableFileWriteTransaction,
    PlannedFileWrite,
    file_write_transaction_journal_path,
)
from app.llm import LLMHandler
from app.native_file_hashing import NativeFileHashInput, NativeFileHashResult
from app.persistence import GameRegistry, RecoveryRequiredError
from app.persistence.records import WriteTransactionRecord


class _SimulatedProcessCrash(BaseException):
    """模拟进程直接退出，测试中故意跳过 prepare 的清理。"""


class _CopyFileDurably(Protocol):
    def __call__(self, *, source_path: Path, target_path: Path) -> None: ...


class _WriteBytesDurably(Protocol):
    def __call__(self, *, target_path: Path, content: bytes) -> None: ...


class _HashNativeFiles(Protocol):
    def __call__(
        self,
        *,
        root: Path,
        files: Sequence[NativeFileHashInput],
    ) -> list[NativeFileHashResult]: ...


@pytest.fixture(autouse=True)
def isolate_native_file_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """崩溃测试只验证文件事务，不依赖当前进程已安装的 native 构建。"""

    def hash_files(
        *,
        root: Path,
        files: Sequence[NativeFileHashInput],
    ) -> list[NativeFileHashResult]:
        results: list[NativeFileHashResult] = []
        for item in files:
            content = (root / item.relative_path).read_bytes()
            results.append(
                NativeFileHashResult(
                    id=item.id,
                    relative_path=item.relative_path,
                    sha256=sha256(content).hexdigest(),
                    byte_size=len(content),
                )
            )
        return results

    monkeypatch.setattr("app.rmmz.source_snapshot.hash_native_files", hash_files)
    monkeypatch.setattr("app.application.write_transaction.hash_native_files", hash_files)


async def _create_preparing_record(
    registry: GameRegistry,
    game_title: str,
    *,
    transaction_id: str,
    operation: str = "write_back",
) -> tuple[Path, Path]:
    """在真实 schema 12 数据库中创建尚无 payload 的写事务。"""
    async with await registry.open_game(game_title) as session:
        journal_path = file_write_transaction_journal_path(
            content_root=session.content_root,
            transaction_id=transaction_id,
        )
        await session.create_write_transaction(
            WriteTransactionRecord(
                transaction_id=transaction_id,
                operation=operation,
                game_path=session.game_path,
                state="preparing",
                journal_path=journal_path,
                payload=None,
                created_at="2026-07-18T00:00:00+00:00",
                updated_at="2026-07-18T00:00:00+00:00",
                error="",
            )
        )
        return session.content_root, journal_path


def _inject_prepare_process_crash(
    monkeypatch: pytest.MonkeyPatch,
    *,
    content_root: Path,
    transaction_id: str,
    phase: str,
) -> tuple[Path, Path]:
    """在真实 journal/暂存/备份持久化点留下与强制退出等价的现场。"""
    first_target = content_root / "data" / "CrashOne.bin"
    second_target = content_root / "data" / "CrashTwo.bin"
    first_target.parent.mkdir(parents=True, exist_ok=True)
    _ = first_target.write_bytes(b"first-old")
    _ = second_target.write_bytes(b"second-old")

    persist_method = cast(
        Callable[[DurableFileWriteTransaction], None],
        getattr(DurableFileWriteTransaction, "_persist_journal"),
    )
    persist_call_count = 0
    crash_after_persist_call = {
        "initial_journal": 1,
        "prepared_journal": 2,
    }.get(phase)

    def persist_then_maybe_crash(transaction: DurableFileWriteTransaction) -> None:
        nonlocal persist_call_count
        persist_method(transaction)
        persist_call_count += 1
        if persist_call_count == crash_after_persist_call:
            raise _SimulatedProcessCrash(phase)

    monkeypatch.setattr(
        "app.application.write_transaction.DurableFileWriteTransaction._persist_journal",
        persist_then_maybe_crash,
    )

    def leave_crash_artifacts(_transaction: DurableFileWriteTransaction) -> None:
        return

    monkeypatch.setattr(
        "app.application.write_transaction.DurableFileWriteTransaction._cleanup_uncommitted_artifacts",
        leave_crash_artifacts,
    )

    module = importlib.import_module("app.application.write_transaction")
    if phase == "final_hash_before_journal":
        hash_native_files = cast(
            _HashNativeFiles,
            getattr(module, "hash_native_files"),
        )
        hash_call_count = 0

        def crash_after_final_hash(
            *,
            root: Path,
            files: Sequence[NativeFileHashInput],
        ) -> list[NativeFileHashResult]:
            nonlocal hash_call_count
            results = hash_native_files(root=root, files=files)
            hash_call_count += 1
            if hash_call_count == 2:
                raise _SimulatedProcessCrash(phase)
            return results

        monkeypatch.setattr(
            "app.application.write_transaction.hash_native_files",
            crash_after_final_hash,
        )

    stage_crash_call = {
        "first_stage_before_backup": 1,
        "second_stage_before_backup": 2,
    }.get(phase)
    if stage_crash_call is not None:
        write_bytes_durably = cast(
            _WriteBytesDurably,
            getattr(module, "_write_bytes_durably"),
        )
        stage_call_count = 0

        def crash_after_stage(*, target_path: Path, content: bytes) -> None:
            nonlocal stage_call_count
            stage_call_count += 1
            write_bytes_durably(target_path=target_path, content=content)
            if stage_call_count == stage_crash_call:
                raise _SimulatedProcessCrash(phase)

        monkeypatch.setattr(
            "app.application.write_transaction._write_bytes_durably",
            crash_after_stage,
        )

    backup_crash_call = {
        "first_backup_before_journal": 1,
        "second_backup_before_journal": 2,
    }.get(phase)
    if backup_crash_call is not None:
        copy_file_durably = cast(
            _CopyFileDurably,
            getattr(module, "_copy_file_durably"),
        )
        backup_call_count = 0

        def crash_after_backup(*, source_path: Path, target_path: Path) -> None:
            nonlocal backup_call_count
            backup_call_count += 1
            copy_file_durably(source_path=source_path, target_path=target_path)
            if backup_call_count == backup_crash_call:
                raise _SimulatedProcessCrash(phase)

        monkeypatch.setattr(
            "app.application.write_transaction._copy_file_durably",
            crash_after_backup,
        )

    with pytest.raises(_SimulatedProcessCrash):
        _ = DurableFileWriteTransaction.prepare(
            mode="write_back",
            content_root=content_root,
            transaction_id=transaction_id,
            writes=[
                PlannedFileWrite(target_path=first_target, content=b"first-new"),
                PlannedFileWrite(target_path=second_target, content=b"second-new"),
            ],
        )
    return first_target, second_target


async def _recover(registry: GameRegistry, game_title: str) -> WriteTransactionRecoverySummary:
    handler = TranslationHandler(registry, LLMHandler())
    try:
        return await handler.recover_write_transaction(game_title)
    finally:
        await handler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "initial_journal",
        "first_stage_before_backup",
        "first_backup_before_journal",
        "second_stage_before_backup",
        "second_backup_before_journal",
        "final_hash_before_journal",
        "prepared_journal",
    ],
)
async def test_preparing_process_crash_with_valid_journal_restores_complete_old_state(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """journal 已完整落盘时，任一逐项暂存阶段崩溃都必须确定性回到旧状态。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    transaction_id = f"crash{phase.replace('_', '')}"
    content_root, journal_path = await _create_preparing_record(
        registry,
        game.game_title,
        transaction_id=transaction_id,
    )
    first_target, second_target = _inject_prepare_process_crash(
        monkeypatch,
        content_root=content_root,
        transaction_id=transaction_id,
        phase=phase,
    )
    assert journal_path.is_file()
    if phase == "final_hash_before_journal":
        persisted = cast(
            dict[str, object],
            json.loads(journal_path.read_text(encoding="utf-8")),
        )
        persisted_entries = cast(list[dict[str, object]], persisted["entries"])
        assert all(entry["staged_sha256"] is None for entry in persisted_entries)

    summary = await _recover(registry, game.game_title)

    assert summary.previous_state == "preparing"
    assert summary.final_state == "rolled_back"
    assert first_target.read_bytes() == b"first-old"
    assert second_target.read_bytes() == b"second-old"
    assert not journal_path.exists()
    assert not list(content_root.rglob("*.att-mz-write-*"))
    async with await registry.open_game(game.game_title) as session:
        stored = await session.read_write_transaction(transaction_id)
        assert stored is not None
        assert stored.state == "rolled_back"
        assert stored.payload is None
        assert await session.read_unfinished_write_transactions() == []


@pytest.mark.asyncio
async def test_preparing_process_crash_without_journal_keeps_recovery_evidence(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """create 成功但首个 journal 尚未落盘时，只能保留数据库证据并要求人工恢复。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    transaction_id = "crashbeforejournal"
    _content_root, journal_path = await _create_preparing_record(
        registry,
        game.game_title,
        transaction_id=transaction_id,
    )
    assert not journal_path.exists()

    with pytest.raises(RecoveryRequiredError) as captured:
        _ = await _recover(registry, game.game_title)

    assert captured.value.code == "recovery_required"
    assert captured.value.details["transaction_id"] == transaction_id
    async with await registry.open_game(game.game_title) as session:
        stored = await session.read_write_transaction(transaction_id)
        assert stored is not None
        assert stored.state == "recovery_required"
        assert stored.payload is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_journal",
    ["corrupt", "extra_field", "transaction_mismatch", "mode_mismatch"],
)
async def test_preparing_process_crash_with_untrusted_journal_preserves_scene(
    minimal_game_dir: Path,
    tmp_path: Path,
    invalid_journal: str,
) -> None:
    """损坏或与 DB 绑定不一致的 journal 不得触碰目标和崩溃产物。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    transaction_id = f"invalid{invalid_journal.replace('_', '')}"
    content_root, journal_path = await _create_preparing_record(
        registry,
        game.game_title,
        transaction_id=transaction_id,
    )
    target_path = content_root / "data" / "CrashEvidence.bin"
    _ = target_path.write_bytes(b"old-evidence")
    if invalid_journal == "corrupt":
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        _ = journal_path.write_text("{not-json", encoding="utf-8")
        _ = target_path.with_name(f".{target_path.name}.att-mz-write-{transaction_id}.stage").write_bytes(
            b"untrusted-stage-evidence"
        )
        _ = target_path.with_name(f".{target_path.name}.att-mz-write-{transaction_id}.backup").write_bytes(
            b"old-evidence"
        )
    elif invalid_journal == "extra_field":
        transaction = DurableFileWriteTransaction.prepare(
            mode="write_back",
            content_root=content_root,
            transaction_id=transaction_id,
            writes=[PlannedFileWrite(target_path=target_path, content=b"new-evidence")],
        )
        journal = cast(dict[str, object], json.loads(transaction.journal_path.read_text(encoding="utf-8")))
        journal["unexpected"] = True
        _ = transaction.journal_path.write_text(
            json.dumps(journal, ensure_ascii=False),
            encoding="utf-8",
        )
    elif invalid_journal == "transaction_mismatch":
        other_transaction = DurableFileWriteTransaction.prepare(
            mode="write_back",
            content_root=content_root,
            transaction_id="othertransaction",
            writes=[PlannedFileWrite(target_path=target_path, content=b"new-evidence")],
        )
        _ = other_transaction.journal_path.replace(journal_path)
    else:
        _ = DurableFileWriteTransaction.prepare(
            mode="restore_font",
            content_root=content_root,
            transaction_id=transaction_id,
            writes=[PlannedFileWrite(target_path=target_path, content=b"new-evidence")],
        )

    evidence_before = sorted(path.name for path in content_root.rglob("*att-mz-write-*"))
    with pytest.raises(RecoveryRequiredError) as captured:
        _ = await _recover(registry, game.game_title)

    assert captured.value.code == "recovery_required"
    assert target_path.read_bytes() == b"old-evidence"
    assert journal_path.is_file()
    assert sorted(path.name for path in content_root.rglob("*att-mz-write-*")) == evidence_before
    async with await registry.open_game(game.game_title) as session:
        stored = await session.read_write_transaction(transaction_id)
        assert stored is not None
        assert stored.state == "recovery_required"
        assert stored.payload is None


@pytest.mark.asyncio
async def test_preparing_recovery_rejects_tampered_partial_artifact(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """journal 声明哈希的暂存产物被改动时，必须保留现场而不是当作可清理文件。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    transaction_id = "crashtamperedstage"
    content_root, journal_path = await _create_preparing_record(
        registry,
        game.game_title,
        transaction_id=transaction_id,
    )
    first_target, _second_target = _inject_prepare_process_crash(
        monkeypatch,
        content_root=content_root,
        transaction_id=transaction_id,
        phase="prepared_journal",
    )
    journal = cast(dict[str, object], json.loads(journal_path.read_text(encoding="utf-8")))
    entries = cast(list[dict[str, object]], journal["entries"])
    staged_relative_path = cast(str, entries[0]["staged_relative_path"])
    _ = (content_root / staged_relative_path).write_bytes(b"tampered-stage")

    with pytest.raises(RecoveryRequiredError):
        _ = await _recover(registry, game.game_title)

    assert first_target.read_bytes() == b"first-old"
    assert journal_path.is_file()
    assert (content_root / staged_relative_path).read_bytes() == b"tampered-stage"
