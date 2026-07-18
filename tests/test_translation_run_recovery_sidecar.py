"""翻译终态恢复文件的身份、上限和精确状态测试。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, cast

import pytest

from app.application.handler import (
    _persist_terminal_translation_run,  # pyright: ignore[reportPrivateUsage]
)
from app.persistence import GameRegistry, TargetGameSession, TranslationRunRecoveryRequiredError
from app.persistence.translation_run_recovery import (
    build_bounded_persistence_failure_text,
    translation_run_recovery_path,
)
from app.rmmz.schema import LlmFailureRecord, TranslationRunRecord


class _MutationLeaseHandle(Protocol):
    def release(self) -> None: ...


async def _simulate_process_exit(session: TargetGameSession) -> None:
    await session.connection.close()
    session_state = vars(session)
    lease = cast(_MutationLeaseHandle | None, session_state.get("_mutation_lease"))
    session_state["_mutation_lease"] = None
    session_state["_closed"] = True
    if lease is not None:
        lease.release()


async def _start_run(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> tuple[GameRegistry, TargetGameSession, TranslationRunRecord]:
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    session = await registry.open_game_with_mutation_lease(game.game_title)
    run = await session.start_translation_run(
        total_extracted=5,
        pending_count=5,
        deduplicated_count=4,
        batch_count=2,
    )
    return registry, session, run


def _terminal_records(
    run: TranslationRunRecord,
    *,
    physical_request_count: int = 0,
    retry_request_count: int = 0,
    fallback_reason: str = "终态保存失败",
) -> tuple[TranslationRunRecord, TranslationRunRecord]:
    attempted = run.model_copy(
        update={
            "status": "completed",
            "physical_request_count": physical_request_count,
            "retry_request_count": retry_request_count,
            "finished_at": "2026-01-01T00:00:01",
        }
    )
    fallback = attempted.model_copy(
        update={
            "status": "failed",
            "llm_failure_count": 0,
            "finished_at": "2026-01-01T00:00:02",
            "stop_reason": fallback_reason,
            "last_error": "persistence_failed",
        }
    )
    return attempted, fallback


async def test_recovery_sidecar_no_clobber_preserves_existing_evidence(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    _registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run)
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    original = path.read_bytes()

    with pytest.raises(TranslationRunRecoveryRequiredError):
        _ = await session.write_translation_run_recovery(
            attempted_record=attempted,
            fallback_record=fallback.model_copy(update={"stop_reason": "另一个恢复意图"}),
        )
    assert path.read_bytes() == original
    await _simulate_process_exit(session)


@pytest.mark.skipif(os.name != "nt", reason="该竞态断言固定 Windows os.rename 的 no-clobber 契约")
async def test_recovery_sidecar_publish_race_does_not_overwrite_winner(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run)
    path = translation_run_recovery_path(session.db_path)
    original_rename = os.rename
    winner = b"existing recovery evidence"

    def race_rename(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        if Path(os.fsdecode(target)) == path:
            _ = path.write_bytes(winner)
        original_rename(source, target)

    monkeypatch.setattr(os, "rename", race_rename)
    with pytest.raises(TranslationRunRecoveryRequiredError):
        _ = await session.write_translation_run_recovery(
            attempted_record=attempted,
            fallback_record=fallback,
        )
    assert path.read_bytes() == winner
    await _simulate_process_exit(session)


async def test_recovery_sidecar_rejects_hardlink_and_preserves_both_paths(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run)
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    hardlink_path = path.with_name("hardlink-evidence.json")
    os.link(path, hardlink_path)
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError):
        async with await registry.open_game(session.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert path.exists()
    assert hardlink_path.exists()


async def test_recovery_sidecar_rejects_reparse_symlink(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    probe_target = tmp_path / "probe-target"
    probe_link = tmp_path / "probe-link"
    _ = probe_target.write_text("probe", encoding="utf-8")
    try:
        os.symlink(probe_target, probe_link)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("当前普通用户没有创建符号链接的权限")
        raise
    probe_link.unlink()

    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run)
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    target = path.with_name("symlink-target.json")
    _ = target.write_bytes(path.read_bytes())
    path.unlink()
    os.symlink(target, path)
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError):
        async with await registry.open_game(session.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert path.is_symlink()
    assert target.exists()


async def test_recovery_sidecar_rejects_oversized_replacement(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run)
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    _ = path.write_bytes(b"x" * (64 * 1024 + 1))
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError):
        async with await registry.open_game(session.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert path.stat().st_size == 64 * 1024 + 1


async def test_recovery_sidecar_restores_uncommitted_physical_request_counts(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(
        run,
        physical_request_count=5,
        retry_request_count=3,
    )
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    await _simulate_process_exit(session)

    async with await registry.open_game(session.game_title) as reopened:
        recovered = await reopened.read_translation_run(run.run_id)
        assert recovered is not None
        assert recovered.status == "failed"
        assert recovered.physical_request_count == 5
        assert recovered.retry_request_count == 3
    assert not path.exists()
    assert len(list(path.parent.glob(f".{path.name}.*.resolved"))) == 1


async def test_recovery_sidecar_allows_dynamic_batch_plan_growth(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run.model_copy(update={"batch_count": 3}))
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    await _simulate_process_exit(session)

    async with await registry.open_game(session.game_title) as reopened:
        recovered = await reopened.read_translation_run(run.run_id)
        assert recovered is not None
        assert recovered.status == "failed"
        assert recovered.batch_count == 3
    assert not path.exists()


async def test_recovery_sidecar_rejects_unpersisted_success_and_quality_counts(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run.model_copy(update={"success_count": 1, "quality_error_count": 1}))
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError):
        async with await registry.open_game(session.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert path.exists()


async def test_recovery_sidecar_rejects_impossible_retry_delta(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    current = run.model_copy(update={"physical_request_count": 4, "retry_request_count": 0})
    await session.write_translation_run(current)
    attempted, fallback = _terminal_records(
        current,
        physical_request_count=5,
        retry_request_count=3,
    )
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError):
        async with await registry.open_game(session.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert path.exists()


async def test_recovery_sidecar_rejects_replaced_llm_failure_with_same_row_count(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run)
    attempted = attempted.model_copy(
        update={
            "status": "failed",
            "llm_failure_count": 1,
            "stop_reason": "模型失败",
            "last_error": "llm_request_failed",
        }
    )
    failure = LlmFailureRecord(
        run_id=run.run_id,
        category="rate_limit",
        error_type="RateLimitError",
        error_message="原始故障",
        retryable=True,
        attempt_count=2,
        created_at="2026-01-01T00:00:01",
    )
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        attempted_failure=failure,
        fallback_record=fallback,
    )
    _ = await session.connection.execute(
        """
        UPDATE translation_runs
        SET status = ?, llm_failure_count = 1, finished_at = ?, stop_reason = ?, last_error = ?
        WHERE run_id = ?
        """,
        (
            attempted.status,
            attempted.finished_at,
            attempted.stop_reason,
            attempted.last_error,
            run.run_id,
        ),
    )
    _ = await session.connection.execute(
        """
        INSERT INTO llm_failures
        (run_id, category, error_type, error_message, retryable, attempt_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run.run_id,
            failure.category,
            failure.error_type,
            "被替换的同数量故障",
            1,
            failure.attempt_count,
            failure.created_at,
        ),
    )
    await session.connection.commit()
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError):
        async with await registry.open_game(session.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert path.exists()


async def test_recovery_sidecar_rejects_unrelated_terminal_state(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(run)
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    _ = await session.connection.execute(
        """
        UPDATE translation_runs
        SET status = 'completed', finished_at = ?, stop_reason = 'external terminal'
        WHERE run_id = ?
        """,
        ("2026-01-01T00:00:03", run.run_id),
    )
    await session.connection.commit()
    await _simulate_process_exit(session)

    with pytest.raises(TranslationRunRecoveryRequiredError):
        async with await registry.open_game(session.game_title) as reopened:
            _ = await reopened.read_translation_run(run.run_id)
    assert path.exists()


async def test_translation_recovery_required_error_is_not_wrapped_or_retried(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    error = TranslationRunRecoveryRequiredError(
        db_path=session.db_path,
        recovery_path=translation_run_recovery_path(session.db_path),
        reason="existing recovery evidence",
        run_id=run.run_id,
    )
    call_count = 0

    async def raise_recovery_required(
        _record: TranslationRunRecord,
        _failure: LlmFailureRecord | None = None,
    ) -> None:
        nonlocal call_count
        call_count += 1
        raise error

    monkeypatch.setattr(session, "persist_translation_run_terminal", raise_recovery_required)
    with pytest.raises(TranslationRunRecoveryRequiredError) as raised:
        _ = await _persist_terminal_translation_run(
            session=session,
            record=run.model_copy(update={"status": "completed", "finished_at": "2026-01-01T00:00:01"}),
        )
    assert raised.value is error
    assert call_count == 1
    assert not translation_run_recovery_path(session.db_path).exists()
    await _simulate_process_exit(session)


async def test_bounded_error_summary_keeps_sidecar_below_reader_limit(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    _registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    summary = build_bounded_persistence_failure_text(RuntimeError("错" * 100_000))
    assert len(summary) <= 2048
    assert "sha256=" in summary
    attempted, fallback = _terminal_records(run, fallback_reason=summary)
    path = await session.write_translation_run_recovery(
        attempted_record=attempted,
        fallback_record=fallback,
    )
    assert 0 < path.stat().st_size <= 64 * 1024
    await _simulate_process_exit(session)


async def test_recovery_sidecar_rejects_counts_outside_sqlite_integer_range(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    _registry, session, run = await _start_run(minimal_game_dir, tmp_path)
    attempted, fallback = _terminal_records(
        run,
        physical_request_count=1 << 63,
    )
    with pytest.raises(TranslationRunRecoveryRequiredError):
        _ = await session.write_translation_run_recovery(
            attempted_record=attempted,
            fallback_record=fallback,
        )
    assert not translation_run_recovery_path(session.db_path).exists()
    await _simulate_process_exit(session)
