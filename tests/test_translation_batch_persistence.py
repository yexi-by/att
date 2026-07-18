"""正文批次原子持久化测试。"""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.application.handler import (
    _persist_terminal_translation_run,  # pyright: ignore[reportPrivateUsage]
)
from app.persistence import GameRegistry, TranslationReuseContext
from app.rmmz.schema import LlmFailureRecord, TranslationErrorItem, TranslationItem, TranslationRunRecord


def _success_item() -> TranslationItem:
    return TranslationItem(
        location_path="CommonEvents.json/1/0",
        item_type="short_text",
        original_lines=["こんにちは"],
        translation_lines=["你好"],
    )


def _error_item() -> TranslationErrorItem:
    return TranslationErrorItem(
        location_path="CommonEvents.json/1/1",
        item_type="short_text",
        role=None,
        original_lines=["さようなら"],
        translation_lines=[],
        error_type="AI漏翻",
        error_detail=["模型缺少对应短 ID"],
        model_response="[]",
    )


def _reuse_context() -> TranslationReuseContext:
    key_json = json.dumps(
        {"original_lines": ["こんにちは"], "owner": "CommonEvents/1"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return TranslationReuseContext(
        context_key_json=key_json,
        context_key_hash=hashlib.sha256(key_json.encode("utf-8")).hexdigest(),
        source_fingerprint="source-v1",
        rule_fingerprint="rules-v1",
        terminology_fingerprint="terms-v1",
        language_fingerprint="ja-to-zh-Hans",
        prompt_protocol_version="translation-json-v2",
    )


def _llm_failure(run_id: str) -> LlmFailureRecord:
    return LlmFailureRecord(
        run_id=run_id,
        category="rate_limit",
        error_type="RateLimitError",
        error_message="请求过于频繁",
        retryable=True,
        attempt_count=3,
        created_at="2026-01-01T00:00:00",
    )


async def test_persist_translation_batch_commits_items_errors_and_progress_together(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=2,
            pending_count=2,
            deduplicated_count=2,
            batch_count=1,
        )
        updated = run.model_copy(update={"success_count": 1, "quality_error_count": 1})

        success_item = _success_item()
        reuse_context = _reuse_context()
        await session.persist_translation_batch(
            updated,
            [success_item],
            [_error_item()],
            reuse_contexts_by_path={success_item.location_path: reuse_context},
            physical_request_count_delta=2,
            retry_request_count_delta=1,
        )

        assert len(await session.read_translated_items()) == 1
        reuse_candidates = await session.read_reusable_translations_by_context_keys([reuse_context])
        assert [candidate.translation_item.location_path for candidate in reuse_candidates] == [
            success_item.location_path
        ]
        assert len(await session.read_translation_quality_errors(run.run_id)) == 1
        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.success_count == 1
        assert persisted_run.quality_error_count == 1
        assert persisted_run.physical_request_count == 2
        assert persisted_run.retry_request_count == 1

        await session.persist_translation_batch(
            updated,
            [],
            [],
            physical_request_count_delta=1,
            retry_request_count_delta=0,
        )
        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.physical_request_count == 3
        assert persisted_run.retry_request_count == 1

        await session.persist_translation_run_terminal(
            persisted_run.model_copy(
                update={
                    "status": "completed",
                    "finished_at": "2026-01-01T00:00:00",
                }
            )
        )
        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.physical_request_count == 3
        assert persisted_run.retry_request_count == 1


async def test_persist_translation_batch_rolls_back_everything_when_commit_fails(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=2,
            pending_count=2,
            deduplicated_count=2,
            batch_count=1,
        )
        updated = run.model_copy(update={"success_count": 1, "quality_error_count": 1})
        original_commit: Callable[[], Awaitable[None]] = session.connection.commit

        async def fail_commit() -> None:
            raise OSError("simulated commit failure")

        monkeypatch.setattr(session.connection, "commit", fail_commit)
        with pytest.raises(OSError, match="simulated commit failure"):
            await session.persist_translation_batch(
                updated,
                [_success_item()],
                [_error_item()],
                physical_request_count_delta=2,
                retry_request_count_delta=1,
            )
        monkeypatch.setattr(session.connection, "commit", original_commit)

        assert await session.read_translated_items() == []
        assert await session.read_translation_quality_errors(run.run_id) == []
        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.success_count == 0
        assert persisted_run.quality_error_count == 0
        assert persisted_run.physical_request_count == 0
        assert persisted_run.retry_request_count == 0


async def test_start_translation_run_accepts_lost_commit_acknowledgement(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        original_commit: Callable[[], Awaitable[None]] = session.connection.commit

        async def commit_then_raise() -> None:
            await original_commit()
            raise OSError("simulated start commit acknowledgement loss")

        monkeypatch.setattr(session.connection, "commit", commit_then_raise)
        run = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        monkeypatch.setattr(session.connection, "commit", original_commit)

        persisted = await session.read_translation_run(run.run_id)
        assert persisted is not None
        assert persisted.status == "running"
        assert session.active_translation_run_id == run.run_id


async def test_persist_translation_batch_accepts_lost_commit_acknowledgement(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=2,
            pending_count=2,
            deduplicated_count=2,
            batch_count=1,
        )
        updated = run.model_copy(update={"success_count": 1, "quality_error_count": 1})
        original_commit: Callable[[], Awaitable[None]] = session.connection.commit

        async def commit_then_raise() -> None:
            await original_commit()
            raise OSError("simulated batch commit acknowledgement loss")

        monkeypatch.setattr(session.connection, "commit", commit_then_raise)
        await session.persist_translation_batch(
            updated,
            [_success_item()],
            [_error_item()],
            physical_request_count_delta=2,
            retry_request_count_delta=1,
        )
        monkeypatch.setattr(session.connection, "commit", original_commit)

        persisted = await session.read_translation_run(run.run_id)
        assert persisted is not None
        assert persisted.success_count == 1
        assert persisted.quality_error_count == 1
        assert persisted.physical_request_count == 2
        assert persisted.retry_request_count == 1
        assert len(await session.read_translated_items()) == 1
        assert len(await session.read_translation_quality_errors(run.run_id)) == 1


async def test_write_translation_run_rolls_back_when_commit_fails(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态 commit 失败后连接必须回滚，原 running 状态仍可读取。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        original_commit: Callable[[], Awaitable[None]] = session.connection.commit

        async def fail_commit() -> None:
            raise OSError("simulated terminal commit failure")

        monkeypatch.setattr(session.connection, "commit", fail_commit)
        with pytest.raises(OSError, match="terminal commit failure"):
            await session.write_translation_run(run.model_copy(update={"success_count": 1}))
        monkeypatch.setattr(session.connection, "commit", original_commit)

        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.status == "running"


async def test_persist_translation_terminal_commits_llm_failure_and_status_together(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """正常模型故障必须与非 running 终态在同一提交中可见。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        failure = _llm_failure(run.run_id)
        await session.persist_translation_run_terminal(
            run.model_copy(
                update={
                    "status": "failed",
                    "llm_failure_count": 1,
                    "finished_at": "2026-01-01T00:00:01",
                    "stop_reason": "模型请求失败",
                    "last_error": "llm_request_failed",
                }
            ),
            failure,
        )

        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.status == "failed"
        assert persisted_run.llm_failure_count == 1
        assert await session.read_llm_failures(run.run_id) == [failure]


async def test_persist_translation_terminal_rolls_back_failure_when_run_update_fails(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """故障行插入后若终态更新失败，整个事务必须回到原 running 状态。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        _ = await session.connection.execute(
            """
            CREATE TRIGGER [test_fail_translation_terminal_update]
            BEFORE UPDATE ON [translation_runs]
            BEGIN
                SELECT RAISE(ABORT, 'simulated terminal update failure');
            END
            """
        )
        await session.connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="simulated terminal update failure"):
            await session.persist_translation_run_terminal(
                run.model_copy(
                    update={
                        "status": "failed",
                        "llm_failure_count": 1,
                        "finished_at": "2026-01-01T00:00:01",
                        "stop_reason": "模型请求失败",
                        "last_error": "llm_request_failed",
                    }
                ),
                _llm_failure(run.run_id),
            )

        _ = await session.connection.execute("DROP TRIGGER [test_fail_translation_terminal_update]")
        await session.connection.commit()
        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.status == "running"
        assert persisted_run.llm_failure_count == 0
        assert await session.read_llm_failures(run.run_id) == []


async def test_terminal_write_failure_is_retried_as_explicit_failed_state(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态首次写入失败时不得把运行永久留在 running。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        original_commit: Callable[[], Awaitable[None]] = session.connection.commit
        commit_count = 0

        async def fail_once_then_commit() -> None:
            nonlocal commit_count
            commit_count += 1
            if commit_count == 1:
                raise OSError("simulated first terminal commit failure")
            await original_commit()

        monkeypatch.setattr(session.connection, "commit", fail_once_then_commit)
        message = await _persist_terminal_translation_run(
            session=session,
            record=run.model_copy(
                update={
                    "status": "failed",
                    "llm_failure_count": 1,
                    "finished_at": "2026-01-01T00:00:00",
                }
            ),
            llm_failure=_llm_failure(run.run_id),
        )

        assert message is not None
        assert "simulated first terminal commit failure" in message
        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.status == "failed"
        assert persisted_run.llm_failure_count == 0
        assert persisted_run.last_error == "persistence_failed"
        assert await session.read_llm_failures(run.run_id) == []


async def test_terminal_write_cancellation_waits_for_started_transaction(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消终态写入时仍等待当前事务结束，不留下后台数据库任务。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")

    async with await registry.open_game_with_mutation_lease(record.game_title) as session:
        run = await session.start_translation_run(
            total_extracted=1,
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        write_started = asyncio.Event()
        release_write = asyncio.Event()
        write_completed = asyncio.Event()
        original_write = session.persist_translation_run_terminal

        async def blocking_write(
            run_record: TranslationRunRecord,
            llm_failure: LlmFailureRecord | None = None,
        ) -> None:
            _ = write_started.set()
            _ = await release_write.wait()
            await original_write(run_record, llm_failure)
            _ = write_completed.set()

        monkeypatch.setattr(session, "persist_translation_run_terminal", blocking_write)
        terminal_task = asyncio.create_task(
            _persist_terminal_translation_run(
                session=session,
                record=run.model_copy(
                    update={
                        "status": "completed",
                        "finished_at": "2026-01-01T00:00:00",
                    }
                ),
            )
        )
        _ = await asyncio.wait_for(write_started.wait(), timeout=1)
        _ = terminal_task.cancel()
        await asyncio.sleep(0)
        _ = terminal_task.cancel()
        await asyncio.sleep(0)
        _ = terminal_task.cancel()
        await asyncio.sleep(0)

        assert not terminal_task.done()
        _ = release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await terminal_task

        assert write_completed.is_set()
        persisted_run = await session.read_translation_run(run.run_id)
        assert persisted_run is not None
        assert persisted_run.status == "completed"
