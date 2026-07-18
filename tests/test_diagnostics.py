"""CLI 阶段诊断契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pytest import CaptureFixture, MonkeyPatch

from app.agent_toolkit import AgentReport, AgentToolkitService
from app.agent_toolkit.placeholder_scan import scan_placeholder_candidates
from app.diagnostics import command_diagnostics, current_diagnostic_snapshot, diagnostic_stage, record_scan_counts
from app.game_analysis import build_game_analysis_context
from app.persistence import GameRegistry
from app.rmmz.json_types import JsonObject, coerce_json_value, ensure_json_object
from app.rmmz.loader import load_game_data
from app.rmmz.schema import TranslationData, TranslationItem
from app.rmmz.text_rules import TextRules
from app.utils.config_loader_utils import load_setting
from main import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


def _load_stdout_json(captured: str) -> JsonObject:
    """把 CLI stdout 收窄为测试可读的 JSON 对象。"""
    raw_payload = cast(object, json.loads(captured))
    return ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")


def test_report_omits_diagnostics_outside_debug_scope() -> None:
    """普通模式保持既有 JSON 契约，不出现空诊断对象。"""
    payload = _load_stdout_json(AgentReport(status="ok").to_json_text())

    assert "diagnostics" not in payload


def test_debug_report_contains_only_stage_timings_and_scan_counts() -> None:
    """debug 诊断只公开粗粒度阶段耗时与扫描次数。"""
    with command_diagnostics(enabled=True):
        with diagnostic_stage("command"):
            record_scan_counts(
                {
                    "plugin_ast_scan_count": 1,
                    "text_scope_build_count": 2,
                }
            )
            payload = _load_stdout_json(AgentReport(status="ok").to_json_text())

    diagnostics = ensure_json_object(payload["diagnostics"], "diagnostics")
    timings = ensure_json_object(diagnostics["timings"], "diagnostics.timings")
    scan_counts = ensure_json_object(diagnostics["scan_counts"], "diagnostics.scan_counts")

    assert set(diagnostics) == {"timings", "scan_counts"}
    assert set(timings) == {"command"}
    assert isinstance(timings["command"], int)
    assert timings["command"] >= 0
    assert scan_counts == {
        "plugin_ast_scan_count": 1,
        "text_scope_build_count": 2,
    }
    assert all("row" not in name and "candidate" not in name and "item" not in name for name in timings)


def test_debug_report_counts_each_placeholder_candidate_batch_once(monkeypatch: MonkeyPatch) -> None:
    """整批 occurrence 扫描必须计数一次，不能按正文行或候选数量膨胀。"""
    translation_data_map = {
        "CommonEvents.json": TranslationData(
            display_name=None,
            translation_items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    original_lines=[r"\X[1]こんにちは", r"\Y[2]さようなら"],
                )
            ],
        )
    }
    rules = TextRules.from_setting(load_setting(EXAMPLE_SETTING_PATH, source_language="ja").text_rules)
    native_batch_sizes: list[int] = []

    def fake_native_scan(*, texts: list[object], text_rules: TextRules) -> list[object]:
        """只隔离 native ABI，保留整批输入数量供诊断断言。"""
        _ = text_rules
        native_batch_sizes.append(len(texts))
        return []

    monkeypatch.setattr("app.agent_toolkit.placeholder_scan.scan_native_placeholder_occurrences", fake_native_scan)

    with command_diagnostics(enabled=True):
        candidates = scan_placeholder_candidates(translation_data_map, rules)
        payload = _load_stdout_json(AgentReport(status="ok").to_json_text())

    diagnostics = ensure_json_object(payload["diagnostics"], "diagnostics")
    scan_counts = ensure_json_object(diagnostics["scan_counts"], "diagnostics.scan_counts")
    assert candidates == []
    assert native_batch_sizes == [2]
    assert scan_counts == {"placeholder_candidate_scan_count": 1}


@pytest.mark.asyncio
async def test_game_analysis_context_records_shared_scan_counts_and_stage_timings(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """共享分析上下文只在阶段边界计时，并公开真实扫描次数。"""
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)

    with command_diagnostics(enabled=True):
        async with await registry.open_game(game_record.game_title) as session:
            setting = load_setting(
                EXAMPLE_SETTING_PATH,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            text_rules = TextRules.from_setting(setting.text_rules)
            context = await build_game_analysis_context(
                session=session,
                game_data=game_data,
                text_rules=text_rules,
            )
            _ = context.build_scope_for_text_rules(text_rules=text_rules)
        snapshot = current_diagnostic_snapshot()

    assert snapshot is not None
    assert set(snapshot.timings) == {
        "game_analysis_context",
        "plugin_ast_scan",
        "text_analysis_index_build",
        "text_scope_build",
    }
    assert all(isinstance(value, int) and value >= 0 for value in snapshot.timings.values())
    assert snapshot.scan_counts == {
        "event_index_scan_count": 1,
        "note_index_scan_count": 1,
        "plugin_ast_scan_count": 1,
        "plugin_parameter_index_scan_count": 1,
        "text_scope_build_count": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "placeholder_rules_payload",
    [
        {},
        {r"\\ZZ\[[^\]\r\n]+\]": "[CUSTOM_DIAGNOSTIC_ZZ_{index}]"},
    ],
    ids=["empty-rules", "nonempty-rules"],
)
async def test_workspace_validation_scans_each_placeholder_domain_once(
    tmp_path: Path,
    minimal_game_dir: Path,
    placeholder_rules_payload: dict[str, str],
) -> None:
    """工作区空规则和非空规则都复用同一批候选事实。"""
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=game_record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    _ = (workspace / "placeholder-rules.json").write_text(
        f"{json.dumps(placeholder_rules_payload, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )

    with command_diagnostics(enabled=True):
        _ = await service.validate_agent_workspace(
            game_title=game_record.game_title,
            workspace=workspace,
        )
        snapshot = current_diagnostic_snapshot()

    assert snapshot is not None
    assert snapshot.scan_counts["placeholder_candidate_scan_count"] == 1
    assert snapshot.scan_counts["structured_placeholder_candidate_scan_count"] == 1


@pytest.mark.asyncio
async def test_doctor_reuses_placeholder_facts_for_hashes(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """doctor 的覆盖统计和空规则哈希不得各扫一遍。"""
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    with command_diagnostics(enabled=True):
        _ = await service.doctor(game_title=game_record.game_title, check_llm=False)
        snapshot = current_diagnostic_snapshot()

    assert snapshot is not None
    assert snapshot.scan_counts["placeholder_candidate_scan_count"] == 1
    assert snapshot.scan_counts["structured_placeholder_candidate_scan_count"] == 1


def test_cli_debug_switch_controls_diagnostics_field(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """同一命令只有显式 `--debug` 时才输出诊断，并在命令后清理作用域。"""

    class FakeRegistry:
        """避免列表测试读取开发机数据库。"""

        async def list_games(self) -> list[object]:
            """返回空注册表。"""
            return []

    monkeypatch.setattr("app.cli.commands.registry.GameRegistry", FakeRegistry)

    debug_exit_code = main(["--debug", "list"])
    debug_payload = _load_stdout_json(capsys.readouterr().out)
    normal_exit_code = main(["list"])
    normal_payload = _load_stdout_json(capsys.readouterr().out)

    diagnostics = ensure_json_object(debug_payload["diagnostics"], "diagnostics")
    timings = ensure_json_object(diagnostics["timings"], "diagnostics.timings")
    assert debug_exit_code == 0
    assert normal_exit_code == 0
    assert "argument_parsing" in timings
    assert "command" in timings
    assert diagnostics["scan_counts"] == {}
    assert "diagnostics" not in normal_payload
    assert current_diagnostic_snapshot() is None


def test_debug_argument_error_includes_finalized_parsing_timing(
    capsys: CaptureFixture[str],
) -> None:
    """解析失败仍返回同一结构化 debug 诊断，不伪装成普通业务错误。"""
    exit_code = main(["--debug", "unknown-command"])
    payload = _load_stdout_json(capsys.readouterr().out)
    diagnostics = ensure_json_object(payload["diagnostics"], "diagnostics")
    timings = ensure_json_object(diagnostics["timings"], "diagnostics.timings")

    assert exit_code == 2
    assert set(timings) == {"argument_parsing"}
    assert diagnostics["scan_counts"] == {}
