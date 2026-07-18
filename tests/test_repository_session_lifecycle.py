"""数据库 no-create 打开、会话清理与修改入口排序契约。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

import app.persistence.repository as repository_module
from app.application.mutation_guard import open_game_for_mutation, open_game_for_recovery
from app.persistence import GameRegistry, RecoveryRequiredError, TranslationRunRecoveryRequiredError
from app.persistence.errors import MutationLeaseError
from app.persistence.mutation_lease import GameMutationLease
from app.persistence.records import WriteTransactionRecord


@pytest.mark.asyncio
async def test_existing_database_open_never_recreates_file_deleted_after_availability_check(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exists 与 SQLite connect 之间删除数据库时，mode=rw 必须失败且不得留下空库。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    original_open = repository_module.open_existing_connection

    async def delete_then_open(db_path: Path):
        db_path.unlink()
        return await original_open(db_path)

    monkeypatch.setattr(repository_module, "open_existing_connection", delete_then_open)

    with pytest.raises(ValueError, match="未找到游戏数据库"):
        _ = await registry.open_game(game.game_title)

    assert not game.db_path.exists()


@pytest.mark.asyncio
async def test_open_existing_connection_does_not_create_missing_database(tmp_path: Path) -> None:
    """既有库连接助手自身也必须保持 no-create 语义。"""
    missing_path = tmp_path / "不存在的 游戏.db"

    with pytest.raises(FileNotFoundError):
        _ = await repository_module.open_existing_connection(missing_path)

    assert not missing_path.exists()


@pytest.mark.asyncio
async def test_close_drains_repeated_cancellation_and_reuses_one_cleanup_task(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续取消 close 等待者不能取消共享清理，也不能遗留修改租约。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session = await registry.open_game_with_mutation_lease(game.game_title)
    _ = await session.reconcile_translation_run_recovery()
    reconcile_started = asyncio.Event()
    allow_reconcile = asyncio.Event()
    reconcile_calls = 0

    async def blocking_reconcile() -> bool:
        nonlocal reconcile_calls
        reconcile_calls += 1
        reconcile_started.set()
        _ = await allow_reconcile.wait()
        return False

    monkeypatch.setattr(session, "reconcile_translation_run_recovery", blocking_reconcile)
    close_waiter = asyncio.create_task(session.close())
    _ = await asyncio.wait_for(reconcile_started.wait(), timeout=1)
    shared_waiter = asyncio.create_task(session.close())
    await asyncio.sleep(0)

    _ = close_waiter.cancel()
    await asyncio.sleep(0)
    assert not close_waiter.done()
    _ = close_waiter.cancel()
    await asyncio.sleep(0)
    assert not close_waiter.done()

    allow_reconcile.set()
    await shared_waiter
    with pytest.raises(asyncio.CancelledError):
        await close_waiter

    assert reconcile_calls == 1
    await session.close()
    reopened = await registry.open_game_with_mutation_lease(game.game_title)
    await reopened.close()


@pytest.mark.asyncio
async def test_close_preserves_recovery_error_and_attaches_cleanup_failures_as_notes(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """翻译恢复错误必须保持主错误，连接与租约失败只能成为附注。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session = await registry.open_game_with_mutation_lease(game.game_title)
    _ = await session.reconcile_translation_run_recovery()
    recovery_error = TranslationRunRecoveryRequiredError(
        db_path=game.db_path,
        recovery_path=game.db_path.with_suffix(".recovery.json"),
        reason="故障注入",
    )
    original_connection_close = session.connection.close
    lease = session._mutation_lease  # pyright: ignore[reportPrivateUsage]
    assert isinstance(lease, GameMutationLease)
    original_lease_release = lease.release

    async def fail_recovery() -> bool:
        raise recovery_error

    async def close_then_fail() -> None:
        await original_connection_close()
        raise OSError("connection close failed")

    def release_then_fail() -> None:
        original_lease_release()
        raise MutationLeaseError(
            db_path=game.db_path,
            lock_path=lease.lock_path,
            reason="lease release failed",
        )

    monkeypatch.setattr(session, "reconcile_translation_run_recovery", fail_recovery)
    monkeypatch.setattr(session.connection, "close", close_then_fail)
    monkeypatch.setattr(lease, "release", release_then_fail)

    with pytest.raises(TranslationRunRecoveryRequiredError) as raised:
        await session.close()

    assert raised.value is recovery_error
    notes = cast(list[str], getattr(raised.value, "__notes__", []))
    assert any("关闭 SQLite 连接" in note and "connection close failed" in note for note in notes)
    assert any("释放修改租约" in note and "lease release failed" in note for note in notes)

    with pytest.raises(TranslationRunRecoveryRequiredError) as repeated:
        await session.close()
    assert repeated.value is recovery_error


@pytest.mark.asyncio
async def test_context_manager_preserves_body_error_when_close_also_fails(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上下文正文错误优先于退出清理错误，后者仅作为正文异常附注。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session = await registry.open_game_with_mutation_lease(game.game_title)
    _ = await session.reconcile_translation_run_recovery()
    close_error = TranslationRunRecoveryRequiredError(
        db_path=game.db_path,
        recovery_path=game.db_path.with_suffix(".recovery.json"),
        reason="退出故障注入",
    )

    async def fail_recovery() -> bool:
        raise close_error

    monkeypatch.setattr(session, "reconcile_translation_run_recovery", fail_recovery)
    body_error = RuntimeError("body failed")

    with pytest.raises(RuntimeError, match="body failed") as raised:
        async with session:
            raise body_error

    assert raised.value is body_error
    notes = cast(list[str], getattr(raised.value, "__notes__", []))
    assert any("关闭游戏数据库会话" in note and "退出故障注入" in note for note in notes)


@pytest.mark.asyncio
async def test_unfinished_write_transaction_blocks_before_stale_translation_is_changed(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """写回事务优先级更高；阻断时不得顺手把旧 running 改成 failed。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    setup_session = await registry.open_game_with_mutation_lease(game.game_title)
    run = await setup_session.start_translation_run(
        total_extracted=1,
        pending_count=1,
        deduplicated_count=1,
        batch_count=1,
    )
    transaction_id = "write-blocks-translation-reconcile"
    await setup_session.create_write_transaction(
        WriteTransactionRecord(
            transaction_id=transaction_id,
            operation="write_back",
            game_path=game.game_path,
            state="preparing",
            journal_path=game.content_root / ".att-mz-write-transactions" / f"{transaction_id}.json",
            payload=None,
            created_at="2026-07-18T00:00:00+00:00",
            updated_at="2026-07-18T00:00:00+00:00",
            error="",
        )
    )
    await setup_session.close()

    with pytest.raises(RecoveryRequiredError):
        _ = await open_game_for_mutation(registry, game.game_title)

    recovery_session = await open_game_for_recovery(registry, game.game_title)
    try:
        async with recovery_session.connection.execute(
            "SELECT status FROM translation_runs WHERE run_id = ?",
            (run.run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "running"
    finally:
        await recovery_session.close()


@pytest.mark.asyncio
async def test_read_only_and_write_recovery_opens_do_not_reconcile_stale_translation(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """注册表只负责打开；普通读取和写回恢复入口都不隐式修改翻译状态。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    setup_session = await registry.open_game_with_mutation_lease(game.game_title)
    run = await setup_session.start_translation_run(
        total_extracted=1,
        pending_count=1,
        deduplicated_count=1,
        batch_count=1,
    )
    await setup_session.close()

    read_session = await registry.open_game(game.game_title)
    await _assert_run_status(read_session, run.run_id, "running")
    await read_session.close()

    recovery_session = await open_game_for_recovery(registry, game.game_title)
    await _assert_run_status(recovery_session, run.run_id, "running")
    await recovery_session.close()


async def _assert_run_status(session: repository_module.TargetGameSession, run_id: str, expected: str) -> None:
    async with session.connection.execute(
        "SELECT status FROM translation_runs WHERE run_id = ?",
        (run_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == expected
