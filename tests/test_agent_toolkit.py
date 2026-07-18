"""Agent 工具包诊断、扫描和质量报告测试。"""
# pyright: reportPrivateUsage=false

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import NoReturn, cast

import pytest

from app.agent_toolkit import AgentToolkitService
from app.agent_toolkit.placeholder_scan import (
    analyze_placeholder_candidates,
    analyze_structured_placeholder_candidates,
    count_uncovered_candidates,
    placeholder_candidates_to_details,
)
from app.agent_toolkit.placeholder_scan import (
    scan_placeholder_candidates as scan_placeholder_candidate_spans,
)
from app.agent_toolkit.reports import AgentReport, issue
from app.application.flow_gate import (
    collect_workflow_gate_errors,
    event_command_rule_codes_for_setting,
    event_command_rule_scope_hash_for_command_codes,
    event_command_rule_scope_hash_for_setting,
    normal_placeholder_scope_hash_from_analysis,
    note_tag_rule_scope_hash_for_text_rules,
    structured_placeholder_scope_hash_from_analysis,
)
from app.application.handler import TranslationHandler
from app.config import SettingOverrides
from app.config.schemas import Setting, TextRulesSetting
from app.game_analysis import build_game_analysis_context
from app.language import SourceLanguage
from app.llm import LLMHandler
from app.native_quality import collect_native_quality_counts, collect_native_quality_details
from app.persistence import GameRegistry, TargetGameSession
from app.plugin_source_text import (
    PluginSourceBatchTextScan,
    build_plugin_source_raw_index,
    derive_plugin_source_scan,
    scan_plugin_source_file_text_strict,
)
from app.plugin_source_text import (
    scan_plugin_source_files_text_strict as real_scan_plugin_source_files_text_strict,
)
from app.plugin_source_text.runtime_mapping import (
    plugin_source_runtime_hash_lines,
    plugin_source_runtime_hash_text,
)
from app.plugin_text import build_plugin_hash
from app.rmmz.control_codes import CustomPlaceholderRule, StructuredPlaceholderRule
from app.rmmz.json_types import (
    JsonArray,
    JsonObject,
    JsonValue,
    coerce_json_value,
    ensure_json_array,
    ensure_json_object,
)
from app.rmmz.loader import load_active_runtime_game_data, load_game_data
from app.rmmz.mv_namebox import mv_virtual_namebox_candidate_details
from app.rmmz.schema import (
    EventCommandParameterFilter,
    EventCommandTextRuleRecord,
    GameData,
    NoteTagTextRuleRecord,
    PlaceholderRuleRecord,
    PluginSourceRuntimeWriteMapRecord,
    PluginSourceTextRuleRecord,
    PluginTextRuleRecord,
    SourceResidualRuleRecord,
    StructuredPlaceholderRuleRecord,
    TranslationData,
    TranslationErrorItem,
    TranslationItem,
)
from app.rmmz.text_rules import TextRules
from app.rule_review import (
    EVENT_COMMAND_TEXT_RULE_DOMAIN,
    MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
    NOTE_TAG_TEXT_RULE_DOMAIN,
    PLACEHOLDER_RULE_DOMAIN,
    PLUGIN_SOURCE_TEXT_RULE_DOMAIN,
    PLUGIN_TEXT_RULE_DOMAIN,
    STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
    mv_virtual_namebox_rule_scope_hash,
    plugin_rule_scope_hash,
)
from app.runtime_paths import APP_HOME_ENV_NAME
from app.terminology import (
    TerminologyCategory,
    TerminologyGlossary,
    TerminologyPromptIndex,
    TerminologyRegistry,
)
from app.text_scope import TextScopeEntry, TextScopeResult
from app.text_scope.write_probe import collect_write_back_probe_reasons
from app.translation import TranslationCache, evaluate_translation_freshness
from app.utils.config_loader_utils import load_setting

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


class _AgentToolkitServiceProbe(AgentToolkitService):
    """暴露测试用公开方法，避免测试直接访问受保护 mixin API。"""

    async def load_translation_source_for_test(
        self,
        session: TargetGameSession,
        *,
        include_writable_copies: bool | None = None,
    ) -> GameData:
        """调用翻译源加载入口并保留默认参数行为。"""
        if include_writable_copies is None:
            return await self._load_translation_source_game_data(session)
        return await self._load_translation_source_game_data(
            session,
            include_writable_copies=include_writable_copies,
        )


def load_json_object(path: Path) -> dict[str, object]:
    """读取测试产物 JSON 对象，并在边界处收窄动态解析结果。"""
    raw_value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    json_object = ensure_json_object(coerce_json_value(raw_value), str(path))
    return {key: value for key, value in json_object.items()}


async def _write_fresh_translation_items(
    *,
    service: AgentToolkitService,
    session: TargetGameSession,
    items: list[TranslationItem],
) -> None:
    """按当前完整上下文保存质检 fixture，禁止测试绕过生产新鲜度契约。"""
    setting = load_setting(
        EXAMPLE_SETTING_PATH,
        source_language=session.source_language,
        additional_source_languages=session.additional_source_languages,
    )
    custom_rules = await service._resolve_custom_rules(
        session=session,
        custom_placeholder_rules_text=None,
    )
    structured_rules = await service._resolve_structured_rules(session=session)
    text_rules = TextRules.from_setting(
        setting.text_rules,
        custom_placeholder_rules=custom_rules,
        structured_placeholder_rules=structured_rules,
    )
    game_data = await service._load_translation_source_game_data(session)
    translated_items = await session.read_translated_items()
    analysis_context = await service._build_game_analysis_context(
        session=session,
        game_data=game_data,
        text_rules=text_rules,
        translated_items=translated_items,
        placeholder_rules=[
            PlaceholderRuleRecord(
                pattern_text=rule.pattern_text,
                placeholder_template=rule.placeholder_template,
            )
            for rule in custom_rules
        ],
        structured_placeholder_rules=[
            StructuredPlaceholderRuleRecord(
                rule_name=rule.rule_name,
                rule_type=rule.rule_type,
                pattern_text=rule.pattern_text,
                translatable_group=rule.translatable_group,
                protected_groups=dict(rule.protected_groups),
            )
            for rule in structured_rules
        ],
    )
    terminology_registry = await session.read_terminology_registry()
    terminology_glossary = await session.read_terminology_glossary()
    if terminology_registry is None or terminology_glossary is None:
        terminology_registry = TerminologyRegistry()
        terminology_glossary = TerminologyGlossary()
        await session.replace_terminology_bundle(
            registry=terminology_registry,
            glossary=terminology_glossary,
        )
    freshness = await evaluate_translation_freshness(
        reuse_reader=session,
        translation_cache=TranslationCache(),
        scope=analysis_context.scope,
        translated_items=translated_items,
        terminology_prompt_index=TerminologyPromptIndex.from_glossary(
            terminology_glossary,
            game_data=game_data,
        ),
        source_language=setting.text_rules.source_language,
        additional_source_languages=setting.text_rules.additional_source_languages,
        target_language=session.target_language,
        source_snapshot_records=await session.read_source_snapshot_records(),
        source_residual_records=await session.read_source_residual_rules(),
    )
    normalized_items: list[TranslationItem] = []
    for item in items:
        current_item = freshness.current_items_by_path.get(item.location_path)
        if current_item is None:
            raise AssertionError(f"质检 fixture 不在当前翻译范围: {item.location_path}")
        normalized_items.append(current_item.model_copy(update={"translation_lines": list(item.translation_lines)}))
    await session.write_translation_items(
        normalized_items,
        reuse_contexts_by_path={
            item.location_path: freshness.current_contexts_by_path[item.location_path] for item in normalized_items
        },
    )


def load_json_array(path: Path) -> list[object]:
    """读取测试产物 JSON 数组，并在边界处收窄动态解析结果。"""
    raw_value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    json_array = ensure_json_array(coerce_json_value(raw_value), str(path))
    return [item for item in json_array]


def _contains_japanese_test_char(text: str) -> bool:
    """判断测试样本文本是否含有日文假名。"""
    return any("\u3040" <= char <= "\u30ff" for char in text)


def _translated_test_line_preserving_controls(line: str, text_rules: TextRules) -> str:
    """生成不含源语言残留且保留原控制符的测试译文。"""
    spans = text_rules.iter_control_sequence_spans(line)
    if not spans:
        return "测试"
    translated_parts: list[str] = []
    last_end = 0
    visible_text_inserted = False
    for span in spans:
        if span.start_index > last_end and not visible_text_inserted:
            translated_parts.append("测试")
            visible_text_inserted = True
        translated_parts.append(span.original)
        last_end = span.end_index
    if last_end < len(line) and not visible_text_inserted:
        translated_parts.append("测试")
    if not visible_text_inserted:
        translated_parts.append("测试")
    return "".join(translated_parts)


def _mv_virtual_namebox_rules_text() -> str:
    """生成测试用 MV 虚拟名字框规则 JSON。"""
    return json.dumps(
        {
            "rules": [
                {
                    "name": "standalone-colon",
                    "pattern": r"^(?P<speaker>案内人)：$",
                    "speaker_group": "speaker",
                    "speaker_policy": "translate",
                    "render_template": "{speaker}：",
                }
            ]
        },
        ensure_ascii=False,
    )


def _broad_mv_angle_namebox_rules_text() -> str:
    """生成会吞掉尖括号候选的测试用 MV 虚拟名字框规则 JSON。"""
    return json.dumps(
        {
            "rules": [
                {
                    "name": "broad-angle",
                    "pattern": r"^<(?P<speaker>[^>\r\n]{1,80})>$",
                    "speaker_group": "speaker",
                    "speaker_policy": "translate",
                    "render_template": "<{speaker}>",
                }
            ]
        },
        ensure_ascii=False,
    )


async def _install_minimal_external_text_rules(
    *,
    registry: GameRegistry,
    game_title: str,
    game_dir: Path,
) -> None:
    """为占位符草稿测试安装三类外部文本规则前置状态。"""
    game_data = await load_game_data(game_dir)
    plugin_name = str(game_data.plugins_js[0].get("name", ""))
    async with await registry.open_game(game_title) as session:
        await session.replace_plugin_text_rules(
            [
                PluginTextRuleRecord(
                    plugin_index=0,
                    plugin_name=plugin_name,
                    plugin_hash=build_plugin_hash(game_data.plugins_js[0]),
                    path_templates=["$['parameters']['Message']"],
                )
            ]
        )
        await session.replace_event_command_text_rules(
            [
                EventCommandTextRuleRecord(
                    command_code=357,
                    parameter_filters=[
                        EventCommandParameterFilter(index=0, value=plugin_name),
                        EventCommandParameterFilter(index=1, value="Show"),
                    ],
                    path_templates=["$['parameters'][3]['message']"],
                )
            ]
        )
        await session.replace_note_tag_text_rules(
            [NoteTagTextRuleRecord(file_name="Items.json", tag_names=["拡張説明"])]
        )


@pytest.mark.asyncio
async def test_doctor_uses_fake_llm_check_without_real_request(tmp_path: Path) -> None:
    """doctor 可以注入模型检查函数，测试环境不触发真实 API。"""
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    called_models: list[str] = []

    async def fake_llm_check(_llm_handler: LLMHandler, model: str) -> None:
        """记录模型名称，不发起网络请求。"""
        called_models.append(model)

    service = AgentToolkitService(
        game_registry=GameRegistry(db_dir),
        llm_check=fake_llm_check,
        setting_path=EXAMPLE_SETTING_PATH,
    )

    report = await service.doctor(game_title=None, check_llm=True)

    assert report.status in {"ok", "warning"}
    assert called_models
    assert report.summary["llm_model"]


@pytest.mark.asyncio
async def test_doctor_creates_missing_db_directory(tmp_path: Path) -> None:
    """doctor 会自愈创建缺失的固定数据库目录。"""
    db_dir = tmp_path / "missing-db"
    service = AgentToolkitService(
        game_registry=GameRegistry(db_dir),
        setting_path=EXAMPLE_SETTING_PATH,
    )

    report = await service.doctor(game_title=None, check_llm=False)

    error_codes = {error.code for error in report.errors}
    assert "db_dir" not in error_codes
    assert db_dir.exists()
    assert any(warning.message == "仅完成本地对话结构检查，未访问模型" for warning in report.warnings)
    checks = report.details["checks"]
    assert isinstance(checks, list)
    assert any(
        isinstance(check, dict) and check.get("name") == "llm_local_dialogue_structure" and check.get("status") == "ok"
        for check in checks
    )


@pytest.mark.asyncio
async def test_agent_translation_source_load_skips_writable_copies_by_default(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """Agent 只读翻译源加载默认不构造大型可写副本。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = _AgentToolkitServiceProbe(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    async with await registry.open_game("テストゲーム") as session:
        game_data = await service.load_translation_source_for_test(session)
        writable_game_data = await service.load_translation_source_for_test(
            session,
            include_writable_copies=True,
        )

    assert game_data.data
    assert game_data.writable_data == {}
    assert game_data.writable_plugins_js == []
    assert game_data.writable_plugin_source_files == {}
    assert writable_game_data.writable_data
    assert writable_game_data.writable_plugins_js


@pytest.mark.asyncio
async def test_doctor_reports_missing_standard_data_file(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """doctor 会把目标游戏标准 data 文件缺失报告为错误。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    (minimal_game_dir / "data" / "Animations.json").unlink()
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.doctor(game_title="テストゲーム", check_llm=False)

    assert report.status == "error"
    game_errors = [error.message for error in report.errors if error.code == "game"]
    assert game_errors
    assert "Animations.json" in game_errors[0]


@pytest.mark.asyncio
async def test_doctor_respects_reviewed_empty_rule_state_until_scope_changes(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """doctor 能区分规则未处理、已确认空结果和输入范围变化后的过期空结果。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    game_data = await load_game_data(minimal_game_dir)
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    text_rules = TextRules.from_setting(setting.text_rules)
    async with await registry.open_game("テストゲーム") as session:
        reviewed_event_codes = event_command_rule_codes_for_setting(
            game_data=game_data,
            setting=setting,
        )
        await session.replace_rule_review_state(
            rule_domain=PLUGIN_TEXT_RULE_DOMAIN,
            scope_hash=plugin_rule_scope_hash(game_data),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN,
            scope_hash=event_command_rule_scope_hash_for_setting(
                game_data=game_data,
                setting=setting,
            ),
            reviewed_empty=True,
            scope_contract_version=1,
            scope_payload={
                "kind": "event_command_codes",
                "command_codes": [code for code in sorted(reviewed_event_codes)],
            },
        )
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
    fresh_report = await service.doctor(game_title="テストゲーム", check_llm=False)

    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), "CommonEvents.json")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    command = ensure_json_object(commands[4], "CommonEvents[1].list[4]")
    parameters = ensure_json_array(command["parameters"], "CommonEvents[1].list[4].parameters")
    parameters[2] = "追加参数"
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False, indent=2), encoding="utf-8")
    stale_report = await service.doctor(game_title="テストゲーム", check_llm=False)

    fresh_warning_codes = {warning.code for warning in fresh_report.warnings}
    stale_warning_codes = {warning.code for warning in stale_report.warnings}
    assert "plugin_rules" not in fresh_warning_codes
    assert "event_command_rules" not in fresh_warning_codes
    assert "note_tag_rules" not in fresh_warning_codes
    assert fresh_report.summary["plugin_rules_reviewed_empty"] is True
    assert fresh_report.summary["event_command_rules_reviewed_empty"] is True
    assert fresh_report.summary["note_tag_rules_reviewed_empty"] is True
    assert stale_report.summary["event_command_rules_reviewed_empty"] is True
    assert stale_report.summary["event_command_rules_review_state_stale"] is False
    assert "event_command_rules_review_state_stale" not in stale_warning_codes


@pytest.mark.asyncio
async def test_doctor_reports_mv_virtual_namebox_rule_state(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
) -> None:
    """doctor 会报告 MV 虚拟名字框规则导入和空规则确认状态。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_mv_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    missing_report = await service.doctor(game_title="MVテストゲーム", check_llm=False)
    empty_report = await service.import_mv_virtual_namebox_rules(
        game_title="MVテストゲーム",
        rules_text='{"rules":[]}',
        confirm_empty=True,
    )
    confirmed_report = await service.doctor(game_title="MVテストゲーム", check_llm=False)

    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), "CommonEvents.json")
    common_events.append(
        {
            "id": 99,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["新しい候補："]},
                {"code": 401, "parameters": ["本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False, indent=2), encoding="utf-8")
    stale_report = await service.doctor(game_title="MVテストゲーム", check_llm=False)

    missing_warning_codes = {warning.code for warning in missing_report.warnings}
    confirmed_warning_codes = {warning.code for warning in confirmed_report.warnings}
    stale_warning_codes = {warning.code for warning in stale_report.warnings}
    assert empty_report.status == "warning"
    assert "mv_virtual_namebox_rules" in missing_warning_codes
    assert missing_report.summary["mv_virtual_namebox_rule_count"] == 0
    assert missing_report.summary["mv_virtual_namebox_rules_reviewed_empty"] is False
    assert "mv_virtual_namebox_rules" not in confirmed_warning_codes
    assert confirmed_report.summary["mv_virtual_namebox_rule_count"] == 0
    assert confirmed_report.summary["mv_virtual_namebox_rules_reviewed_empty"] is True
    assert confirmed_report.summary["mv_virtual_namebox_rules_review_state_stale"] is False
    assert stale_report.summary["mv_virtual_namebox_rules_reviewed_empty"] is True
    assert stale_report.summary["mv_virtual_namebox_rules_review_state_stale"] is False
    assert "mv_virtual_namebox_rules_review_state_stale" not in stale_warning_codes


@pytest.mark.asyncio
async def test_import_empty_plugin_rules_requires_explicit_empty_confirmation(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件规则为空时默认报错；显式确认后允许保存当前插件范围的空结果。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    setting_text = EXAMPLE_SETTING_PATH.read_text(encoding="utf-8").replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{(ROOT / "prompts" / "text_translation_ja_to_zh_system.md").as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv(APP_HOME_ENV_NAME, str(app_home))
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    rules_path = tmp_path / "plugin-rules.json"
    empty_rules_path = tmp_path / "plugin-rules-empty.json"
    _ = rules_path.write_text(
        json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "TestPlugin",
                    "paths": ["$['parameters']['Message']"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _ = empty_rules_path.write_text("[]\n", encoding="utf-8")

    _ = await handler.import_plugin_rules(game_title="テストゲーム", input_path=rules_path)
    async with await registry.open_game("テストゲーム") as session:
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path="plugins.js/0/Message",
                    item_type="short_text",
                    original_lines=["プラグイン台詞"],
                    translation_lines=["插件译文"],
                )
            ]
        )

    with pytest.raises(RuntimeError, match="--confirm-empty"):
        _ = await handler.import_plugin_rules(game_title="テストゲーム", input_path=empty_rules_path)
    summary = await handler.import_plugin_rules(
        game_title="テストゲーム",
        input_path=empty_rules_path,
        confirm_empty=True,
    )
    async with await registry.open_game("テストゲーム") as session:
        translated_items = await session.read_translated_items()
        state = await session.read_rule_review_state(rule_domain=PLUGIN_TEXT_RULE_DOMAIN)

    assert summary.imported_plugin_count == 0
    assert summary.imported_rule_count == 0
    assert summary.deleted_translation_items == 1
    assert summary.deleted_translation_backup_path
    assert translated_items == []
    assert state is not None
    assert state.scope_hash == plugin_rule_scope_hash(await load_game_data(minimal_game_dir))


@pytest.mark.asyncio
@pytest.mark.usefixtures("app_home_with_example_setting")
async def test_import_plugin_rules_rejects_english_protocol_value_paths(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """插件规则导入会拒绝英文模式下只命中协议值的路径。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    rules_path = tmp_path / "plugin-rules.json"
    _ = rules_path.write_text(
        json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "VisiblePlugin",
                    "paths": ["$['parameters']['Enabled']"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())

    try:
        with pytest.raises(ValueError, match="没有命中玩家可见可翻译文本"):
            _ = await handler.import_plugin_rules(
                game_title="English Fixture Game",
                input_path=rules_path,
            )
    finally:
        await handler.close()


@pytest.mark.asyncio
async def test_import_plugin_rules_uses_configured_text_rules(
    minimal_english_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件规则导入按当前配置判断命中文本，不能退回固定语言档案。"""
    configured_text_rules = TextRulesSetting(
        source_language="en",
        source_residual_label="英文",
        source_text_required_pattern="true",
        source_text_exclusion_profile="none",
        source_residual_segment_pattern="true",
    )

    def fake_load_setting(
        setting_path: str | Path | None = None,
        overrides: SettingOverrides | None = None,
        source_language: SourceLanguage = "ja",
        additional_source_languages: tuple[SourceLanguage, ...] = (),
    ) -> Setting:
        """返回带测试文本规则的配置。"""
        target_setting_path = EXAMPLE_SETTING_PATH if setting_path is None else Path(setting_path)
        setting = load_setting(
            target_setting_path,
            overrides=overrides,
            source_language=source_language,
            additional_source_languages=additional_source_languages,
        )
        return setting.model_copy(update={"text_rules": configured_text_rules})

    monkeypatch.setattr("app.application.handler.load_setting", fake_load_setting)
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    rules_path = tmp_path / "plugin-rules.json"
    _ = rules_path.write_text(
        json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "VisiblePlugin",
                    "paths": ["$['parameters']['Enabled']"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())

    try:
        summary = await handler.import_plugin_rules(
            game_title="English Fixture Game",
            input_path=rules_path,
        )
    finally:
        await handler.close()

    assert summary.imported_rule_count == 1


@pytest.mark.asyncio
async def test_import_empty_event_command_rules_requires_explicit_empty_confirmation(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事件指令规则为空时默认报错；显式确认后允许保存当前编码范围的空结果。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    setting_text = EXAMPLE_SETTING_PATH.read_text(encoding="utf-8").replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{(ROOT / "prompts" / "text_translation_ja_to_zh_system.md").as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv(APP_HOME_ENV_NAME, str(app_home))
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    rules_path = tmp_path / "event-command-rules.json"
    empty_rules_path = tmp_path / "event-command-rules-empty.json"
    _ = rules_path.write_text(
        json.dumps(
            {
                "357": [
                    {
                        "match": {
                            "0": "TestPlugin",
                            "1": "Show",
                        },
                        "paths": ["$['parameters'][3]['message']"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _ = empty_rules_path.write_text("{}\n", encoding="utf-8")

    _ = await handler.import_event_command_rules(game_title="テストゲーム", input_path=rules_path)
    async with await registry.open_game("テストゲーム") as session:
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path="CommonEvents.json/1/4/parameters/3/message",
                    item_type="short_text",
                    original_lines=["プラグイン台詞"],
                    translation_lines=["事件指令译文"],
                )
            ]
        )

    with pytest.raises(RuntimeError, match="--confirm-empty"):
        _ = await handler.import_event_command_rules(
            game_title="テストゲーム",
            input_path=empty_rules_path,
        )
    summary = await handler.import_event_command_rules(
        game_title="テストゲーム",
        input_path=empty_rules_path,
        confirm_empty=True,
        command_codes={357},
    )
    async with await registry.open_game("テストゲーム") as session:
        translated_items = await session.read_translated_items()
        state = await session.read_rule_review_state(rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN)

    game_data = await load_game_data(minimal_game_dir)
    assert summary.imported_rule_group_count == 0
    assert summary.imported_path_rule_count == 0
    assert summary.deleted_translation_items == 1
    assert summary.deleted_translation_backup_path
    assert translated_items == []
    assert state is not None
    assert state.scope_hash == event_command_rule_scope_hash_for_command_codes(
        game_data=game_data,
        command_codes=frozenset({357}),
    )
    assert state.scope_contract_version == 1
    assert state.scope_payload == {
        "kind": "event_command_codes",
        "command_codes": [357],
    }


@pytest.mark.asyncio
async def test_import_empty_event_command_rules_records_cli_code_scope(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事件指令空规则确认使用 CLI 显式编码计算范围。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    setting_text = EXAMPLE_SETTING_PATH.read_text(encoding="utf-8").replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{(ROOT / "prompts" / "text_translation_ja_to_zh_system.md").as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv(APP_HOME_ENV_NAME, str(app_home))
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    handler = TranslationHandler(game_registry=registry, llm_handler=LLMHandler())
    empty_rules_path = tmp_path / "event-command-rules-empty.json"
    _ = empty_rules_path.write_text("{}\n", encoding="utf-8")
    game_data = await load_game_data(minimal_game_dir)

    _ = await handler.import_event_command_rules(
        game_title="テストゲーム",
        input_path=empty_rules_path,
        confirm_empty=True,
        command_codes={999},
    )

    async with await registry.open_game("テストゲーム") as session:
        state = await session.read_rule_review_state(rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN)

    assert state is not None
    assert state.scope_hash == event_command_rule_scope_hash_for_command_codes(
        game_data=game_data,
        command_codes=frozenset({999}),
    )
    assert state.scope_payload == {
        "kind": "event_command_codes",
        "command_codes": [999],
    }


@pytest.mark.asyncio
async def test_mv_virtual_namebox_rule_commands_validate_import_and_reject_mz(
    minimal_mv_game_dir: Path,
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """MV 虚拟名字框规则只能用于 MV，并通过 CLI 服务校验后保存。"""
    mv_common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    mv_common_events = ensure_json_array(
        coerce_json_value(cast(object, json.loads(mv_common_events_path.read_text(encoding="utf-8")))),
        "CommonEvents.json",
    )
    mv_common_events.append(
        {
            "id": 2,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["案内人："]},
                {"code": 401, "parameters": ["本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _ = mv_common_events_path.write_text(json.dumps(mv_common_events, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_mv_game_dir, source_language="ja")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    candidates_path = tmp_path / "mv-namebox-candidates.json"

    export_report = await service.export_mv_virtual_namebox_candidates(
        game_title="MVテストゲーム",
        output_path=candidates_path,
    )
    validate_report = await service.validate_mv_virtual_namebox_rules(
        game_title="MVテストゲーム",
        rules_text=_mv_virtual_namebox_rules_text(),
    )
    import_report = await service.import_mv_virtual_namebox_rules(
        game_title="MVテストゲーム",
        rules_text=_mv_virtual_namebox_rules_text(),
    )
    mz_report = await service.validate_mv_virtual_namebox_rules(
        game_title="テストゲーム",
        rules_text=_mv_virtual_namebox_rules_text(),
    )

    assert export_report.status in {"ok", "warning"}
    assert candidates_path.exists()
    candidate_count = export_report.summary["candidate_count"]
    assert isinstance(candidate_count, int)
    assert candidate_count >= 1
    assert validate_report.status == "ok"
    assert validate_report.summary["rule_count"] == 1
    assert validate_report.summary["matched_candidate_count"] == 1
    assert import_report.status == "ok"
    assert import_report.summary["rule_count"] == 1
    assert {error.code for error in mz_report.errors} == {"mv_virtual_namebox_rules_forbidden"}
    async with await registry.open_game("MVテストゲーム") as session:
        records = await session.read_mv_virtual_namebox_rules()
    assert len(records) == 1
    assert records[0].rule_name == "standalone-colon"


@pytest.mark.asyncio
async def test_mv_virtual_namebox_validation_reports_overwide_angle_rule_hits(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
) -> None:
    """校验会指出尖括号宽规则误吞动态控制符，并列出新命中的候选。"""
    mv_common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    raw_value = coerce_json_value(cast(object, json.loads(mv_common_events_path.read_text(encoding="utf-8"))))
    mv_common_events = ensure_json_array(raw_value, "CommonEvents.json")
    mv_common_events.extend(
        [
            {
                "id": 2,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<\\n[1]>"]},
                    {"code": 401, "parameters": ["動的名の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 3,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<シナリオ>"]},
                    {"code": 401, "parameters": ["制作表示です"]},
                    {"code": 0, "parameters": []},
                ],
            },
        ]
    )
    _ = mv_common_events_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_mv_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    validate_report = await service.validate_mv_virtual_namebox_rules(
        game_title="MVテストゲーム",
        rules_text=_broad_mv_angle_namebox_rules_text(),
    )
    import_report = await service.import_mv_virtual_namebox_rules(
        game_title="MVテストゲーム",
        rules_text=_broad_mv_angle_namebox_rules_text(),
    )
    details = ensure_json_object(validate_report.details, "details")
    newly_matched_candidates = ensure_json_array(
        details["newly_matched_candidates"],
        "details.newly_matched_candidates",
    )
    newly_matched_texts: set[str] = set()
    for index, raw_detail in enumerate(newly_matched_candidates):
        detail = ensure_json_object(raw_detail, f"details.newly_matched_candidates[{index}]")
        text = detail.get("text")
        if isinstance(text, str):
            newly_matched_texts.add(text)
    validate_json = validate_report.to_json_text()
    newly_matched_count = validate_report.summary["newly_matched_candidate_count"]

    assert validate_report.status == "error"
    assert import_report.status == "error"
    assert "broad-angle" in validate_json
    assert "标准角色名控制符被 translate 规则命中" in validate_json
    assert isinstance(newly_matched_count, int)
    assert newly_matched_count >= 2
    assert "<\\n[1]>" in newly_matched_texts
    assert "<シナリオ>" in newly_matched_texts


@pytest.mark.asyncio
async def test_mv_workflow_gate_requires_namebox_rules_or_confirmed_empty(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
) -> None:
    """MV 翻译流程在虚拟名字框规则未导入也未确认空规则时阻断。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_mv_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("MVテストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        game_data = await load_game_data(minimal_mv_game_dir)
        session.set_game_data(game_data)
        text_rules = TextRules.from_setting(setting.text_rules)
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        before_errors = await collect_workflow_gate_errors(
            session=session,
            context=analysis_context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )
    empty_rejected_report = await service.import_mv_virtual_namebox_rules(
        game_title="MVテストゲーム",
        rules_text='{"rules":[]}',
    )
    empty_import_report = await service.import_mv_virtual_namebox_rules(
        game_title="MVテストゲーム",
        rules_text='{"rules":[]}',
        confirm_empty=True,
    )
    async with await registry.open_game("MVテストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        game_data = await load_game_data(minimal_mv_game_dir)
        session.set_game_data(game_data)
        text_rules = TextRules.from_setting(setting.text_rules)
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        after_errors = await collect_workflow_gate_errors(
            session=session,
            context=analysis_context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )
        state = await session.read_rule_review_state(rule_domain=MV_VIRTUAL_NAMEBOX_RULE_DOMAIN)

    assert "mv_virtual_namebox_missing" in {error.code for error in before_errors}
    assert empty_rejected_report.status == "error"
    assert empty_import_report.status == "warning"
    assert "mv_virtual_namebox_missing" not in {error.code for error in after_errors}
    assert state is not None
    assert state.scope_hash == mv_virtual_namebox_rule_scope_hash(mv_virtual_namebox_candidate_details(game_data))


@pytest.mark.asyncio
async def test_text_scope_and_audit_coverage_use_unified_contract(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """统一文本清单和覆盖审计暴露同一批可处理文本。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    scope_report = await service.text_scope(game_title="テストゲーム", include_write_probe=True)
    audit_report = await service.audit_coverage(game_title="テストゲーム", include_write_probe=True)

    entries = ensure_json_array(scope_report.details["entries"], "entries")
    first_entry = ensure_json_object(entries[0], "entries[0]")
    assert scope_report.status == "ok"
    assert scope_report.summary["entry_count"] == len(entries)
    assert first_entry.keys() >= {
        "location_path",
        "source_type",
        "rule_source",
        "original_lines",
        "enters_translation",
        "can_save_translation",
        "can_write_back",
        "cannot_process_reason",
    }
    assert audit_report.status == "error"
    assert audit_report.summary["extractable_count"] == scope_report.summary["extractable_count"]
    assert audit_report.summary["pending_count"] == scope_report.summary["extractable_count"]


@pytest.mark.asyncio
async def test_text_scope_and_audit_coverage_use_real_write_probe(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统一文本清单的可写状态来自真实写入协议探针。"""

    def fake_collect_native_write_protocol_details(
        *,
        game_data: JsonObject,
        plugins_js: list[JsonValue],
        items: list[TranslationItem],
    ) -> list[JsonValue]:
        """把第一条文本标记为不可写，模拟 Rust 写入协议失败。"""
        _ = (game_data, plugins_js)
        return [{"location_path": items[0].location_path, "reason": "探针失败"}]

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        fake_collect_native_write_protocol_details,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    scope_report = await service.text_scope(game_title="テストゲーム", include_write_probe=True)
    audit_report = await service.audit_coverage(game_title="テストゲーム", include_write_probe=True)
    unwritable_items = ensure_json_array(scope_report.details["unwritable_items"], "unwritable_items")
    first_unwritable = ensure_json_object(unwritable_items[0], "unwritable_items[0]")

    assert scope_report.summary["unwritable_count"] == 1
    assert scope_report.summary["write_back_probe_enabled"] is True
    assert first_unwritable["can_write_back"] is False
    assert first_unwritable["cannot_process_reason"] == "探针失败"
    assert audit_report.status == "error"
    assert {error.code for error in audit_report.errors} >= {"coverage_unwritable"}
    extractable_count = scope_report.summary["extractable_count"]
    assert isinstance(extractable_count, int)
    assert audit_report.summary["writable_count"] == extractable_count - 1


@pytest.mark.asyncio
async def test_write_back_probe_uses_shallow_probe_items(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入探针只替换译文行，不深拷贝原文和定位结构。"""
    game_data = await load_game_data(minimal_game_dir)
    source_item = TranslationItem(
        location_path="Items.json/1/name",
        item_type="short_text",
        role="item_name",
        original_lines=["薬草"],
        source_line_paths=["Items.json/1/name"],
        translation_lines=["既存译文"],
        placeholder_map={"[RMMZ_TEST_1]": "\\C[1]"},
    )
    received_items: list[TranslationItem] = []

    def fake_collect_native_write_protocol_details(
        *,
        game_data: JsonObject,
        plugins_js: list[JsonValue],
        items: list[TranslationItem],
    ) -> list[JsonValue]:
        """捕获探针条目，避免测试依赖 Rust 写入协议结果。"""
        _ = (game_data, plugins_js)
        received_items.extend(items)
        return []

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        fake_collect_native_write_protocol_details,
    )

    reasons = collect_write_back_probe_reasons(
        game_data=game_data,
        active_items=[source_item],
    )

    assert reasons == {}
    assert len(received_items) == 1
    probe_item = received_items[0]
    assert probe_item is not source_item
    assert probe_item.original_lines is source_item.original_lines
    assert probe_item.source_line_paths is source_item.source_line_paths
    assert probe_item.placeholder_map is source_item.placeholder_map
    assert probe_item.translation_lines == ["回写校验"]
    assert source_item.translation_lines == ["既存译文"]


@pytest.mark.asyncio
async def test_read_only_scope_reports_skip_write_probe_by_default(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只读文本范围报告默认不执行写入探针。"""

    def forbidden_write_probe(*args: object, **kwargs: object) -> NoReturn:
        """默认只读报告不应触碰写入协议探针。"""
        _ = (args, kwargs)
        raise AssertionError("只读文本范围报告默认不应执行写入探针")

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        forbidden_write_probe,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending.json"

    scope_report = await service.text_scope(game_title="テストゲーム")
    audit_report = await service.audit_coverage(game_title="テストゲーム")
    quality_report = await service.quality_report(game_title="テストゲーム")
    pending_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=1,
    )

    assert scope_report.summary["write_back_probe_enabled"] is False
    assert audit_report.summary["write_back_probe_enabled"] is False
    assert quality_report.summary["write_back_probe_enabled"] is False
    assert pending_report.summary["write_back_probe_enabled"] is False


@pytest.mark.asyncio
async def test_text_scope_reports_global_write_probe_failure(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入协议探针整体不可用时返回全局错误，不伪装成每条文本不可写。"""

    def broken_collect_native_write_protocol_details(
        *,
        game_data: JsonObject,
        plugins_js: list[JsonValue],
        items: list[TranslationItem],
    ) -> list[JsonValue]:
        """模拟原生探针基础设施故障。"""
        _ = (game_data, plugins_js, items)
        raise RuntimeError("native probe unavailable")

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        broken_collect_native_write_protocol_details,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    scope_report = await service.text_scope(game_title="テストゲーム", include_write_probe=True)
    audit_report = await service.audit_coverage(game_title="テストゲーム", include_write_probe=True)
    quality_report = await service.quality_report(game_title="テストゲーム", include_write_probe=True)

    assert scope_report.status == "error"
    assert audit_report.status == "error"
    assert quality_report.status == "error"
    assert {error.code for error in scope_report.errors} == {"write_probe_failed"}
    assert {error.code for error in audit_report.errors} >= {"write_probe_failed"}
    assert {error.code for error in quality_report.errors} >= {"write_probe_failed"}
    assert scope_report.summary["write_back_probe_failed"] is True
    assert scope_report.summary["unwritable_count"] == 0


@pytest.mark.asyncio
async def test_text_scope_reports_batch_write_probe_failure_directly(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入协议批量探针失败时直接报告全局错误。"""
    failed_single_path = ""

    def flaky_collect_native_write_protocol_details(
        *,
        game_data: JsonObject,
        plugins_js: list[JsonValue],
        items: list[TranslationItem],
    ) -> list[JsonValue]:
        """模拟批量探针失败，不再逐条重试。"""
        nonlocal failed_single_path
        _ = (game_data, plugins_js)
        if len(items) > 1:
            raise RuntimeError("batch probe unavailable")
        item = items[0]
        if not failed_single_path:
            failed_single_path = item.location_path
            raise RuntimeError("single path probe unavailable")
        return []

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        flaky_collect_native_write_protocol_details,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    scope_report = await service.text_scope(game_title="テストゲーム", include_write_probe=True)

    assert scope_report.status == "error"
    assert not failed_single_path
    assert scope_report.summary["write_back_probe_failed"] is True
    assert scope_report.summary["unwritable_count"] == 0
    assert {error.code for error in scope_report.errors} == {"write_probe_failed"}


@pytest.mark.asyncio
async def test_quality_report_stops_on_coverage_error_before_native_checks(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖审计不通过时，质量报告不能继续执行后续原生质检。"""

    def fake_scope_write_protocol(
        *,
        game_data: JsonObject,
        plugins_js: list[JsonValue],
        items: list[TranslationItem],
    ) -> list[JsonValue]:
        """把第一条文本标记为不可写，制造覆盖审计错误。"""
        _ = (game_data, plugins_js)
        return [{"location_path": items[0].location_path, "reason": "探针失败"}]

    def forbidden_quality_check(*args: object, **kwargs: object) -> NoReturn:
        """覆盖错误后不应再进入译文本体质检。"""
        _ = (args, kwargs)
        raise AssertionError("覆盖错误后不应继续执行译文本体质检")

    def forbidden_write_protocol(*args: object, **kwargs: object) -> NoReturn:
        """覆盖错误后不应再进入写入协议质检。"""
        _ = (args, kwargs)
        raise AssertionError("覆盖错误后不应继续执行写入协议质检")

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        fake_scope_write_protocol,
    )
    monkeypatch.setattr(
        "app.agent_toolkit.services.quality.collect_native_quality_details",
        forbidden_quality_check,
    )
    monkeypatch.setattr(
        "app.agent_toolkit.services.quality.collect_native_write_protocol_details",
        forbidden_write_protocol,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.quality_report(game_title="テストゲーム", include_write_probe=True)

    assert report.status == "error"
    assert "coverage_unwritable" in {error.code for error in report.errors}
    assert report.summary["source_residual_count"] == 0
    assert report.summary["write_back_protocol_count"] == 0


@pytest.mark.asyncio
async def test_export_quality_fix_template_stops_on_text_scope_blocker(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """质量修复表遇到文本范围阻断错误时不能继续生成半可信结果。"""

    def fake_scope_write_protocol(
        *,
        game_data: JsonObject,
        plugins_js: list[JsonValue],
        items: list[TranslationItem],
    ) -> list[JsonValue]:
        """把第一条文本标记为不可写，制造文本范围阻断错误。"""
        _ = (game_data, plugins_js)
        return [{"location_path": items[0].location_path, "reason": "探针失败"}]

    def forbidden_quality_check(*args: object, **kwargs: object) -> NoReturn:
        """文本范围阻断后不应再进入译文本体质检。"""
        _ = (args, kwargs)
        raise AssertionError("文本范围阻断后不应继续执行译文本体质检")

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        fake_scope_write_protocol,
    )
    monkeypatch.setattr(
        "app.agent_toolkit.services.quality.collect_native_quality_details",
        forbidden_quality_check,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    output_path = tmp_path / "quality-fix.json"

    report = await service.export_quality_fix_template(
        game_title="テストゲーム",
        output_path=output_path,
        include_write_probe=True,
    )

    assert report.status == "error"
    assert "coverage_unwritable" in {error.code for error in report.errors}
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_read_only_placeholder_scan_does_not_run_write_probe(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只读占位符候选扫描不能暗中执行写入探针。"""

    def forbidden_write_probe(*args: object, **kwargs: object) -> NoReturn:
        """只读扫描不应触碰写入协议探针。"""
        _ = (args, kwargs)
        raise AssertionError("只读扫描不应执行写入探针")

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        forbidden_write_probe,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.scan_placeholder_candidates(
        game_title="テストゲーム",
        custom_placeholder_rules_text=None,
    )

    assert report.status in {"ok", "warning"}
    assert "candidate_count" in report.summary


@pytest.mark.asyncio
async def test_validate_placeholder_rules_blocks_translatable_text_loss() -> None:
    """自定义占位符规则不能把含源语言正文的样本文本整体吞掉。"""
    service = AgentToolkitService(setting_path=EXAMPLE_SETTING_PATH)

    unsafe_report = await service.validate_placeholder_rules(
        game_title=None,
        custom_placeholder_rules_text=json.dumps({r"こんにちは": "[CUSTOM_SWALLOW_{index}]"}, ensure_ascii=False),
        sample_texts=["こんにちは"],
    )
    safe_report = await service.validate_placeholder_rules(
        game_title=None,
        custom_placeholder_rules_text=json.dumps({r"^◆<[^>]+>ｔ": "[CUSTOM_VOICE_{index}]"}, ensure_ascii=False),
        sample_texts=["◆<アリス>ｔこんにちは"],
    )

    unsafe_error_codes = {error.code for error in unsafe_report.errors}
    safe_error_codes = {error.code for error in safe_report.errors}
    assert "placeholder_rule_loses_translatable_text" in unsafe_error_codes
    assert "placeholder_rule_loses_translatable_text" not in safe_error_codes


@pytest.mark.asyncio
async def test_feedback_verification_and_plugin_source_scan_are_structural_only(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """反馈反查能提示源码命中，完整候选只能通过 AST 地图导出。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), str(common_events_path))
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    command_list = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    parameters: JsonArray = ["一行\n二行"]
    command: JsonObject = {"code": 401, "parameters": parameters}
    command_list.insert(2, command)
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False, indent=2), encoding="utf-8")
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "\n".join(
            [
                "Window_Base.prototype.drawText('プラグイン直書き', 0, 0, 320);",
                "Window_Base.prototype.drawText('img/system/日本語.png', 0, 0, 320);",
            ]
        ),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    feedback_path = tmp_path / "feedback-texts.json"
    risk_report_path = tmp_path / "plugin-source-risk-report.json"
    ast_map_path = tmp_path / "plugin-source-ast-map.json"
    _ = feedback_path.write_text(
        json.dumps(["こんにちは", "プラグイン直書き", "一行\n二行"], ensure_ascii=False),
        encoding="utf-8",
    )

    verify_report = await service.verify_feedback_text(game_title="テストゲーム", input_path=feedback_path)
    scan_report = await service.scan_plugin_source_text(game_title="テストゲーム", output_path=risk_report_path)
    ast_report = await service.export_plugin_source_ast_map(game_title="テストゲーム", output_path=ast_map_path)
    risk_report = load_json_object(risk_report_path)
    ast_map = load_json_object(ast_map_path)
    ast_files = ensure_json_array(coerce_json_value(ast_map["files"]), "plugin-source-ast-map.files")
    candidates: JsonArray = []
    for ast_file in ast_files:
        ast_file_object = ensure_json_object(coerce_json_value(ast_file), "plugin-source-ast-map.files[]")
        candidates.extend(
            ensure_json_array(
                coerce_json_value(ast_file_object["candidates"]),
                "plugin-source-ast-map.files[].candidates",
            )
        )
    occurrence_count = verify_report.summary["occurrence_count"]

    assert verify_report.status == "error"
    assert isinstance(occurrence_count, int)
    assert occurrence_count >= 1
    occurrences = ensure_json_array(verify_report.details["occurrences"], "occurrences")
    gap_types: set[str] = set()
    for occurrence in occurrences:
        occurrence_object = ensure_json_object(coerce_json_value(occurrence), "occurrence")
        gap_type = occurrence_object.get("gap_type")
        if isinstance(gap_type, str):
            gap_types.add(gap_type)
    assert "translation_gap" in gap_types
    assert "plugin_source_hardcoded" in gap_types
    assert any(
        ensure_json_object(coerce_json_value(occurrence), "occurrence").get("text") == "一行\n二行"
        for occurrence in occurrences
    )
    assert scan_report.status == "ok"
    assert ast_report.status == "ok"
    assert "candidates" not in risk_report
    assert "files" not in risk_report
    assert risk_report["candidate_count"] == 2
    assert any(
        ensure_json_object(coerce_json_value(candidate), "candidate").get("text") == "プラグイン直書き"
        for candidate in candidates
    )
    resource_candidate = next(
        ensure_json_object(coerce_json_value(candidate), "candidate")
        for candidate in candidates
        if ensure_json_object(coerce_json_value(candidate), "candidate").get("text") == "img/system/日本語.png"
    )
    assert resource_candidate["structural_flags"] == ["resource_path_like"]


@pytest.mark.asyncio
async def test_feedback_verification_reads_active_files_not_origin_backups(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """反馈反查必须检查当前激活文件，不能把原始备份误报成激活文件残留。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    source_items = coerce_json_value(cast(object, json.loads(items_path.read_text(encoding="utf-8"))))
    source_item = ensure_json_object(ensure_json_array(source_items, "source Items.json")[1], "source Items.json[1]")
    source_item["description"] = "With this rope..."
    _ = items_path.write_text(json.dumps(source_items, ensure_ascii=False, indent=2), encoding="utf-8")

    active_plugins = [
        {
            "name": "OriginOnlyPlugin",
            "status": True,
            "description": "已修复",
            "parameters": {"message": "是否读取此存档文件？"},
        }
    ]
    origin_plugins = [
        {
            "name": "OriginOnlyPlugin",
            "status": True,
            "description": "original",
            "parameters": {"message": "Whether to load this save file?"},
        }
    ]
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(origin_plugins, ensure_ascii=False, indent=2)};\n", encoding="utf-8"
    )

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="en")
    active_items = coerce_json_value(cast(object, json.loads(items_path.read_text(encoding="utf-8"))))
    active_item = ensure_json_object(ensure_json_array(active_items, "active Items.json")[1], "active Items.json[1]")
    active_item["description"] = "有了这根绳子，说不定能到达世界的中心。"
    _ = items_path.write_text(json.dumps(active_items, ensure_ascii=False, indent=2), encoding="utf-8")
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(active_plugins, ensure_ascii=False, indent=2)};\n", encoding="utf-8"
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    feedback_path = tmp_path / "feedback-texts.json"
    _ = feedback_path.write_text(
        json.dumps(["With this rope...", "Whether to load this save file?"], ensure_ascii=False),
        encoding="utf-8",
    )

    report = await service.verify_feedback_text(game_title="テストゲーム", input_path=feedback_path)

    assert report.status == "ok"
    assert report.summary["occurrence_count"] == 0
    assert report.details["occurrences"] == []


@pytest.mark.asyncio
async def test_default_active_runtime_audit_skips_plugin_source_text_branch(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """未启动插件源码支线时，当前运行审计只做运行完整性检查。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BadSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    bad_source_path = plugin_source_dir / "BadSource.js"
    _ = bad_source_path.write_text(
        "const Messages = { param2: ['頑張ってガマンする\\\\nn[0]くん…素敵よ♥'] };\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = bad_source_path.write_text(
        "const Messages = { param2: ['努力忍耐着的\\nn[0]君…真棒哦♥'] };\n",
        encoding="utf-8",
    )
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        game_data = await load_game_data(session.game_path)
        text_rules = TextRules.from_setting(setting.text_rules)
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        translated_items = [
            TranslationItem(
                location_path=item.location_path,
                item_type=item.item_type,
                role=item.role,
                original_lines=list(item.original_lines),
                source_line_paths=list(item.source_line_paths),
                translation_lines=[
                    _translated_test_line_preserving_controls(line, text_rules) for line in item.original_lines
                ],
            )
            for item in scope.active_items()
            if item.location_path in scope.writable_paths
        ]
        await session.write_translation_items(translated_items)

    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    quality_report = await service.quality_report(game_title="テストゲーム")
    runtime_report = await service.audit_active_runtime(game_title="テストゲーム")

    assert runtime_report.status == "ok"
    assert "active_runtime_placeholder_risk" not in {error.code for error in quality_report.errors}
    assert "active_runtime_placeholder_risk" not in {error.code for error in runtime_report.errors}
    assert "active_runtime_placeholder_risk_count" not in quality_report.summary
    assert runtime_report.summary["active_runtime_text_issue_audit_enabled"] is False
    assert runtime_report.summary["active_runtime_placeholder_risk_count"] == 0


@pytest.mark.asyncio
async def test_active_runtime_audit_rejects_plugin_source_read_errors(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """当前运行插件源码读取失败时必须报错，不能只写进摘要计数。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BrokenEncoding", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "BrokenEncoding.js").write_bytes(b"\xff\xfe\xff")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    runtime_report = await service.audit_active_runtime(game_title="テストゲーム")
    quality_report = await service.quality_report(game_title="テストゲーム")

    assert runtime_report.status == "error"
    assert quality_report.status == "error"
    assert "active_runtime_read_error" in {error.code for error in runtime_report.errors}
    assert "active_runtime_read_error" not in {error.code for error in quality_report.errors}
    runtime_read_error_count = runtime_report.summary["active_runtime_read_error_count"]
    assert isinstance(runtime_read_error_count, int)
    assert runtime_read_error_count >= 1
    assert "BrokenEncoding.js" in json.dumps(runtime_report.details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_active_runtime_audit_rejects_missing_enabled_plugin_source_file(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """当前运行插件配置启用了源码文件但文件不存在时必须报错。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "MissingSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    runtime_report = await service.audit_active_runtime(game_title="テストゲーム")
    quality_report = await service.quality_report(game_title="テストゲーム")

    assert runtime_report.status == "error"
    assert quality_report.status == "error"
    assert "active_runtime_read_error" in {error.code for error in runtime_report.errors}
    assert "active_runtime_read_error" not in {error.code for error in quality_report.errors}
    runtime_read_error_count = runtime_report.summary["active_runtime_read_error_count"]
    assert isinstance(runtime_read_error_count, int)
    assert runtime_read_error_count >= 1
    assert "active_runtime_read_error_count" not in quality_report.summary
    assert "MissingSource.js" in json.dumps(runtime_report.details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_active_runtime_audit_rejects_missing_plugin_source_directory(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """当前运行插件源码目录缺失时，启用插件必须按缺失源码报错。"""
    shutil.rmtree(minimal_game_dir / "js" / "plugins")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    runtime_report = await service.audit_active_runtime(game_title="テストゲーム")
    quality_report = await service.quality_report(game_title="テストゲーム")

    assert runtime_report.status == "error"
    assert quality_report.status == "error"
    assert "active_runtime_read_error" in {error.code for error in runtime_report.errors}
    assert "active_runtime_read_error" not in {error.code for error in quality_report.errors}
    assert "TestPlugin.js" in json.dumps(runtime_report.details, ensure_ascii=False)
    assert "ComplexPlugin.js" in json.dumps(runtime_report.details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_active_runtime_audit_rejects_plugin_source_syntax_errors(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """当前运行插件源码 JS 语法错误时必须报错，不能退回轻量扫描后放行。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BrokenSyntax", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    broken_source_path = plugin_source_dir / "BrokenSyntax.js"
    _ = broken_source_path.write_text(
        "const Messages = { title: '原文' };\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = broken_source_path.write_text(
        "const Messages = { title: '坏掉 };\n",
        encoding="utf-8",
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    runtime_report = await service.audit_active_runtime(game_title="テストゲーム")
    quality_report = await service.quality_report(game_title="テストゲーム")

    assert runtime_report.status == "error"
    assert quality_report.status == "error"
    assert "active_runtime_syntax_error" in {error.code for error in runtime_report.errors}
    assert "active_runtime_syntax_error" not in {error.code for error in quality_report.errors}
    assert runtime_report.summary["active_runtime_syntax_error_count"] == 1
    assert "active_runtime_syntax_error_count" not in quality_report.summary
    assert "BrokenSyntax.js" in json.dumps(runtime_report.details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_audit_active_runtime_reuses_scan_cache_and_invalidates_changed_files(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前运行审计跨命令复用 AST 缓存，并在文件 hash 变化时重新扫描。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "CacheSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    source_path = plugin_source_dir / "CacheSource.js"
    _ = source_path.write_text("const Messages = { title: 'カテゴリ' };\n", encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    first_report = await service.audit_active_runtime(game_title="テストゲーム")
    assert first_report.status == "ok"
    async with await registry.open_game("テストゲーム") as session:
        cached_records = await session.read_plugin_source_runtime_scan_cache()
    cached_by_name = {record.file_name: record for record in cached_records}
    assert "CacheSource.js" in cached_by_name
    assert cached_by_name["CacheSource.js"].literals
    cached_file_count = len(cached_records)
    assert first_report.summary["active_runtime_scan_cache_input_record_count"] == 0
    assert first_report.summary["active_runtime_scan_cache_current_file_count"] == cached_file_count
    assert first_report.summary["active_runtime_scan_cache_hit_file_count"] == 0
    assert first_report.summary["active_runtime_scan_cache_miss_file_count"] == cached_file_count
    assert first_report.summary["active_runtime_scan_cache_rescan_file_count"] == cached_file_count

    scan_calls: list[tuple[str, ...]] = []

    def counting_scan(
        *,
        files: dict[str, str],
        active_file_names: frozenset[str],
        text_rules: TextRules | None = None,
    ) -> PluginSourceBatchTextScan:
        """记录真正进入 AST 扫描的文件。"""
        scan_calls.append(tuple(sorted(files)))
        return real_scan_plugin_source_files_text_strict(
            files=files,
            active_file_names=active_file_names,
            text_rules=text_rules,
        )

    monkeypatch.setattr(
        "app.plugin_source_text.runtime_audit.scan_plugin_source_files_text_strict",
        counting_scan,
    )
    second_report = await service.audit_active_runtime(game_title="テストゲーム")
    assert second_report.status == "ok"
    assert second_report.summary["active_runtime_scan_cache_hit_file_count"] == cached_file_count
    assert second_report.summary["active_runtime_scan_cache_miss_file_count"] == 0
    assert second_report.summary["active_runtime_scan_cache_stale_file_count"] == 0
    assert second_report.summary["active_runtime_scan_cache_rescan_file_count"] == 0
    assert scan_calls == []

    _ = source_path.write_text("const Messages = { title: 'カテゴリ変更' };\n", encoding="utf-8")
    third_report = await service.audit_active_runtime(game_title="テストゲーム")
    assert third_report.status == "ok"
    assert third_report.summary["active_runtime_scan_cache_hit_file_count"] == cached_file_count - 1
    assert third_report.summary["active_runtime_scan_cache_stale_file_count"] == 1
    assert third_report.summary["active_runtime_scan_cache_rescan_file_count"] == 1
    assert scan_calls == [("CacheSource.js",)]


@pytest.mark.asyncio
async def test_diagnose_active_runtime_maps_plugin_source_issue_to_translation_cache(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """当前运行源码诊断必须用写回映射精确反推已保存译文记录。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BadSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    active_source = "const Messages = { line: '努力忍耐着的\\nn[0]君…真棒哦♥' };\n"
    origin_source = "const Messages = { line: '頑張ってガマンする\\\\nn[0]くん…素敵よ♥' };\n"
    bad_source_path = plugin_source_dir / "BadSource.js"
    _ = bad_source_path.write_text(origin_source, encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = bad_source_path.write_text(active_source, encoding="utf-8")
    origin_source_dir = minimal_game_dir / "js" / "plugins_source_origin"
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        source_game_data = await load_game_data(session.game_path)
        active_game_data = await load_active_runtime_game_data(session.game_path)
        runtime_source = active_game_data.plugin_source_files["BadSource.js"]
        source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=source_game_data),
            text_rules=text_rules,
        )
        source_file_scan = next(file_scan for file_scan in source_scan.files if file_scan.file_name == "BadSource.js")
        source_candidate = next(
            candidate for candidate in source_scan.candidates if candidate.file_name == "BadSource.js"
        )
        runtime_scan = scan_plugin_source_file_text_strict(
            file_name="BadSource.js",
            source=runtime_source,
            active=True,
        )
        runtime_literal = runtime_scan.literals[0]
        location_path = f"js/plugins/BadSource.js/{source_candidate.selector}"
        translation_item = TranslationItem(
            location_path=location_path,
            item_type="short_text",
            original_lines=[source_candidate.text],
            source_line_paths=[location_path],
            translation_lines=["努力忍耐着的\nn[0]君…真棒哦♥"],
        )
        await session.write_translation_items([translation_item])
        await session.replace_plugin_source_runtime_write_maps(
            [
                PluginSourceRuntimeWriteMapRecord(
                    location_path=location_path,
                    source_file_name="BadSource.js",
                    source_selector=source_candidate.selector,
                    source_file_hash=source_file_scan.file_hash,
                    source_text_hash=plugin_source_runtime_hash_text(source_candidate.text),
                    translation_lines_hash=plugin_source_runtime_hash_lines(translation_item.translation_lines),
                    runtime_file_name="BadSource.js",
                    runtime_selector=runtime_literal.selector,
                    runtime_file_hash=runtime_scan.file_hash,
                    runtime_text_hash=plugin_source_runtime_hash_text(runtime_literal.text),
                    runtime_line=runtime_literal.line,
                    created_at="2026-05-24T00:00:00",
                )
            ]
        )

    output_path = tmp_path / "diagnosis.json"
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    report = await service.diagnose_active_runtime(game_title="テストゲーム", output_path=output_path)

    assert report.status == "error"
    assert output_path.exists()
    assert report.summary["mapped_translate_count"] == 1
    diagnosis_items = ensure_json_array(report.details["active_runtime_diagnosis_items"], "diagnosis")
    diagnosis_item = next(
        item
        for item in diagnosis_items
        if ensure_json_object(
            ensure_json_object(item, "diagnosis_item")["issue"],
            "diagnosis_item.issue",
        )["file"]
        == "BadSource.js"
    )
    diagnosis = ensure_json_object(diagnosis_item, "diagnosis_item")
    assert diagnosis["diagnosis_status"] == "mapped_translate"
    assert diagnosis["location_path"] == location_path
    assert diagnosis["current_translation_lines"] == ["努力忍耐着的\nn[0]君…真棒哦♥"]
    assert "无法反推" not in json.dumps(diagnosis, ensure_ascii=False)

    async with await registry.open_game("テストゲーム") as session:
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path=location_path,
                    item_type="short_text",
                    original_lines=[source_candidate.text],
                    source_line_paths=[location_path],
                    translation_lines=["已经修复的译文记录"],
                )
            ]
        )
    cache_changed_report = await service.diagnose_active_runtime(
        game_title="テストゲーム",
        output_path=tmp_path / "diagnosis-cache-changed.json",
    )
    cache_changed_items = ensure_json_array(
        cache_changed_report.details["active_runtime_diagnosis_items"],
        "diagnosis",
    )
    cache_changed_item = next(
        ensure_json_object(item, "diagnosis_item")
        for item in cache_changed_items
        if ensure_json_object(
            ensure_json_object(item, "diagnosis_item")["issue"],
            "diagnosis_item.issue",
        )["file"]
        == "BadSource.js"
    )
    assert cache_changed_item["diagnosis_status"] == "mapped_translate"
    assert cache_changed_item["cache_hash_matches"] is False
    assert cache_changed_item["current_translation_lines"] == ["已经修复的译文记录"]

    _ = (origin_source_dir / "BadSource.js").write_text(
        "const Messages = { line: '源文件已变化' };\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="可信源快照 manifest"):
        _ = await service.diagnose_active_runtime(
            game_title="テストゲーム",
            output_path=tmp_path / "diagnosis-source-changed.json",
        )


@pytest.mark.asyncio
async def test_diagnose_active_runtime_batches_translation_source_scans(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前运行诊断反推翻译源时必须批量扫描源插件文件。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugin_names = ["BadSourceA", "BadSourceB"]
    for plugin_name in plugin_names:
        plugins.append({"name": plugin_name, "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    origin_sources = {
        "BadSourceA.js": "const Messages = { category: '原文A' };\n",
        "BadSourceB.js": "const Messages = { category: '原文B' };\n",
    }
    active_sources = {
        "BadSourceA.js": "const Messages = { category: 'カテゴリA' };\n",
        "BadSourceB.js": "const Messages = { category: 'カテゴリB' };\n",
    }
    for file_name, source in origin_sources.items():
        _ = (plugin_source_dir / file_name).write_text(source, encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    for file_name, source in active_sources.items():
        _ = (plugin_source_dir / file_name).write_text(source, encoding="utf-8")

    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        source_game_data = await load_game_data(session.game_path)
        active_game_data = await load_active_runtime_game_data(session.game_path)
        source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=source_game_data),
            text_rules=text_rules,
        )
        translation_items: list[TranslationItem] = []
        runtime_maps: list[PluginSourceRuntimeWriteMapRecord] = []
        for index, file_name in enumerate(sorted(origin_sources)):
            source_file_scan = next(file_scan for file_scan in source_scan.files if file_scan.file_name == file_name)
            source_candidate = next(
                candidate for candidate in source_scan.candidates if candidate.file_name == file_name
            )
            runtime_source = active_game_data.plugin_source_files[file_name]
            runtime_scan = scan_plugin_source_file_text_strict(
                file_name=file_name,
                source=runtime_source,
                active=True,
            )
            runtime_literal = runtime_scan.literals[0]
            location_path = f"js/plugins/{file_name}/{source_candidate.selector}"
            translation_item = TranslationItem(
                location_path=location_path,
                item_type="short_text",
                original_lines=[source_candidate.text],
                source_line_paths=[location_path],
                translation_lines=[runtime_literal.text],
            )
            translation_items.append(translation_item)
            runtime_maps.append(
                PluginSourceRuntimeWriteMapRecord(
                    location_path=location_path,
                    source_file_name=file_name,
                    source_selector=source_candidate.selector,
                    source_file_hash=source_file_scan.file_hash,
                    source_text_hash=plugin_source_runtime_hash_text(source_candidate.text),
                    translation_lines_hash=plugin_source_runtime_hash_lines(translation_item.translation_lines),
                    runtime_file_name=file_name,
                    runtime_selector=runtime_literal.selector,
                    runtime_file_hash=runtime_scan.file_hash,
                    runtime_text_hash=plugin_source_runtime_hash_text(runtime_literal.text),
                    runtime_line=runtime_literal.line,
                    created_at=f"2026-05-24T00:00:0{index}",
                )
            )
        await session.write_translation_items(translation_items)
        await session.replace_plugin_source_runtime_write_maps(runtime_maps)

    from app.plugin_source_text.scanner import scan_plugin_source_files_text_strict as real_batch_scan

    batch_calls: list[tuple[str, ...]] = []

    def counting_source_batch_scan(
        *,
        files: dict[str, str],
        active_file_names: frozenset[str],
        text_rules: TextRules | None,
    ) -> PluginSourceBatchTextScan:
        """记录诊断反推阶段扫描过的翻译源插件文件。"""
        batch_calls.append(tuple(sorted(files)))
        return real_batch_scan(
            files=files,
            active_file_names=active_file_names,
            text_rules=text_rules,
        )

    def forbidden_single_ast_hash_scan(source: str) -> object:
        _ = source
        raise AssertionError("诊断不应为 runtime/source map 哈希逐条重跑单文件 AST")

    monkeypatch.setattr(
        "app.agent_toolkit.services.quality.scan_plugin_source_files_text_strict",
        counting_source_batch_scan,
    )
    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans",
        forbidden_single_ast_hash_scan,
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    report = await service.diagnose_active_runtime(game_title="テストゲーム")

    assert report.status == "error"
    assert report.summary["mapped_translate_count"] == 2
    assert batch_calls == [("BadSourceA.js", "BadSourceB.js")]


@pytest.mark.asyncio
async def test_diagnose_active_runtime_default_mode_never_guesses_without_runtime_map(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """默认模式没有写回映射时，不把源码字符串猜成漏翻诊断。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BadSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    active_source = "const Messages = { line: '努力忍耐着的\\nn[0]君…真棒哦♥' };\n"
    bad_source_path = plugin_source_dir / "BadSource.js"
    _ = bad_source_path.write_text(
        "const Messages = { line: '頑張ってガマンする\\\\nn[0]くん…素敵よ♥' };\n",
        encoding="utf-8",
    )

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = bad_source_path.write_text(active_source, encoding="utf-8")
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        source_game_data = await load_game_data(session.game_path)
        source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=source_game_data),
            text_rules=text_rules,
        )
        source_candidate = next(
            candidate for candidate in source_scan.candidates if candidate.file_name == "BadSource.js"
        )
        location_path = f"js/plugins/BadSource.js/{source_candidate.selector}"
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path=location_path,
                    item_type="short_text",
                    original_lines=[source_candidate.text],
                    source_line_paths=[location_path],
                    translation_lines=["努力忍耐着的\nn[0]君…真棒哦♥"],
                )
            ]
        )

    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    report = await service.diagnose_active_runtime(game_title="テストゲーム", output_path=tmp_path / "diagnosis.json")

    assert report.status == "ok"
    assert report.summary["active_runtime_text_issue_audit_enabled"] is False
    assert report.summary["runtime_mapping_missing_count"] == 0
    diagnosis_items = ensure_json_array(report.details["active_runtime_diagnosis_items"], "diagnosis")
    assert diagnosis_items == []


@pytest.mark.asyncio
async def test_diagnose_active_runtime_default_mode_skips_unmapped_source_residual(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """未启动插件源码支线时，当前运行源码残留不是补译诊断。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BadSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    active_source = "const Messages = { line: '未審査テキスト' };\n"
    _ = (plugin_source_dir / "BadSource.js").write_text(active_source, encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    report = await service.diagnose_active_runtime(game_title="テストゲーム")

    assert report.status == "ok"
    assert report.summary["active_runtime_text_issue_audit_enabled"] is False
    assert report.summary["runtime_mapping_missing_count"] == 0
    diagnosis_items = ensure_json_array(report.details["active_runtime_diagnosis_items"], "diagnosis")
    assert diagnosis_items == []


@pytest.mark.asyncio
async def test_diagnose_active_runtime_does_not_ignore_excluded_runtime_residual_without_map(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """插件源码规则排除 selector 不会让当前运行源文残留从诊断里消失。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BadSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    active_source = "const Messages = { category: 'カテゴリ' };\n"
    _ = (plugin_source_dir / "BadSource.js").write_text(active_source, encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        source_game_data = await load_game_data(session.game_path)
        source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=source_game_data),
            text_rules=text_rules,
        )
        source_file_scan = next(file_scan for file_scan in source_scan.files if file_scan.file_name == "BadSource.js")
        source_candidate = next(
            candidate for candidate in source_scan.candidates if candidate.file_name == "BadSource.js"
        )
        await session.replace_plugin_source_text_rules(
            [
                PluginSourceTextRuleRecord(
                    file_name="BadSource.js",
                    file_hash=source_file_scan.file_hash,
                    selectors=[],
                    excluded_selectors=[source_candidate.selector],
                )
            ]
        )

    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    report = await service.diagnose_active_runtime(game_title="テストゲーム")

    assert report.status == "error"
    assert report.summary["active_runtime_source_residual_count"] == 1
    assert report.summary["runtime_mapping_missing_count"] == 1
    diagnosis_items = ensure_json_array(report.details["active_runtime_diagnosis_items"], "diagnosis")
    assert len(diagnosis_items) == 1


@pytest.mark.asyncio
async def test_active_runtime_audit_ignores_excluded_residual_with_exact_runtime_map(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """已审查排除 selector 有精确 runtime map 时，不再当作插件源码漏翻。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "BadSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    active_source = "const Messages = { category: 'カテゴリ' };\n"
    _ = (plugin_source_dir / "BadSource.js").write_text(active_source, encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        source_game_data = await load_game_data(session.game_path)
        active_game_data = await load_active_runtime_game_data(session.game_path)
        source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=source_game_data),
            text_rules=text_rules,
        )
        source_file_scan = next(file_scan for file_scan in source_scan.files if file_scan.file_name == "BadSource.js")
        source_candidate = next(
            candidate for candidate in source_scan.candidates if candidate.file_name == "BadSource.js"
        )
        runtime_source = active_game_data.plugin_source_files["BadSource.js"]
        runtime_scan = scan_plugin_source_file_text_strict(
            file_name="BadSource.js",
            source=runtime_source,
            active=True,
        )
        runtime_literal = runtime_scan.literals[0]
        await session.replace_plugin_source_text_rules(
            [
                PluginSourceTextRuleRecord(
                    file_name="BadSource.js",
                    file_hash=source_file_scan.file_hash,
                    selectors=[],
                    excluded_selectors=[source_candidate.selector],
                )
            ]
        )
        await session.replace_plugin_source_runtime_write_maps(
            [
                PluginSourceRuntimeWriteMapRecord(
                    mapping_kind="excluded",
                    location_path=f"js/plugins/BadSource.js/{source_candidate.selector}",
                    source_file_name="BadSource.js",
                    source_selector=source_candidate.selector,
                    source_file_hash=source_file_scan.file_hash,
                    source_text_hash=plugin_source_runtime_hash_text(source_candidate.text),
                    translation_lines_hash=plugin_source_runtime_hash_lines([]),
                    runtime_file_name="BadSource.js",
                    runtime_selector=runtime_literal.selector,
                    runtime_file_hash=runtime_scan.file_hash,
                    runtime_text_hash=plugin_source_runtime_hash_text(runtime_literal.text),
                    runtime_line=runtime_literal.line,
                    created_at="2026-05-24T00:00:00",
                )
            ]
        )

    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    report = await service.audit_active_runtime(game_title="テストゲーム")
    diagnosis = await service.diagnose_active_runtime(game_title="テストゲーム")

    assert report.status == "ok"
    assert report.summary["active_runtime_text_issue_audit_enabled"] is True
    assert report.summary["active_runtime_source_residual_count"] == 0
    assert diagnosis.status == "ok"
    assert diagnosis.summary["diagnosis_issue_count"] == 0


@pytest.mark.asyncio
async def test_import_placeholder_rules_runs_validation_before_save(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """占位符规则导入不能绕过可翻译内容损失校验。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.import_placeholder_rules(
        game_title="テストゲーム",
        rules_text=json.dumps({r"こんにちは": "[CUSTOM_SWALLOW_{index}]"}, ensure_ascii=False),
    )

    assert report.status == "error"
    assert "placeholder_rule_loses_translatable_text" in {error.code for error in report.errors}
    async with await registry.open_game("テストゲーム") as session:
        records = await session.read_placeholder_rules()
    assert records == []


@pytest.mark.asyncio
async def test_import_placeholder_rules_preserves_existing_records_when_coverage_scan_fails(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通占位符覆盖扫描报错时必须显式失败，且不能替换已有规则。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    existing_record = PlaceholderRuleRecord(
        pattern_text=r"\\KEEP\[[^\]]+\]",
        placeholder_template="[CUSTOM_KEEP_{index}]",
    )
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_placeholder_rules([existing_record])

    async def failed_coverage_scan(
        self: AgentToolkitService,
        *,
        game_title: str,
        custom_placeholder_rules_text: str | None,
    ) -> AgentReport:
        _ = (self, game_title, custom_placeholder_rules_text)
        return AgentReport.from_parts(
            errors=[issue("placeholder_scan_failed", "占位符覆盖扫描失败")],
            warnings=[],
            summary={"uncovered_count": 0},
            details={},
        )

    monkeypatch.setattr(AgentToolkitService, "scan_placeholder_candidates", failed_coverage_scan)
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.import_placeholder_rules(
        game_title="テストゲーム",
        rules_text=json.dumps({r"\\NEW\[[^\]]+\]": "[CUSTOM_NEW_{index}]"}),
    )

    async with await registry.open_game("テストゲーム") as session:
        records = await session.read_placeholder_rules()
    assert report.status == "error"
    assert [error.code for error in report.errors] == ["placeholder_scan_failed"]
    assert report.summary["imported_rule_count"] == 0
    assert records == [existing_record]


@pytest.mark.asyncio
async def test_import_structured_placeholder_rules_preserves_existing_records_when_coverage_scan_fails(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构化覆盖扫描报错时不能把已保存规则替换成空规则。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    existing_record = StructuredPlaceholderRuleRecord(
        rule_name="KEEP_SHELL",
        rule_type="paired_shell",
        pattern_text=r"(?P<open><Keep:)(?P<text>[^>]+)(?P<close>>)",
        translatable_group="text",
        protected_groups={
            "open": "[CUSTOM_KEEP_OPEN_{index}]",
            "close": "[CUSTOM_KEEP_CLOSE_{index}]",
        },
    )
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_structured_placeholder_rules([existing_record])

    async def failed_coverage_scan(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        _ = (self, game_title, rules_text)
        return AgentReport.from_parts(
            errors=[issue("structured_placeholder_scan_failed", "结构化占位符覆盖扫描失败")],
            warnings=[],
            summary={"uncovered_count": 0},
            details={},
        )

    monkeypatch.setattr(AgentToolkitService, "scan_structured_placeholder_candidates", failed_coverage_scan)
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.import_structured_placeholder_rules(
        game_title="テストゲーム",
        rules_text='{"paired_shell_rules": []}',
        confirm_empty=True,
    )

    async with await registry.open_game("テストゲーム") as session:
        records = await session.read_structured_placeholder_rules()
    assert report.status == "error"
    assert [error.code for error in report.errors] == ["structured_placeholder_scan_failed"]
    assert report.summary["imported_rule_count"] == 0
    assert records == [existing_record]


@pytest.mark.asyncio
async def test_import_structured_placeholder_rules_saves_separate_records(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """结构化占位符规则单独保存，不混入普通正则占位符规则表。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="en")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    rules_text = json.dumps(
        {
            "paired_shell_rules": [
                {
                    "name": "MINI_LABEL",
                    "pattern": r"(?P<open><Mini\s+Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)",
                    "translatable_group": "text",
                    "protected_groups": {
                        "open": "[CUSTOM_MINI_LABEL_OPEN_{index}]",
                        "close": "[CUSTOM_MINI_LABEL_CLOSE_{index}]",
                    },
                }
            ]
        },
        ensure_ascii=False,
    )

    validate_report = await service.validate_structured_placeholder_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
        sample_texts=["<Mini Label: Alraune>"],
    )
    import_report = await service.import_structured_placeholder_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )

    async with await registry.open_game("テストゲーム") as session:
        placeholder_records = await session.read_placeholder_rules()
        structured_records = await session.read_structured_placeholder_rules()

    assert validate_report.status == "ok"
    assert import_report.status in {"ok", "warning"}
    assert placeholder_records == []
    assert structured_records == [
        StructuredPlaceholderRuleRecord(
            rule_name="MINI_LABEL",
            rule_type="paired_shell",
            pattern_text=r"(?P<open><Mini\s+Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)",
            translatable_group="text",
            protected_groups={
                "open": "[CUSTOM_MINI_LABEL_OPEN_{index}]",
                "close": "[CUSTOM_MINI_LABEL_CLOSE_{index}]",
            },
        )
    ]


@pytest.mark.asyncio
async def test_structured_placeholder_candidate_report_keeps_all_details_for_output(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务报告不得提前截断候选，stdout 的样例限制由 CLI 展示层负责。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    candidates: JsonArray = [
        {
            "location_path": f"data/CommonEvents.json/{index}",
            "line_number": 1,
            "candidate": f"<Label:{index}>",
            "covered": False,
            "matching_rules": [],
        }
        for index in range(125)
    ]

    def fake_collect_candidate_details(**kwargs: object) -> JsonArray:
        _ = kwargs
        return candidates

    monkeypatch.setattr(
        "app.agent_toolkit.services.placeholder_rules._collect_structured_placeholder_candidate_details",
        fake_collect_candidate_details,
    )

    report = await service.scan_structured_placeholder_candidates(
        game_title="テストゲーム",
        rules_text='{"paired_shell_rules": []}',
    )

    details = ensure_json_array(report.details["candidates"], "details.candidates")
    assert report.summary["candidate_count"] == 125
    assert len(details) == 125


@pytest.mark.asyncio
async def test_validate_structured_rules_uses_baseline_before_candidate_application(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """候选规则把原文藏进保护外壳时，基线抽取仍必须发现并拒绝它。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), "CommonEvents.json")
    event = ensure_json_object(common_events[1], "CommonEvents.json[1]")
    commands = ensure_json_array(event["list"], "CommonEvents.json[1].list")
    commands.insert(
        1,
        {
            "code": 401,
            "parameters": ["<Mini Label: アメリ!>"],
        },
    )
    _ = common_events_path.write_text(
        json.dumps(common_events, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    rules_text = json.dumps(
        {
            "paired_shell_rules": [
                {
                    "name": "HIDDEN_SOURCE",
                    "pattern": (
                        r"(?P<open><Mini Label: アメリ)"
                        r"(?P<text>!)"
                        r"(?P<close>>)"
                    ),
                    "translatable_group": "text",
                    "protected_groups": {
                        "open": "[CUSTOM_HIDDEN_SOURCE_OPEN_{index}]",
                        "close": "[CUSTOM_HIDDEN_SOURCE_CLOSE_{index}]",
                    },
                }
            ]
        },
        ensure_ascii=False,
    )

    report = await service.validate_structured_placeholder_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
        sample_texts=[],
    )

    assert report.summary["sample_count"] == 1
    assert "structured_placeholder_loses_translatable_text" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_english_profile_exports_visible_pending_text_without_protocol_noise(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """英文档案能提取玩家可见英文，并跳过资源路径、公式和布尔值。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-english.json"
    workspace_path = tmp_path / "workspace"

    async with await registry.open_game("English Fixture Game") as session:
        game_data = await load_game_data(session.game_path)
        await session.replace_plugin_text_rules(
            [
                PluginTextRuleRecord(
                    plugin_index=0,
                    plugin_name="VisiblePlugin",
                    plugin_hash=build_plugin_hash(game_data.plugins_js[0]),
                    path_templates=[
                        "$['parameters']['Message']",
                        "$['parameters']['Title']",
                        "$['parameters']['Image']",
                        "$['parameters']['Formula']",
                        "$['parameters']['Enabled']",
                    ],
                )
            ]
        )

    workspace_report = await service.prepare_agent_workspace(
        game_title="English Fixture Game",
        output_dir=workspace_path,
        command_codes=None,
    )
    export_report = await service.export_pending_translations(
        game_title="English Fixture Game",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    exported_lines: list[str] = []
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        exported_lines.extend(
            line
            for line in ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
            if isinstance(line, str)
        )

    assert workspace_report.status in {"ok", "warning"}
    assert workspace_report.summary["source_language"] == "en"
    assert export_report.status == "ok"
    assert "Are you really going in there?" in exported_lines
    assert "Open the door" in exported_lines
    assert "Welcome to the old gate." in exported_lines
    assert "Gate Menu" in exported_lines
    assert "img/pictures/Gate.png" not in exported_lines
    assert "a.hpRate() >= 0.5" not in exported_lines
    assert "true" not in exported_lines


@pytest.mark.asyncio
async def test_scan_placeholder_candidates_marks_custom_rule_coverage(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """扫描命令能区分内置控制符、未覆盖自定义控制符和 CLI 覆盖规则。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    uncovered_report = await service.scan_placeholder_candidates(
        game_title="テストゲーム",
        custom_placeholder_rules_text="{}",
    )
    covered_report = await service.scan_placeholder_candidates(
        game_title="テストゲーム",
        custom_placeholder_rules_text='{"\\\\\\\\F\\\\[[^\\\\]]+\\\\]":"[CUSTOM_FACE_PORTRAIT_{index}]"}',
    )

    assert uncovered_report.summary["uncovered_count"] != 0
    assert covered_report.summary["uncovered_count"] == 0
    raw_json = covered_report.to_json_text()
    assert r"\F[GuideA]" in raw_json
    assert "テスト一行目です" not in raw_json


@pytest.mark.asyncio
async def test_placeholder_candidate_rule_cannot_hide_entire_source_item(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """候选规则不能先吞掉整条源文，再让覆盖扫描误报为没有候选。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_value = coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8"))))
    common_events = ensure_json_array(raw_value, "CommonEvents.json")
    common_events.append(
        {
            "id": 999,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2, ""]},
                {"code": 401, "parameters": [r"\Hide[秘密]"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _ = common_events_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    rules_text = json.dumps(
        {r"\\Hide\[[^\]\r\n]+\]": "[CUSTOM_HIDE_MARKER_{index}]"},
        ensure_ascii=False,
    )

    coverage_report = await service.scan_placeholder_candidates(
        game_title="テストゲーム",
        custom_placeholder_rules_text=rules_text,
    )
    validation_report = await service.validate_placeholder_rules(
        game_title="テストゲーム",
        custom_placeholder_rules_text=rules_text,
        sample_texts=[],
    )

    assert r"\Hide[秘密]" in coverage_report.to_json_text()
    assert "placeholder_rule_loses_translatable_text" in {error.code for error in validation_report.errors}


def test_placeholder_candidate_scan_requires_full_span_coverage() -> None:
    """覆盖扫描不能把标准短前缀当成长候选已覆盖。"""
    translation_data_map = {
        "Map001.json": TranslationData(
            display_name=None,
            translation_items=[
                TranslationItem(
                    location_path="Map001.json/1/0",
                    item_type="long_text",
                    original_lines=[r"\nn[Name]こんにちは"],
                )
            ],
        )
    }

    uncovered_candidates = scan_placeholder_candidate_spans(
        translation_data_map,
        TextRules.from_setting(TextRulesSetting()),
    )
    covered_candidates = scan_placeholder_candidate_spans(
        translation_data_map,
        TextRules.from_setting(
            TextRulesSetting(),
            custom_placeholder_rules=(
                CustomPlaceholderRule.create(
                    r"\\nn\[[^\]\r\n]+\]",
                    "[CUSTOM_PLUGIN_NAME_{index}]",
                ),
            ),
        ),
    )

    assert count_uncovered_candidates(uncovered_candidates) == 1
    assert uncovered_candidates[0].marker == r"\nn[Name]"
    assert uncovered_candidates[0].standard_covered is False
    assert count_uncovered_candidates(covered_candidates) == 0
    assert covered_candidates[0].marker == r"\nn[Name]"
    assert covered_candidates[0].custom_covered is True


def test_placeholder_candidate_scan_rejects_custom_prefix_overlap() -> None:
    """普通规则只覆盖候选前缀时仍必须保留未覆盖 occurrence。"""
    translation_data_map = {
        "Map001.json": TranslationData(
            display_name=None,
            translation_items=[
                TranslationItem(
                    location_path="Map001.json/1/0",
                    item_type="long_text",
                    original_lines=[r"\X[1]こんにちは"],
                )
            ],
        )
    }
    prefix_rules = TextRules.from_setting(
        TextRulesSetting(),
        custom_placeholder_rules=(
            CustomPlaceholderRule.create(
                r"\\X",
                "[CUSTOM_X_PREFIX_{index}]",
            ),
        ),
    )
    complete_rules = TextRules.from_setting(
        TextRulesSetting(),
        custom_placeholder_rules=(
            CustomPlaceholderRule.create(
                r"\\X\[[^\]\r\n]+\]",
                "[CUSTOM_X_MARKER_{index}]",
            ),
        ),
    )

    prefix_candidates = scan_placeholder_candidate_spans(translation_data_map, prefix_rules)
    complete_candidates = scan_placeholder_candidate_spans(translation_data_map, complete_rules)

    assert count_uncovered_candidates(prefix_candidates) == 1
    assert prefix_candidates[0].marker == r"\X[1]"
    assert prefix_candidates[0].uncovered_count == 1
    assert prefix_candidates[0].occurrences[0].coverage_kind == "uncovered"
    assert prefix_candidates[0].occurrences[0].matched_rule_ids == ()
    assert count_uncovered_candidates(complete_candidates) == 0
    assert complete_candidates[0].marker == r"\X[1]"
    assert complete_candidates[0].custom_covered is True
    assert complete_candidates[0].occurrences[0].coverage_kind == "custom"


def test_placeholder_candidate_scan_accepts_custom_span_wrapping_candidate() -> None:
    """自定义规则包住内部标准形态候选时，扫描门禁应认定已覆盖。"""
    translation_data_map = {
        "Items.json": TranslationData(
            display_name=None,
            translation_items=[
                TranslationItem(
                    location_path="Items.json/293/note/SG説明",
                    item_type="short_text",
                    original_lines=[r"\\v[104] / 5"],
                )
            ],
        )
    }
    text_rules = TextRules.from_setting(
        TextRulesSetting(),
        custom_placeholder_rules=(
            CustomPlaceholderRule.create(
                r"\\\\v\[[0-9]+\]",
                "[CUSTOM_ESCAPED_VARIABLE_{index}]",
            ),
        ),
    )

    candidates = scan_placeholder_candidate_spans(translation_data_map, text_rules)

    assert count_uncovered_candidates(candidates) == 0
    assert candidates[0].marker == r"\\v[104]"
    assert candidates[0].custom_covered is True


def test_placeholder_candidate_scan_counts_mv_nw_structured_shell_as_covered() -> None:
    """字面 NW 外壳的往返已由结构化规则证明时，普通扫描不得再阻断。"""
    translation_data_map = {
        "CommonEvents.json": TranslationData(
            display_name=None,
            translation_items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    original_lines=[
                        r"\NW[神父]",
                        r"\NW[\N[1]]",
                        r"\NW[神父]\SV[A0001]",
                    ],
                )
            ],
        )
    }
    custom_rules = (
        CustomPlaceholderRule.create(
            r"\\NW\[\\N\[\d+\]\]",
            "[CUSTOM_DYNAMIC_NW_MARKER_{index}]",
        ),
        CustomPlaceholderRule.create(
            r"\\SV\[[^\]\r\n]+\]",
            "[CUSTOM_PLUGIN_SV_MARKER_{index}]",
        ),
    )
    structured_rule = StructuredPlaceholderRule.create(
        rule_name="MV_NW",
        rule_type="paired_shell",
        pattern_text=r"(?P<open>\\NW\[)(?P<text>[^\\\]\r\n]+?)(?P<close>\])",
        translatable_group="text",
        protected_groups={
            "open": "[CUSTOM_MV_NW_OPEN_{index}]",
            "close": "[CUSTOM_MV_NW_CLOSE_{index}]",
        },
    )
    text_rules = TextRules.from_setting(
        TextRulesSetting(),
        custom_placeholder_rules=custom_rules,
        structured_placeholder_rules=(structured_rule,),
    )

    candidates = scan_placeholder_candidate_spans(translation_data_map, text_rules)
    details = placeholder_candidates_to_details(candidates)

    assert count_uncovered_candidates(candidates) == 0
    literal_candidate = next(candidate for candidate in candidates if candidate.marker == r"\NW[神父]")
    assert literal_candidate.structured_covered is True
    assert literal_candidate.uncovered_count == 0
    literal_detail = next(
        ensure_json_object(detail, "candidate")
        for detail in details
        if ensure_json_object(detail, "candidate")["marker"] == r"\NW[神父]"
    )
    assert literal_detail["matched_rule_ids"] == ["MV_NW"]
    assert literal_detail["source_count"] == 2


def test_placeholder_candidate_aggregation_never_hides_uncovered_occurrence() -> None:
    """同一 marker 只要有一处未覆盖，聚合报告仍必须显式失败。"""
    translation_data_map = {
        "CommonEvents.json": TranslationData(
            display_name=None,
            translation_items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    original_lines=[r"ALLOW:\X[1]", r"DENY:\X[1]"],
                )
            ],
        )
    }
    text_rules = TextRules.from_setting(
        TextRulesSetting(),
        structured_placeholder_rules=(
            StructuredPlaceholderRule.create(
                rule_name="SAFE_X",
                rule_type="paired_shell",
                pattern_text=r"(?<=ALLOW:)(?P<open>\\X\[)(?P<text>1)(?P<close>\])",
                translatable_group="text",
                protected_groups={
                    "open": "[CUSTOM_SAFE_X_OPEN_{index}]",
                    "close": "[CUSTOM_SAFE_X_CLOSE_{index}]",
                },
            ),
        ),
    )

    candidates = scan_placeholder_candidate_spans(translation_data_map, text_rules)
    candidate = next(candidate for candidate in candidates if candidate.marker == r"\X[1]")
    detail = ensure_json_object(placeholder_candidates_to_details([candidate])[0], "candidate")

    assert count_uncovered_candidates(candidates) == 1
    assert candidate.count == 2
    assert candidate.structured_covered is True
    assert candidate.uncovered_count == 1
    assert candidate.covered is False
    assert detail["coverage_conflict"] is True
    occurrences = ensure_json_array(detail["occurrences"], "candidate.occurrences")
    coverage_kinds: list[str] = []
    for occurrence in occurrences:
        coverage_kind = ensure_json_object(occurrence, "occurrence")["coverage_kind"]
        assert isinstance(coverage_kind, str)
        coverage_kinds.append(coverage_kind)
    assert set(coverage_kinds) == {"structured", "uncovered"}


def test_structured_inner_match_does_not_cover_overlapping_outer_candidate() -> None:
    """只覆盖外层候选内部片段的结构化命中，不能证明完整候选已覆盖。"""
    text = r"\OUT[\NW[神父]]"
    text_rules = TextRules.from_setting(
        TextRulesSetting(),
        structured_placeholder_rules=(
            StructuredPlaceholderRule.create(
                rule_name="INNER_NW",
                rule_type="paired_shell",
                pattern_text=r"(?P<open>\\NW\[)(?P<text>[^\]\r\n]+)(?P<close>\])",
                translatable_group="text",
                protected_groups={
                    "open": "[CUSTOM_INNER_NW_OPEN_{index}]",
                    "close": "[CUSTOM_INNER_NW_CLOSE_{index}]",
                },
            ),
        ),
    )

    coverages = text_rules.iter_control_sequence_candidate_coverages(text)

    assert len(coverages) == 1
    assert coverages[0].candidate.original.startswith(r"\OUT[")
    assert coverages[0].coverage_kind == "uncovered"


@pytest.mark.asyncio
async def test_build_placeholder_rules_groups_similar_candidates(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """规则草稿会把同类自定义控制符合并成少量通用正则。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    await _install_minimal_external_text_rules(
        registry=registry,
        game_title="テストゲーム",
        game_dir=minimal_game_dir,
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    output_path = tmp_path / "placeholder-rules.json"

    report = await service.build_placeholder_rules(game_title="テストゲーム", output_path=output_path)

    assert report.status == "ok"
    rules = load_json_object(output_path)
    uncovered_before = report.summary["uncovered_count_before_draft"]
    assert rules == {r"(?i)\\F\d*\[[^\]\r\n]+\]": "[CUSTOM_FACE_PORTRAIT_{index}]"}
    assert report.summary["draft_rule_count"] == 1
    assert isinstance(uncovered_before, int)
    assert uncovered_before > 0
    assert report.summary["uncovered_count_after_draft_preview"] == 0


@pytest.mark.asyncio
async def test_build_placeholder_rules_keeps_bare_uppercase_marker_case_sensitive(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """裸大写自定义标记草稿不能忽略大小写误匹配字面量换行。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_value = coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8"))))
    common_events = ensure_json_array(raw_value, "CommonEvents.json")
    event = ensure_json_object(common_events[1], "CommonEvents.json[1]")
    commands = ensure_json_array(event["list"], "CommonEvents.json[1].list")
    text_command = ensure_json_object(commands[1], "CommonEvents.json[1].list[1]")
    parameters = ensure_json_array(text_command["parameters"], "CommonEvents.json[1].list[1].parameters")
    parameters[0] = r"\N<案内人>こんにちは"
    _ = common_events_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    await _install_minimal_external_text_rules(
        registry=registry,
        game_title="テストゲーム",
        game_dir=minimal_game_dir,
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    output_path = tmp_path / "placeholder-rules.json"

    report = await service.build_placeholder_rules(game_title="テストゲーム", output_path=output_path)

    assert report.status == "ok"
    rules = load_json_object(output_path)
    assert rules[r"\\N\d*(?![A-Za-z\[])"] == "[CUSTOM_PLUGIN_N_MARKER_{index}]"
    assert r"(?i)\\N\d*(?![A-Za-z\[])" not in rules


@pytest.mark.asyncio
async def test_build_placeholder_rules_requires_manual_boundary_for_joined_control_text(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """英文正文紧贴无参数控制符时，草稿不自动猜测短前缀。"""
    common_events_path = minimal_english_game_dir / "data" / "CommonEvents.json"
    raw_value = coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8"))))
    common_events = ensure_json_array(raw_value, "CommonEvents.json")
    event = ensure_json_object(common_events[1], "CommonEvents.json[1]")
    commands = ensure_json_array(event["list"], "CommonEvents.json[1].list")
    commands[1:1] = [
        {"code": 401, "parameters": [r"\ShakeStop this!!!"]},
        {"code": 401, "parameters": [r"\ShakeNo, NO!!!"]},
        {"code": 401, "parameters": [r"\ShakeAhhh..."]},
        {"code": 401, "parameters": [r"\FXStop this!!!"]},
        {"code": 401, "parameters": [r"\ScreenShake"]},
        {"code": 401, "parameters": [r"\ScreenFlash"]},
    ]
    _ = common_events_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    await _install_minimal_external_text_rules(
        registry=registry,
        game_title="English Fixture Game",
        game_dir=minimal_english_game_dir,
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    output_path = tmp_path / "placeholder-rules.json"

    report = await service.build_placeholder_rules(game_title="English Fixture Game", output_path=output_path)
    rules = load_json_object(output_path)
    warning_codes = {warning.code for warning in report.warnings}
    manual_coverage_report = await service.scan_placeholder_candidates(
        game_title="English Fixture Game",
        custom_placeholder_rules_text=json.dumps(
            {r"\\Shake": "[CUSTOM_PLUGIN_SHAKE_MARKER_{index}]"},
            ensure_ascii=False,
        ),
    )
    manual_coverage_json = manual_coverage_report.to_json_text()

    assert report.status == "warning"
    assert rules == {}
    assert report.summary["manual_boundary_candidate_count"] == 6
    assert report.summary["uncovered_count_after_draft_preview"] == report.summary["uncovered_count_before_draft"]
    assert "placeholder_boundary_needs_review" in warning_codes
    assert r"\Screen" not in json.dumps(rules, ensure_ascii=False)
    assert r"\FXStop" not in json.dumps(rules, ensure_ascii=False)
    assert manual_coverage_report.summary["uncovered_count"] == 6
    assert r"\ShakeStop" in manual_coverage_json
    assert '"coverage_kind": "uncovered"' in manual_coverage_json
    assert r"\Shake" in manual_coverage_json


@pytest.mark.asyncio
async def test_placeholder_rule_draft_requires_external_rules_and_uses_active_sources(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """占位符草稿必须等外部规则完成后再基于完整文本集合生成。"""
    game_data = await load_game_data(minimal_game_dir)
    plugin_parameters = ensure_json_object(game_data.plugins_js[0]["parameters"], "plugins[0].parameters")
    plugin_parameters["Message"] = r"\PX[PluginFace]プラグイン本文"
    plugins_text = f"var $plugins = {json.dumps(game_data.plugins_js, ensure_ascii=False, indent=2)};\n"
    _ = (minimal_game_dir / "js" / "plugins.js").write_text(plugins_text, encoding="utf-8")

    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), "CommonEvents.json")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    command = ensure_json_object(commands[4], "CommonEvents[1].list[4]")
    parameters = ensure_json_array(command["parameters"], "CommonEvents[1].list[4].parameters")
    payload = ensure_json_object(parameters[3], "CommonEvents[1].list[4].parameters[3]")
    payload["message"] = r"\EV[CommandFace]プラグイン台詞"
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False, indent=2), encoding="utf-8")

    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = cast(object, json.loads(items_path.read_text(encoding="utf-8")))
    items = ensure_json_array(coerce_json_value(raw_items), "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = r"<拡張説明:\NT[NoteFace]薬草の詳細説明>"
    _ = items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    before_rules_path = tmp_path / "before-placeholder-rules.json"
    after_rules_path = tmp_path / "after-placeholder-rules.json"

    blocked_report = await service.build_placeholder_rules(game_title="テストゲーム", output_path=before_rules_path)

    fresh_game_data = await load_game_data(minimal_game_dir)
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_plugin_text_rules(
            [
                PluginTextRuleRecord(
                    plugin_index=0,
                    plugin_name="TestPlugin",
                    plugin_hash=build_plugin_hash(fresh_game_data.plugins_js[0]),
                    path_templates=["$['parameters']['Message']"],
                )
            ]
        )
        await session.replace_event_command_text_rules(
            [
                EventCommandTextRuleRecord(
                    command_code=357,
                    parameter_filters=[
                        EventCommandParameterFilter(index=0, value="TestPlugin"),
                        EventCommandParameterFilter(index=1, value="Show"),
                    ],
                    path_templates=["$['parameters'][3]['message']"],
                )
            ]
        )
        await session.replace_note_tag_text_rules(
            [NoteTagTextRuleRecord(file_name="Items.json", tag_names=["拡張説明"])]
        )

    report = await service.build_placeholder_rules(game_title="テストゲーム", output_path=after_rules_path)
    after_rules = load_json_object(after_rules_path)
    draft_rule_count = report.summary["draft_rule_count"]

    assert blocked_report.status == "error"
    assert {error.code for error in blocked_report.errors} >= {
        "plugin_text_missing",
        "event_command_text_missing",
        "note_tag_text_missing",
    }
    assert not before_rules_path.exists()
    assert isinstance(draft_rule_count, int)
    assert draft_rule_count >= 4
    assert any(r"\\PX" in pattern for pattern in after_rules)
    assert any(r"\\EV" in pattern for pattern in after_rules)
    assert any(r"\\NT" in pattern for pattern in after_rules)


@pytest.mark.asyncio
async def test_prepare_agent_workspace_includes_placeholder_rule_draft(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """Agent 工作区会携带占位符和 Note 标签规则草稿，避免重复手写解析脚本。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    report = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "ok"
    rules_path = workspace / "placeholder-rules.json"
    structured_rules_path = workspace / "structured-placeholder-rules.json"
    note_candidates_path = workspace / "note-tag-candidates.json"
    note_rules_path = workspace / "note-tag-rules.json"
    plugin_json_string_candidates_path = workspace / "plugin-json-string-leaf-candidates.json"
    field_terms_path = workspace / "terminology" / "field-terms.json"
    glossary_path = workspace / "terminology" / "glossary.json"
    speaker_source_path = workspace / "terminology" / "subtasks" / "sources" / "speaker_and_actor_terms.json"
    speaker_candidate_path = workspace / "terminology" / "subtasks" / "candidates" / "speaker_and_actor_terms.json"
    item_source_path = workspace / "terminology" / "subtasks" / "sources" / "item_terms.json"
    item_candidate_path = workspace / "terminology" / "subtasks" / "candidates" / "item_terms.json"
    manifest_path = workspace / "manifest.json"
    assert rules_path.exists()
    assert structured_rules_path.exists()
    assert note_candidates_path.exists()
    assert note_rules_path.exists()
    assert plugin_json_string_candidates_path.exists()
    assert field_terms_path.exists()
    assert glossary_path.exists()
    assert speaker_source_path.exists()
    assert speaker_candidate_path.exists()
    assert item_source_path.exists()
    assert item_candidate_path.exists()
    assert manifest_path.exists()
    rules = load_json_object(rules_path)
    structured_rules = load_json_object(structured_rules_path)
    plugin_json_string_candidates = load_json_array(plugin_json_string_candidates_path)
    note_rules = load_json_object(note_rules_path)
    glossary = load_json_object(glossary_path)
    speaker_source = load_json_object(speaker_source_path)
    speaker_candidate = load_json_object(speaker_candidate_path)
    item_source = load_json_object(item_source_path)
    item_candidate = load_json_object(item_candidate_path)
    manifest = load_json_object(manifest_path)
    layout = ensure_json_object(coerce_json_value(manifest["layout"]), "manifest.layout")
    speaker_names = ensure_json_object(coerce_json_value(speaker_source["speaker_names"]), "speaker_names")
    item_names = ensure_json_object(coerce_json_value(item_source["item_names"]), "item_names")
    workflow = ensure_json_object(coerce_json_value(manifest["workflow"]), "manifest.workflow")
    subagent_rounds = ensure_json_array(
        coerce_json_value(cast(object, workflow["subagent_rounds"])),
        "manifest.workflow.subagent_rounds",
    )
    first_round = ensure_json_object(subagent_rounds[0], "manifest.workflow.subagent_rounds[0]")
    second_round = ensure_json_object(subagent_rounds[1], "manifest.workflow.subagent_rounds[1]")
    plugin_json_string_leaf_candidate_count = report.summary["plugin_json_string_leaf_candidate_count"]
    assert rules == {r"(?i)\\F\d*\[[^\]\r\n]+\]": "[CUSTOM_FACE_PORTRAIT_{index}]"}
    assert structured_rules == {"paired_shell_rules": []}
    assert isinstance(plugin_json_string_leaf_candidate_count, int)
    assert plugin_json_string_leaf_candidate_count >= 2
    assert any(
        "$['parameters']['Nested']['text']"
        in ensure_json_array(
            ensure_json_object(coerce_json_value(raw_candidate), "plugin-json-string-leaf-candidates[]")[
                "string_leaf_path_candidates"
            ],
            "string_leaf_path_candidates",
        )
        for raw_candidate in plugin_json_string_candidates
    )
    assert note_rules == {}
    assert glossary == {"terms": {}}
    assert speaker_source == speaker_candidate
    assert item_source == item_candidate
    assert "村人" in speaker_names
    assert "回復薬" in item_names
    assert layout["engine_kind"] == "mz"
    assert report.summary["event_command_codes"] == [357]
    assert report.summary["placeholder_rule_draft_count"] == 1
    assert report.summary["structured_placeholder_rule_count"] == 0
    assert report.summary["terminology_subtask_count"] == 5
    assert "main_agent_rounds" not in workflow
    assert len(subagent_rounds) == 2
    assert first_round["name"] == "terminology_candidates"
    assert first_round["owner"] == "主代理"
    assert "正文术语表清洗规则" in str(first_round["description"])
    assert "非字段表副本" in str(first_round["description"])
    assert second_round["name"] == "external_text_rules"
    assert "placeholder_phase" in workflow
    assert "plugin-json-string-leaf-candidates.json" in json.dumps(report.details, ensure_ascii=False)
    assert "note-tag-rules.json" in json.dumps(report.details, ensure_ascii=False)
    assert "structured-placeholder-rules.json" in json.dumps(report.details, ensure_ascii=False)


@pytest.mark.parametrize(
    ("sample_text", "expected_candidate"),
    [
        ("◆<Alice>ｔ", "◆<Alice>ｔ"),
        ("<名前: Alraune>", "<名前: Alraune>"),
        ("<Voice id=hero name=Alice>", "<Voice id=hero name=Alice>"),
    ],
)
@pytest.mark.asyncio
async def test_validate_agent_workspace_blocks_uncovered_structured_candidates(
    minimal_english_game_dir: Path,
    tmp_path: Path,
    sample_text: str,
    expected_candidate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区验收复用已抽取正文扫描结构化协议外壳候选。"""
    common_events_path = minimal_english_game_dir / "data" / "CommonEvents.json"
    raw_value = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_value), "CommonEvents.json")
    event = ensure_json_object(common_events[1], "CommonEvents.json[1]")
    commands = ensure_json_array(event["list"], "CommonEvents.json[1].list")
    text_command = ensure_json_object(commands[1], "CommonEvents.json[1].list[1]")
    parameters = ensure_json_array(text_command["parameters"], "CommonEvents.json[1].list[1].parameters")
    parameters[0] = sample_text
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False, indent=2), encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="English Fixture Game",
        output_dir=workspace,
        command_codes=None,
    )

    async def forbidden_structured_validation(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
        sample_texts: list[str],
    ) -> AgentReport:
        """工作区验收不应为结构化占位符校验再抽取一次全量正文。"""
        _ = (self, game_title, rules_text, sample_texts)
        raise AssertionError("validate-agent-workspace 不应重新执行结构化占位符全量校验")

    async def forbidden_structured_scan(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """工作区验收不应为结构化占位符覆盖再抽取一次全量正文。"""
        _ = (self, game_title, rules_text)
        raise AssertionError("validate-agent-workspace 不应重新执行结构化占位符全量扫描")

    monkeypatch.setattr(
        AgentToolkitService,
        "validate_structured_placeholder_rules",
        forbidden_structured_validation,
    )
    monkeypatch.setattr(
        AgentToolkitService,
        "scan_structured_placeholder_candidates",
        forbidden_structured_scan,
    )
    report = await service.validate_agent_workspace(game_title="English Fixture Game", workspace=workspace)

    error_codes = {error.code for error in report.errors}
    warning_codes = {warning.code for warning in report.warnings}
    coverage_summary = ensure_json_object(
        ensure_json_object(report.details["structured_placeholder_coverage"], "structured_placeholder_coverage")[
            "summary"
        ],
        "structured_placeholder_coverage.summary",
    )
    assert report.status == "error"
    assert "structured_placeholder_coverage_uncovered" in error_codes
    assert "structured_placeholder_uncovered" in warning_codes
    assert coverage_summary["uncovered_count"] == 1
    coverage_details = ensure_json_object(
        ensure_json_object(report.details["structured_placeholder_coverage"], "structured_placeholder_coverage")[
            "details"
        ],
        "structured_placeholder_coverage.details",
    )
    candidates = ensure_json_array(coverage_details["candidates"], "structured_placeholder_coverage.details.candidates")
    assert any(
        ensure_json_object(candidate, "candidate")["candidate"] == expected_candidate for candidate in candidates
    )


@pytest.mark.asyncio
async def test_workflow_gate_blocks_external_rule_hits_outside_writable_scope(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """规则命中文本没有进入可写文本范围时，翻译前置硬闸必须报错。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    game_data = await load_game_data(minimal_game_dir)
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    text_rules = TextRules.from_setting(setting.text_rules)
    scope = TextScopeResult(
        translation_data_map={},
        entries=[
            TextScopeEntry(
                location_path="plugins.js/0/Message",
                source_type="plugin_parameter",
                rule_source="插件参数规则",
                item_type="short_text",
                original_lines=["これは翻訳対象です"],
                role=None,
                enters_translation=False,
                can_save_translation=False,
                can_write_back=False,
                translated=False,
                cannot_process_reason="规则命中项没有进入统一文本清单",
            )
        ],
    )

    async with await registry.open_game("テストゲーム") as session:
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
            rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN,
            scope_hash=event_command_rule_scope_hash_for_setting(
                game_data=game_data,
                setting=setting,
            ),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=PLACEHOLDER_RULE_DOMAIN,
            scope_hash=normal_placeholder_scope_hash_from_analysis(
                analyze_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        errors = await collect_workflow_gate_errors(
            session=session,
            context=replace(analysis_context, scope=scope),
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )

    assert "rule_hits_unwritable" in {error.code for error in errors}


@pytest.mark.asyncio
async def test_validate_plugin_rules_reports_json_string_leaf_candidates(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """插件规则误指向 JSON 字符串容器时，校验报告提示可写内部字符串叶子。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    rejected_report = await service.validate_plugin_rules(
        game_title="テストゲーム",
        rules_text=json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "TestPlugin",
                    "paths": ["$['parameters']['Nested']"],
                }
            ],
            ensure_ascii=False,
        ),
    )
    accepted_report = await service.validate_plugin_rules(
        game_title="テストゲーム",
        rules_text=json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "TestPlugin",
                    "paths": ["$['parameters']['Nested']['text']"],
                }
            ],
            ensure_ascii=False,
        ),
    )

    assert rejected_report.status == "error"
    assert "解析后的内部字符串叶子" in rejected_report.errors[0].message
    assert "$['parameters']['Nested']['text']" in rejected_report.errors[0].message
    assert accepted_report.status == "ok"


@pytest.mark.asyncio
async def test_validate_plugin_rules_rejects_english_protocol_value_paths(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """插件规则校验会把英文模式下只命中协议值的路径报告为错误。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.validate_plugin_rules(
        game_title="English Fixture Game",
        rules_text=json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "VisiblePlugin",
                    "paths": ["$['parameters']['Enabled']"],
                }
            ],
            ensure_ascii=False,
        ),
    )

    assert report.status == "error"
    assert {error.code for error in report.errors} == {"plugin_rules_invalid"}
    assert "没有命中玩家可见可翻译文本" in report.errors[0].message


@pytest.mark.asyncio
async def test_prepare_agent_workspace_uses_mv_event_command_default(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MV 工作区摘要按 356 插件命令生成，并复用虚拟名字框上下文验收。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_mv_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "mv-workspace"

    report = await service.prepare_agent_workspace(
        game_title="MVテストゲーム",
        output_dir=workspace,
        command_codes=None,
    )

    event_commands = load_json_object(workspace / "event-commands.json")
    mv_namebox_candidates = load_json_object(workspace / "mv-virtual-namebox-candidates.json")
    mv_namebox_rules = load_json_object(workspace / "mv-virtual-namebox-rules.json")
    manifest = load_json_object(workspace / "manifest.json")
    layout = ensure_json_object(coerce_json_value(manifest["layout"]), "manifest.layout")
    workflow = ensure_json_object(coerce_json_value(manifest["workflow"]), "manifest.workflow")
    subagent_rounds = ensure_json_array(
        coerce_json_value(cast(object, workflow["subagent_rounds"])),
        "manifest.workflow.subagent_rounds",
    )
    main_agent_rounds = ensure_json_array(
        coerce_json_value(cast(object, workflow["main_agent_rounds"])),
        "manifest.workflow.main_agent_rounds",
    )
    zero_round = ensure_json_object(main_agent_rounds[0], "manifest.workflow.main_agent_rounds[0]")
    first_round = ensure_json_object(subagent_rounds[0], "manifest.workflow.subagent_rounds[0]")
    commands = ensure_json_array(coerce_json_value(event_commands["356"]), "event-commands.356")
    manifest_files = "\n".join(
        item
        for item in ensure_json_array(coerce_json_value(manifest["files"]), "manifest.files")
        if isinstance(item, str)
    )
    assert report.status == "ok"
    assert report.summary["engine_kind"] == "mv"
    assert report.summary["event_command_codes"] == [356]
    assert report.summary["mv_virtual_namebox_candidate_count"] == mv_namebox_candidates["candidate_count"]
    assert report.summary["mv_virtual_namebox_rule_count"] == 0
    assert layout["engine_kind"] == "mv"
    assert "www" in str(layout["data_dir"])
    assert mv_namebox_candidates["engine_kind"] == "mv"
    assert isinstance(mv_namebox_candidates["candidates"], list)
    assert mv_namebox_rules == {"rules": []}
    assert len(subagent_rounds) == 2
    assert zero_round["name"] == "mv_virtual_namebox_rules"
    assert zero_round["owner"] == "主代理"
    assert "details.newly_matched_candidates" in str(zero_round["description"])
    assert first_round["name"] == "terminology_candidates"
    assert "mv-virtual-namebox-candidates.json" in manifest_files
    assert "mv-virtual-namebox-rules.json" in manifest_files
    assert len(commands) == 1

    async def forbidden_mv_namebox_revalidation(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """工作区验收已经持有 MV 虚拟名字框上下文，不应再调用全量规则校验器。"""
        _ = (self, game_title, rules_text)
        raise AssertionError("validate-agent-workspace 不应重新执行 MV 虚拟名字框全量校验")

    _ = (workspace / "mv-virtual-namebox-rules.json").write_text(
        f"{_mv_virtual_namebox_rules_text()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        AgentToolkitService,
        "validate_mv_virtual_namebox_rules",
        forbidden_mv_namebox_revalidation,
    )
    validation_report = await service.validate_agent_workspace(
        game_title="MVテストゲーム",
        workspace=workspace,
    )
    validation_error_codes = {error.code for error in validation_report.errors}
    mv_validation_details = ensure_json_object(
        coerce_json_value(validation_report.details["mv_virtual_namebox_rules"]),
        "details.mv_virtual_namebox_rules",
    )
    mv_validation_rules = ensure_json_array(
        coerce_json_value(mv_validation_details["rules"]),
        "details.mv_virtual_namebox_rules.rules",
    )
    assert "mv_virtual_namebox_rules_invalid" not in validation_error_codes
    assert len(mv_validation_rules) == 1


@pytest.mark.asyncio
async def test_prepare_agent_workspace_prefills_imported_database_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """二次翻译工作区会回填当前数据库中已导入的规则和术语表。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = cast(object, json.loads(items_path.read_text(encoding="utf-8")))
    items = ensure_json_array(coerce_json_value(raw_items), "Items.json")
    first_item = ensure_json_object(items[1], "Items.json[1]")
    first_item["note"] = "<拡張説明:薬草の詳細説明>"
    _ = items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    seed_workspace = tmp_path / "seed-workspace"
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=seed_workspace,
        command_codes=None,
    )
    exported_registry = TerminologyRegistry.model_validate(
        load_json_object(seed_workspace / "terminology" / "field-terms.json")
    )
    filled_registry = TerminologyRegistry.from_category_map(
        {
            category: {source_text: f"{source_text}译" for source_text in entries}
            for category, entries in exported_registry.as_category_map().items()
        }
    )
    filled_glossary = TerminologyGlossary(
        terms={
            source_text: translated_text
            for entries in filled_registry.as_category_map().values()
            for source_text, translated_text in entries.items()
            if translated_text.strip()
        }
    )
    game_data = await load_game_data(minimal_game_dir)
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_terminology_bundle(registry=filled_registry, glossary=filled_glossary)
        await session.replace_plugin_text_rules(
            [
                PluginTextRuleRecord(
                    plugin_index=0,
                    plugin_name="TestPlugin",
                    plugin_hash=build_plugin_hash(game_data.plugins_js[0]),
                    path_templates=["$['parameters']['Message']"],
                )
            ]
        )
        await session.replace_event_command_text_rules(
            [
                EventCommandTextRuleRecord(
                    command_code=357,
                    parameter_filters=[EventCommandParameterFilter(index=0, value="TestPlugin")],
                    path_templates=["$['parameters'][3]['message']"],
                )
            ]
        )
        await session.replace_note_tag_text_rules(
            [NoteTagTextRuleRecord(file_name="Items.json", tag_names=["拡張説明"])]
        )
        await session.replace_placeholder_rules(
            [
                PlaceholderRuleRecord(
                    pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
                    placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
                )
            ]
        )
        await session.replace_structured_placeholder_rules(
            [
                StructuredPlaceholderRuleRecord(
                    rule_name="MINI_LABEL",
                    rule_type="paired_shell",
                    pattern_text=r"(?P<open><Mini\s+Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)",
                    translatable_group="text",
                    protected_groups={
                        "open": "[CUSTOM_MINI_LABEL_OPEN_{index}]",
                        "close": "[CUSTOM_MINI_LABEL_CLOSE_{index}]",
                    },
                )
            ]
        )

    report = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )

    async def forbidden_plugin_revalidation(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """工作区验收已经持有插件参数校验上下文，不应再调用全量规则校验器。"""
        _ = (self, game_title, rules_text)
        raise AssertionError("validate-agent-workspace 不应重新执行插件参数全量校验")

    async def forbidden_note_tag_revalidation(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """工作区验收已经持有 Note 标签校验上下文，不应再调用全量规则校验器。"""
        _ = (self, game_title, rules_text)
        raise AssertionError("validate-agent-workspace 不应重新执行 Note 标签全量校验")

    async def forbidden_event_command_revalidation(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """工作区验收已经持有事件指令校验上下文，不应再调用全量规则校验器。"""
        _ = (self, game_title, rules_text)
        raise AssertionError("validate-agent-workspace 不应重新执行事件指令全量校验")

    monkeypatch.setattr(
        AgentToolkitService,
        "validate_plugin_rules",
        forbidden_plugin_revalidation,
    )
    monkeypatch.setattr(
        AgentToolkitService,
        "validate_note_tag_rules",
        forbidden_note_tag_revalidation,
    )
    monkeypatch.setattr(
        AgentToolkitService,
        "validate_event_command_rules",
        forbidden_event_command_revalidation,
    )
    validation_report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    prepared_registry = TerminologyRegistry.model_validate(
        load_json_object(workspace / "terminology" / "field-terms.json")
    )
    prepared_glossary = TerminologyGlossary.model_validate(
        load_json_object(workspace / "terminology" / "glossary.json")
    )
    plugin_rules = load_json_array(workspace / "plugin-rules.json")
    event_rules = load_json_object(workspace / "event-command-rules.json")
    note_rules = load_json_object(workspace / "note-tag-rules.json")
    placeholder_rules = load_json_object(workspace / "placeholder-rules.json")
    structured_placeholder_rules = load_json_object(workspace / "structured-placeholder-rules.json")
    warning_codes = {warning.code for warning in validation_report.warnings}
    assert report.status == "ok"
    assert report.summary["plugin_rule_count"] == 1
    assert report.summary["event_command_rule_count"] == 1
    assert report.summary["note_tag_rule_count"] == 1
    assert report.summary["placeholder_rule_count"] == 1
    assert report.summary["structured_placeholder_rule_count"] == 1
    assert report.summary["glossary_term_count"] == filled_glossary.term_count()
    assert prepared_registry == filled_registry
    assert prepared_glossary == filled_glossary
    assert plugin_rules == [
        {
            "plugin_index": 0,
            "plugin_name": "TestPlugin",
            "paths": ["$['parameters']['Message']"],
        }
    ]
    assert event_rules == {
        "357": [
            {
                "match": {"0": "TestPlugin"},
                "paths": ["$['parameters'][3]['message']"],
            }
        ]
    }
    assert note_rules == {"Items.json": ["拡張説明"]}
    assert placeholder_rules == {r"(?i)\\F\d*\[[^\]\r\n]+\]": "[CUSTOM_FACE_PORTRAIT_{index}]"}
    assert structured_placeholder_rules == {
        "paired_shell_rules": [
            {
                "name": "MINI_LABEL",
                "pattern": r"(?P<open><Mini\s+Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)",
                "translatable_group": "text",
                "protected_groups": {
                    "close": "[CUSTOM_MINI_LABEL_CLOSE_{index}]",
                    "open": "[CUSTOM_MINI_LABEL_OPEN_{index}]",
                },
            }
        ]
    }
    assert validation_report.status == "ok"
    assert "plugin_rules_missing" not in warning_codes
    assert "event_command_rules_missing" not in warning_codes
    assert "terminology_empty_translation" not in warning_codes


@pytest.mark.asyncio
async def test_validate_agent_workspace_reports_duplicate_translation_samples(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区验收报告为字段译名表重复译名 warning 附带可审查样例。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    field_terms_path = workspace / "terminology" / "field-terms.json"
    exported_registry = TerminologyRegistry.model_validate(load_json_object(field_terms_path))
    filled_map: dict[TerminologyCategory, dict[str, str]] = {
        category: {source_text: f"{source_text}译" for source_text in entries}
        for category, entries in exported_registry.as_category_map().items()
    }
    field_term_keys: list[tuple[TerminologyCategory, str]] = [
        (category, source_text) for category, entries in filled_map.items() for source_text in entries
    ]
    assert len(field_term_keys) >= 2
    for category, source_text in field_term_keys[:2]:
        filled_map[category][source_text] = "共同译名"
    filled_registry = TerminologyRegistry.from_category_map(filled_map)
    _ = field_terms_path.write_text(
        f"{json.dumps(filled_registry.model_dump(mode='json'), ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )

    validation_report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)
    terminology_details = ensure_json_object(validation_report.details["terminology"], "details.terminology")
    samples = ensure_json_array(
        terminology_details["duplicate_translation_samples"],
        "details.terminology.duplicate_translation_samples",
    )
    target_sample: JsonObject | None = None
    for index, raw_sample in enumerate(samples):
        sample = ensure_json_object(raw_sample, f"duplicate_translation_samples[{index}]")
        if sample.get("translation") == "共同译名":
            target_sample = sample
            break
    assert target_sample is not None
    sources = ensure_json_array(target_sample["sources"], "duplicate_translation_samples.sources")
    first_source = ensure_json_object(sources[0], "duplicate_translation_samples.sources[0]")
    warning_codes = {warning.code for warning in validation_report.warnings}

    assert "terminology_duplicate_translation" in warning_codes
    assert len(samples) <= 10
    assert len(sources) == 2
    assert set(first_source) == {"category", "source_text"}


@pytest.mark.asyncio
async def test_validate_agent_workspace_blocks_missing_note_tag_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """Note 标签规则是第二轮三类规则产物之一，缺失时工作区校验阻断。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    (workspace / "note-tag-rules.json").unlink()
    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    assert report.status == "error"
    assert "note_tag_rules_missing" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_validate_agent_workspace_blocks_missing_manifest(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区验收必须依赖 manifest 确认来源和事件指令编码。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    (workspace / "manifest.json").unlink()
    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    assert report.status == "error"
    assert "manifest_missing" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_validate_agent_workspace_reports_long_task_stages(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区验收向 CLI 报告可观测的长任务阶段。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    progress_updates: list[tuple[int, int]] = []
    advanced_steps: list[int] = []
    statuses: list[str] = []

    def set_progress(current: int, total: int) -> None:
        """记录绝对进度。"""
        progress_updates.append((current, total))

    def advance_progress(count: int) -> None:
        """记录阶段推进。"""
        advanced_steps.append(count)

    def set_status(status: str) -> None:
        """记录阶段状态。"""
        statuses.append(status)

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    _ = await service.validate_agent_workspace(
        game_title="テストゲーム",
        workspace=workspace,
        callbacks=(set_progress, advance_progress, set_status),
    )

    assert progress_updates[0] == (0, 12)
    assert sum(advanced_steps) == 12
    for expected_status in [
        "读取工作区清单",
        "加载翻译源视图",
        "抽取当前文本范围",
        "扫描插件源码",
        "校验插件规则",
        "校验结构化占位符规则",
        "汇总工作区校验报告",
    ]:
        assert expected_status in statuses


@pytest.mark.asyncio
async def test_manual_export_and_status_commands_report_long_task_stages(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """手动修复表和刷新状态查询向 CLI 报告可观测阶段。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"
    quality_fix_path = tmp_path / "quality-fix.json"
    progress_updates: list[tuple[int, int]] = []
    advanced_steps: list[int] = []
    statuses: list[str] = []

    def set_progress(current: int, total: int) -> None:
        """记录绝对进度。"""
        progress_updates.append((current, total))

    def advance_progress(count: int) -> None:
        """记录阶段推进。"""
        advanced_steps.append(count)

    def set_status(status: str) -> None:
        """记录阶段状态。"""
        statuses.append(status)

    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        _ = await session.start_translation_run(
            total_extracted=12,
            pending_count=8,
            deduplicated_count=7,
            batch_count=2,
        )

    pending_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=3,
        callbacks=(set_progress, advance_progress, set_status),
    )
    status_report = await service.translation_status(
        game_title="テストゲーム",
        refresh_scope=True,
        callbacks=(set_progress, advance_progress, set_status),
    )
    quality_fix_report = await service.export_quality_fix_template(
        game_title="テストゲーム",
        output_path=quality_fix_path,
        callbacks=(set_progress, advance_progress, set_status),
    )

    assert pending_report.status == "ok"
    assert status_report.status == "ok"
    assert quality_fix_report.status in {"ok", "warning"}
    assert pending_path.exists()
    assert quality_fix_path.exists()
    assert progress_updates[0] == (0, 5)
    assert advanced_steps
    for expected_status in [
        "加载游戏数据和规则",
        "构建当前文本范围",
        "筛选还没成功保存译文",
        "手动填写译文表已完成",
        "刷新当前文本范围",
        "正文翻译状态已完成",
        "调用 Rust 原生质检核心（",
        "质量修复表已完成",
    ]:
        assert any(status.startswith(expected_status) for status in statuses)


@pytest.mark.asyncio
async def test_validate_agent_workspace_respects_confirmed_empty_external_rule_states(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区空规则文件必须读取数据库里的显式空规则确认状态。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    _ = (workspace / "placeholder-rules.json").write_text("{}\n", encoding="utf-8")
    _ = (workspace / "structured-placeholder-rules.json").write_text(
        '{"paired_shell_rules": []}\n',
        encoding="utf-8",
    )
    before_report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)
    note_import_report = await service.import_note_tag_rules(
        game_title="テストゲーム",
        rules_text="{}",
        confirm_empty=True,
    )
    game_data = await load_game_data(minimal_game_dir)
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        await session.replace_rule_review_state(
            rule_domain=PLUGIN_TEXT_RULE_DOMAIN,
            scope_hash=plugin_rule_scope_hash(game_data),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN,
            scope_hash=event_command_rule_scope_hash_for_setting(
                game_data=game_data,
                setting=setting,
            ),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=PLACEHOLDER_RULE_DOMAIN,
            scope_hash=normal_placeholder_scope_hash_from_analysis(
                analyze_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )

    after_report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    before_error_codes = {error.code for error in before_report.errors}
    after_error_codes = {error.code for error in after_report.errors}
    empty_rule_error_codes = {
        "plugin_rules_empty_unconfirmed",
        "event_command_rules_empty_unconfirmed",
        "note_tag_rules_empty_unconfirmed",
        "placeholder_rules_empty_unconfirmed",
        "structured_placeholder_rules_empty_unconfirmed",
    }
    assert empty_rule_error_codes <= before_error_codes
    assert note_import_report.status == "warning"
    assert {warning.code for warning in note_import_report.warnings} == {"note_tag_rules_empty"}
    assert empty_rule_error_codes.isdisjoint(after_error_codes)


@pytest.mark.asyncio
async def test_validate_agent_workspace_rejects_high_risk_empty_plugin_source_review(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """高风险插件源码空规则复用工作区扫描结果并报错。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins_json_text = plugins_text.removeprefix("var $plugins = ").rstrip(";\r\n ")
    plugins = ensure_json_array(
        coerce_json_value(cast(object, json.loads(plugins_json_text))),
        "plugins",
    )
    plugins.append({"name": "HighRiskSource", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HighRiskSource.js").write_text(
        "\n".join(f"Window_Base.prototype.drawText('高リスク{i}', 0, 0, 320);" for i in range(301)),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    async with await registry.open_game("テストゲーム") as session:
        assessment = await session.read_plugin_source_assessment()
        assert assessment is not None
        await session.replace_rule_review_state(
            rule_domain=PLUGIN_SOURCE_TEXT_RULE_DOMAIN,
            scope_hash=assessment.source_hash,
            reviewed_empty=True,
        )

    async def forbidden_plugin_source_revalidation(
        self: AgentToolkitService,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """工作区验收已经持有插件源码扫描结果，不应再调用全量规则校验器。"""
        _ = (self, game_title, rules_text)
        raise AssertionError("validate-agent-workspace 不应重新执行插件源码全量校验")

    monkeypatch.setattr(
        AgentToolkitService,
        "validate_plugin_source_rules",
        forbidden_plugin_source_revalidation,
    )
    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    assert "plugin_source_rules_empty_high_risk" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_validate_agent_workspace_uses_manifest_event_command_codes_for_empty_review(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区验收按 manifest 里的事件指令编码确认空规则范围。"""
    monkeypatch.setenv(APP_HOME_ENV_NAME, str(tmp_path / "app-home"))
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes={999},
    )
    game_data = await load_game_data(minimal_game_dir)
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_rule_review_state(
            rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN,
            scope_hash=event_command_rule_scope_hash_for_command_codes(
                game_data=game_data,
                command_codes=frozenset({999}),
            ),
            scope_payload={
                "kind": "event_command_codes",
                "command_codes": [999],
            },
            reviewed_empty=True,
        )

    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)
    error_codes = {error.code for error in report.errors}

    assert "event_command_rules_empty_unconfirmed" not in error_codes
    assert "event_command_rules_empty_confirmation_stale" not in error_codes


@pytest.mark.asyncio
async def test_validate_agent_workspace_reports_invalid_terminology_file(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区验收会把坏术语表报告成结构化错误。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    _ = (workspace / "terminology" / "field-terms.json").write_text("{}\n", encoding="utf-8")

    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    assert report.status == "error"
    assert "terminology_validate_failed" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_validate_agent_workspace_reports_invalid_glossary_file(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区验收会把坏正文术语表报告成结构化错误。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    _ = (workspace / "terminology" / "glossary.json").write_text(
        json.dumps({"terms": {"小明": "小明"}, "note": "不允许"}, ensure_ascii=False),
        encoding="utf-8",
    )

    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    assert report.status == "error"
    assert "glossary_validate_failed" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_validate_agent_workspace_blocks_uncovered_placeholder_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区验收复用已抽取正文扫描普通占位符覆盖。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    _ = (workspace / "placeholder-rules.json").write_text("{}\n", encoding="utf-8")

    async def forbidden_placeholder_rescan(
        self: AgentToolkitService,
        *,
        game_title: str,
        custom_placeholder_rules_text: str | None,
    ) -> AgentReport:
        """工作区验收不应为普通占位符覆盖再抽取一次全量正文。"""
        _ = (self, game_title, custom_placeholder_rules_text)
        raise AssertionError("validate-agent-workspace 不应重新执行普通占位符全量扫描")

    monkeypatch.setattr(
        AgentToolkitService,
        "scan_placeholder_candidates",
        forbidden_placeholder_rescan,
    )
    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    assert report.status == "error"
    assert "placeholder_coverage_uncovered" in {error.code for error in report.errors}
    placeholder_coverage = ensure_json_object(report.details["placeholder_coverage"], "placeholder_coverage")
    summary = ensure_json_object(placeholder_coverage["summary"], "placeholder_coverage.summary")
    assert summary["uncovered_count"] != 0


@pytest.mark.asyncio
async def test_validate_agent_workspace_reports_invalid_placeholder_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区验收会把坏占位符规则报告成结构化错误。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    _ = (workspace / "placeholder-rules.json").write_text("{\n", encoding="utf-8")

    report = await service.validate_agent_workspace(game_title="テストゲーム", workspace=workspace)

    error_codes = {error.code for error in report.errors}
    assert report.status == "error"
    assert "placeholder_rules_invalid" in error_codes
    assert "placeholder_coverage_scan_failed" in error_codes


@pytest.mark.asyncio
async def test_note_tag_rule_validation_import_and_pending_export(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """Note 标签规则校验后会让目标标签值进入 pending，机器协议标签会被拒绝。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = cast(object, json.loads(items_path.read_text(encoding="utf-8")))
    items = ensure_json_array(coerce_json_value(raw_items), "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = "<拡張説明:一行目\n二行目>\n<upgrade:1,2,3>\n<ExtendDesc:別説明>"
    items.append({"id": 2, "name": "空タグ項目", "note": "<拡張説明:>", "description": ""})
    _ = items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    candidates_path = tmp_path / "note-tag-candidates.json"
    pending_path = tmp_path / "pending-translations.json"
    rules_text = json.dumps(
        {"Items.json": ["拡張説明", "ExtendDesc"]},
        ensure_ascii=False,
    )
    machine_rules_text = json.dumps({"Items.json": ["upgrade"]}, ensure_ascii=False)

    candidate_report = await service.export_note_tag_candidates(
        game_title="テストゲーム",
        output_path=candidates_path,
    )
    validate_report = await service.validate_note_tag_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )
    rejected_report = await service.validate_note_tag_rules(
        game_title="テストゲーム",
        rules_text=machine_rules_text,
    )
    import_report = await service.import_note_tag_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )
    export_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )

    payload = load_json_object(pending_path)
    assert candidate_report.status == "ok"
    assert candidates_path.exists()
    assert validate_report.status == "ok"
    assert validate_report.summary["hit_count"] == 2
    assert rejected_report.status == "error"
    assert "机器协议" in rejected_report.errors[0].message
    assert import_report.status == "ok"
    assert export_report.status == "ok"
    assert "Items.json/1/note/拡張説明" in payload
    assert "Items.json/1/note/ExtendDesc" in payload
    assert "Items.json/2/note/拡張説明" not in payload


@pytest.mark.asyncio
async def test_import_empty_note_tag_rules_allows_confirmed_empty_with_candidates(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """Note 标签候选经审查没有玩家可见文本时，可以显式保存空结果。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = cast(object, json.loads(items_path.read_text(encoding="utf-8")))
    items = ensure_json_array(coerce_json_value(raw_items), "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = "<PrivateProtocol:内部コード>"
    _ = items_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    missing_report = await service.import_note_tag_rules(
        game_title="テストゲーム",
        rules_text="{}",
    )
    confirmed_report = await service.import_note_tag_rules(
        game_title="テストゲーム",
        rules_text="{}",
        confirm_empty=True,
    )

    game_data = await load_game_data(minimal_game_dir)
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        text_rules = TextRules.from_setting(setting.text_rules)
        state = await session.read_rule_review_state(rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN)

    assert missing_report.status == "error"
    assert "confirm-empty" in missing_report.errors[0].message
    assert confirmed_report.status == "warning"
    assert {warning.code for warning in confirmed_report.warnings} == {"note_tag_rules_empty"}
    assert state is not None
    assert state.scope_hash == note_tag_rule_scope_hash_for_text_rules(
        game_data=game_data,
        text_rules=text_rules,
    )


@pytest.mark.asyncio
async def test_manual_pending_translation_export_and_import(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """Agent 可以导出少量待翻译条目，人工补齐后再由工具校验入库。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    export_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=10,
    )

    assert export_report.status == "ok"
    payload = load_json_object(pending_path)
    target_path = ""
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
        if original_lines == ["こんにちは"]:
            target_path = location_path
            entry["translation_lines"] = ["　你好　"]
            payload[location_path] = entry
            break
    assert target_path
    _ = pending_path.write_text(
        json.dumps({target_path: payload[target_path]}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        run_record = await session.start_translation_run(
            total_extracted=10,
            pending_count=10,
            deduplicated_count=10,
            batch_count=1,
        )
        await session.write_translation_quality_errors(
            run_record.run_id,
            [
                TranslationErrorItem(
                    location_path=target_path,
                    item_type="short_text",
                    role=None,
                    original_lines=["こんにちは"],
                    translation_lines=[],
                    error_type="AI漏翻",
                    error_detail=["人工补译前的历史错误"],
                    model_response='{"bad": true}',
                )
            ],
        )

    import_report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )
    status_report = await service.translation_status(game_title="テストゲーム", refresh_scope=True)
    quality_report = await service.quality_report(game_title="テストゲーム")

    assert import_report.status == "ok"
    assert status_report.summary["pending_count"] == quality_report.summary["pending_count"]
    assert status_report.summary["run_pending_count"] == 10
    assert status_report.summary["quality_error_count"] == 0
    assert status_report.summary["run_quality_error_count"] == 0
    assert quality_report.summary["quality_error_count"] == 0
    assert quality_report.summary["run_quality_error_count"] == 0
    async with await registry.open_game("テストゲーム") as session:
        translated_items = await session.read_translated_items()
        quality_errors = await session.read_translation_quality_errors(run_record.run_id)
    translated_by_path = {item.location_path: item for item in translated_items}
    assert translated_by_path[target_path].translation_lines == ["你好"]
    assert quality_errors == []


@pytest.mark.asyncio
async def test_translation_status_uses_database_fast_path_by_default(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态查询默认不能重新加载游戏文件和构建完整文本范围。"""

    async def forbidden_game_data_load(*args: object, **kwargs: object) -> NoReturn:
        """快速状态查询不应触碰游戏文件加载。"""
        _ = (args, kwargs)
        raise AssertionError("translation-status 默认不应加载游戏文件")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        _ = await session.start_translation_run(
            total_extracted=12,
            pending_count=8,
            deduplicated_count=7,
            batch_count=2,
        )
    monkeypatch.setattr(
        "app.agent_toolkit.services.core.CoreAgentMixin._load_translation_source_game_data",
        forbidden_game_data_load,
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.translation_status(game_title="テストゲーム")

    assert report.status == "ok"
    assert report.summary["scope_refreshed"] is False
    assert report.summary["pending_count"] == 8
    assert report.summary["extractable_count"] == 12


@pytest.mark.asyncio
async def test_manual_translation_keeps_repeated_structured_shell_indices(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """人工译文里的多个相同结构化外壳按原文顺序映射回各自编号。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = cast(object, json.loads(items_path.read_text(encoding="utf-8")))
    items = ensure_json_array(coerce_json_value(raw_items), "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["description"] = "慎重に相手のおっぱいを揉んで愛撫する。\n【自身の我慢-5】【MP＋10】【相手の我慢　↑】"
    _ = items_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_structured_placeholder_rules(
            [
                StructuredPlaceholderRuleRecord(
                    rule_name="BRACKET_TITLE",
                    rule_type="paired_shell",
                    pattern_text=r"(?P<open>【)(?P<text>[^【】\r\n]*?)(?P<close>】)",
                    translatable_group="text",
                    protected_groups={
                        "open": "[CUSTOM_BRACKET_TITLE_OPEN_{index}]",
                        "close": "[CUSTOM_BRACKET_TITLE_CLOSE_{index}]",
                    },
                )
            ]
        )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    export_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    target_path = "Items.json/1/description"
    target_entry = ensure_json_object(coerce_json_value(payload[target_path]), target_path)
    target_entry["translation_lines"] = ["慎重地揉捏对方的胸部进行爱抚。\n【自身忍耐-5】【MP＋10】【对方忍耐　↑】"]
    _ = pending_path.write_text(
        json.dumps({target_path: target_entry}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    import_report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )

    assert export_report.status == "ok"
    assert import_report.status == "ok"
    async with await registry.open_game("テストゲーム") as session:
        translated_items = await session.read_translated_items()
    translated_by_path = {item.location_path: item for item in translated_items}
    assert translated_by_path[target_path].translation_lines == [
        "慎重地揉捏对方的胸部进行爱抚。\n【自身忍耐-5】【MP＋10】【对方忍耐　↑】"
    ]


@pytest.mark.asyncio
async def test_manual_translation_rejects_changed_unprotected_control_sequence(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """人工补译不得改写未被占位符规则覆盖的疑似控制符。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), "CommonEvents.json")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    commands.insert(-1, {"code": 101, "parameters": [0, 0, 0, 2, "アリス"]})
    commands.insert(-1, {"code": 401, "parameters": [r"\F3[66」「ふーん……？」"]})
    _ = common_events_path.write_text(
        json.dumps(common_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    export_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    target_path = ""
    target_entry: JsonObject = {}
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
        if original_lines == [r"\F3[66」「ふーん……？」"]:
            target_path = location_path
            entry["translation_lines"] = [r"\F3[60」「唔——嗯……？」"]
            target_entry = {key: value for key, value in entry.items()}
            break
    assert export_report.status == "ok"
    assert target_path
    _ = pending_path.write_text(
        json.dumps({target_path: target_entry}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rejected_report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )

    assert rejected_report.status == "error"
    assert rejected_report.errors
    assert "疑似控制符不一致" in rejected_report.errors[0].message
    assert r"\F3[66」" in rejected_report.errors[0].message
    assert r"\F3[60」" in rejected_report.errors[0].message


@pytest.mark.asyncio
async def test_manual_translation_uses_source_residual_exception_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """确需保留的源文片段必须先导入显式例外规则才能通过人工补译。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    export_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=10,
    )
    payload = load_json_object(pending_path)
    target_path = ""
    target_entry: JsonObject = {}
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
        if original_lines == ["こんにちは"]:
            target_path = location_path
            entry["translation_lines"] = ["こんにちは"]
            target_entry = {key: value for key, value in entry.items()}
            break
    assert export_report.status == "ok"
    assert target_path
    _ = pending_path.write_text(
        json.dumps({target_path: target_entry}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rejected_report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )
    rules_text = json.dumps(
        {
            "position_rules": {
                target_path: {
                    "allowed_terms": ["こんにちは"],
                    "reason": "proper_noun",
                }
            },
            "structural_rules": [],
        },
        ensure_ascii=False,
    )
    validate_report = await service.validate_source_residual_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )
    import_rules_report = await service.import_source_residual_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )
    accepted_report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )
    quality_report = await service.quality_report(game_title="テストゲーム")

    assert rejected_report.status == "error"
    assert "日文残留" in rejected_report.errors[0].message
    assert validate_report.status == "ok"
    assert import_rules_report.status == "ok"
    assert accepted_report.status == "ok"
    assert quality_report.summary["source_residual_rule_count"] == 1
    assert quality_report.summary["source_residual_count"] == 0
    assert quality_report.details["source_residual_items"] == []
    async with await registry.open_game("テストゲーム") as session:
        translated_items = await session.read_translated_items()
        residual_rules = await session.read_source_residual_rules()
    translated_by_path = {item.location_path: item for item in translated_items}
    assert translated_by_path[target_path].translation_lines == ["こんにちは"]
    assert residual_rules[0].rule_type == "position"
    assert residual_rules[0].location_path == target_path
    assert residual_rules[0].allowed_terms == ["こんにちは"]
    assert residual_rules[0].reason == "proper_noun"


@pytest.mark.asyncio
async def test_quality_report_treats_source_residual_as_error(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告把未放行的源文残留风险作为禁止写进游戏文件的问题。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    residual_path = ""
    residual_original_lines: list[str] = []
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = [
            line
            for line in ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
            if isinstance(line, str)
        ]
        if any(_contains_japanese_test_char(line) for line in original_lines):
            residual_path = location_path
            residual_original_lines = original_lines
            break
    assert residual_path

    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path=residual_path,
                    item_type="short_text",
                    role=None,
                    original_lines=residual_original_lines,
                    source_line_paths=[],
                    translation_lines=residual_original_lines,
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    error_codes = {error.code for error in report.errors}
    warning_codes = {warning.code for warning in report.warnings}
    assert report.status == "error"
    assert "source_residual" in error_codes
    assert "source_residual" not in warning_codes
    assert report.summary["source_residual_count"] == 1


@pytest.mark.asyncio
async def test_quality_report_structural_source_residual_rule_is_line_scoped(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """结构性源文例外在原生质检中只遮蔽协议词，不放行显示文本残留。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"
    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    target_path = ""
    original_lines: list[str] = []
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        entry_original_lines = [
            line
            for line in ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
            if isinstance(line, str)
        ]
        if len(entry_original_lines) == 1:
            target_path = location_path
            original_lines = entry_original_lines
            break
    assert target_path
    rules_text = json.dumps(
        {
            "position_rules": {},
            "structural_rules": [
                {
                    "pattern": r"^(?P<protocol>なまえ):(?P<visible>.*)$",
                    "allowed_terms": ["なまえ"],
                    "check_group": "visible",
                    "reason": "protocol_label",
                }
            ],
        },
        ensure_ascii=False,
    )

    import_rules_report = await service.import_source_residual_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )
    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path=target_path,
                    item_type="short_text",
                    role=None,
                    original_lines=original_lines,
                    source_line_paths=[],
                    translation_lines=["なまえ:你好"],
                )
            ],
        )
    protocol_report = await service.quality_report(game_title="テストゲーム")
    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path=target_path,
                    item_type="short_text",
                    role=None,
                    original_lines=original_lines,
                    source_line_paths=[],
                    translation_lines=["なまえ:なまえ"],
                )
            ],
        )
    leaked_report = await service.quality_report(game_title="テストゲーム")

    assert import_rules_report.status == "ok"
    assert protocol_report.summary["source_residual_count"] == 0
    assert leaked_report.summary["source_residual_count"] == 1


@pytest.mark.asyncio
async def test_quality_report_errors_on_corrupt_source_residual_rule(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告遇到损坏的源文残留例外规则时返回明确业务错误。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_source_residual_rules(
            [
                SourceResidualRuleRecord(
                    rule_id="structural:broken",
                    rule_type="structural",
                    pattern_text="[",
                    allowed_terms=["なまえ"],
                    check_group="visible",
                    reason="broken",
                )
            ]
        )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.quality_report(game_title="テストゲーム")

    assert report.status == "error"
    assert "source_residual_rules_invalid" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_quality_report_ignores_stale_saved_translation_quality_errors(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告把当前不可写的已保存译文作为必须处理的问题。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    async with await registry.open_game("テストゲーム") as session:
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path="Removed.json/1/name",
                    item_type="short_text",
                    role=None,
                    original_lines=["こんにちは"],
                    source_line_paths=[],
                    translation_lines=["こんにちは"],
                )
            ]
        )

    report = await service.quality_report(game_title="テストゲーム")

    error_codes = {error.code for error in report.errors}
    warning_codes = {warning.code for warning in report.warnings}
    assert "source_residual" not in error_codes
    assert "stale_saved_translations" in error_codes
    assert "stale_saved_translations" not in warning_codes
    assert report.summary["stale_translation_count"] == 1
    assert report.summary["source_residual_count"] == 0
    assert report.details["source_residual_items"] == []


@pytest.mark.asyncio
async def test_quality_report_uses_command_setting_overrides(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """写入前质量报告使用本次命令传入的文本规则覆盖。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    residual_path = ""
    residual_original_lines: list[str] = []
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = [
            line
            for line in ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
            if isinstance(line, str)
        ]
        if any(_contains_japanese_test_char(line) for line in original_lines):
            residual_path = location_path
            residual_original_lines = original_lines
            break
    assert residual_path

    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path=residual_path,
                    item_type="short_text",
                    role=None,
                    original_lines=residual_original_lines,
                    source_line_paths=[],
                    translation_lines=["中カ"],
                )
            ],
        )

    default_report = await service.quality_report(game_title="テストゲーム")
    override_report = await service.quality_report(
        game_title="テストゲーム",
        setting_overrides=SettingOverrides(source_residual_allowed_chars=["カ"]),
    )

    default_error_codes = {error.code for error in default_report.errors}
    override_error_codes = {error.code for error in override_report.errors}
    assert "source_residual" in default_error_codes
    assert "source_residual" not in override_error_codes
    assert default_report.summary["source_residual_count"] == 1
    assert override_report.summary["source_residual_count"] == 0


@pytest.mark.asyncio
async def test_agent_reports_error_on_stale_plugin_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """Agent 工具包把过期插件规则作为覆盖审计错误，同时不生成假文本。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_plugin_text_rules(
            [
                PluginTextRuleRecord(
                    plugin_index=0,
                    plugin_name="TestPlugin",
                    plugin_hash="stale-hash",
                    path_templates=["$['parameters']['Message']"],
                )
            ]
        )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    quality_report = await service.quality_report(game_title="テストゲーム")
    export_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    workspace_report = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=tmp_path / "workspace",
        command_codes=None,
    )

    error_codes = {error.code for error in quality_report.errors}
    assert quality_report.summary["plugin_rule_count"] == 0
    assert quality_report.summary["stale_plugin_rule_count"] == 1
    assert "stale_plugin_rules" in error_codes
    assert export_report.status == "error"
    assert {error.code for error in export_report.errors} == {"stale_plugin_rules"}
    assert not pending_path.exists()
    assert workspace_report.status in {"ok", "warning"}
    assert workspace_report.summary["stale_plugin_rule_count"] == 1
    assert (tmp_path / "workspace" / "manifest.json").exists()


@pytest.mark.asyncio
async def test_manual_long_text_import_splits_overwide_lines(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """人工补译 long_text 入库前会按当前行宽配置自动拆短。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    setting_path = tmp_path / "setting.toml"
    setting_text = EXAMPLE_SETTING_PATH.read_text(encoding="utf-8")
    setting_text = setting_text.replace("long_text_line_width_limit = 26", "long_text_line_width_limit = 3")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{(ROOT / "prompts" / "text_translation_ja_to_zh_system.md").as_posix()}"',
    )
    _ = setting_path.write_text(setting_text, encoding="utf-8")
    service = AgentToolkitService(game_registry=registry, setting_path=setting_path)
    pending_path = tmp_path / "pending-translations.json"

    export_report = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    target_path = ""
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        if entry["item_type"] == "long_text":
            target_path = location_path
            entry["translation_lines"] = ["甲乙丙丁戊己庚辛"]
            _ = pending_path.write_text(
                json.dumps({target_path: entry}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            break

    import_report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )

    assert export_report.status == "ok"
    assert target_path
    assert import_report.status == "ok"
    async with await registry.open_game("テストゲーム") as session:
        translated_items = await session.read_translated_items()
    translated_by_path = {item.location_path: item for item in translated_items}
    assert translated_by_path[target_path].translation_lines == ["甲乙丙", "丁戊己", "庚辛"]


@pytest.mark.parametrize(
    "translation_lines",
    [
        ["第一行", "第二行"],
        ["第一行\n第二行"],
        [r"第一行\n第二行"],
        ["译文：你好"],
        ["translation_lines: 你好"],
    ],
)
@pytest.mark.asyncio
async def test_manual_translation_import_rejects_text_structure_errors(
    minimal_game_dir: Path,
    tmp_path: Path,
    translation_lines: list[str],
) -> None:
    """人工补译同样拒绝改动单字段结构或混入模型协议文本的译文。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    target_path = ""
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = [
            line
            for line in ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
            if isinstance(line, str)
        ]
        if entry["item_type"] == "short_text" and not any("\n" in line or r"\n" in line for line in original_lines):
            target_path = location_path
            entry["translation_lines"] = cast(JsonValue, list(translation_lines))
            _ = pending_path.write_text(
                json.dumps({target_path: entry}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            break
    assert target_path

    report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )

    assert report.status == "error"
    assert report.summary["imported_count"] == 0
    assert report.errors[0].code == "manual_translation_invalid"
    invalid_items = ensure_json_array(report.details["invalid_items"], "invalid_items")
    first_invalid = ensure_json_object(coerce_json_value(invalid_items[0]), "invalid_items[0]")
    assert first_invalid["location_path"] == target_path
    assert "message" in first_invalid
    assert "expected_real_line_break_count" in first_invalid


@pytest.mark.asyncio
async def test_manual_translation_import_reports_all_invalid_items_without_partial_write(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """人工补译导入一次报告所有坏条目，并且不保存任何部分成功结果。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    selected: JsonObject = {}
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = [
            line
            for line in ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
            if isinstance(line, str)
        ]
        if entry["item_type"] != "short_text" or any("\n" in line or r"\n" in line for line in original_lines):
            continue
        entry["translation_lines"] = ["第一行\n第二行"] if not selected else ["译文：你好"]
        selected[location_path] = entry
        if len(selected) == 2:
            break
    assert len(selected) == 2
    _ = pending_path.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")

    report = await service.import_manual_translations(
        game_title="テストゲーム",
        input_path=pending_path,
    )
    async with await registry.open_game("テストゲーム") as session:
        translated_items = await session.read_translated_items()

    invalid_items = ensure_json_array(report.details["invalid_items"], "invalid_items")
    assert report.status == "error"
    assert report.summary["imported_count"] == 0
    assert len(invalid_items) == 2
    assert translated_items == []
    assert any(
        ensure_json_object(coerce_json_value(item), "invalid_item")["actual_real_line_break_count"] == 1
        for item in invalid_items
    )


@pytest.mark.asyncio
async def test_export_quality_fix_template_collects_repairable_items(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量修复模板会从报告问题导出标准修复表并预填当前译文。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"
    template_path = tmp_path / "quality-fix-template.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    sorted_paths = sorted(payload)
    quality_error_path = sorted_paths[0]
    residual_path = ""
    for candidate_path in sorted_paths:
        if candidate_path == quality_error_path:
            continue
        candidate_entry = ensure_json_object(coerce_json_value(payload[candidate_path]), candidate_path)
        candidate_lines = ensure_json_array(candidate_entry["original_lines"], f"{candidate_path}.original_lines")
        if any(isinstance(line, str) and _contains_japanese_test_char(line) for line in candidate_lines):
            residual_path = candidate_path
            break
    assert residual_path
    placeholder_path = next(
        path
        for path in sorted_paths
        if path not in {quality_error_path, residual_path}
        and ensure_json_object(coerce_json_value(payload[path]), path)["item_type"] == "long_text"
    )
    quality_error_entry = ensure_json_object(coerce_json_value(payload[quality_error_path]), quality_error_path)
    residual_entry = ensure_json_object(coerce_json_value(payload[residual_path]), residual_path)
    residual_original_lines = [
        line
        for line in ensure_json_array(residual_entry["original_lines"], f"{residual_path}.original_lines")
        if isinstance(line, str)
    ]
    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path=residual_path,
                    item_type="short_text",
                    role=None,
                    original_lines=residual_original_lines,
                    source_line_paths=[],
                    translation_lines=residual_original_lines,
                ),
                TranslationItem(
                    location_path=placeholder_path,
                    item_type="long_text",
                    role=None,
                    original_lines=["こんにちは"],
                    source_line_paths=[],
                    translation_lines=[r"\C[4]甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲"],
                ),
            ],
        )
        run_record = await session.start_translation_run(
            total_extracted=len(sorted_paths),
            pending_count=len(sorted_paths),
            deduplicated_count=len(sorted_paths),
            batch_count=1,
        )
        await session.write_translation_quality_errors(
            run_record.run_id,
            [
                TranslationErrorItem(
                    location_path=quality_error_path,
                    item_type="short_text",
                    role=None,
                    original_lines=[
                        line
                        for line in ensure_json_array(
                            quality_error_entry["original_lines"],
                            f"{quality_error_path}.original_lines",
                        )
                        if isinstance(line, str)
                    ],
                    translation_lines=["候选译文"],
                    error_type="AI漏翻",
                    error_detail=["测试质量错误"],
                    model_response='{"translation_lines":["候选译文"]}',
                )
            ],
        )

    report = await service.export_quality_fix_template(
        game_title="テストゲーム",
        output_path=template_path,
    )

    template = load_json_object(template_path)
    assert report.status == "ok"
    assert report.summary["quality_error_count"] == 1
    assert report.summary["quality_error_items_count"] == 1
    quality_error_category_counts = ensure_json_object(
        report.summary["quality_error_category_counts"],
        "quality_error_category_counts",
    )
    assert quality_error_category_counts["missing_translation"] == 1
    assert report.summary["source_residual_count"] == 1
    assert report.summary["placeholder_risk_count"] == 1
    assert report.summary["overwide_line_count"] == 1
    assert set(template) == {quality_error_path, residual_path, placeholder_path}
    quality_template = ensure_json_object(coerce_json_value(template[quality_error_path]), quality_error_path)
    placeholder_template = ensure_json_object(coerce_json_value(template[placeholder_path]), placeholder_path)
    assert quality_template["translation_lines"] == ["候选译文"]
    assert placeholder_template["translation_lines"] == [
        r"\C[4]甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲"
    ]
    categories = ensure_json_object(report.details["problem_categories_by_path"], "problem_categories_by_path")
    assert categories[placeholder_path] == ["placeholder_risk", "overwide_line"]


@pytest.mark.asyncio
async def test_quality_fix_template_restores_prefilled_model_placeholders(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """修复表会把模型临时译文里的程序占位符还原为游戏原始控制符。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"
    template_path = tmp_path / "quality-fix-template.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=None,
    )
    payload = load_json_object(pending_path)
    target_path = ""
    target_original_lines: list[str] = []
    for location_path, raw_entry in payload.items():
        entry = ensure_json_object(coerce_json_value(raw_entry), location_path)
        original_lines = [
            line
            for line in ensure_json_array(entry["original_lines"], f"{location_path}.original_lines")
            if isinstance(line, str)
        ]
        if any(r"\C[4]" in line for line in original_lines):
            target_path = location_path
            target_original_lines = original_lines
            break

    assert target_path
    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        run_record = await session.start_translation_run(
            total_extracted=len(payload),
            pending_count=len(payload),
            deduplicated_count=len(payload),
            batch_count=1,
        )
        await session.write_translation_quality_errors(
            run_record.run_id,
            [
                TranslationErrorItem(
                    location_path=target_path,
                    item_type="long_text",
                    role=None,
                    original_lines=target_original_lines,
                    translation_lines=[r"[RMMZ_TEXT_COLOR_4]强调[RMMZ_TEXT_COLOR_0]"],
                    error_type="控制符不匹配",
                    error_detail=["测试程序占位符还原"],
                    model_response='{"translation_lines":["[RMMZ_TEXT_COLOR_4]强调[RMMZ_TEXT_COLOR_0]"]}',
                )
            ],
        )

    report = await service.export_quality_fix_template(
        game_title="テストゲーム",
        output_path=template_path,
    )

    template = load_json_object(template_path)
    exported_entry = ensure_json_object(coerce_json_value(template[target_path]), target_path)
    text_for_model_lines = ensure_json_array(
        exported_entry["text_for_model_lines"], f"{target_path}.text_for_model_lines"
    )
    manual_fill_note = exported_entry["manual_fill_note"]
    assert report.status == "ok"
    assert exported_entry["translation_lines"] == [r"\C[4]强调\C[0]"]
    assert any(isinstance(line, str) and "[RMMZ_TEXT_COLOR_4]" in line for line in text_for_model_lines)
    assert isinstance(manual_fill_note, str)
    assert "text_for_model_lines 只供对照" in manual_fill_note
    assert "游戏原始控制符" in manual_fill_note


@pytest.mark.asyncio
async def test_reset_translations_validates_paths_before_deleting(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """重置译文命令遇到非法定位路径时不做部分删除。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"
    reset_path = tmp_path / "reset-translations.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=1,
    )
    payload = load_json_object(pending_path)
    target_path = next(iter(payload))
    async with await registry.open_game("テストゲーム") as session:
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path=target_path,
                    item_type="short_text",
                    role=None,
                    original_lines=["こんにちは"],
                    source_line_paths=[],
                    translation_lines=["你好"],
                )
            ]
        )

    _ = reset_path.write_text(
        json.dumps({"location_paths": [target_path, "Missing.json/1"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    rejected_report = await service.reset_translations(
        game_title="テストゲーム",
        input_path=reset_path,
    )
    async with await registry.open_game("テストゲーム") as session:
        paths_after_reject = await session.read_translation_location_paths()

    _ = reset_path.write_text(
        json.dumps({"location_paths": [target_path]}, ensure_ascii=False),
        encoding="utf-8",
    )
    accepted_report = await service.reset_translations(
        game_title="テストゲーム",
        input_path=reset_path,
    )
    quality_report = await service.quality_report(game_title="テストゲーム")
    async with await registry.open_game("テストゲーム") as session:
        paths_after_accept = await session.read_translation_location_paths()

    assert rejected_report.status == "error"
    assert rejected_report.summary["reset_count"] == 0
    assert target_path in paths_after_reject
    assert accepted_report.status == "ok"
    assert accepted_report.summary["requested_count"] == 1
    assert accepted_report.summary["reset_count"] == 1
    assert target_path not in paths_after_accept
    pending_count = quality_report.summary["pending_count"]
    assert isinstance(pending_count, int)
    assert pending_count >= 1


@pytest.mark.asyncio
async def test_reset_translations_all_deletes_current_active_translation_cache(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """完整重译入口可以清除当前提取范围内全部已入库译文。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    pending_path = tmp_path / "pending-translations.json"

    _ = await service.export_pending_translations(
        game_title="テストゲーム",
        output_path=pending_path,
        limit=2,
    )
    payload = load_json_object(pending_path)
    target_paths = list(payload)[:2]
    async with await registry.open_game("テストゲーム") as session:
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path=target_path,
                    item_type="short_text",
                    role=None,
                    original_lines=["こんにちは"],
                    source_line_paths=[],
                    translation_lines=["你好"],
                )
                for target_path in target_paths
            ]
        )

    report = await service.reset_translations(game_title="テストゲーム", reset_all=True)
    quality_report = await service.quality_report(game_title="テストゲーム")
    async with await registry.open_game("テストゲーム") as session:
        remaining_paths = await session.read_translation_location_paths()

    assert report.status == "warning"
    assert report.summary["mode"] == "all"
    assert report.summary["reset_count"] == len(target_paths)
    requested_count = report.summary["requested_count"]
    assert isinstance(requested_count, int)
    assert requested_count >= len(target_paths)
    assert all(target_path not in remaining_paths for target_path in target_paths)
    pending_count = quality_report.summary["pending_count"]
    assert isinstance(pending_count, int)
    assert pending_count >= len(target_paths)


@pytest.mark.asyncio
async def test_validate_placeholder_rules_previews_roundtrip() -> None:
    """占位符规则校验报告展示模型可见文本与还原结果。"""
    service = AgentToolkitService(setting_path=EXAMPLE_SETTING_PATH)

    report = await service.validate_placeholder_rules(
        game_title=None,
        custom_placeholder_rules_text='{"\\\\\\\\F\\\\[[^\\\\]]+\\\\]":"[CUSTOM_FACE_PORTRAIT_{index}]"}',
        sample_texts=[r"\F[GuideA]こんにちは\V[1]"],
    )

    assert report.status == "ok"
    assert report.summary["rule_count"] == 1
    samples = report.details["samples"]
    assert isinstance(samples, list)
    first_sample = samples[0]
    assert isinstance(first_sample, dict)
    assert first_sample["text_for_model"] == "[CUSTOM_FACE_PORTRAIT_1]こんにちは[RMMZ_VARIABLE_1]"
    assert first_sample["restored_text"] == r"\F[GuideA]こんにちは\V[1]"
    assert first_sample["roundtrip_ok"] is True


@pytest.mark.asyncio
async def test_validate_placeholder_rules_rejects_joined_prefix_overlap() -> None:
    """只保护无分隔符控制符前缀的规则不能通过完整 occurrence 往返校验。"""
    service = AgentToolkitService(setting_path=EXAMPLE_SETTING_PATH)

    report = await service.validate_placeholder_rules(
        game_title=None,
        custom_placeholder_rules_text=json.dumps(
            {r"\\Shake": "[CUSTOM_PLUGIN_SHAKE_MARKER_{index}]"},
            ensure_ascii=False,
        ),
        sample_texts=[r"\ShakeStop this!!!"],
    )

    assert report.status == "error"
    assert {error.code for error in report.errors} == {"placeholder_preview"}
    assert report.summary["sample_count"] == 1


@pytest.mark.asyncio
async def test_validate_placeholder_rules_blocks_bare_escape_match() -> None:
    """占位符规则不得误匹配裸 \\n、\\r、\\t 这类常见文本转义。"""
    service = AgentToolkitService(setting_path=EXAMPLE_SETTING_PATH)

    unsafe_report = await service.validate_placeholder_rules(
        game_title=None,
        custom_placeholder_rules_text=json.dumps(
            {r"(?i)\\N\d*": "[CUSTOM_PLUGIN_N_{index}]"},
            ensure_ascii=False,
        ),
        sample_texts=[r"\n"],
    )
    safe_report = await service.validate_placeholder_rules(
        game_title=None,
        custom_placeholder_rules_text=json.dumps(
            {r"(?i)\\N\d+": "[CUSTOM_PLUGIN_N_{index}]"},
            ensure_ascii=False,
        ),
        sample_texts=[r"\N12"],
    )

    assert unsafe_report.status == "error"
    assert {error.code for error in unsafe_report.errors} == {"placeholder_rule_matches_common_escape"}
    assert safe_report.status == "ok"


@pytest.mark.asyncio
async def test_validate_placeholder_rules_warns_unicode_control_boundary() -> None:
    """占位符校验会提示非 ASCII 控制符边界，避免 Agent 按终端乱码猜测。"""
    service = AgentToolkitService(setting_path=EXAMPLE_SETTING_PATH)

    report = await service.validate_placeholder_rules(
        game_title=None,
        custom_placeholder_rules_text="{}",
        sample_texts=[r"\F3[66」「ふーん……？」"],
    )

    warning_codes = {warning.code for warning in report.warnings}
    assert "unprotected_control_unicode_boundary" in warning_codes
    assert "U+300D" in report.warnings[0].message


@pytest.mark.asyncio
async def test_validate_event_command_rules_previews_direct_parameter_write_back(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """事件指令规则校验会预演 direct parameters[N] 命中项的回写。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), "CommonEvents.json")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    command = ensure_json_object(commands[4], "CommonEvents[1].list[4]")
    parameters = ensure_json_array(command["parameters"], "CommonEvents[1].list[4].parameters")
    parameters[2] = "トップパラメータ"
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)

    report = await service.validate_event_command_rules(
        game_title="テストゲーム",
        rules_text=json.dumps(
            {
                "357": [
                    {
                        "match": {
                            "0": "TestPlugin",
                            "1": "Show",
                        },
                        "paths": [
                            "$['parameters'][2]",
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )

    assert report.status == "ok"
    preview = ensure_json_object(report.details["write_back_preview"], "write_back_preview")
    assert preview["status"] == "ok"
    assert preview["checked_item_count"] == 1


@pytest.mark.asyncio
async def test_validate_event_command_rules_reports_hits_per_rule(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """事件指令规则报告按规则组统计命中数量，避免把总命中数写到每条规则。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    rules_text = json.dumps(
        {
            "357": [
                {
                    "match": {"0": "TestPlugin", "1": "Show"},
                    "paths": ["$['parameters'][3]['message']"],
                },
                {
                    "match": {"0": "ComplexPlugin", "1": "ShowWindow"},
                    "paths": [
                        "$['parameters'][3]['window']['title']",
                        "$['parameters'][3]['choices'][*]",
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )

    report = await service.validate_event_command_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )

    assert report.status == "ok"
    rule_details = ensure_json_array(report.details["rules"], "rules")
    hit_counts = [
        ensure_json_object(coerce_json_value(raw_detail), f"rules[{index}]")["hit_count"]
        for index, raw_detail in enumerate(rule_details)
    ]
    assert hit_counts == [1, 3]


@pytest.mark.asyncio
async def test_quality_report_counts_errors_and_model_response(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告读取译文、质量错误和规则状态，输出阻断级错误摘要。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    role="アリス",
                    original_lines=["こんにちは"],
                    source_line_paths=["CommonEvents.json/1/1"],
                    translation_lines=[r"\C[4]甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲"],
                )
            ],
        )
        run_record = await session.start_translation_run(
            total_extracted=3,
            pending_count=2,
            deduplicated_count=2,
            batch_count=1,
        )
        await session.write_translation_quality_errors(
            run_record.run_id,
            [
                TranslationErrorItem(
                    location_path="CommonEvents.json/1/2",
                    item_type="array",
                    role=None,
                    original_lines=["はい", "いいえ"],
                    translation_lines=[],
                    error_type="AI漏翻",
                    error_detail=["缺少键"],
                    model_response='{"bad": true}',
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    assert report.status == "error"
    assert report.summary["quality_error_count"] == 1
    assert report.summary["model_response_error_count"] == 1
    assert report.summary["placeholder_risk_count"] == 1
    assert report.summary["overwide_line_count"] == 1
    assert report.details["error_type_counts"] == {"AI漏翻": 1}
    quality_error_items = ensure_json_array(report.details["quality_error_items"], "quality_error_items")
    placeholder_items = ensure_json_array(report.details["placeholder_risk_items"], "placeholder_risk_items")
    overwide_items = ensure_json_array(report.details["overwide_line_items"], "overwide_line_items")
    quality_error_detail = ensure_json_object(quality_error_items[0], "quality_error_items[0]")
    placeholder_detail = ensure_json_object(placeholder_items[0], "placeholder_risk_items[0]")
    overwide_detail = ensure_json_object(overwide_items[0], "overwide_line_items[0]")
    assert quality_error_detail["location_path"] == "CommonEvents.json/1/2"
    assert quality_error_detail["error_type"] == "AI漏翻"
    assert placeholder_detail["location_path"] == "CommonEvents.json/1/0"
    assert overwide_detail["location_path"] == "CommonEvents.json/1/0"
    assert overwide_detail["line_width"] == 30


@pytest.mark.asyncio
async def test_quality_report_flags_internal_placeholder_leak(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告必须拦截译文里的项目内部占位符。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    role="アリス",
                    original_lines=["こんにちは"],
                    source_line_paths=["CommonEvents.json/1/1"],
                    translation_lines=["你好[RMMZ_TEXT_COLOR_0]"],
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    assert report.status == "error"
    assert report.summary["placeholder_risk_count"] == 1
    placeholder_items = ensure_json_array(report.details["placeholder_risk_items"], "placeholder_risk_items")
    placeholder_detail = ensure_json_object(placeholder_items[0], "placeholder_risk_items[0]")
    assert placeholder_detail["location_path"] == "CommonEvents.json/1/0"
    assert "译文残留项目内部占位符" in str(placeholder_detail["reason"])


@pytest.mark.asyncio
async def test_quality_report_accepts_saved_short_text_real_line_breaks(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告复查已保存译文时允许游戏文件需要的真实换行。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="Items.json/1/description",
                    item_type="short_text",
                    role=None,
                    original_lines=["説明\n本文"],
                    source_line_paths=["Items.json/1/description"],
                    translation_lines=["说明\n正文"],
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    assert report.summary["placeholder_risk_count"] == 0


@pytest.mark.asyncio
async def test_quality_report_allows_common_english_rpg_abbreviations(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """英文质量检查允许常见 RPG 与系统缩写保留在中文译文中。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("English Fixture Game") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    role="Guide",
                    original_lines=["Play the BGM before the NPC raises ATK."],
                    source_line_paths=["CommonEvents.json/1/1"],
                    translation_lines=["在 NPC 提升 ATK 前播放 BGM。"],
                )
            ],
        )

    report = await service.quality_report(game_title="English Fixture Game")

    assert report.summary["source_residual_count"] == 0
    assert report.details["source_residual_items"] == []


@pytest.mark.asyncio
async def test_quality_report_accepts_structured_placeholder_shell_and_rejects_changed_shell(
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告不把已保护外壳当英文残留，但会拦截被改坏的外壳。"""
    common_events_path = minimal_english_game_dir / "data" / "CommonEvents.json"
    raw_common_events = coerce_json_value(cast(object, json.loads(common_events_path.read_text(encoding="utf-8"))))
    common_events = ensure_json_array(raw_common_events, "CommonEvents.json")
    common_event = ensure_json_object(common_events[1], "CommonEvents.json[1]")
    command_list = ensure_json_array(common_event["list"], "CommonEvents.json[1].list")
    message_command = ensure_json_object(command_list[1], "CommonEvents.json[1].list[1]")
    message_parameters = ensure_json_array(
        message_command["parameters"],
        "CommonEvents.json[1].list[1].parameters",
    )
    message_parameters[0] = "<Mini Label: Alraune>"
    _ = common_events_path.write_text(
        json.dumps(raw_common_events, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_english_game_dir, source_language="en")
    structured_rule = StructuredPlaceholderRuleRecord(
        rule_name="MINI_LABEL",
        rule_type="paired_shell",
        pattern_text=r"(?P<open><Mini\s+Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)",
        translatable_group="text",
        protected_groups={
            "open": "[CUSTOM_MINI_LABEL_OPEN_{index}]",
            "close": "[CUSTOM_MINI_LABEL_CLOSE_{index}]",
        },
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("English Fixture Game") as session:
        await session.replace_structured_placeholder_rules([structured_rule])
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    role="Guide",
                    original_lines=["<Mini Label: Alraune>"],
                    source_line_paths=["CommonEvents.json/1/1"],
                    translation_lines=["<Mini Label: 阿尔劳娜>"],
                )
            ],
        )

    accepted_report = await service.quality_report(game_title="English Fixture Game")

    assert accepted_report.summary["placeholder_risk_count"] == 0
    assert accepted_report.summary["source_residual_count"] == 0

    async with await registry.open_game("English Fixture Game") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    role="Guide",
                    original_lines=["<Mini Label: Alraune>"],
                    source_line_paths=["CommonEvents.json/1/1"],
                    translation_lines=["<迷你标签: 阿尔劳娜>"],
                )
            ],
        )

    rejected_report = await service.quality_report(game_title="English Fixture Game")

    assert rejected_report.summary["placeholder_risk_count"] == 1


@pytest.mark.asyncio
async def test_structured_placeholder_rule_with_standard_control_passes_validation_and_quality(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """临时游戏副本中结构化壳内的内置控制符应被项目检查放行。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_common_events = cast(object, json.loads(common_events_path.read_text(encoding="utf-8")))
    common_events = ensure_json_array(coerce_json_value(raw_common_events), "CommonEvents.json")
    event = ensure_json_object(common_events[1], "CommonEvents.json[1]")
    commands = ensure_json_array(event["list"], "CommonEvents.json[1].list")
    commands.insert(
        2,
        {
            "code": 401,
            "parameters": [r"D_TEXT \c[17]決定ボタンを連打しろ！ 48"],
        },
    )
    _ = common_events_path.write_text(json.dumps(common_events, ensure_ascii=False), encoding="utf-8")

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    rules_text = json.dumps(
        {
            "paired_shell_rules": [
                {
                    "name": "D_TEXT_LABEL",
                    "pattern": r"(?P<open>^D_TEXT\s+)(?P<text>.*?)(?P<close>\s+48$)",
                    "translatable_group": "text",
                    "protected_groups": {
                        "open": "[CUSTOM_D_TEXT_OPEN_{index}]",
                        "close": "[CUSTOM_D_TEXT_CLOSE_{index}]",
                    },
                }
            ]
        },
        ensure_ascii=False,
    )

    validate_report = await service.validate_structured_placeholder_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
        sample_texts=[],
    )
    import_report = await service.import_structured_placeholder_rules(
        game_title="テストゲーム",
        rules_text=rules_text,
    )
    async with await registry.open_game("テストゲーム") as session:
        await session.write_translation_items(
            [
                TranslationItem(
                    location_path="CommonEvents.json/1/2",
                    item_type="long_text",
                    role="アリス",
                    original_lines=[r"D_TEXT \c[17]決定ボタンを連打しろ！ 48"],
                    source_line_paths=["CommonEvents.json/1/2"],
                    translation_lines=[r"D_TEXT \c[17]狂按决定键！ 48"],
                )
            ]
        )
    quality_report = await service.quality_report(game_title="テストゲーム")

    assert validate_report.errors == []
    assert import_report.errors == []
    assert quality_report.summary["placeholder_risk_count"] == 0
    assert quality_report.summary["source_residual_count"] == 0


def test_native_quality_reports_structured_placeholder_conflicts() -> None:
    """Rust 质检核心必须和 Python 文本规则一样拒绝结构化保护范围冲突。"""
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="en")
    text_rules = TextRules.from_setting(
        setting.text_rules,
        custom_placeholder_rules=(
            CustomPlaceholderRule.create(
                pattern_text=r">",
                placeholder_template="[CUSTOM_CLOSE_{index}]",
            ),
        ),
        structured_placeholder_rules=(
            StructuredPlaceholderRule.create(
                rule_name="MINI_LABEL",
                rule_type="paired_shell",
                pattern_text=r"(?P<open><Mini\s+Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)",
                translatable_group="text",
                protected_groups={
                    "open": "[CUSTOM_MINI_LABEL_OPEN_{index}]",
                    "close": "[CUSTOM_MINI_LABEL_CLOSE_{index}]",
                },
            ),
        ),
    )
    items = [
        TranslationItem(
            location_path="CommonEvents.json/1/0",
            item_type="long_text",
            role="Guide",
            original_lines=["<Mini Label: Alraune>"],
            source_line_paths=["CommonEvents.json/1/1"],
            translation_lines=["<Mini Label: 阿尔劳娜>"],
        )
    ]
    details = collect_native_quality_details(
        items=items,
        text_rules=text_rules,
        source_residual_rules=[],
    )
    counts = collect_native_quality_counts(
        items=items,
        text_rules=text_rules,
        source_residual_rules=[],
    )

    assert len(details.placeholder_risk_items) == 1
    assert counts.placeholder_risk_count == 1
    assert counts.source_residual_count == len(details.source_residual_items)
    assert counts.text_structure_count == len(details.text_structure_items)
    assert counts.overwide_line_count == len(details.overwide_line_items)
    assert "结构化占位符保护片段与已有控制符规则重叠" in json.dumps(
        details.placeholder_risk_items,
        ensure_ascii=False,
    )


def test_native_quality_accepts_structured_placeholder_lookahead_pattern() -> None:
    """Python 已校验的结构化正则能力，Rust 质检核心不能再用更窄子集误拒。"""
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="en")
    text_rules = TextRules.from_setting(
        setting.text_rules,
        structured_placeholder_rules=(
            StructuredPlaceholderRule.create(
                rule_name="LOOK_LABEL",
                rule_type="paired_shell",
                pattern_text=r"(?P<open><Label:\s*)(?P<text>[^<>\r\n]*?)(?P<close>>)(?!x)",
                translatable_group="text",
                protected_groups={
                    "open": "[CUSTOM_LOOK_LABEL_OPEN_{index}]",
                    "close": "[CUSTOM_LOOK_LABEL_CLOSE_{index}]",
                },
            ),
        ),
    )
    details = collect_native_quality_details(
        items=[
            TranslationItem(
                location_path="CommonEvents.json/1/0",
                item_type="long_text",
                role="Guide",
                original_lines=["<Label: Alice>"],
                source_line_paths=["CommonEvents.json/1/1"],
                translation_lines=["<Label: 爱丽丝>"],
            )
        ],
        text_rules=text_rules,
        source_residual_rules=[],
    )

    assert details.placeholder_risk_items == []
    assert details.source_residual_items == []


def test_native_quality_rejects_changed_long_control_candidate_hidden_by_standard_prefix() -> None:
    """Rust 质检不能让标准短控制符静默吞掉更长自定义候选。"""
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    text_rules = TextRules.from_setting(setting.text_rules)
    details = collect_native_quality_details(
        items=[
            TranslationItem(
                location_path="CommonEvents.json/1/0",
                item_type="long_text",
                role=None,
                original_lines=[r"\nn[Name]OK"],
                source_line_paths=["CommonEvents.json/1/0"],
                translation_lines=[r"\nn[Other]OK"],
            )
        ],
        text_rules=text_rules,
        source_residual_rules=[],
    )

    assert len(details.placeholder_risk_items) == 1
    assert r"\nn[Name]" in json.dumps(details.placeholder_risk_items, ensure_ascii=False)
    assert r"\nn[Other]" in json.dumps(details.placeholder_risk_items, ensure_ascii=False)


def test_native_quality_accepts_repeated_structured_shell_markers() -> None:
    """Rust 质检按原文顺序反查重复结构化外壳，不把所有外壳归到最后编号。"""
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    text_rules = TextRules.from_setting(
        setting.text_rules,
        structured_placeholder_rules=(
            StructuredPlaceholderRule.create(
                rule_name="BRACKET_TITLE",
                rule_type="paired_shell",
                pattern_text=r"(?P<open>【)(?P<text>[^【】\r\n]*?)(?P<close>】)",
                translatable_group="text",
                protected_groups={
                    "open": "[CUSTOM_BRACKET_TITLE_OPEN_{index}]",
                    "close": "[CUSTOM_BRACKET_TITLE_CLOSE_{index}]",
                },
            ),
        ),
    )
    details = collect_native_quality_details(
        items=[
            TranslationItem(
                location_path="Skills.json/282/description",
                item_type="short_text",
                role=None,
                original_lines=["【自身の我慢-5】【MP＋10】【相手の我慢　↑】"],
                source_line_paths=["Skills.json/282/description"],
                translation_lines=["【自身忍耐-5】【MP＋10】【对方忍耐　↑】"],
            )
        ],
        text_rules=text_rules,
        source_residual_rules=[],
    )

    assert details.placeholder_risk_items == []
    assert details.text_structure_items == []
    assert details.source_residual_items == []


def test_native_quality_rejects_extra_repeated_structured_shell_marker() -> None:
    """Rust 质检遇到额外同类结构化外壳时必须报告占位符风险。"""
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    text_rules = TextRules.from_setting(
        setting.text_rules,
        structured_placeholder_rules=(
            StructuredPlaceholderRule.create(
                rule_name="BRACKET_TITLE",
                rule_type="paired_shell",
                pattern_text=r"(?P<open>【)(?P<text>[^【】\r\n]*?)(?P<close>】)",
                translatable_group="text",
                protected_groups={
                    "open": "[CUSTOM_BRACKET_TITLE_OPEN_{index}]",
                    "close": "[CUSTOM_BRACKET_TITLE_CLOSE_{index}]",
                },
            ),
        ),
    )
    details = collect_native_quality_details(
        items=[
            TranslationItem(
                location_path="Skills.json/282/description",
                item_type="short_text",
                role=None,
                original_lines=["【自身の我慢-5】【MP＋10】"],
                source_line_paths=["Skills.json/282/description"],
                translation_lines=["【自身忍耐-5】【MP＋10】【额外】"],
            )
        ],
        text_rules=text_rules,
        source_residual_rules=[],
    )

    assert len(details.placeholder_risk_items) == 1
    assert "CUSTOM_UNEXPECTED_1" in json.dumps(
        details.placeholder_risk_items,
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_quality_report_flags_multiline_short_text_overwide_line(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告按单值文本的实际显示行检查 Note 标签超宽风险。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_value = coerce_json_value(cast(object, json.loads(items_path.read_text(encoding="utf-8"))))
    items = ensure_json_array(raw_value, "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = "<拡張説明:説明\n原文>"
    _ = items_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_note_tag_text_rules(
            [
                NoteTagTextRuleRecord(
                    file_name="Items.json",
                    tag_names=["拡張説明"],
                )
            ]
        )
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="Items.json/1/note/拡張説明",
                    item_type="short_text",
                    original_lines=["説明\n原文"],
                    translation_lines=["说明\n甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲"],
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    overwide_items = ensure_json_array(report.details["overwide_line_items"], "overwide_line_items")
    overwide_detail = ensure_json_object(overwide_items[0], "overwide_line_items[0]")
    assert report.summary["overwide_line_count"] == 1
    assert overwide_detail["location_path"] == "Items.json/1/note/拡張説明"
    assert overwide_detail["item_type"] == "short_text"
    assert overwide_detail["line_index"] == 1
    assert overwide_detail["line_width"] == 30


@pytest.mark.asyncio
async def test_quality_report_flags_literal_line_break_short_text_overwide_line(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告会把字面量反斜杠 n 也当作游戏显示换行检查行宽。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_value = coerce_json_value(cast(object, json.loads(items_path.read_text(encoding="utf-8"))))
    items = ensure_json_array(raw_value, "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = r"<拡張説明:説明\n原文>"
    _ = items_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_note_tag_text_rules(
            [
                NoteTagTextRuleRecord(
                    file_name="Items.json",
                    tag_names=["拡張説明"],
                )
            ]
        )
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="Items.json/1/note/拡張説明",
                    item_type="short_text",
                    original_lines=[r"説明\n原文"],
                    translation_lines=[r"说明\n甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲"],
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    overwide_items = ensure_json_array(report.details["overwide_line_items"], "overwide_line_items")
    overwide_detail = ensure_json_object(overwide_items[0], "overwide_line_items[0]")
    assert report.summary["overwide_line_count"] == 1
    assert overwide_detail["location_path"] == "Items.json/1/note/拡張説明"
    assert overwide_detail["item_type"] == "short_text"
    assert overwide_detail["line_index"] == 1
    assert overwide_detail["line_width"] == 30


@pytest.mark.asyncio
async def test_quality_report_allows_original_overwide_short_text_line(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """原文同一显示行本来很长时，单值文本不按普通对话框宽度误报。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_value = coerce_json_value(cast(object, json.loads(items_path.read_text(encoding="utf-8"))))
    items = ensure_json_array(raw_value, "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = "<拡張説明:説明\n原原原原原原原原原原原原原原原原原原原原原原原原原原原原原原>"
    _ = items_path.write_text(json.dumps(raw_value, ensure_ascii=False, indent=2), encoding="utf-8")
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_note_tag_text_rules(
            [
                NoteTagTextRuleRecord(
                    file_name="Items.json",
                    tag_names=["拡張説明"],
                )
            ]
        )
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="Items.json/1/note/拡張説明",
                    item_type="short_text",
                    original_lines=["説明\n原原原原原原原原原原原原原原原原原原原原原原原原原原原原原原"],
                    translation_lines=["说明\n甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲甲"],
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    assert report.summary["overwide_line_count"] == 0


@pytest.mark.asyncio
async def test_quality_report_flags_saved_short_text_structure_errors(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告会拦截已保存译文中改动单字段结构的问题。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="Items.json/1/description",
                    item_type="short_text",
                    original_lines=["アイテム説明"],
                    translation_lines=["说明\n额外一行"],
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    error_codes = {error.code for error in report.errors}
    text_structure_items = ensure_json_array(report.details["text_structure_items"], "text_structure_items")
    text_structure_detail = ensure_json_object(text_structure_items[0], "text_structure_items[0]")
    assert "text_structure" in error_codes
    assert report.summary["text_structure_count"] == 1
    assert text_structure_detail["location_path"] == "Items.json/1/description"


@pytest.mark.asyncio
async def test_quality_report_flags_saved_long_text_artifacts(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """质量报告会拦截已保存 long_text 中的异常空行和转义碎片。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    async with await registry.open_game("テストゲーム") as session:
        await _write_fresh_translation_items(
            service=service,
            session=session,
            items=[
                TranslationItem(
                    location_path="CommonEvents.json/1/0",
                    item_type="long_text",
                    role="アリス",
                    original_lines=["こんにちは", "怖がらなくていい"],
                    source_line_paths=["CommonEvents.json/1/1", "CommonEvents.json/1/2"],
                    translation_lines=[
                        "「不用那么害怕也行。",
                        "　看样子你是不习惯吧……？\\",
                        "",
                        "　来，把身体交给我吧。」",
                    ],
                )
            ],
        )

    report = await service.quality_report(game_title="テストゲーム")

    error_codes = {error.code for error in report.errors}
    text_structure_items = ensure_json_array(report.details["text_structure_items"], "text_structure_items")
    text_structure_detail = ensure_json_object(text_structure_items[0], "text_structure_items[0]")
    reason_text = str(text_structure_detail["reason"])
    assert "text_structure" in error_codes
    assert report.summary["text_structure_count"] == 1
    assert text_structure_detail["location_path"] == "CommonEvents.json/1/0"
    assert "原文没有空行" in reason_text
    assert "行尾裸反斜杠" in reason_text


def test_native_quality_accepts_long_text_empty_line_and_standard_controls() -> None:
    """Rust 质检允许原文需要的空行和正常 RPG Maker 控制符。"""
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    text_rules = TextRules.from_setting(setting.text_rules)
    details = collect_native_quality_details(
        items=[
            TranslationItem(
                location_path="CommonEvents.json/1/0",
                item_type="long_text",
                role="アリス",
                original_lines=[r"\N[1]\C[4]こんにちは\C[0]\!", "", r"\\"],
                source_line_paths=["CommonEvents.json/1/1"],
                translation_lines=[r"\N[1]\C[4]你好\C[0]\!", "", r"\\"],
            )
        ],
        text_rules=text_rules,
        source_residual_rules=[],
    )

    assert details.text_structure_items == []
    assert details.placeholder_risk_items == []
