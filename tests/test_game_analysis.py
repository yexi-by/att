"""单命令游戏分析上下文测试。"""
# pyright: reportPrivateLocalImportUsage=false, reportPrivateUsage=false

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

import app.event_command_text.index as event_index_module
import app.game_analysis as game_analysis_module
import app.note_tag_text.sources as note_sources_module
import app.plugin_text.index as plugin_index_module
import app.rmmz.extraction as data_extraction_module
import app.rmmz.mv_namebox as mv_namebox_module
from app.agent_toolkit import AgentToolkitService
from app.application.flow_gate import collect_workflow_gate_errors, note_tag_rule_scope_hash_for_text_rules
from app.application.handler import TranslationHandler
from app.game_analysis import build_game_analysis_context
from app.llm import LLMHandler
from app.persistence import GameRegistry, TargetGameSession
from app.plugin_source_text import (
    PluginSourceRawIndex,
    PluginSourceScan,
    build_plugin_source_raw_index,
    derive_plugin_source_scan,
)
from app.rmmz.loader import load_game_data
from app.rmmz.schema import (
    GameData,
    PlaceholderRuleRecord,
    PluginSourceTextRuleRecord,
    StructuredPlaceholderRuleRecord,
)
from app.rmmz.text_rules import TextRules, coerce_json_value, ensure_json_array
from app.rule_review import (
    MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
    PLUGIN_TEXT_RULE_DOMAIN,
    event_command_rule_scope_hash_for_codes,
    event_command_rule_scope_hash_for_snapshots,
    mv_virtual_namebox_rule_scope_hash,
    note_tag_rule_scope_hash_for_candidates,
    plugin_rule_scope_hash,
    plugin_source_rule_scope_hash,
    plugin_source_text_rules_hash,
)
from app.terminology import TerminologyGlossary, TerminologyRegistry
from app.utils.config_loader_utils import load_setting

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


def _ignore_progress(_completed: int, _total: int) -> None:
    """忽略测试中的进度初始化。"""


def _ignore_advance(_step: int) -> None:
    """忽略测试中的进度推进。"""


def _ignore_status(_status: str) -> None:
    """忽略测试中的状态消息。"""


@pytest.mark.asyncio
async def test_game_analysis_context_loads_facts_and_plugin_ast_once(
    tmp_path: Path,
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一上下文重建规则视图时不重读数据库、不重扫插件 AST。"""
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)

    ast_scan_count = 0
    real_build_plugin_source_raw_index = game_analysis_module.build_plugin_source_raw_index

    def counted_build_plugin_source_raw_index(
        *,
        game_data: GameData,
    ) -> PluginSourceRawIndex:
        nonlocal ast_scan_count
        ast_scan_count += 1
        return real_build_plugin_source_raw_index(
            game_data=game_data,
        )

    monkeypatch.setattr(
        game_analysis_module,
        "build_plugin_source_raw_index",
        counted_build_plugin_source_raw_index,
    )
    event_iter = Mock(wraps=event_index_module.iter_all_commands)
    event_leaf_resolver = Mock(wraps=event_index_module.resolve_event_command_leaves)
    note_source_scan = Mock(wraps=note_sources_module.collect_native_note_tag_sources)
    plugin_leaf_resolver = Mock(wraps=plugin_index_module.resolve_plugin_leaves)
    monkeypatch.setattr(event_index_module, "iter_all_commands", event_iter)
    monkeypatch.setattr(event_index_module, "resolve_event_command_leaves", event_leaf_resolver)
    monkeypatch.setattr(note_sources_module, "collect_native_note_tag_sources", note_source_scan)
    monkeypatch.setattr(plugin_index_module, "resolve_plugin_leaves", plugin_leaf_resolver)

    def unexpected_event_rescan(_game_data: GameData) -> object:
        raise AssertionError("正文提取或 MV 候选不得再次遍历事件指令")

    monkeypatch.setattr(data_extraction_module, "iter_all_commands", unexpected_event_rescan)
    monkeypatch.setattr(mv_namebox_module, "iter_all_commands", unexpected_event_rescan)

    read_counts: dict[str, int] = {}
    read_method_names = (
        "read_plugin_text_rules",
        "read_event_command_text_rules",
        "read_plugin_source_text_rules",
        "read_note_tag_text_rules",
        "read_mv_virtual_namebox_rules",
        "read_translated_items",
        "read_placeholder_rules",
        "read_structured_placeholder_rules",
    )
    for method_name in read_method_names:
        original = cast(
            Callable[..., Awaitable[object]],
            getattr(TargetGameSession, method_name),
        )

        async def counted_read(
            self: TargetGameSession,
            *args: object,
            _method_name: str = method_name,
            _original: Callable[..., Awaitable[object]] = original,
            **kwargs: object,
        ) -> object:
            read_counts[_method_name] = read_counts.get(_method_name, 0) + 1
            return await _original(self, *args, **kwargs)

        monkeypatch.setattr(TargetGameSession, method_name, counted_read)

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
        first_rule_view = context.build_scope_for_text_rules(text_rules=text_rules)
        second_rule_view = context.build_scope_for_text_rules(text_rules=text_rules)
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=context.plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=context.plugin_source_scan.risk.high_risk,
            candidate_count=len(context.plugin_source_scan.candidates),
            summary=context.plugin_source_scan.risk_report_json(),
        )
        first_gate_errors = await collect_workflow_gate_errors(
            session=session,
            context=context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )
        second_gate_errors = await collect_workflow_gate_errors(
            session=session,
            context=context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )

    assert ast_scan_count == 1
    assert event_iter.call_count == 1
    assert event_leaf_resolver.call_count == len(context.event_commands)
    assert note_source_scan.call_count == 1
    assert plugin_leaf_resolver.call_count == len(game_data.plugins_js)
    assert read_counts == {method_name: 1 for method_name in read_method_names}
    assert context.scan_counts.plugin_ast_scan_count == 1
    assert context.scan_counts.event_index_scan_count == 1
    assert context.scan_counts.note_index_scan_count == 1
    assert context.scan_counts.plugin_parameter_index_scan_count == 1
    assert context.scan_counts.text_scope_build_count == 3
    assert first_rule_view.entries_json() == context.scope.entries_json()
    assert second_rule_view.entries_json() == context.scope.entries_json()
    assert first_gate_errors == second_gate_errors
    assert context.translation_data_map
    assert set(context.translated_item_index) == {item.location_path for item in context.translated_items}
    command_codes = frozenset(entry.command.code for entry in context.event_commands)
    assert event_command_rule_scope_hash_for_snapshots(
        command_snapshots=context.event_command_snapshots,
        command_codes=command_codes,
    ) == event_command_rule_scope_hash_for_codes(
        game_data=game_data,
        command_codes=command_codes,
    )
    assert note_tag_rule_scope_hash_for_candidates(
        [candidate for candidate in context.note_candidates]
    ) == note_tag_rule_scope_hash_for_text_rules(
        game_data=game_data,
        text_rules=context.text_rules,
    )


@pytest.mark.asyncio
async def test_game_analysis_context_reuses_one_index_for_extraction_and_rule_hits(
    tmp_path: Path,
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单命令 Context 只建一次索引，并由提取与规则命中共同消费。"""
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)
    event_iter = Mock(wraps=event_index_module.iter_all_commands)
    event_leaf_resolver = Mock(wraps=event_index_module.resolve_event_command_leaves)
    note_source_scan = Mock(wraps=note_sources_module.collect_native_note_tag_sources)
    plugin_leaf_resolver = Mock(wraps=plugin_index_module.resolve_plugin_leaves)
    monkeypatch.setattr(event_index_module, "iter_all_commands", event_iter)
    monkeypatch.setattr(event_index_module, "resolve_event_command_leaves", event_leaf_resolver)
    monkeypatch.setattr(note_sources_module, "collect_native_note_tag_sources", note_source_scan)
    monkeypatch.setattr(plugin_index_module, "resolve_plugin_leaves", plugin_leaf_resolver)

    def unexpected_event_rescan(_game_data: GameData) -> object:
        raise AssertionError("GameAnalysisContext 构建范围时不得再次遍历事件指令")

    monkeypatch.setattr(data_extraction_module, "iter_all_commands", unexpected_event_rescan)

    async with await registry.open_game(game_record.game_title) as session:
        setting = load_setting(
            EXAMPLE_SETTING_PATH,
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=TextRules.from_setting(setting.text_rules),
        )
        scope = analysis_context.scope

    assert scope.translation_data_map
    assert event_iter.call_count == 1
    assert event_leaf_resolver.call_count > 0
    assert note_source_scan.call_count == 1
    assert plugin_leaf_resolver.call_count == len(game_data.plugins_js)


@pytest.mark.asyncio
async def test_game_analysis_context_derives_plugin_source_rule_views_from_one_raw_index(
    tmp_path: Path,
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切换语言规则必须重派生源码候选，但不能再次解析插件 AST。"""
    source_path = minimal_game_dir / "js" / "plugins" / "TestPlugin.js"
    _ = source_path.write_text(
        "Window_Base.prototype.drawText('SOLD OUT', 0, 0, 320);\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)

    async with await registry.open_game(game_record.game_title) as session:
        setting = load_setting(
            EXAMPLE_SETTING_PATH,
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
        )
        japanese_rules = TextRules.from_setting(setting.text_rules)
        multilingual_setting = load_setting(
            EXAMPLE_SETTING_PATH,
            source_language=session.source_language,
            additional_source_languages=("en",),
        )
        multilingual_rules = TextRules.from_setting(multilingual_setting.text_rules)
        raw_index = build_plugin_source_raw_index(game_data=game_data)
        japanese_scan = derive_plugin_source_scan(index=raw_index, text_rules=japanese_rules)
        multilingual_scan = derive_plugin_source_scan(index=raw_index, text_rules=multilingual_rules)
        candidate = next(
            candidate
            for candidate in multilingual_scan.candidates
            if candidate.file_name == "TestPlugin.js" and candidate.text == "SOLD OUT"
        )
        file_scan = next(file for file in multilingual_scan.files if file.file_name == "TestPlugin.js")
        await session.replace_plugin_source_text_rules(
            [
                PluginSourceTextRuleRecord(
                    file_name="TestPlugin.js",
                    file_hash=file_scan.file_hash,
                    selectors=[candidate.selector],
                )
            ]
        )

        raw_index_build_count = 0
        expected_game_data = game_data

        def reuse_counted_raw_index(*, game_data: GameData) -> PluginSourceRawIndex:
            nonlocal raw_index_build_count
            assert game_data is expected_game_data
            raw_index_build_count += 1
            return raw_index

        derive_candidate_views: list[frozenset[str]] = []
        real_derive_plugin_source_scan = game_analysis_module.derive_plugin_source_scan

        def record_derived_view(
            *,
            index: PluginSourceRawIndex,
            text_rules: TextRules,
        ) -> PluginSourceScan:
            scan = real_derive_plugin_source_scan(index=index, text_rules=text_rules)
            derive_candidate_views.append(frozenset(item.text for item in scan.candidates))
            return scan

        monkeypatch.setattr(
            game_analysis_module,
            "build_plugin_source_raw_index",
            reuse_counted_raw_index,
        )
        monkeypatch.setattr(
            game_analysis_module,
            "derive_plugin_source_scan",
            record_derived_view,
        )
        context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=japanese_rules,
        )
        multilingual_scope = context.build_scope_for_text_rules(text_rules=multilingual_rules)
        japanese_scope = context.build_scope_for_text_rules(text_rules=japanese_rules)

    location_path = f"js/plugins/TestPlugin.js/{candidate.selector}"
    assert raw_index_build_count == 1
    assert context.scan_counts.plugin_ast_scan_count == 1
    assert context.plugin_source_raw_index is raw_index
    assert "SOLD OUT" not in {item.text for item in japanese_scan.candidates}
    assert "SOLD OUT" in {item.text for item in multilingual_scan.candidates}
    assert ["SOLD OUT" in view for view in derive_candidate_views] == [False, True, False]
    assert location_path not in {entry.location_path for entry in context.scope.entries}
    assert location_path in {entry.location_path for entry in multilingual_scope.entries}
    assert location_path not in {entry.location_path for entry in japanese_scope.entries}


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_translation_entry_uses_context_gate_before_model_request(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """翻译入口必须先完成统一上下文阻断检查，不能提前访问模型。"""
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())

    try:
        summary = await handler.translate_text(
            game_record.game_title,
            None,
            None,
            None,
            (_ignore_progress, _ignore_advance, _ignore_status),
        )
    finally:
        await handler.close()

    assert summary.outcome == "blocked"
    assert summary.stop_code == "plugin_source_assessment_missing"
    assert "插件源码" in summary.stop_message


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_translation_entry_preserves_event_rule_gate_code(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """插件和术语前置项通过后，翻译摘要必须保留事件规则的具体阻断代码。"""
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)
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
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=context.plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=context.plugin_source_scan.risk.high_risk,
            candidate_count=len(context.plugin_source_scan.candidates),
            summary=context.plugin_source_scan.risk_report_json(),
        )
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )
        await session.replace_rule_review_state(
            rule_domain=PLUGIN_TEXT_RULE_DOMAIN,
            scope_hash=plugin_rule_scope_hash(game_data),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
            scope_hash=mv_virtual_namebox_rule_scope_hash(
                [candidate for candidate in context.mv_virtual_namebox_candidates]
            ),
            reviewed_empty=True,
        )

    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    try:
        summary = await handler.translate_text(
            game_record.game_title,
            None,
            None,
            None,
            (_ignore_progress, _ignore_advance, _ignore_status),
        )
    finally:
        await handler.close()

    assert summary.outcome == "blocked"
    assert summary.stop_code == "event_command_text_missing"
    assert "事件指令" in summary.stop_message


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_nw_placeholder_coverage_is_consistent_in_scan_doctor_and_translation(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """NW 三类报告样本在扫描、doctor 和翻译闸门中必须得到相同结论。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_value = coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8"))))
    common_events = ensure_json_array(raw_value, "CommonEvents.json")
    common_events.append(
        coerce_json_value(
            cast(
                object,
                {
                    "id": 999,
                    "list": [
                        {"code": 101, "parameters": [0, 0, 0, 2, ""]},
                        {"code": 401, "parameters": [r"\NW[神父]"]},
                        {"code": 401, "parameters": [r"\NW[\N[1]]"]},
                        {"code": 401, "parameters": [r"\NW[神父]\SV[A0001]"]},
                        {"code": 0, "parameters": []},
                    ],
                },
            )
        )
    )
    _ = common_events_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game(game_record.game_title) as session:
        await session.replace_placeholder_rules(
            [
                PlaceholderRuleRecord(
                    pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
                    placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
                ),
                PlaceholderRuleRecord(
                    pattern_text=r"\\NW\[\\N\[\d+\]\]",
                    placeholder_template="[CUSTOM_DYNAMIC_NW_MARKER_{index}]",
                ),
                PlaceholderRuleRecord(
                    pattern_text=r"\\SV\[[^\]\r\n]+\]",
                    placeholder_template="[CUSTOM_PLUGIN_SV_MARKER_{index}]",
                ),
            ]
        )
        await session.replace_structured_placeholder_rules(
            [
                StructuredPlaceholderRuleRecord(
                    rule_name="MV_NW",
                    rule_type="paired_shell",
                    pattern_text=r"(?P<open>\\NW\[)(?P<text>[^\\\]\r\n]+?)(?P<close>\])",
                    translatable_group="text",
                    protected_groups={
                        "open": "[CUSTOM_MV_NW_OPEN_{index}]",
                        "close": "[CUSTOM_MV_NW_CLOSE_{index}]",
                    },
                )
            ]
        )

    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    scan_report = await service.scan_placeholder_candidates(
        game_title=game_record.game_title,
        custom_placeholder_rules_text=None,
    )
    doctor_report = await service.doctor(game_title=game_record.game_title, check_llm=False)
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    try:
        translation_summary = await handler.translate_text(
            game_record.game_title,
            None,
            None,
            None,
            (_ignore_progress, _ignore_advance, _ignore_status),
        )
    finally:
        await handler.close()

    assert scan_report.summary["uncovered_count"] == 0
    assert doctor_report.summary["uncovered_placeholder_count"] == 0
    assert "疑似自定义控制符" not in translation_summary.stop_message
    assert "协议外壳" not in translation_summary.stop_message


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_custom_prefix_overlap_is_uncovered_in_scan_workspace_doctor_and_translation(
    tmp_path: Path,
    minimal_game_dir: Path,
) -> None:
    """普通规则只命中控制符前缀时，四个生产入口必须一致阻断。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_value = coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8"))))
    common_events = ensure_json_array(raw_value, "CommonEvents.json")
    common_events.append(
        coerce_json_value(
            cast(
                object,
                {
                    "id": 999,
                    "list": [
                        {"code": 101, "parameters": [0, 0, 0, 2, ""]},
                        {"code": 401, "parameters": [r"\X[1]こんにちは"]},
                        {"code": 0, "parameters": []},
                    ],
                },
            )
        )
    )
    _ = common_events_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    game_record = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game(game_record.game_title) as session:
        await session.replace_placeholder_rules(
            [
                PlaceholderRuleRecord(
                    pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
                    placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
                ),
                PlaceholderRuleRecord(
                    pattern_text=r"\\X",
                    placeholder_template="[CUSTOM_X_PREFIX_{index}]",
                ),
            ]
        )

    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    scan_report = await service.scan_placeholder_candidates(
        game_title=game_record.game_title,
        custom_placeholder_rules_text=None,
    )
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=game_record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    workspace_report = await service.validate_agent_workspace(
        game_title=game_record.game_title,
        workspace=workspace,
    )
    doctor_report = await service.doctor(game_title=game_record.game_title, check_llm=False)
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    try:
        translation_summary = await handler.translate_text(
            game_record.game_title,
            None,
            None,
            None,
            (_ignore_progress, _ignore_advance, _ignore_status),
        )
    finally:
        await handler.close()

    assert scan_report.summary["uncovered_count"] == 1
    assert doctor_report.summary["uncovered_placeholder_count"] == 1
    assert "placeholder_coverage_uncovered" in {error.code for error in workspace_report.errors}
    assert "疑似自定义控制符" in translation_summary.stop_message
