"""修改命令跨进程租约与会话生命周期契约。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.application.mutation_guard import open_game_for_mutation, open_game_for_recovery
from app.persistence import GameRegistry, MutationLeaseContendedError, RecoveryRequiredError
from app.persistence.mutation_lease import GameMutationLease, mutation_lease_path
from app.persistence.records import WriteTransactionRecord

_PROJECT_ROOT = Path(__file__).parents[1]
_LOCK_HOLDER_CODE = """
import sys
from pathlib import Path
from app.persistence.mutation_lease import GameMutationLease

lease = GameMutationLease.acquire(db_path=Path(sys.argv[1]))
print("locked", flush=True)
if sys.stdin.readline().strip() == "release":
    lease.release()
"""


def test_same_process_second_lease_fails_immediately_and_release_is_idempotent(tmp_path: Path) -> None:
    """同一进程的独立会话不能依赖平台锁的进程内特殊语义。"""
    db_path = tmp_path / "game.db"
    first = GameMutationLease.acquire(db_path=db_path)
    try:
        with pytest.raises(MutationLeaseContendedError) as raised:
            _ = GameMutationLease.acquire(db_path=db_path)
        assert raised.value.code == "mutation_in_progress"
        assert raised.value.details == {
            "db_path": str(db_path.resolve()),
            "lock_path": str(mutation_lease_path(db_path.resolve())),
        }
    finally:
        first.release()
        first.release()

    second = GameMutationLease.acquire(db_path=db_path)
    second.release()
    assert mutation_lease_path(db_path.resolve()).is_file()


def test_independent_process_holds_same_nonblocking_lease(tmp_path: Path) -> None:
    """另一进程持锁时必须返回稳定冲突，释放后无需删除锁文件即可继续。"""
    db_path = tmp_path / "game.db"
    process = _start_lock_holder(db_path)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(MutationLeaseContendedError):
            _ = GameMutationLease.acquire(db_path=db_path)
        assert process.stdin is not None
        _ = process.stdin.write("release\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 0
    finally:
        _stop_process(process)

    lease = GameMutationLease.acquire(db_path=db_path)
    lease.release()


def test_process_crash_releases_operating_system_lease(tmp_path: Path) -> None:
    """持锁进程被强制终止后，操作系统必须释放锁且持久锁文件继续复用。"""
    db_path = tmp_path / "game.db"
    process = _start_lock_holder(db_path)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        process.kill()
        _ = process.wait(timeout=10)
    finally:
        _stop_process(process)

    lease = GameMutationLease.acquire(db_path=db_path)
    lease.release()
    assert mutation_lease_path(db_path.resolve()).is_file()


@pytest.mark.asyncio
async def test_mutation_session_holds_lease_until_close(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """规则导入等长命令持有会话期间，第二个修改入口不能穿透。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    first = await open_game_for_mutation(registry, record.game_title)
    try:
        with pytest.raises(MutationLeaseContendedError):
            _ = await open_game_for_mutation(registry, record.game_title)
    finally:
        await first.close()
        await first.close()

    second = await open_game_for_mutation(registry, record.game_title)
    await second.close()


@pytest.mark.asyncio
async def test_recovery_lease_allows_unfinished_transaction_but_excludes_mutations(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """恢复入口允许读取未完成事务，同时继续独占全部修改入口。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    mutation_session = await open_game_for_mutation(registry, record.game_title)
    transaction_id = "lease-recovery"
    try:
        await mutation_session.create_write_transaction(
            WriteTransactionRecord(
                transaction_id=transaction_id,
                operation="write_back",
                game_path=record.game_path,
                state="preparing",
                journal_path=record.content_root / ".att-mz-write-transactions" / f"{transaction_id}.json",
                payload=None,
                created_at="2026-07-18T00:00:00+00:00",
                updated_at="2026-07-18T00:00:00+00:00",
                error="",
            )
        )
    finally:
        await mutation_session.close()

    with pytest.raises(RecoveryRequiredError):
        _ = await open_game_for_mutation(registry, record.game_title)

    recovery_session = await open_game_for_recovery(registry, record.game_title)
    try:
        unfinished = await recovery_session.read_unfinished_write_transactions()
        assert [item.transaction_id for item in unfinished] == [transaction_id]
        with pytest.raises(MutationLeaseContendedError):
            _ = await open_game_for_mutation(registry, record.game_title)
    finally:
        await recovery_session.close()


@pytest.mark.asyncio
async def test_reregister_uses_same_game_lease(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """重复注册在重判数据库状态前必须取得与其他修改命令相同的锁。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    lease = GameMutationLease.acquire(db_path=record.db_path)
    try:
        with pytest.raises(MutationLeaseContendedError):
            _ = await registry.register_game(minimal_game_dir, source_language="ja")
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_mutation_session_reads_registration_path_only_after_acquiring_lease(
    tmp_path: Path,
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注册路径更新不能插入 mutation session 取得租约与读取元数据之间。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    replacement_game_dir = tmp_path / "replacement-game"
    _ = shutil.copytree(minimal_game_dir, replacement_game_dir)

    original_open = registry._open_game  # pyright: ignore[reportPrivateUsage]
    metadata_read_started = asyncio.Event()
    continue_metadata_read = asyncio.Event()

    async def paused_open(*, game_title: str, mutation_lease: GameMutationLease | None):
        assert mutation_lease is not None
        metadata_read_started.set()
        _ = await continue_metadata_read.wait()
        return await original_open(game_title=game_title, mutation_lease=mutation_lease)

    monkeypatch.setattr(registry, "_open_game", paused_open)
    open_task = asyncio.create_task(open_game_for_mutation(registry, record.game_title))
    _ = await metadata_read_started.wait()

    registration_error: MutationLeaseContendedError | None = None
    try:
        _ = await registry.register_game(replacement_game_dir, source_language="ja")
    except MutationLeaseContendedError as error:
        registration_error = error
    finally:
        continue_metadata_read.set()

    session = await open_task
    try:
        assert registration_error is not None
        assert session.game_path == record.game_path
    finally:
        await session.close()

    monkeypatch.setattr(registry, "_open_game", original_open)
    updated = await registry.register_game(replacement_game_dir, source_language="ja")
    assert updated.game_path == replacement_game_dir.resolve()
    refreshed = await open_game_for_mutation(registry, record.game_title)
    try:
        assert refreshed.game_path == replacement_game_dir.resolve()
    finally:
        await refreshed.close()


def _start_lock_holder(db_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [_subprocess_python_executable(), "-c", _LOCK_HOLDER_CODE, str(db_path)],
        cwd=_PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)},
        text=True,
        encoding="utf-8",
    )


def _subprocess_python_executable() -> str:
    """绕过 Windows venv 启动器，确保 kill 作用于真正持锁的解释器。"""
    base_executable = getattr(sys, "_base_executable", None)
    if sys.platform == "win32" and isinstance(base_executable, str) and base_executable:
        return base_executable
    return sys.executable


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.kill()
    _ = process.wait(timeout=10)
