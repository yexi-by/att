"""插件源码风险评估的 fail-closed 工作流测试。"""

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from app.agent_toolkit import AgentToolkitService
from app.application.flow_gate import collect_workflow_gate_errors
from app.game_analysis import build_game_analysis_context
from app.persistence import GameRegistry
from app.plugin_source_text import PluginSourceRisk
from app.rmmz.loader import load_game_data
from app.rmmz.text_rules import JsonObject, TextRules, ensure_json_array
from app.rule_review import (
    plugin_source_rule_scope_hash,
    plugin_source_text_rules_hash,
)
from app.utils.config_loader_utils import load_setting

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


def _plugin_source_error_codes(errors: Sequence[object]) -> set[str]:
    return {
        code
        for error in errors
        if isinstance((code := getattr(error, "code", None)), str)
        and (code.startswith("plugin_source") or code == "stale_plugin_source_rules")
    }


async def test_plugin_source_assessment_is_required_and_bound_to_current_facts(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """缺失或任一绑定指纹漂移必须失败，当前低风险无规则才可通过。"""
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    _ = (plugin_source_dir / "DormantSource.js").write_text(
        "const title = '未启用插件源码';\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game(record.game_title) as session:
        game_data = await load_game_data(minimal_game_dir)
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scan = context.plugin_source_scan
        assert scan.risk.high_risk is False

        missing_errors = await collect_workflow_gate_errors(
            session=session,
            context=context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )
        assert "plugin_source_assessment_missing" in _plugin_source_error_codes(missing_errors)

        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=scan.risk.high_risk,
            candidate_count=len(scan.candidates),
            summary=scan.risk_report_json(),
        )
        current_errors = await collect_workflow_gate_errors(
            session=session,
            context=context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )
        assert _plugin_source_error_codes(current_errors) == set()

        newly_enabled_plugin: JsonObject = {
            "name": "DormantSource",
            "status": True,
            "description": "",
            "parameters": {},
        }
        game_data.plugins_js.append(newly_enabled_plugin)
        changed_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        stale_errors = await collect_workflow_gate_errors(
            session=session,
            context=changed_context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )
        assert "plugin_source_assessment_stale" in _plugin_source_error_codes(stale_errors)


async def test_current_high_risk_assessment_without_rules_requires_review(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """即使评估记录新鲜，高风险扫描没有规则时仍必须明确提示审查。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game(record.game_title) as session:
        game_data = await load_game_data(minimal_game_dir)
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        high_risk_scan = replace(
            context.plugin_source_scan,
            risk=PluginSourceRisk(
                high_risk=True,
                risk_score=2000,
                strong_context_text_count=300,
                medium_confidence_text_count=0,
                scanned_file_count=0,
                ignored_file_count=0,
                read_error_file_count=0,
                files_score_ge_250=0,
                max_file_score=0,
            ),
            candidates=(),
        )
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=context.plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=True,
            candidate_count=0,
            summary=high_risk_scan.risk_report_json(),
        )

        errors = await collect_workflow_gate_errors(
            session=session,
            context=replace(context, plugin_ast=high_risk_scan),
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )

    assert "plugin_source_text_high_risk" in _plugin_source_error_codes(errors)


async def test_enabled_plugin_source_read_error_cannot_create_trusted_assessment_or_pass_gate(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """启用插件源码无法按 UTF-8 读取时，扫描必须失败且任何现存评估都不能放行。"""
    plugin_source_path = minimal_game_dir / "js" / "plugins" / "TestPlugin.js"
    _ = plugin_source_path.write_bytes("Window_Base.drawText('読取失敗');".encode("cp932"))
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.scan_plugin_source_text(
        game_title=record.game_title,
        output_path=tmp_path / "plugin-source-risk.json",
    )
    ast_report = await service.export_plugin_source_ast_map(
        game_title=record.game_title,
        output_path=tmp_path / "plugin-source-ast.json",
    )
    workspace = tmp_path / "workspace"
    workspace_report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    validation_report = await service.validate_plugin_source_rules(
        game_title=record.game_title,
        rules_text="[]",
    )
    import_report = await service.import_plugin_source_rules(
        game_title=record.game_title,
        rules_text="[]",
        confirm_empty=True,
    )

    assert report.status == "error"
    assert [error.code for error in report.errors] == ["plugin_source_read_error"]
    assert ast_report.status == "error"
    assert [error.code for error in ast_report.errors] == ["plugin_source_read_error"]
    assert workspace_report.status == "error"
    assert [error.code for error in workspace_report.errors] == ["plugin_source_read_error"]
    assert not (workspace / "manifest.json").exists()
    assert validation_report.status == "error"
    assert [error.code for error in validation_report.errors] == ["plugin_source_read_error"]
    assert import_report.status == "error"
    assert [error.code for error in import_report.errors] == ["plugin_source_read_error"]
    async with await registry.open_game(record.game_title) as session:
        assert await session.read_plugin_source_assessment() is None
        assert await session.read_plugin_source_text_rules() == []
        game_data = await load_game_data(minimal_game_dir)
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        assert context.plugin_source_scan.risk.read_error_file_count == 1
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=context.plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=False,
            candidate_count=len(context.plugin_source_scan.candidates),
            summary=context.plugin_source_scan.risk_report_json(),
        )
        errors = await collect_workflow_gate_errors(
            session=session,
            context=context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )

    assert "plugin_source_read_error" in _plugin_source_error_codes(errors)


async def test_enabled_missing_translation_source_file_cannot_create_assessment_or_pass_gate(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """启用但翻译源中缺失的插件源码必须计入读取失败并阻断后续流程。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    closing_index = plugins_text.rfind("]")
    assert closing_index >= 0
    missing_plugin: JsonObject = {
        "name": "MissingTranslationSource",
        "status": True,
        "description": "",
        "parameters": {},
    }
    _ = plugins_path.write_text(
        plugins_text[:closing_index]
        + ",\n"
        + json.dumps(missing_plugin, ensure_ascii=False)
        + "\n"
        + plugins_text[closing_index:],
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.scan_plugin_source_text(
        game_title=record.game_title,
        output_path=tmp_path / "missing-plugin-source-risk.json",
    )

    assert report.status == "error"
    assert [error.code for error in report.errors] == ["plugin_source_read_error"]
    assert "缺失 1 个" in report.errors[0].message
    enabled_file_states = ensure_json_array(
        report.details["enabled_plugin_file_states"],
        "enabled_plugin_file_states",
    )
    assert {"file": "MissingTranslationSource.js", "status": "missing"} in enabled_file_states
    async with await registry.open_game(record.game_title) as session:
        assert await session.read_plugin_source_assessment() is None
        game_data = await load_game_data(minimal_game_dir)
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scan = context.plugin_source_scan
        assert scan.risk.read_error_file_count == 1
        assert scan.missing_enabled_file_count == 1
        assert scan.unreadable_enabled_file_count == 0
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=scan.risk.high_risk,
            candidate_count=len(scan.candidates),
            summary=scan.risk_report_json(),
        )
        errors = await collect_workflow_gate_errors(
            session=session,
            context=context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )

    assert _plugin_source_error_codes(errors) == {"plugin_source_read_error"}
    plugin_source_error = next(error for error in errors if error.code == "plugin_source_read_error")
    assert "缺失 1 个" in plugin_source_error.message
