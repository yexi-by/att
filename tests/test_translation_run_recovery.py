"""翻译运行终态双重持久化失败的 durable 恢复测试。"""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast

import pytest

from app.application.handler import (
    _persist_terminal_translation_run,  # pyright: ignore[reportPrivateUsage]
)
from app.persistence import GameRegistry, TargetGameSession, TranslationRunRecoveryRequiredError
from app.persistence.translation_run_recovery import translation_run_recovery_path
from app.rmmz.schema import LlmFailureRecord, TranslationRunRecord


class _MutationLeaseHandle(Protocol):
    """测试中模拟进程退出时需要释放的系统锁句柄。"""

    def release(self) -> None: ...


async def _simulate_process_exit(session: TargetGameSession) -> None:
    """模拟操作系统在进程退出时关闭连接并释放独立锁句柄。"""
    await session.connection.close()
    session_state = vars(session)
    lease = cast(_MutationLeaseHandle | None, session_state.get("_mutation_lease"))
    session_state["_mutation_lease"] = None
    session_state["_closed"] = True
    if lease is not None:
        lease.release()


def _llm_failure(run_id: str) -> LlmFailureRecord:
    return LlmFailureRecord(
        run_id=run_id,
        category="server",
        error_type="InternalServerError",
        error_message="模型服务暂时不可用",
        retryable=True,
        attempt_count=2,
        created_at="2026-01-01T00:00:00",
    )


async def _start_run(registry: GameRegistry, game_title: str) -> tuple[TargetGameSession, TranslationRunRecord]:
    session = await registry.open_game_with_mutation_lease(game_title)
    run = await session.start_translation_run(
        total_extracted=3,
        pending_count=3,
        deduplicated_count=2,
        batch_count=1,
    )
    await session.persist_translation_batch(
        run,
        [],
        [],
        physical_request_count_delta=2,
        retry_request_count_delta=1,
    )
    return session, run.model_copy(update={"physical_request_count": 2, "retry_request_count": 1})


async def test_terminal_success_never_creates_recovery_sidecar(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(game.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        message = await _persist_terminal_translation_run(
            session=session,
            record=run.model_copy(
                update={
                    "status": "completed",
                    "finished_at": "2026-01-01T00:00:01",
                }
            ),
        )
        assert message is None
        assert not translation_run_recovery_path(game.db_path).exists()

    assert not translation_run_recovery_path(game.db_path).exists()


@pytest.mark.parametrize("failure_mode", ["persist", "update", "commit"])
async def test_continuous_terminal_failures_recover_on_process_reopen(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session, run = await _start_run(registry, game.game_title)
    original_terminal = session.persist_translation_run_terminal
    original_commit: Callable[[], Awaitable[None]] = session.connection.commit

    if failure_mode == "persist":

        async def fail_persist(
            _record: TranslationRunRecord,
            _failure: LlmFailureRecord | None = None,
        ) -> None:
            raise OSError("simulated persistent terminal failure")

        monkeypatch.setattr(session, "persist_translation_run_terminal", fail_persist)
    elif failure_mode == "update":
        _ = await session.connection.execute(
            """
            CREATE TRIGGER [test_continuous_terminal_update_failure]
            BEFORE UPDATE ON [translation_runs]
            BEGIN
                SELECT RAISE(ABORT, 'simulated continuous terminal update failure');
            END
            """
        )
        await session.connection.commit()
    else:

        async def fail_commit() -> None:
            raise OSError("simulated continuous terminal commit failure")

        monkeypatch.setattr(session.connection, "commit", fail_commit)

    message = await _persist_terminal_translation_run(
        session=session,
        record=run.model_copy(
            update={
                "status": "failed",
                "llm_failure_count": 1,
                "finished_at": "2026-01-01T00:00:02",
                "stop_reason": "模型请求失败",
                "last_error": "llm_request_failed",
            }
        ),
        llm_failure=_llm_failure(run.run_id),
    )

    assert message is not None
    recovery_path = translation_run_recovery_path(game.db_path)
    assert recovery_path.is_file()
    async with session.connection.execute(
        "SELECT status, physical_request_count, retry_request_count FROM translation_runs WHERE run_id = ?",
        (run.run_id,),
    ) as cursor:
        running_row = await cursor.fetchone()
    assert running_row is not None
    assert tuple(running_row) == ("running", 2, 1)

    if failure_mode == "persist":
        monkeypatch.setattr(session, "persist_translation_run_terminal", original_terminal)
    elif failure_mode == "update":
        _ = await session.connection.execute("DROP TRIGGER [test_continuous_terminal_update_failure]")
        await session.connection.commit()
    else:
        monkeypatch.setattr(session.connection, "commit", original_commit)
    # 模拟进程在写入 durable 日志后退出，不执行当前会话的 close 协调。
    await _simulate_process_exit(session)

    async with await registry.open_game(game.game_title) as reopened:
        recovered = await reopened.read_translation_run(run.run_id)
        assert recovered is not None
        assert recovered.status == "failed"
        assert recovered.last_error == "persistence_failed"
        assert recovered.llm_failure_count == 0
        assert recovered.physical_request_count == 2
        assert recovered.retry_request_count == 1
        assert await reopened.read_llm_failures(run.run_id) == []
    assert not recovery_path.exists()


async def test_commit_then_raise_preserves_already_committed_terminal(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一次 commit 已成功但抛错时，恢复不得把合法 completed 覆盖成 failed。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session, run = await _start_run(registry, game.game_title)
    original_commit: Callable[[], Awaitable[None]] = session.connection.commit
    commit_calls = 0

    async def commit_once_then_raise_and_fail_before_second_commit() -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            await original_commit()
            raise OSError("simulated commit acknowledgement loss")
        raise OSError("simulated fallback commit failure")

    monkeypatch.setattr(session.connection, "commit", commit_once_then_raise_and_fail_before_second_commit)
    message = await _persist_terminal_translation_run(
        session=session,
        record=run.model_copy(
            update={
                "status": "completed",
                "finished_at": "2026-01-01T00:00:03",
                "stop_reason": "",
                "last_error": "",
            }
        ),
    )
    assert message is None
    assert commit_calls == 1
    recovery_path = translation_run_recovery_path(game.db_path)
    assert not recovery_path.exists()

    monkeypatch.setattr(session.connection, "commit", original_commit)
    await session.close()
    async with await registry.open_game(game.game_title) as reopened:
        recovered = await reopened.read_translation_run(run.run_id)
        assert recovered is not None
        assert recovered.status == "completed"
        assert recovered.finished_at == "2026-01-01T00:00:03"
        assert recovered.last_error == ""
        assert recovered.physical_request_count == 2
        assert recovered.retry_request_count == 1
    assert not recovery_path.exists()


async def test_fallback_commit_then_raise_preserves_committed_persistence_failed(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fallback commit 已落盘但确认丢失时，精确回读且不创建 sidecar。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session, run = await _start_run(registry, game.game_title)
    original_terminal = session.persist_translation_run_terminal
    original_commit: Callable[[], Awaitable[None]] = session.connection.commit
    commit_calls = 0
    terminal_calls = 0

    async def commit_then_raise() -> None:
        nonlocal commit_calls
        commit_calls += 1
        await original_commit()
        raise OSError(f"simulated lost commit acknowledgement {commit_calls}")

    async def fail_attempted_then_persist_fallback(
        terminal_record: TranslationRunRecord,
        failure: LlmFailureRecord | None = None,
    ) -> None:
        nonlocal terminal_calls
        terminal_calls += 1
        if terminal_calls == 1:
            raise OSError("simulated attempted terminal failure")
        await original_terminal(terminal_record, failure)

    monkeypatch.setattr(session.connection, "commit", commit_then_raise)
    monkeypatch.setattr(session, "persist_translation_run_terminal", fail_attempted_then_persist_fallback)
    message = await _persist_terminal_translation_run(
        session=session,
        record=run.model_copy(
            update={
                "status": "completed",
                "finished_at": "2026-01-01T00:00:03",
            }
        ),
    )
    assert message is not None
    assert terminal_calls == 2
    assert commit_calls == 1
    recovery_path = translation_run_recovery_path(game.db_path)
    assert not recovery_path.exists()

    monkeypatch.setattr(session.connection, "commit", original_commit)
    monkeypatch.setattr(session, "persist_translation_run_terminal", original_terminal)
    await session.close()
    async with await registry.open_game(game.game_title) as reopened:
        recovered = await reopened.read_translation_run(run.run_id)
        assert recovered is not None
        assert recovered.status == "failed"
        assert recovered.last_error == "persistence_failed"
        assert recovered.llm_failure_count == 0
        assert recovered.physical_request_count == 2
        assert recovered.retry_request_count == 1
    assert not recovery_path.exists()


async def test_corrupt_recovery_log_blocks_read_and_reopen_with_structured_error(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session, run = await _start_run(registry, game.game_title)

    async def fail_persist(
        _record: TranslationRunRecord,
        _failure: LlmFailureRecord | None = None,
    ) -> None:
        raise OSError("simulated terminal failure")

    monkeypatch.setattr(session, "persist_translation_run_terminal", fail_persist)
    _ = await _persist_terminal_translation_run(
        session=session,
        record=run.model_copy(update={"status": "failed", "finished_at": "2026-01-01T00:00:04"}),
    )
    recovery_path = translation_run_recovery_path(game.db_path)
    _ = recovery_path.write_text('{"version":1,"damaged":true}', encoding="utf-8")

    with pytest.raises(TranslationRunRecoveryRequiredError) as read_error:
        _ = await session.read_translation_run(run.run_id)
    assert read_error.value.code == "translation_run_recovery_required"
    with pytest.raises(TranslationRunRecoveryRequiredError):
        _ = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
    assert recovery_path.exists()
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError) as open_error:
        async with await registry.open_game(game.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert open_error.value.code == "translation_run_recovery_required"
    assert open_error.value.details["recovery_path"] == str(recovery_path)
    assert recovery_path.exists()
    with sqlite3.connect(game.db_path) as raw_connection:
        status = cast(
            tuple[str] | None,
            raw_connection.execute(
                "SELECT status FROM translation_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone(),
        )
    assert status == ("running",)


async def test_recovery_rejects_database_identity_drift_without_deleting_evidence(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session, run = await _start_run(registry, game.game_title)

    async def fail_persist(
        _record: TranslationRunRecord,
        _failure: LlmFailureRecord | None = None,
    ) -> None:
        raise OSError("simulated terminal failure")

    monkeypatch.setattr(session, "persist_translation_run_terminal", fail_persist)
    _ = await _persist_terminal_translation_run(
        session=session,
        record=run.model_copy(update={"status": "failed", "finished_at": "2026-01-01T00:00:05"}),
    )
    recovery_path = translation_run_recovery_path(game.db_path)
    _ = await session.connection.execute(
        "UPDATE translation_runs SET started_at = 'externally-modified' WHERE run_id = ?",
        (run.run_id,),
    )
    await session.connection.commit()
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError) as raised:
        async with await registry.open_game(game.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert raised.value.code == "translation_run_recovery_required"
    assert raised.value.details["run_id"] == run.run_id
    assert recovery_path.exists()
    with sqlite3.connect(game.db_path) as raw_connection:
        started_at = cast(
            tuple[str] | None,
            raw_connection.execute(
                "SELECT started_at FROM translation_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone(),
        )
    assert started_at == ("externally-modified",)


async def test_registration_does_not_replace_missing_database_with_recovery_evidence(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session, run = await _start_run(registry, game.game_title)

    async def fail_persist(
        _record: TranslationRunRecord,
        _failure: LlmFailureRecord | None = None,
    ) -> None:
        raise OSError("simulated terminal failure")

    monkeypatch.setattr(session, "persist_translation_run_terminal", fail_persist)
    _ = await _persist_terminal_translation_run(
        session=session,
        record=run.model_copy(update={"status": "failed", "finished_at": "2026-01-01T00:00:06"}),
    )
    recovery_path = translation_run_recovery_path(game.db_path)
    await _simulate_process_exit(session)
    game.db_path.unlink()

    with pytest.raises(TranslationRunRecoveryRequiredError) as open_error:
        _ = await registry.open_game(game.game_title)
    assert open_error.value.code == "translation_run_recovery_required"
    with pytest.raises(TranslationRunRecoveryRequiredError) as raised:
        _ = await registry.register_game(minimal_game_dir, source_language="ja")
    assert raised.value.code == "translation_run_recovery_required"
    assert not game.db_path.exists()
    assert recovery_path.exists()


async def test_cancellation_waits_until_double_failure_sidecar_is_durable(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session, run = await _start_run(registry, game.game_title)
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    call_count = 0

    async def blocking_failure(
        _record: TranslationRunRecord,
        _failure: LlmFailureRecord | None = None,
    ) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_write_started.set()
            _ = await release_first_write.wait()
        raise OSError("simulated terminal failure during cancellation")

    monkeypatch.setattr(session, "persist_translation_run_terminal", blocking_failure)
    terminal_task = asyncio.create_task(
        _persist_terminal_translation_run(
            session=session,
            record=run.model_copy(update={"status": "failed", "finished_at": "2026-01-01T00:00:06"}),
        )
    )
    _ = await asyncio.wait_for(first_write_started.wait(), timeout=1)
    _ = terminal_task.cancel()
    await asyncio.sleep(0)
    _ = terminal_task.cancel()
    await asyncio.sleep(0)
    assert not terminal_task.done()

    release_first_write.set()
    with pytest.raises(asyncio.CancelledError):
        await terminal_task
    assert terminal_task.done()
    assert call_count == 2
    recovery_path = translation_run_recovery_path(game.db_path)
    assert recovery_path.is_file()

    # close 在释放资源前协调恢复；没有遗留后台任务或永久 running。
    await session.close()
    assert not recovery_path.exists()
    async with await registry.open_game(game.game_title) as reopened:
        recovered = await reopened.read_translation_run(run.run_id)
        assert recovered is not None
        assert recovered.status == "failed"
        assert recovered.last_error == "persistence_failed"
