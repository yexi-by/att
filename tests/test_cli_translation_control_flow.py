"""正文翻译取消与 run-all 写回门槛的 CLI 端到端契约测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import NoReturn, cast

import pytest
from pytest import CaptureFixture, MonkeyPatch

from app.application.handler import TranslationHandler
from app.application.summaries import TextTranslationSummary, TranslationOutcome, WriteBackSummary
from app.cli_main import main
from app.persistence import GameRegistry
from app.rmmz.json_types import JsonObject, coerce_json_value, ensure_json_object


def _read_stdout_json(capsys: CaptureFixture[str]) -> JsonObject:
    """读取一次 CLI stdout，并按项目 JSON 契约完成类型收窄。"""
    raw_payload = cast(object, json.loads(capsys.readouterr().out))
    return ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")


def _translation_summary(
    *,
    outcome: TranslationOutcome,
    error_count: int = 0,
) -> TextTranslationSummary:
    """构造只包含 run-all 分支所需字段的翻译终态。"""
    stop_code = {
        "completed": "none",
        "completed_with_quality_errors": "none",
        "stopped": "quality_error_rate_reached",
        "blocked": "workflow_blocked",
        "failed": "persistence_failed",
        "cancelled": "user_cancelled",
    }[outcome]
    return TextTranslationSummary(
        total_extracted_items=2,
        pending_count=2,
        deduplicated_count=2,
        batch_count=1,
        success_count=2 - error_count,
        error_count=error_count,
        outcome=outcome,
        stop_code=stop_code,
        stop_message="" if stop_code == "none" else "正文翻译未干净完成",
    )


def test_cancelled_real_handler_reaches_cli_json_and_exit_130(
    app_home_with_example_setting: Path,
    minimal_game_dir: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """当前任务被取消后，真实 handler 必须保存终态并由 CLI 返回 130。"""
    _ = app_home_with_example_setting
    registry = GameRegistry()
    game_record = asyncio.run(registry.register_game(minimal_game_dir, source_language="ja"))

    async def allow_workflow_gate(**kwargs: object) -> None:
        """本测试只隔离此前已有独立覆盖的规则闸门。"""
        _ = kwargs

    async def skip_terminology_prompt(self: TranslationHandler, **kwargs: object) -> None:
        """取消测试不需要术语提示词；模型请求开始前就会取消。"""
        _ = self
        _ = kwargs
        return None

    async def cancel_current_translation_task(self: TranslationHandler, **kwargs: object) -> NoReturn:
        """模拟 asyncio.run 收到 Ctrl-C 后取消当前顶层任务。"""
        _ = self
        _ = kwargs
        task = asyncio.current_task()
        assert task is not None
        _ = task.cancel()
        await asyncio.sleep(0)
        raise AssertionError("当前任务取消后不应继续执行")

    monkeypatch.setattr("app.application.handler.assert_workflow_gate_passed", allow_workflow_gate)
    monkeypatch.setattr(TranslationHandler, "_load_terminology_prompt_index", skip_terminology_prompt)
    monkeypatch.setattr(TranslationHandler, "_run_text_translation_batches", cancel_current_translation_task)

    exit_code = main(["translate", "--game", game_record.game_title])
    payload = _read_stdout_json(capsys)
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")

    assert exit_code == 130
    assert payload["status"] == "error"
    assert summary["outcome"] == "cancelled"
    assert summary["stop_code"] == "user_cancelled"
    run_id = summary["run_id"]
    assert isinstance(run_id, str)
    assert run_id

    async def read_persisted_status() -> str | None:
        async with await registry.open_game(game_record.game_title) as session:
            record = await session.read_translation_run(run_id)
            return None if record is None else record.status

    assert asyncio.run(read_persisted_status()) == "cancelled"


@dataclass(frozen=True, slots=True)
class _RunAllCase:
    outcome: TranslationOutcome
    error_count: int
    expected_exit_code: int
    expected_write_back: bool


@pytest.mark.parametrize(
    "case",
    [
        _RunAllCase("completed", 0, 0, True),
        _RunAllCase("completed_with_quality_errors", 1, 0, False),
        _RunAllCase("stopped", 0, 1, False),
        _RunAllCase("blocked", 0, 1, False),
        _RunAllCase("failed", 0, 1, False),
        _RunAllCase("cancelled", 0, 130, False),
    ],
    ids=["completed", "quality-errors", "stopped", "blocked", "failed", "cancelled"],
)
def test_run_all_writes_back_only_after_clean_completion(
    case: _RunAllCase,
    app_home_with_example_setting: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """run-all 仅允许干净完成进入 write-back，其余终态一律停在翻译阶段。"""
    _ = app_home_with_example_setting
    write_back_calls: list[str] = []

    class FakeHandlerSession:
        """隔离 handler 构造；本测试只验证真实 CLI 编排分支。"""

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            _ = exc_type
            _ = exc_value
            _ = traceback

    async def fake_translate_text_for_handler(**kwargs: object) -> TextTranslationSummary:
        _ = kwargs
        return _translation_summary(outcome=case.outcome, error_count=case.error_count)

    async def fake_write_back_for_handler(**kwargs: object) -> WriteBackSummary:
        _ = kwargs
        write_back_calls.append("write-back")
        return WriteBackSummary(
            data_item_count=1,
            plugin_item_count=0,
            terminology_written_count=0,
            target_font_name=None,
            source_font_count=0,
            replaced_font_reference_count=0,
            font_copied=False,
            planned_file_count=1,
        )

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)
    monkeypatch.setattr(
        "app.cli.commands.write_back.translate_text_for_handler",
        fake_translate_text_for_handler,
    )
    monkeypatch.setattr(
        "app.cli.commands.write_back.write_back_for_handler",
        fake_write_back_for_handler,
    )

    exit_code = main(["run-all", "--game", "demo"])
    payload = _read_stdout_json(capsys)
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")

    assert exit_code == case.expected_exit_code
    assert summary["outcome"] == case.outcome
    assert summary["write_back_performed"] is case.expected_write_back
    assert write_back_calls == (["write-back"] if case.expected_write_back else [])
