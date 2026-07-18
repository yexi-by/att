"""RPG Maker MV/MZ 标准数据加载、提取与正文回写测试。"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest

from app.agent_toolkit.placeholder_scan import analyze_structured_placeholder_candidates
from app.application.errors import WriteBackGateError
from app.application.flow_gate import (
    event_command_rule_codes_for_setting,
    event_command_rule_scope_hash_for_setting,
    note_tag_rule_scope_hash_for_text_rules,
    structured_placeholder_scope_hash_from_analysis,
)
from app.application.font_replacement.files import read_plugins_js_file
from app.application.handler import TranslationHandler, build_translation_reuse_contexts_by_path
from app.application.summaries import WriteBackSummary
from app.config import SettingOverrides
from app.config.schemas import Setting, TextRulesSetting, WriteBackSetting
from app.game_analysis import build_game_analysis_context
from app.llm import LLMHandler
from app.native_write_plan import NativePlannedFile, NativeWriteBackPlan, NativeWriteBackSummary
from app.note_tag_text import NoteTagTextExtraction, build_note_tag_rule_records_from_import
from app.note_tag_text.exporter import collect_note_tag_candidates
from app.persistence import GameRegistry, TargetGameSession
from app.persistence.records import WriteTransactionPayload, WriteTransactionRecord
from app.plugin_source_text import (
    ActiveRuntimePluginSourceAudit,
    ActiveRuntimePluginSourceIssue,
    build_plugin_source_raw_index,
    build_plugin_source_rule_records_from_import,
    derive_plugin_source_scan,
    parse_plugin_source_rule_import_text,
)
from app.plugin_text import build_plugin_hash
from app.rmmz import (
    DataTextExtraction,
    GameFileView,
    load_active_runtime_game_data,
    load_game_data,
    load_game_data_for_view,
    read_game_title,
    resolve_game_layout,
)
from app.rmmz.control_codes import CustomPlaceholderRule
from app.rmmz.schema import (
    PLUGINS_FILE_NAME,
    EventCommandParameterFilter,
    EventCommandTextRuleRecord,
    GameData,
    MvVirtualNameboxRuleRecord,
    NoteTagTextRuleRecord,
    PlaceholderRuleRecord,
    PluginSourceRuntimeWriteMapRecord,
    PluginTextRuleRecord,
    TranslationErrorItem,
    TranslationItem,
)
from app.rmmz.source_snapshot import validate_source_snapshot_manifest
from app.rmmz.text_rules import (
    JsonValue,
    TextRules,
    coerce_json_value,
    ensure_json_array,
    ensure_json_object,
    get_default_text_rules,
)
from app.rule_review import (
    EVENT_COMMAND_TEXT_RULE_DOMAIN,
    NOTE_TAG_TEXT_RULE_DOMAIN,
    PLACEHOLDER_RULE_DOMAIN,
    PLUGIN_TEXT_RULE_DOMAIN,
    STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
    plugin_rule_scope_hash,
    plugin_source_rule_scope_hash,
    plugin_source_text_rules_hash,
)
from app.terminology import TerminologyGlossary, TerminologyPromptIndex, TerminologyRegistry
from app.text_scope import TextScopeResult
from app.translation import TranslationCache, prepare_translation_cache_for_scope
from app.utils.config_loader_utils import load_setting
from tests._native_write_plan_helper import reset_writable_copies, write_data_text, write_game_files


def _rewrite_json(path: Path, value: JsonValue) -> None:
    """以 UTF-8 写回测试 JSON。"""
    _ = path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_test_json(path: Path) -> JsonValue:
    """读取测试 JSON 并收窄为项目 JSON 类型。"""
    return coerce_json_value(cast(object, json.loads(path.read_text(encoding="utf-8"))))


def _translated_test_line_preserving_controls(line: str, text_rules: TextRules) -> str:
    """生成测试译文，并保留原文中必须原样保留的控制符。"""
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


def _mv_virtual_namebox_rule_records() -> list[MvVirtualNameboxRuleRecord]:
    """生成测试用 MV 虚拟名字框外部规则。"""
    return [
        MvVirtualNameboxRuleRecord(
            rule_order=0,
            rule_name="quote-inline",
            pattern_text=r"^(?P<speaker>[^\\「（:：<>\r\n]{1,40})\s*(?P<connector>[:：]?「)(?P<body>.*)$",
            speaker_group="speaker",
            body_group="body",
            speaker_policy="translate",
            render_template="{speaker}{connector}{body}",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=1,
            rule_name="standalone-colon",
            pattern_text=r"^(?P<speaker>[^\\「『【\[\]()（）:：\r\n]{1,40})\s*[:：]\s*$",
            speaker_group="speaker",
            body_group="",
            speaker_policy="translate",
            render_template="{speaker}：",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=2,
            rule_name="actor-inline",
            pattern_text=r"^(?P<speaker>\\[Nn]\[(?P<actor_id>1)\])(?P<separator>[:：])(?P<body>.*)$",
            speaker_group="speaker",
            body_group="body",
            speaker_policy="actor_name",
            render_template="{speaker}{separator}{body}",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=3,
            rule_name="yep-inline",
            pattern_text=r"^(?P<command>\\(?:[Nn](?:[CcRr])?|[Rr]))<(?P<speaker>[^>\r\n]{1,80})>(?P<body>.*)$",
            speaker_group="speaker",
            body_group="body",
            speaker_policy="translate",
            render_template="{command}<{speaker}>{body}",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=4,
            rule_name="angle-standalone",
            pattern_text=r"^<(?P<speaker>[^\\<>\r\n]{1,80})>\s*$",
            speaker_group="speaker",
            body_group="",
            speaker_policy="translate",
            render_template="<{speaker}>",
        ),
        MvVirtualNameboxRuleRecord(
            rule_order=5,
            rule_name="dynamic-angle",
            pattern_text=r"^<(?P<speaker>\\[Nn]\[\d+\])>\s*$",
            speaker_group="speaker",
            body_group="",
            speaker_policy="preserve",
            render_template="<{speaker}>",
        ),
    ]


async def _prepare_write_gate_session(
    *,
    session: TargetGameSession,
    game_dir: Path,
    registry: TerminologyRegistry | None = None,
    glossary: TerminologyGlossary | None = None,
) -> tuple[GameData, Setting, TextRules]:
    """让最小游戏通过写文件前置规则，便于测试特定写入风险。"""
    game_data = await load_game_data(game_dir)
    setting = load_setting(
        source_language=session.source_language,
        additional_source_languages=session.additional_source_languages,
    )
    placeholder_record = PlaceholderRuleRecord(
        pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
        placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
    )
    await session.replace_terminology_bundle(
        registry=registry or TerminologyRegistry(),
        glossary=glossary or TerminologyGlossary(),
    )
    await session.replace_placeholder_rules([placeholder_record])
    text_rules = TextRules.from_setting(
        setting.text_rules,
        custom_placeholder_rules=(
            CustomPlaceholderRule.create(
                pattern_text=placeholder_record.pattern_text,
                placeholder_template=placeholder_record.placeholder_template,
            ),
        ),
        structured_placeholder_rules=(),
    )
    plugin_source_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    await session.replace_plugin_source_assessment(
        source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
        text_rules_hash=plugin_source_text_rules_hash(text_rules),
        high_risk=plugin_source_scan.risk.high_risk,
        candidate_count=len(plugin_source_scan.candidates),
        summary=plugin_source_scan.risk_report_json(),
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
        scope_payload={
            "kind": "event_command_codes",
            "command_codes": sorted(
                event_command_rule_codes_for_setting(
                    game_data=game_data,
                    setting=setting,
                )
            ),
        },
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
    analysis_context = await build_game_analysis_context(
        session=session,
        game_data=game_data,
        text_rules=text_rules,
    )
    scope = analysis_context.scope
    await session.replace_rule_review_state(
        rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
        scope_hash=structured_placeholder_scope_hash_from_analysis(
            analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
        ),
        reviewed_empty=True,
    )
    await session.replace_rule_review_state(
        rule_domain=PLACEHOLDER_RULE_DOMAIN,
        scope_hash="placeholder-rules-imported",
        reviewed_empty=False,
    )
    return game_data, setting, text_rules


async def _write_current_translation_items(
    *,
    session: TargetGameSession,
    game_data: GameData,
    setting: Setting,
    text_rules: TextRules,
    scope: TextScopeResult,
    items: list[TranslationItem],
) -> None:
    """按生产上下文契约保存测试译文，不伪造旧版无指纹记录。"""
    plugin_source_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    await session.replace_plugin_source_assessment(
        source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
        text_rules_hash=plugin_source_text_rules_hash(text_rules),
        high_risk=plugin_source_scan.risk.high_risk,
        candidate_count=len(plugin_source_scan.candidates),
        summary=plugin_source_scan.risk_report_json(),
    )
    glossary = await session.read_terminology_glossary()
    if glossary is None:
        raise RuntimeError("测试写译文前必须准备术语表")
    translation_cache = TranslationCache()
    prepare_translation_cache_for_scope(
        translation_cache=translation_cache,
        scope=scope,
        terminology_prompt_index=TerminologyPromptIndex.from_glossary(glossary, game_data=game_data),
        source_language=setting.text_rules.source_language,
        additional_source_languages=setting.text_rules.additional_source_languages,
        target_language=session.target_language,
        source_snapshot_records=await session.read_source_snapshot_records(),
        source_residual_records=await session.read_source_residual_rules(),
    )
    await session.write_translation_items(
        items,
        reuse_contexts_by_path=build_translation_reuse_contexts_by_path(
            translation_cache=translation_cache,
            items=items,
        ),
    )


class _NativePlanSessionStub:
    """测试 Rust 写回计划应用层协议的会话桩。"""

    game_path: Path
    db_path: Path
    content_root: Path
    runtime_map_replace_count: int
    runtime_map_replace_calls: int
    font_record_replace_count: int
    write_transaction_record: WriteTransactionRecord | None

    def __init__(self, tmp_path: Path) -> None:
        """初始化可满足写回 helper 的最小会话字段。"""
        self.game_path = tmp_path / "game"
        self.db_path = tmp_path / "game.db"
        self.content_root = tmp_path / "game"
        self.content_root.mkdir(parents=True)
        self.runtime_map_replace_count = 0
        self.runtime_map_replace_calls = 0
        self.font_record_replace_count = 0
        self.write_transaction_record = None

    async def replace_plugin_source_runtime_write_maps(self, records: list[object]) -> None:
        """记录插件源码当前运行映射是否被替换。"""
        self.runtime_map_replace_calls += 1
        self.runtime_map_replace_count = len(records)

    async def replace_font_replacement_records(self, records: list[object]) -> None:
        """测试中不触发字体记录替换。"""
        _ = records

    async def create_write_transaction(self, record: WriteTransactionRecord) -> None:
        """记录文件暂存前持久化的 preparing 状态。"""
        assert self.write_transaction_record is None
        assert record.state == "preparing"
        assert record.payload is None
        self.write_transaction_record = record

    async def read_write_transaction(self, transaction_id: str) -> WriteTransactionRecord | None:
        """读取测试会话中的当前写事务。"""
        record = self.write_transaction_record
        if record is None or record.transaction_id != transaction_id:
            return None
        return record

    async def mark_write_transaction_prepared(
        self,
        transaction_id: str,
        payload: WriteTransactionPayload,
    ) -> None:
        """记录暂存完成后的数据库恢复清单。"""
        assert self.write_transaction_record is not None
        assert transaction_id == self.write_transaction_record.transaction_id
        assert not payload.database_committed
        self.write_transaction_record.state = "prepared"
        self.write_transaction_record.payload = payload

    async def finalize_write_transaction_commit(
        self,
        *,
        transaction_id: str,
        runtime_maps: list[PluginSourceRuntimeWriteMapRecord],
        font_records: list[object] | None,
    ) -> None:
        """模拟诊断映射和 committed 状态的原子提交。"""
        assert self.write_transaction_record is not None
        assert transaction_id == self.write_transaction_record.transaction_id
        self.runtime_map_replace_calls += 1
        self.runtime_map_replace_count = len(runtime_maps)
        if font_records is not None:
            self.font_record_replace_count = len(font_records)
        assert self.write_transaction_record.payload is not None
        self.write_transaction_record.payload = WriteTransactionPayload(
            version=self.write_transaction_record.payload.version,
            database_committed=True,
            files=self.write_transaction_record.payload.files,
        )
        self.write_transaction_record.state = "committed"

    async def mark_write_transaction_finalized(self, transaction_id: str) -> None:
        """记录文件事务产物已清理。"""
        assert self.write_transaction_record is not None
        assert transaction_id == self.write_transaction_record.transaction_id
        self.write_transaction_record.state = "finalized"

    async def mark_write_transaction_rolled_back(
        self,
        transaction_id: str,
        error: str = "",
    ) -> None:
        """记录失败路径已恢复原文件。"""
        assert self.write_transaction_record is not None
        assert transaction_id == self.write_transaction_record.transaction_id
        self.write_transaction_record.state = "rolled_back"
        self.write_transaction_record.error = error

    async def mark_write_transaction_recovery_required(
        self,
        transaction_id: str,
        error: str,
    ) -> None:
        """记录无法自动恢复的测试失败。"""
        assert self.write_transaction_record is not None
        assert transaction_id == self.write_transaction_record.transaction_id
        self.write_transaction_record.state = "recovery_required"
        self.write_transaction_record.error = error


def _empty_active_runtime_audit() -> ActiveRuntimePluginSourceAudit:
    """构造没有问题的当前运行文件审计结果。"""
    return ActiveRuntimePluginSourceAudit(
        issues=(),
        text_issue_audit_enabled=True,
        scanned_file_count=0,
        active_file_count=0,
        literal_count=0,
        active_literal_count=0,
        read_error_file_count=0,
    )


def _fake_empty_active_runtime_audit(
    **kwargs: object,
) -> ActiveRuntimePluginSourceAudit:
    """返回空审计结果，并为 monkeypatch 提供完整类型签名。"""
    _ = kwargs
    return _empty_active_runtime_audit()


@pytest.mark.asyncio
async def test_native_write_back_helper_applies_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通写回快路径必须应用 Rust 计划并执行事务替换。"""
    session = _NativePlanSessionStub(tmp_path)
    statuses: list[str] = []

    def fake_build_native_write_back_plan(**kwargs: object) -> NativeWriteBackPlan:
        """返回最小 Rust 写回计划，并记录调用模式。"""
        assert kwargs["mode"] == "write_back"
        content_output_dir = kwargs["content_output_dir"]
        assert isinstance(content_output_dir, Path)
        assert content_output_dir.is_dir()
        return NativeWriteBackPlan(
            files=[
                NativePlannedFile(
                    target_path=session.content_root / "data" / "System.json",
                    relative_path="data/System.json",
                    content='{"gameTitle":"测试"}\n',
                )
            ],
            plugin_source_runtime_write_maps=[],
            font_replacement_records=[],
            summary=NativeWriteBackSummary(
                data_item_count=1,
                plugin_item_count=0,
                terminology_written_count=0,
                target_font_name=None,
                source_font_count=0,
                replaced_font_reference_count=0,
                font_copied=False,
                planned_file_count=1,
                skipped_file_count=0,
            ),
            timings_ms={"total": 1, "active_runtime_audit": 12345},
        )

    async def fake_load_active_runtime_game_data(game_path: Path) -> GameData:
        """模拟加载尚未替换到真实目录的暂存运行视图。"""
        assert game_path != session.game_path
        assert (game_path / "data" / "System.json").read_text(encoding="utf-8") == '{"gameTitle":"测试"}\n'
        assert not (session.content_root / "data" / "System.json").exists()
        return cast(GameData, cast(object, SimpleNamespace()))

    def fake_audit_active_runtime_plugin_source(
        *,
        game_data: GameData,
        text_rules: TextRules,
        runtime_write_map_records: list[PluginSourceRuntimeWriteMapRecord],
        audit_text_issues: bool,
    ) -> ActiveRuntimePluginSourceAudit:
        """模拟当前运行文件审计通过。"""
        _ = game_data
        _ = text_rules
        assert runtime_write_map_records == []
        assert audit_text_issues is False
        return _empty_active_runtime_audit()

    monkeypatch.setattr("app.application.handler.build_native_write_back_plan", fake_build_native_write_back_plan)
    monkeypatch.setattr("app.application.handler.load_active_runtime_game_data", fake_load_active_runtime_game_data)
    monkeypatch.setattr(
        "app.application.handler.audit_active_runtime_plugin_source", fake_audit_active_runtime_plugin_source
    )

    handler = TranslationHandler(GameRegistry(tmp_path / "db"), LLMHandler())
    try:
        summary = await handler.write_runtime_files_with_native_plan(
            session=cast(TargetGameSession, cast(object, session)),
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None, statuses.append),
            setting=cast(
                Setting,
                cast(
                    object,
                    SimpleNamespace(
                        text_rules=TextRulesSetting(),
                        write_back=WriteBackSetting(),
                    ),
                ),
            ),
            text_rules=TextRules.from_setting(TextRulesSetting()),
            mode="write_back",
            writable_location_paths=[],
            confirm_font_overwrite=False,
            success_phase="游戏文本回写完成",
        )
    finally:
        await handler.close()

    assert summary.data_item_count == 1
    assert (session.content_root / "data" / "System.json").read_text(encoding="utf-8") == '{"gameTitle":"测试"}\n'
    assert summary.post_write_audit_ms < 12345
    assert statuses == [
        "准备 Rust 写回计划输入",
        "生成 Rust 写回计划",
        "暂存并校验游戏运行文件",
        "审计暂存运行视图",
        "原子替换游戏运行文件",
        "校验已替换文件哈希",
        "提交写事务诊断状态",
    ]
    assert session.runtime_map_replace_calls == 1
    assert session.runtime_map_replace_count == 0
    assert session.write_transaction_record is not None
    assert session.write_transaction_record.state == "finalized"
    assert not list(session.content_root.rglob("*.att-mz-write-*"))


@pytest.mark.asyncio
async def test_native_write_back_helper_includes_font_and_css_in_same_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """字体文件、CSS 和首次原件留档必须与运行文件共用同一事务。"""
    session = _NativePlanSessionStub(tmp_path)
    font_directory = session.content_root / "fonts"
    font_directory.mkdir()
    css_path = font_directory / "gamefont.css"
    original_css = "@font-face { font-family: GameFont; src: url('OldFont.woff'); }\n"
    _ = css_path.write_text(original_css, encoding="utf-8")
    replacement_font_path = tmp_path / "Replacement.ttf"
    _ = replacement_font_path.write_bytes(b"replacement font")

    def fake_build_setting_payload(
        _handler: TranslationHandler,
        **kwargs: object,
    ) -> tuple[dict[str, object], Path, list[str]]:
        """固定返回已确认的字体覆盖输入。"""
        _ = kwargs
        return {}, replacement_font_path, ["OldFont.woff"]

    def fake_build_native_write_back_plan(**kwargs: object) -> NativeWriteBackPlan:
        """让本测试只观察 Python 负责的字体文件事务。"""
        _ = kwargs
        return NativeWriteBackPlan(
            files=[],
            plugin_source_runtime_write_maps=[],
            font_replacement_records=[],
            summary=NativeWriteBackSummary(
                data_item_count=0,
                plugin_item_count=0,
                terminology_written_count=0,
                target_font_name="Replacement.ttf",
                source_font_count=1,
                replaced_font_reference_count=0,
                font_copied=True,
                planned_file_count=0,
                skipped_file_count=0,
            ),
            timings_ms={"total": 1},
        )

    async def fake_load_active_runtime_game_data(game_path: Path) -> GameData:
        assert game_path != session.game_path
        assert (game_path / "fonts" / "Replacement.ttf").read_bytes() == b"replacement font"
        assert (game_path / "fonts" / "gamefont_origin.css").read_text(encoding="utf-8") == original_css
        assert "Replacement.ttf" in (game_path / "fonts" / "gamefont.css").read_text(encoding="utf-8")
        assert not (font_directory / "Replacement.ttf").exists()
        assert not (font_directory / "gamefont_origin.css").exists()
        assert css_path.read_text(encoding="utf-8") == original_css
        return cast(GameData, cast(object, SimpleNamespace()))

    monkeypatch.setattr(
        TranslationHandler,
        "_build_native_write_back_setting_payload",
        fake_build_setting_payload,
    )
    monkeypatch.setattr(
        "app.application.handler.build_native_write_back_plan",
        fake_build_native_write_back_plan,
    )
    monkeypatch.setattr(
        "app.application.handler.load_active_runtime_game_data",
        fake_load_active_runtime_game_data,
    )
    monkeypatch.setattr(
        "app.application.handler.audit_active_runtime_plugin_source",
        _fake_empty_active_runtime_audit,
    )

    handler = TranslationHandler(GameRegistry(tmp_path / "db"), LLMHandler())
    try:
        summary = await handler.write_runtime_files_with_native_plan(
            session=cast(TargetGameSession, cast(object, session)),
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None),
            setting=cast(
                Setting,
                cast(
                    object,
                    SimpleNamespace(
                        text_rules=TextRulesSetting(),
                        write_back=WriteBackSetting(),
                    ),
                ),
            ),
            text_rules=TextRules.from_setting(TextRulesSetting()),
            mode="write_back",
            writable_location_paths=[],
            confirm_font_overwrite=True,
            success_phase="游戏文本回写完成",
        )
    finally:
        await handler.close()

    assert (font_directory / "Replacement.ttf").read_bytes() == b"replacement font"
    assert (font_directory / "gamefont_origin.css").read_text(encoding="utf-8") == original_css
    assert "Replacement.ttf" in css_path.read_text(encoding="utf-8")
    assert summary.planned_file_count == 3
    assert summary.replaced_font_reference_count == 1
    assert session.font_record_replace_count == 1
    assert session.write_transaction_record is not None
    assert session.write_transaction_record.state == "finalized"


@pytest.mark.asyncio
async def test_native_write_back_helper_commits_diagnostics_when_files_are_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有文件差异时，运行映射与事务完成状态仍须原子提交。"""
    session = _NativePlanSessionStub(tmp_path)
    statuses: list[str] = []

    def fake_build_native_write_back_plan(**kwargs: object) -> NativeWriteBackPlan:
        _ = kwargs
        return NativeWriteBackPlan(
            files=[],
            plugin_source_runtime_write_maps=[],
            font_replacement_records=[],
            summary=NativeWriteBackSummary(
                data_item_count=0,
                plugin_item_count=0,
                terminology_written_count=0,
                target_font_name=None,
                source_font_count=0,
                replaced_font_reference_count=0,
                font_copied=False,
                planned_file_count=0,
                skipped_file_count=1,
            ),
            timings_ms={"total": 1},
        )

    async def fake_load_active_runtime_game_data(game_path: Path) -> GameData:
        assert game_path == session.game_path
        return cast(GameData, cast(object, SimpleNamespace()))

    monkeypatch.setattr(
        "app.application.handler.build_native_write_back_plan",
        fake_build_native_write_back_plan,
    )
    monkeypatch.setattr(
        "app.application.handler.load_active_runtime_game_data",
        fake_load_active_runtime_game_data,
    )
    monkeypatch.setattr(
        "app.application.handler.audit_active_runtime_plugin_source",
        _fake_empty_active_runtime_audit,
    )

    handler = TranslationHandler(GameRegistry(tmp_path / "db"), LLMHandler())
    try:
        summary = await handler.write_runtime_files_with_native_plan(
            session=cast(TargetGameSession, cast(object, session)),
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None, statuses.append),
            setting=cast(
                Setting,
                cast(
                    object,
                    SimpleNamespace(
                        text_rules=TextRulesSetting(),
                        write_back=WriteBackSetting(),
                    ),
                ),
            ),
            text_rules=TextRules.from_setting(TextRulesSetting()),
            mode="write_back",
            writable_location_paths=[],
            confirm_font_overwrite=False,
            success_phase="游戏文本回写完成",
        )
    finally:
        await handler.close()

    assert summary.planned_file_count == 0
    assert session.runtime_map_replace_calls == 1
    assert session.write_transaction_record is not None
    assert session.write_transaction_record.state == "finalized"
    assert statuses == [
        "准备 Rust 写回计划输入",
        "生成 Rust 写回计划",
        "审计写入后的当前运行文件",
        "提交写事务诊断状态",
    ]


@pytest.mark.asyncio
async def test_native_write_back_helper_rolls_back_before_saving_runtime_map_on_audit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入后审计失败时连同字体副作用回滚，且不提交新诊断记录。"""
    session = _NativePlanSessionStub(tmp_path)
    events: list[str] = []
    font_directory = session.content_root / "fonts"
    font_directory.mkdir()
    css_path = font_directory / "gamefont.css"
    original_css = "@font-face { font-family: GameFont; src: url('OldFont.woff'); }\n"
    _ = css_path.write_text(original_css, encoding="utf-8")
    replacement_font_path = tmp_path / "Replacement.ttf"
    _ = replacement_font_path.write_bytes(b"replacement font")

    def fake_build_setting_payload(
        _handler: TranslationHandler,
        **kwargs: object,
    ) -> tuple[dict[str, object], Path, list[str]]:
        _ = kwargs
        return {}, replacement_font_path, ["OldFont.woff"]

    def fake_build_native_write_back_plan(**kwargs: object) -> NativeWriteBackPlan:
        """返回会触发写入后审计失败的最小计划。"""
        assert kwargs["mode"] == "write_back"
        content_output_dir = kwargs["content_output_dir"]
        assert isinstance(content_output_dir, Path)
        assert content_output_dir.is_dir()
        return NativeWriteBackPlan(
            files=[
                NativePlannedFile(
                    target_path=session.content_root / "js" / "plugins" / "Broken.js",
                    relative_path="js/plugins/Broken.js",
                    content="if (\n",
                )
            ],
            plugin_source_runtime_write_maps=[],
            font_replacement_records=[],
            summary=NativeWriteBackSummary(
                data_item_count=0,
                plugin_item_count=1,
                terminology_written_count=0,
                target_font_name=None,
                source_font_count=0,
                replaced_font_reference_count=0,
                font_copied=False,
                planned_file_count=1,
                skipped_file_count=0,
            ),
            timings_ms={"total": 1, "active_runtime_audit": 999},
        )

    async def fake_load_active_runtime_game_data(game_path: Path) -> GameData:
        """记录审计暂存运行视图，并确认真实目录尚未变更。"""
        assert game_path != session.game_path
        assert (game_path / "js" / "plugins" / "Broken.js").read_text(encoding="utf-8") == "if (\n"
        assert not (session.content_root / "js" / "plugins" / "Broken.js").exists()
        assert not (font_directory / "Replacement.ttf").exists()
        assert not (font_directory / "gamefont_origin.css").exists()
        assert css_path.read_text(encoding="utf-8") == original_css
        events.append("load")
        return cast(GameData, cast(object, SimpleNamespace()))

    def fake_audit_active_runtime_plugin_source(
        *,
        game_data: GameData,
        text_rules: TextRules,
        runtime_write_map_records: list[PluginSourceRuntimeWriteMapRecord],
        audit_text_issues: bool,
    ) -> ActiveRuntimePluginSourceAudit:
        """模拟当前运行文件审计发现 JS 语法错误。"""
        _ = game_data
        _ = text_rules
        assert runtime_write_map_records == []
        assert audit_text_issues is False
        events.append("audit")
        return ActiveRuntimePluginSourceAudit(
            issues=(
                ActiveRuntimePluginSourceIssue(
                    code="active_runtime_syntax_error",
                    message="当前游戏运行文件里的插件源码无法完成 JS 语法检查",
                    file_name="Broken.js",
                    syntax_error="RuntimeError: 原生 AST 解析报告 JS 语法错误",
                ),
            ),
            text_issue_audit_enabled=True,
            scanned_file_count=1,
            active_file_count=1,
            literal_count=0,
            active_literal_count=0,
            read_error_file_count=0,
        )

    monkeypatch.setattr("app.application.handler.build_native_write_back_plan", fake_build_native_write_back_plan)
    monkeypatch.setattr(
        TranslationHandler,
        "_build_native_write_back_setting_payload",
        fake_build_setting_payload,
    )
    monkeypatch.setattr("app.application.handler.load_active_runtime_game_data", fake_load_active_runtime_game_data)
    monkeypatch.setattr(
        "app.application.handler.audit_active_runtime_plugin_source", fake_audit_active_runtime_plugin_source
    )

    handler = TranslationHandler(GameRegistry(tmp_path / "db"), LLMHandler())
    try:
        with pytest.raises(WriteBackGateError, match="写入后当前运行文件审计未通过"):
            _ = await handler.write_runtime_files_with_native_plan(
                session=cast(TargetGameSession, cast(object, session)),
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
                setting=cast(
                    Setting,
                    cast(
                        object,
                        SimpleNamespace(
                            text_rules=TextRulesSetting(),
                            write_back=WriteBackSetting(),
                        ),
                    ),
                ),
                text_rules=TextRules.from_setting(TextRulesSetting()),
                mode="write_back",
                writable_location_paths=[],
                confirm_font_overwrite=True,
                success_phase="游戏文本回写完成",
            )
    finally:
        await handler.close()

    assert events == ["load", "audit"]
    assert session.runtime_map_replace_calls == 0
    assert session.runtime_map_replace_count == 0
    assert session.write_transaction_record is not None
    assert session.write_transaction_record.state == "rolled_back"
    assert not (session.content_root / "js" / "plugins" / "Broken.js").exists()
    assert not (font_directory / "Replacement.ttf").exists()
    assert not (font_directory / "gamefont_origin.css").exists()
    assert css_path.read_text(encoding="utf-8") == original_css
    assert session.font_record_replace_count == 0


@pytest.mark.asyncio
async def test_loader_only_keeps_standard_rmmz_data_files(minimal_game_dir: Path) -> None:
    """加载器接收官方 data 文件，并跳过未知插件衍生 JSON。"""
    game_data = await load_game_data(minimal_game_dir)

    assert "UnknownPluginData.json" not in game_data.data
    assert "System.json" in game_data.data
    assert "Map001.json" in game_data.map_data
    assert "Map002.json" in game_data.map_data
    assert game_data.plugins_js[0]["name"] == "TestPlugin"
    assert game_data.plugins_js[1]["name"] == "ComplexPlugin"


@pytest.mark.asyncio
async def test_loader_and_font_reader_accept_json5_plugins_array(minimal_game_dir: Path) -> None:
    """加载与字体恢复共用 Rust/json5 解析能力，支持标准 JS 对象写法。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    _ = plugins_path.write_text(
        """var $plugins = [
  {name: 'TestPlugin', status: true, description: '説明', parameters: {Message: '本文',},},
];
""",
        encoding="utf-8",
    )

    game_data = await load_game_data(minimal_game_dir)
    font_plugins = read_plugins_js_file(plugins_path)

    assert game_data.plugins_js == font_plugins
    assert game_data.plugins_js[0]["name"] == "TestPlugin"
    assert game_data.plugins_js[0]["parameters"] == {"Message": "本文"}


@pytest.mark.asyncio
async def test_mv_outer_layout_loads_www_data_and_system_title(minimal_mv_game_dir: Path) -> None:
    """MV 外层目录布局会定位到 www 内容目录，并用 System 标题兜底注册。"""
    layout = resolve_game_layout(minimal_mv_game_dir)
    game_data = await load_game_data(minimal_mv_game_dir)

    assert layout.engine_kind == "mv"
    assert layout.engine_version == "1.6.1"
    assert layout.content_root == minimal_mv_game_dir / "www"
    assert layout.data_dir == minimal_mv_game_dir / "www" / "data"
    assert read_game_title(minimal_mv_game_dir) == "MVテストゲーム"
    assert game_data.layout.engine_kind == "mv"
    assert "Map001.json" in game_data.map_data
    assert game_data.plugins_js[0]["name"] == "MvPlugin"


@pytest.mark.asyncio
async def test_mv_data_extraction_reads_role_from_first_401(
    minimal_mv_game_dir: Path,
) -> None:
    """MV 对话会从首条正文协议提取内部说话人。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.extend(
        [
            {
                "id": 2,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["案内人「こんにちは」"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 3,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["案内人："]},
                    {"code": 401, "parameters": ["次の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 4,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\N[1]:"]},
                    {"code": 401, "parameters": ["役者の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 5,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\n<店員>いらっしゃいませ"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 6,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\n[999]:普通の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 7,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2, "誤った名前"]},
                    {"code": 401, "parameters": ["案内人「第五参数を無視します」"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 8,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["まだやるかい？掛け金はそのままだぜ？（掛け金\\V[48])"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 9,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<案内人>"]},
                    {"code": 401, "parameters": ["独立行の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 10,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\N<店員>大文字制御の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 11,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<\\n[1]>"]},
                    {"code": 401, "parameters": ["動的名の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 12,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["こちらはステラ（1戦目）の回想です。"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 13,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<ステラ><ソフィア>"]},
                    {"code": 401, "parameters": ["複合名の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
        ]
    )
    _rewrite_json(common_events_path, common_events)

    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    extracted = DataTextExtraction(
        game_data,
        get_default_text_rules(),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    ).extract_all_text()
    items_by_path = {item.location_path: item for data in extracted.values() for item in data.translation_items}

    assert items_by_path["CommonEvents.json/1/0"].role == "旁白"
    assert items_by_path["CommonEvents.json/2/0"].role == "案内人"
    assert items_by_path["CommonEvents.json/2/0"].original_lines == ["こんにちは」"]
    assert items_by_path["CommonEvents.json/2/0"].source_line_paths == ["CommonEvents.json/2/1"]
    assert items_by_path["CommonEvents.json/3/0"].role == "案内人"
    assert items_by_path["CommonEvents.json/3/0"].original_lines == ["次の本文です"]
    assert items_by_path["CommonEvents.json/3/0"].source_line_paths == ["CommonEvents.json/3/2"]
    assert items_by_path["CommonEvents.json/4/0"].role == "MV勇者"
    assert items_by_path["CommonEvents.json/4/0"].original_lines == ["役者の本文です"]
    assert items_by_path["CommonEvents.json/5/0"].role == "店員"
    assert items_by_path["CommonEvents.json/5/0"].original_lines == ["いらっしゃいませ"]
    assert items_by_path["CommonEvents.json/6/0"].role == "旁白"
    assert items_by_path["CommonEvents.json/6/0"].original_lines == ["\\n[999]:普通の本文です"]
    assert items_by_path["CommonEvents.json/7/0"].role == "案内人"
    assert items_by_path["CommonEvents.json/7/0"].original_lines == ["第五参数を無視します」"]
    assert items_by_path["CommonEvents.json/8/0"].role == "旁白"
    assert items_by_path["CommonEvents.json/8/0"].original_lines == [
        "まだやるかい？掛け金はそのままだぜ？（掛け金\\V[48])"
    ]
    assert items_by_path["CommonEvents.json/9/0"].role == "案内人"
    assert items_by_path["CommonEvents.json/9/0"].original_lines == ["独立行の本文です"]
    assert items_by_path["CommonEvents.json/9/0"].source_line_paths == ["CommonEvents.json/9/2"]
    assert items_by_path["CommonEvents.json/10/0"].role == "店員"
    assert items_by_path["CommonEvents.json/10/0"].original_lines == ["大文字制御の本文です"]
    assert items_by_path["CommonEvents.json/11/0"].role == "\\n[1]"
    assert items_by_path["CommonEvents.json/11/0"].original_lines == ["動的名の本文です"]
    assert items_by_path["CommonEvents.json/11/0"].source_line_paths == ["CommonEvents.json/11/2"]
    assert items_by_path["CommonEvents.json/12/0"].role == "旁白"
    assert items_by_path["CommonEvents.json/12/0"].original_lines == ["こちらはステラ（1戦目）の回想です。"]
    assert items_by_path["CommonEvents.json/13/0"].role == "旁白"
    assert items_by_path["CommonEvents.json/13/0"].original_lines == ["<ステラ><ソフィア>", "複合名の本文です"]


@pytest.mark.asyncio
async def test_mv_data_extraction_without_virtual_namebox_rules_keeps_401_as_body(
    minimal_mv_game_dir: Path,
) -> None:
    """MV 没有外部规则时不会用内置格式猜测虚拟名字框。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 2,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["案内人："]},
                {"code": 401, "parameters": ["次の本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    game_data = await load_game_data(minimal_mv_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/2/0"
    )

    assert item.role == "旁白"
    assert item.original_lines == ["案内人：", "次の本文です"]


@pytest.mark.asyncio
async def test_english_visible_401_short_fragment_is_extracted(
    minimal_game_dir: Path,
) -> None:
    """英文 `401` 短断句也是玩家可见正文，不能按协议噪音跳过。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 3,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2, "Adriel"]},
                {"code": 401, "parameters": ["But-"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    text_rules = TextRules.from_setting(
        TextRulesSetting(
            source_text_required_pattern=r"[A-Za-z]+",
            source_text_exclusion_profile="english_protocol_noise",
        )
    )
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, text_rules).extract_all_text()
    items_by_path = {item.location_path: item for data in extracted.values() for item in data.translation_items}

    assert items_by_path["CommonEvents.json/3/0"].role == "Adriel"
    assert items_by_path["CommonEvents.json/3/0"].original_lines == ["But-"]
    assert items_by_path["CommonEvents.json/3/0"].source_line_paths == ["CommonEvents.json/3/1"]


@pytest.mark.asyncio
async def test_english_visible_401_control_only_letters_do_not_extract_chinese_text(
    minimal_game_dir: Path,
) -> None:
    """英文事件正文判断先剥离控制符，避免把颜色控制符当英文源文。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 3,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2, "Guide"]},
                {"code": 401, "parameters": [r"\c[14]水池的水位已然降低..."]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    text_rules = TextRules.from_setting(
        TextRulesSetting(
            source_language="en",
            source_text_required_pattern=r"[A-Za-z][A-Za-z0-9'’_-]*",
            source_text_exclusion_profile="english_protocol_noise",
        )
    )
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, text_rules).extract_all_text()
    items_by_path = {item.location_path: item for data in extracted.values() for item in data.translation_items}

    assert "CommonEvents.json/3/0" not in items_by_path


@pytest.mark.asyncio
async def test_english_description_with_this_is_extracted(minimal_english_game_dir: Path) -> None:
    """英文说明里的自然语言 this 不能被当作脚本协议噪音过滤。"""
    items_path = minimal_english_game_dir / "data" / "Items.json"
    items = ensure_json_array(_read_test_json(items_path), "Items.json")
    item = ensure_json_object(items[1], "Items[1]")
    item["description"] = "With this rope, you can cross the old bridge."
    _rewrite_json(items_path, items)

    text_rules = TextRules.from_setting(
        TextRulesSetting(
            source_language="en",
            source_text_required_pattern=r"[A-Za-z][A-Za-z0-9'’_-]*",
            source_text_exclusion_profile="english_protocol_noise",
        )
    )
    game_data = await load_game_data(minimal_english_game_dir)
    extracted = DataTextExtraction(game_data, text_rules).extract_all_text()
    items_by_path = {item.location_path: item for data in extracted.values() for item in data.translation_items}

    assert items_by_path["Items.json/1/description"].original_lines == ["With this rope, you can cross the old bridge."]


@pytest.mark.asyncio
async def test_write_back_keeps_english_visible_401_short_fragment(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写回前过滤不能再次跳过已经保存的英文短断句正文。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 3,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2, "Adriel"]},
                {"code": 401, "parameters": ["But-"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="en")
    async with await registry.open_game("テストゲーム") as session:
        game_data = await load_game_data(minimal_game_dir)
        placeholder_record = PlaceholderRuleRecord(
            pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
            placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
        )
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )
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
        await session.replace_placeholder_rules([placeholder_record])
        setting = load_setting(source_language=session.source_language)
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=(
                CustomPlaceholderRule.create(
                    pattern_text=placeholder_record.pattern_text,
                    placeholder_template=placeholder_record.placeholder_template,
                ),
            ),
            structured_placeholder_rules=(),
        )
        plugin_source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=game_data),
            text_rules=text_rules,
        )
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=plugin_source_scan.risk.high_risk,
            candidate_count=len(plugin_source_scan.candidates),
            summary=plugin_source_scan.risk_report_json(),
        )
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        active_items = scope.active_items()
        await _write_current_translation_items(
            session=session,
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            scope=scope,
            items=[
                TranslationItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=[line for line in item.original_lines],
                    source_line_paths=[path for path in item.source_line_paths],
                    translation_lines=["但是——"]
                    if item.location_path == "CommonEvents.json/3/0"
                    else [_translated_test_line_preserving_controls(line, text_rules) for line in item.original_lines],
                )
                for item in active_items
            ],
        )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        _ = await handler.write_back(
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None),
        )
    finally:
        await handler.close()

    written_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    written_commands = ensure_json_array(
        ensure_json_object(written_events[3], "CommonEvents[3]")["list"],
        "CommonEvents[3].list",
    )
    written_line = ensure_json_array(
        ensure_json_object(written_commands[1], "CommonEvents[3].list[1]")["parameters"],
        "CommonEvents[3].list[1].parameters",
    )[0]

    assert written_line == "但是——"


@pytest.mark.asyncio
async def test_direct_write_back_rejects_latest_quality_errors(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接调用业务写回也必须拦截模型翻了但项目检查没通过的译文。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        game_data = await load_game_data(minimal_game_dir)
        placeholder_record = PlaceholderRuleRecord(
            pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
            placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
        )
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )
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
        await session.replace_placeholder_rules([placeholder_record])
        setting = load_setting(source_language=session.source_language)
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=(
                CustomPlaceholderRule.create(
                    pattern_text=placeholder_record.pattern_text,
                    placeholder_template=placeholder_record.placeholder_template,
                ),
            ),
            structured_placeholder_rules=(),
        )
        plugin_source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=game_data),
            text_rules=text_rules,
        )
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=plugin_source_scan.risk.high_risk,
            candidate_count=len(plugin_source_scan.candidates),
            summary=plugin_source_scan.risk_report_json(),
        )
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        active_items = scope.active_items()
        assert active_items
        failed_item = active_items[0]
        run_record = await session.start_translation_run(
            total_extracted=len(active_items),
            pending_count=1,
            deduplicated_count=1,
            batch_count=1,
        )
        await session.write_translation_quality_errors(
            run_record.run_id,
            [
                TranslationErrorItem(
                    location_path=failed_item.location_path,
                    item_type=failed_item.item_type,
                    role=failed_item.role,
                    original_lines=[line for line in failed_item.original_lines],
                    translation_lines=[],
                    error_type="AI漏翻",
                    error_detail=["无法解析模型输出"],
                    model_response="{}",
                )
            ],
        )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        with pytest.raises(RuntimeError, match="项目检查没通过"):
            _ = await handler.write_back(
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
            )
    finally:
        await handler.close()


@pytest.mark.asyncio
async def test_direct_write_back_rejects_missing_workflow_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接写入游戏文件不能在外部规则未完成时静默成功。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    handler = TranslationHandler(registry, LLMHandler())
    try:
        with pytest.raises(RuntimeError, match="检查没通过"):
            _ = await handler.write_back(
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
            )
    finally:
        await handler.close()


@pytest.mark.asyncio
async def test_direct_rebuild_active_runtime_rejects_missing_workflow_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重建当前运行文件也是写文件操作，必须受同一前置规则约束。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    handler = TranslationHandler(registry, LLMHandler())
    try:
        with pytest.raises(RuntimeError, match="检查没通过"):
            _ = await handler.rebuild_active_runtime(
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
            )
    finally:
        await handler.close()


@pytest.mark.asyncio
async def test_direct_rebuild_active_runtime_uses_real_native_success_path(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重建当前运行文件成功路径必须穿过真实 handler 和 Rust 写回计划。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        game_data = await load_game_data(minimal_game_dir)
        placeholder_record = PlaceholderRuleRecord(
            pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
            placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
        )
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )
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
        await session.replace_placeholder_rules([placeholder_record])
        setting = load_setting(source_language=session.source_language)
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=(
                CustomPlaceholderRule.create(
                    pattern_text=placeholder_record.pattern_text,
                    placeholder_template=placeholder_record.placeholder_template,
                ),
            ),
            structured_placeholder_rules=(),
        )
        plugin_source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=game_data),
            text_rules=text_rules,
        )
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=plugin_source_scan.risk.high_risk,
            candidate_count=len(plugin_source_scan.candidates),
            summary=plugin_source_scan.risk_report_json(),
        )
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        await _write_current_translation_items(
            session=session,
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            scope=scope,
            items=[
                TranslationItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=[line for line in item.original_lines],
                    source_line_paths=[path for path in item.source_line_paths],
                    translation_lines=[
                        _translated_test_line_preserving_controls(line, text_rules) for line in item.original_lines
                    ],
                )
                for item in scope.active_items()
            ],
        )

    _rewrite_json(
        minimal_game_dir / "data" / "System.json",
        {
            "gameTitle": "损坏的当前运行文件",
            "terms": {},
        },
    )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        summary = await handler.rebuild_active_runtime(
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None),
        )
    finally:
        await handler.close()

    rebuilt_system = ensure_json_object(
        _read_test_json(minimal_game_dir / "data" / "System.json"),
        "System.json",
    )
    assert rebuilt_system["gameTitle"] == "测试"
    assert summary.data_item_count > 0
    assert summary.planned_file_count > 0
    assert summary.rust_plan_ms >= 0
    assert summary.file_replacement_ms >= 0
    assert summary.post_write_audit_ms >= 0


@pytest.mark.asyncio
async def test_direct_write_back_rejects_missing_source_snapshot_manifest(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 快路径进入 Rust 前必须校验数据库可信源快照 manifest。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        _ = await _prepare_write_gate_session(session=session, game_dir=minimal_game_dir)
        await session.replace_source_snapshot_records([])

    handler = TranslationHandler(registry, LLMHandler())
    try:
        with pytest.raises(RuntimeError, match="可信源快照 manifest"):
            _ = await handler.write_back(
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
            )
    finally:
        await handler.close()


@pytest.mark.asyncio
async def test_write_terminology_allows_pending_body_translation_run(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """术语写回只要求术语和写入协议可用，不因正文译文未完成而失败。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game_with_mutation_lease("テストゲーム") as session:
        _ = await _prepare_write_gate_session(
            session=session,
            game_dir=minimal_game_dir,
            registry=TerminologyRegistry(speaker_names={"アリス": "爱丽丝"}),
            glossary=TerminologyGlossary(terms={"アリス": "爱丽丝"}),
        )
        _ = await session.start_translation_run(
            total_extracted=100,
            pending_count=100,
            deduplicated_count=100,
            batch_count=1,
        )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        summary = await handler.write_terminology(
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None),
        )
    finally:
        await handler.close()

    common_events = ensure_json_array(_read_test_json(minimal_game_dir / "data" / "CommonEvents.json"), "CommonEvents")
    first_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(first_event["list"], "CommonEvents[1].list")
    name_parameters = ensure_json_array(
        ensure_json_object(commands[0], "CommonEvents[1].list[0]")["parameters"],
        "CommonEvents[1].list[0].parameters",
    )
    assert summary.written_count > 0
    assert name_parameters[4] == "爱丽丝"


@pytest.mark.asyncio
async def test_direct_write_back_rejects_active_runtime_read_error_before_writing_data(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前运行插件源码读取失败时，Rust 计划阶段直接失败且不写 data。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

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
    broken_source_path = plugin_source_dir / "BrokenEncoding.js"
    _ = broken_source_path.write_text(
        "const Messages = { title: 'origin only' };\n",
        encoding="utf-8",
    )
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    original_events = _read_test_json(common_events_path)

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = broken_source_path.write_bytes(b"\xff\xfe\xff")
    async with await registry.open_game("テストゲーム") as session:
        game_data = await load_game_data(minimal_game_dir)
        placeholder_record = PlaceholderRuleRecord(
            pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
            placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
        )
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )
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
        await session.replace_placeholder_rules([placeholder_record])
        setting = load_setting(source_language=session.source_language)
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=(
                CustomPlaceholderRule.create(
                    pattern_text=placeholder_record.pattern_text,
                    placeholder_template=placeholder_record.placeholder_template,
                ),
            ),
            structured_placeholder_rules=(),
        )
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        await _write_current_translation_items(
            session=session,
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            scope=scope,
            items=[
                TranslationItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=[line for line in item.original_lines],
                    source_line_paths=[path for path in item.source_line_paths],
                    translation_lines=["但是——"]
                    if item.location_path == "CommonEvents.json/3/0"
                    else [_translated_test_line_preserving_controls(line, text_rules) for line in item.original_lines],
                )
                for item in scope.active_items()
            ],
        )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        with pytest.raises(RuntimeError, match="插件源码读取失败"):
            _ = await handler.write_back(
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
            )
    finally:
        await handler.close()

    assert _read_test_json(common_events_path) == original_events


@pytest.mark.asyncio
async def test_direct_write_back_ignores_excluded_plugin_source_text_issues_during_plan_audit(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust 计划检查不把已排除的插件源码内部字符串当正文漏翻。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins_text = plugins_path.read_text(encoding="utf-8")
    plugins = ensure_json_array(
        coerce_json_value(
            cast(object, json.loads(plugins_text[plugins_text.index("[") : plugins_text.rindex("]") + 1]))
        ),
        "plugins",
    )
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _ = plugins_path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { category: 'カテゴリ', protocol: '\\\\TRP' };\n",
        encoding="utf-8",
    )
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    original_events = _read_test_json(common_events_path)

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        game_data = await load_game_data(minimal_game_dir)
        placeholder_record = PlaceholderRuleRecord(
            pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
            placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
        )
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )
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
        await session.replace_placeholder_rules([placeholder_record])
        setting = load_setting(source_language=session.source_language)
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=(
                CustomPlaceholderRule.create(
                    pattern_text=placeholder_record.pattern_text,
                    placeholder_template=placeholder_record.placeholder_template,
                ),
            ),
            structured_placeholder_rules=(),
        )
        plugin_source_scan = derive_plugin_source_scan(
            index=build_plugin_source_raw_index(game_data=game_data),
            text_rules=text_rules,
        )
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=plugin_source_scan.risk.high_risk,
            candidate_count=len(plugin_source_scan.candidates),
            summary=plugin_source_scan.risk_report_json(),
        )
        candidate = next(
            candidate
            for candidate in plugin_source_scan.candidates
            if candidate.file_name == "HardcodedText.js" and candidate.text == "カテゴリ"
        )
        plugin_source_records = build_plugin_source_rule_records_from_import(
            import_file=parse_plugin_source_rule_import_text(
                json.dumps(
                    [
                        {
                            "file": "HardcodedText.js",
                            "selectors": [],
                            "excluded_selectors": [candidate.selector],
                        }
                    ],
                    ensure_ascii=False,
                )
            ),
            scan=plugin_source_scan,
        )
        await session.replace_plugin_source_text_rules(plugin_source_records)
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        await _write_current_translation_items(
            session=session,
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            scope=scope,
            items=[
                TranslationItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=[line for line in item.original_lines],
                    source_line_paths=[path for path in item.source_line_paths],
                    translation_lines=[
                        _translated_test_line_preserving_controls(line, text_rules) for line in item.original_lines
                    ],
                )
                for item in scope.active_items()
            ],
        )

    def forbidden_python_native_check(*args: object, **kwargs: object) -> NoReturn:
        """写入路径不应在 Python 侧重复执行 Rust 计划会执行的原生检查。"""
        _ = (args, kwargs)
        raise AssertionError("写入路径不应重复执行 Python 侧原生检查")

    monkeypatch.setattr("app.application.write_back_gate.collect_native_quality_counts", forbidden_python_native_check)
    monkeypatch.setattr(
        "app.application.write_back_gate.count_native_write_protocol_issues", forbidden_python_native_check
    )
    handler = TranslationHandler(registry, LLMHandler())
    try:
        summary = await handler.write_back(
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None),
        )
    finally:
        await handler.close()

    assert summary.data_item_count > 0
    assert _read_test_json(common_events_path) != original_events


@pytest.mark.asyncio
async def test_direct_write_back_rejects_active_runtime_read_error_before_font_side_effects(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前运行源码读取失败时，Rust 计划阶段直接失败且不改字体。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    fonts_dir = minimal_game_dir / "fonts"
    fonts_dir.mkdir()
    old_font = "OldFont.woff"
    _ = (fonts_dir / old_font).write_bytes(b"old font")
    gamefont_css_path = fonts_dir / "gamefont.css"
    original_css = "@font-face { font-family: GameFont; src: url('OldFont.woff'); }\n"
    _ = gamefont_css_path.write_text(original_css, encoding="utf-8")
    replacement_font = tmp_path / "NotoSansSC-Regular.ttf"
    _ = replacement_font.write_bytes(b"new font")

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
    broken_source_path = plugin_source_dir / "BrokenEncoding.js"
    _ = broken_source_path.write_text(
        "const Messages = { title: 'origin only' };\n",
        encoding="utf-8",
    )

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = broken_source_path.write_bytes(b"\xff\xfe\xff")
    async with await registry.open_game("テストゲーム") as session:
        game_data = await load_game_data(minimal_game_dir)
        placeholder_record = PlaceholderRuleRecord(
            pattern_text=r"(?i)\\F\d*\[[^\]\r\n]+\]",
            placeholder_template="[CUSTOM_FACE_PORTRAIT_{index}]",
        )
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )
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
        await session.replace_placeholder_rules([placeholder_record])
        setting = load_setting(source_language=session.source_language)
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=(
                CustomPlaceholderRule.create(
                    pattern_text=placeholder_record.pattern_text,
                    placeholder_template=placeholder_record.placeholder_template,
                ),
            ),
            structured_placeholder_rules=(),
        )
        await session.replace_rule_review_state(
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            scope_hash=note_tag_rule_scope_hash_for_text_rules(
                game_data=game_data,
                text_rules=text_rules,
            ),
            reviewed_empty=True,
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        await session.replace_rule_review_state(
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            scope_hash=structured_placeholder_scope_hash_from_analysis(
                analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
            ),
            reviewed_empty=True,
        )
        await _write_current_translation_items(
            session=session,
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            scope=scope,
            items=[
                TranslationItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=[line for line in item.original_lines],
                    source_line_paths=[path for path in item.source_line_paths],
                    translation_lines=[
                        _translated_test_line_preserving_controls(line, text_rules) for line in item.original_lines
                    ],
                )
                for item in scope.active_items()
            ],
        )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        with pytest.raises(RuntimeError, match="插件源码读取失败"):
            _ = await handler.write_back(
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
                setting_overrides=SettingOverrides(
                    write_back_replacement_font_path=str(replacement_font),
                ),
                confirm_font_overwrite=True,
            )
    finally:
        await handler.close()

    assert not (fonts_dir / replacement_font.name).exists()
    assert not (fonts_dir / "gamefont_origin.css").exists()
    assert gamefont_css_path.read_text(encoding="utf-8") == original_css


@pytest.mark.asyncio
async def test_direct_write_terminology_rejects_missing_workflow_rules(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接调用术语写回也必须经过写入前流程检查。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_terminology_bundle(
            registry=TerminologyRegistry(),
            glossary=TerminologyGlossary(),
        )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        with pytest.raises(RuntimeError, match="检查没通过"):
            _ = await handler.write_terminology(
                game_title="テストゲーム",
                callbacks=(lambda _current, _total: None, lambda _count: None),
            )
    finally:
        await handler.close()


@pytest.mark.asyncio
async def test_mv_virtual_name_box_write_back_rebuilds_speaker_lines(minimal_mv_game_dir: Path) -> None:
    """MV 写回用术语表译名重建虚拟名字框，正文只写剥离后的对白。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.extend(
        [
            {
                "id": 2,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["案内人："]},
                    {"code": 401, "parameters": ["次の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 3,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["案内人「こんにちは」"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 4,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\n<店員>いらっしゃいませ"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 5,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\N[1]:役者の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 6,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<案内人>"]},
                    {"code": 401, "parameters": ["独立行の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 7,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\N<店員>大文字制御の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 8,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["<\\n[1]>"]},
                    {"code": 401, "parameters": ["動的名の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
        ]
    )
    _rewrite_json(common_events_path, common_events)

    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    extracted = DataTextExtraction(
        game_data,
        get_default_text_rules(),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    ).extract_all_text()
    items_by_path = {item.location_path: item for data in extracted.values() for item in data.translation_items}
    items_by_path["CommonEvents.json/2/0"].translation_lines = ["你好"]
    items_by_path["CommonEvents.json/3/0"].translation_lines = ["你好」"]
    items_by_path["CommonEvents.json/4/0"].translation_lines = ["欢迎光临"]
    items_by_path["CommonEvents.json/5/0"].translation_lines = ["勇者正文"]
    items_by_path["CommonEvents.json/6/0"].translation_lines = ["独立正文"]
    items_by_path["CommonEvents.json/7/0"].translation_lines = ["大写正文"]
    items_by_path["CommonEvents.json/8/0"].translation_lines = ["动态正文"]

    reset_writable_copies(game_data)
    write_data_text(
        game_data,
        [
            items_by_path["CommonEvents.json/2/0"],
            items_by_path["CommonEvents.json/3/0"],
            items_by_path["CommonEvents.json/4/0"],
            items_by_path["CommonEvents.json/5/0"],
            items_by_path["CommonEvents.json/6/0"],
            items_by_path["CommonEvents.json/7/0"],
            items_by_path["CommonEvents.json/8/0"],
        ],
        speaker_name_translations={"案内人": "向导", "店員": "店员", "MV勇者": "勇者"},
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    )

    writable_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    standalone_commands = ensure_json_array(
        ensure_json_object(writable_events[2], "CommonEvents[2]")["list"],
        "CommonEvents[2].list",
    )
    inline_commands = ensure_json_array(
        ensure_json_object(writable_events[3], "CommonEvents[3]")["list"],
        "CommonEvents[3].list",
    )
    yep_commands = ensure_json_array(
        ensure_json_object(writable_events[4], "CommonEvents[4]")["list"],
        "CommonEvents[4].list",
    )
    actor_commands = ensure_json_array(
        ensure_json_object(writable_events[5], "CommonEvents[5]")["list"],
        "CommonEvents[5].list",
    )
    angle_commands = ensure_json_array(
        ensure_json_object(writable_events[6], "CommonEvents[6]")["list"],
        "CommonEvents[6].list",
    )
    upper_commands = ensure_json_array(
        ensure_json_object(writable_events[7], "CommonEvents[7]")["list"],
        "CommonEvents[7].list",
    )
    dynamic_commands = ensure_json_array(
        ensure_json_object(writable_events[8], "CommonEvents[8]")["list"],
        "CommonEvents[8].list",
    )

    assert (
        ensure_json_array(
            ensure_json_object(standalone_commands[1], "standalone.speaker")["parameters"],
            "standalone.speaker.parameters",
        )[0]
        == "向导："
    )
    assert (
        ensure_json_array(
            ensure_json_object(standalone_commands[2], "standalone.body")["parameters"], "standalone.body.parameters"
        )[0]
        == "你好"
    )
    assert (
        ensure_json_array(
            ensure_json_object(inline_commands[1], "inline.speaker")["parameters"], "inline.speaker.parameters"
        )[0]
        == "向导「你好」"
    )
    assert (
        ensure_json_array(ensure_json_object(yep_commands[1], "yep.speaker")["parameters"], "yep.speaker.parameters")[0]
        == "\\n<店员>欢迎光临"
    )
    assert (
        ensure_json_array(
            ensure_json_object(actor_commands[1], "actor.speaker")["parameters"], "actor.speaker.parameters"
        )[0]
        == "勇者:勇者正文"
    )
    assert (
        ensure_json_array(
            ensure_json_object(angle_commands[1], "angle.speaker")["parameters"], "angle.speaker.parameters"
        )[0]
        == "<向导>"
    )
    assert (
        ensure_json_array(ensure_json_object(angle_commands[2], "angle.body")["parameters"], "angle.body.parameters")[0]
        == "独立正文"
    )
    assert (
        ensure_json_array(
            ensure_json_object(upper_commands[1], "upper.speaker")["parameters"], "upper.speaker.parameters"
        )[0]
        == "\\N<店员>大写正文"
    )
    assert (
        ensure_json_array(
            ensure_json_object(dynamic_commands[1], "dynamic.speaker")["parameters"], "dynamic.speaker.parameters"
        )[0]
        == "<\\n[1]>"
    )
    assert (
        ensure_json_array(
            ensure_json_object(dynamic_commands[2], "dynamic.body")["parameters"], "dynamic.body.parameters"
        )[0]
        == "动态正文"
    )


@pytest.mark.asyncio
async def test_native_write_back_rebuilds_mv_virtual_name_box_runtime_files(
    minimal_mv_game_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实写回入口用 Rust 计划重建 MV 虚拟名字框运行文件。"""
    app_home = tmp_path / "app-home"
    app_home.mkdir()
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "text_translation_ja_to_zh_system.md"
    setting_text = (Path(__file__).resolve().parents[1] / "setting.example.toml").read_text(encoding="utf-8")
    setting_text = setting_text.replace(
        'system_prompt_file = "prompts/text_translation_ja_to_zh_system.md"',
        f'system_prompt_file = "{prompt_path.as_posix()}"',
    )
    _ = (app_home / "setting.toml").write_text(setting_text, encoding="utf-8")
    monkeypatch.setenv("ATT_MZ_HOME", str(app_home))

    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.extend(
        [
            {
                "id": 2,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["案内人："]},
                    {"code": 401, "parameters": ["次の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 3,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["案内人「こんにちは」"]},
                    {"code": 0, "parameters": []},
                ],
            },
            {
                "id": 4,
                "list": [
                    {"code": 101, "parameters": [0, 0, 0, 2]},
                    {"code": 401, "parameters": ["\\N[1]:役者の本文です"]},
                    {"code": 0, "parameters": []},
                ],
            },
        ]
    )
    _rewrite_json(common_events_path, common_events)

    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_mv_game_dir, source_language="ja")
    async with await registry.open_game("MVテストゲーム") as session:
        await session.replace_mv_virtual_namebox_rules(_mv_virtual_namebox_rule_records())
        game_data, setting, text_rules = await _prepare_write_gate_session(
            session=session,
            game_dir=minimal_mv_game_dir,
            registry=TerminologyRegistry(
                speaker_names={"案内人": "向导", "MV勇者": "勇者"},
            ),
            glossary=TerminologyGlossary(terms={"案内人": "向导", "MV勇者": "勇者"}),
        )
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        scope = analysis_context.scope
        custom_translations = {
            "CommonEvents.json/2/0": ["你好"],
            "CommonEvents.json/3/0": ["你好」"],
            "CommonEvents.json/4/0": ["勇者正文"],
        }
        await _write_current_translation_items(
            session=session,
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            scope=scope,
            items=[
                TranslationItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=[line for line in item.original_lines],
                    source_line_paths=[path for path in item.source_line_paths],
                    translation_lines=custom_translations.get(
                        item.location_path,
                        [_translated_test_line_preserving_controls(line, text_rules) for line in item.original_lines],
                    ),
                )
                for item in scope.active_items()
            ],
        )

    handler = TranslationHandler(registry, LLMHandler())
    try:
        summary = await handler.write_back(
            game_title="MVテストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None),
        )
    finally:
        await handler.close()

    written_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    standalone_commands = ensure_json_array(
        ensure_json_object(written_events[2], "CommonEvents[2]")["list"],
        "CommonEvents[2].list",
    )
    inline_commands = ensure_json_array(
        ensure_json_object(written_events[3], "CommonEvents[3]")["list"],
        "CommonEvents[3].list",
    )
    actor_commands = ensure_json_array(
        ensure_json_object(written_events[4], "CommonEvents[4]")["list"],
        "CommonEvents[4].list",
    )

    assert summary.data_item_count >= 3
    assert (
        ensure_json_array(
            ensure_json_object(standalone_commands[1], "standalone.speaker")["parameters"],
            "standalone.speaker.parameters",
        )[0]
        == "向导："
    )
    assert (
        ensure_json_array(
            ensure_json_object(standalone_commands[2], "standalone.body")["parameters"], "standalone.body.parameters"
        )[0]
        == "你好"
    )
    assert (
        ensure_json_array(
            ensure_json_object(inline_commands[1], "inline.speaker")["parameters"], "inline.speaker.parameters"
        )[0]
        == "向导「你好」"
    )
    assert (
        ensure_json_array(
            ensure_json_object(actor_commands[1], "actor.speaker")["parameters"], "actor.speaker.parameters"
        )[0]
        == "勇者:勇者正文"
    )


@pytest.mark.asyncio
async def test_mv_virtual_name_box_write_back_requires_speaker_translation(minimal_mv_game_dir: Path) -> None:
    """MV 虚拟名字框缺少说话人译名时禁止写回。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 2,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["案内人："]},
                {"code": 401, "parameters": ["次の本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    extracted = DataTextExtraction(
        game_data,
        get_default_text_rules(),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    ).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/2/0"
    )
    item.translation_lines = ["你好"]

    reset_writable_copies(game_data)
    with pytest.raises(ValueError) as exc_info:
        write_data_text(
            game_data,
            [item],
            speaker_name_translations={},
            mv_virtual_namebox_rule_records=mv_namebox_rules,
        )
    message = str(exc_info.value)
    assert "缺少术语译名" in message
    assert "文本路径=CommonEvents.json/2/0" in message
    assert "触发路径=CommonEvents.json/2/1" in message
    assert "规则=standalone-colon" in message
    assert "原始匹配=案内人：" in message


@pytest.mark.asyncio
async def test_mv_virtual_name_box_write_back_keeps_dynamic_speaker_without_translation(
    minimal_mv_game_dir: Path,
) -> None:
    """MV 动态名字框控制符写回时原样保留，不要求术语译名。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 2,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["<\\n[1]>"]},
                {"code": 401, "parameters": ["動的名の本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    extracted = DataTextExtraction(
        game_data,
        get_default_text_rules(),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    ).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/2/0"
    )
    item.translation_lines = ["动态正文"]

    reset_writable_copies(game_data)
    write_data_text(
        game_data,
        [item],
        speaker_name_translations={},
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    )

    writable_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    commands = ensure_json_array(
        ensure_json_object(writable_events[2], "CommonEvents[2]")["list"],
        "CommonEvents[2].list",
    )

    assert (
        ensure_json_array(
            ensure_json_object(commands[1], "dynamic.speaker")["parameters"], "dynamic.speaker.parameters"
        )[0]
        == "<\\n[1]>"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[2], "dynamic.body")["parameters"], "dynamic.body.parameters")[0]
        == "动态正文"
    )


@pytest.mark.asyncio
async def test_mv_virtual_name_box_write_back_rejects_speaker_line_paths_in_body(minimal_mv_game_dir: Path) -> None:
    """MV 译文把独立说话人行当正文时，写回必须提示完整重置。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 2,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["案内人："]},
                {"code": 401, "parameters": ["次の本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    extracted = DataTextExtraction(
        game_data,
        get_default_text_rules(),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    ).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/2/0"
    )
    invalid_item = item.model_copy(deep=True)
    invalid_item.source_line_paths = ["CommonEvents.json/2/1", "CommonEvents.json/2/2"]
    invalid_item.translation_lines = ["向导：", "你好"]

    reset_writable_copies(game_data)
    with pytest.raises(ValueError) as exc_info:
        write_data_text(
            game_data,
            [invalid_item],
            speaker_name_translations={"案内人": "向导"},
            mv_virtual_namebox_rule_records=mv_namebox_rules,
        )
    message = str(exc_info.value)
    assert "当前 MV 译文仍包含说话人行" in message
    assert "文本路径=CommonEvents.json/2/0" in message
    assert "触发路径=CommonEvents.json/2/1" in message


@pytest.mark.asyncio
async def test_mv_virtual_name_box_rule_conflict_reports_text_location(minimal_mv_game_dir: Path) -> None:
    """MV 虚拟名字框规则冲突时报告触发的正文行路径。"""
    common_events_path = minimal_mv_game_dir / "www" / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_events.append(
        {
            "id": 2,
            "list": [
                {"code": 101, "parameters": [0, 0, 0, 2]},
                {"code": 401, "parameters": ["案内人："]},
                {"code": 401, "parameters": ["次の本文です"]},
                {"code": 0, "parameters": []},
            ],
        }
    )
    _rewrite_json(common_events_path, common_events)

    game_data = await load_game_data(minimal_mv_game_dir)
    mv_namebox_rules = _mv_virtual_namebox_rule_records()
    extracted = DataTextExtraction(
        game_data,
        get_default_text_rules(),
        mv_virtual_namebox_rule_records=mv_namebox_rules,
    ).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/2/0"
    )
    item.translation_lines = ["你好"]
    conflict_rules = [
        *mv_namebox_rules,
        MvVirtualNameboxRuleRecord(
            rule_order=999,
            rule_name="standalone-colon-copy",
            pattern_text=r"^(?P<speaker>[^\\「『【\[\]()（）:：\r\n]{1,40})\s*[:：]\s*$",
            speaker_group="speaker",
            body_group="",
            speaker_policy="translate",
            render_template="{speaker}：",
        ),
    ]

    reset_writable_copies(game_data)
    with pytest.raises(ValueError) as exc_info:
        write_data_text(
            game_data,
            [item],
            speaker_name_translations={"案内人": "向导"},
            mv_virtual_namebox_rule_records=conflict_rules,
        )
    message = str(exc_info.value)
    assert "MV 虚拟名字框规则命中冲突" in message
    assert "文本路径=CommonEvents.json/2/1" in message
    assert "standalone-colon" in message
    assert "standalone-colon-copy" in message


def test_empty_metadata_title_falls_back_to_game_directory_name(minimal_mv_game_dir: Path) -> None:
    """窗口标题和系统标题都为空时，注册标题使用游戏目录名。"""
    package_path = minimal_mv_game_dir / "package.json"
    package_object = ensure_json_object(_read_test_json(package_path), "package.json")
    window_object = ensure_json_object(package_object["window"], "package.window")
    window_object["title"] = ""
    _rewrite_json(package_path, package_object)
    system_path = minimal_mv_game_dir / "www" / "data" / "System.json"
    system_object = ensure_json_object(_read_test_json(system_path), "System.json")
    system_object["gameTitle"] = ""
    _rewrite_json(system_path, system_object)

    assert read_game_title(minimal_mv_game_dir) == minimal_mv_game_dir.name


@pytest.mark.asyncio
async def test_add_game_creates_complete_source_snapshot_manifest(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """注册游戏时创建完整可信源快照和数据库 manifest。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")

    assert (minimal_game_dir / "data_origin" / "System.json").is_file()
    assert (minimal_game_dir / "js" / "plugins_origin.js").is_file()
    assert (minimal_game_dir / "js" / "plugins_source_origin" / "TestPlugin.js").is_file()
    async with await registry.open_game("テストゲーム") as session:
        records = await session.read_source_snapshot_records()
    relative_paths = {record.relative_path for record in records}
    assert "data_origin/System.json" in relative_paths
    assert "js/plugins_origin.js" in relative_paths
    assert "js/plugins_source_origin/TestPlugin.js" in relative_paths


@pytest.mark.asyncio
async def test_source_snapshot_manifest_ignores_active_plugin_source_drift(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """可信源 manifest 只校验快照自身，不被当前运行插件源码新增文件影响。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = (minimal_game_dir / "js" / "plugins" / "ExtraRuntimeOnly.js").write_text(
        "const label = '追加実行ファイル';\n",
        encoding="utf-8",
    )

    async with await registry.open_game("テストゲーム") as session:
        records = await session.read_source_snapshot_records()

    validate_source_snapshot_manifest(
        layout=resolve_game_layout(minimal_game_dir),
        records=records,
    )
    game_data = await load_game_data_for_view(
        minimal_game_dir,
        source_view=GameFileView.TRANSLATION_SOURCE,
    )
    assert "ExtraRuntimeOnly.js" not in game_data.plugin_source_files


@pytest.mark.asyncio
async def test_add_game_rejects_existing_source_snapshot_artifacts(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """首次注册只接受没有可信源快照文件的干净游戏目录。"""
    _ = shutil.copytree(minimal_game_dir / "data", minimal_game_dir / "data_origin")
    registry = GameRegistry(tmp_path / "db")

    with pytest.raises(FileExistsError, match="干净游戏目录"):
        _ = await registry.register_game(minimal_game_dir, source_language="ja")


@pytest.mark.asyncio
async def test_translation_source_view_requires_source_snapshot(minimal_game_dir: Path) -> None:
    """显式翻译源视图缺少可信源快照时必须 fail-fast。"""
    with pytest.raises(FileNotFoundError, match="原始 data 备份"):
        _ = await load_game_data_for_view(
            minimal_game_dir,
            source_view=GameFileView.TRANSLATION_SOURCE,
        )


@pytest.mark.asyncio
async def test_translation_source_view_ignores_damaged_active_data_when_snapshot_valid(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """当前运行 data 损坏时，显式翻译源视图仍只读取可信源快照。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    (minimal_game_dir / "data" / "System.json").unlink()

    game_data = await load_game_data_for_view(
        minimal_game_dir,
        source_view=GameFileView.TRANSLATION_SOURCE,
    )

    assert game_data.system.gameTitle == "テストゲーム"


@pytest.mark.asyncio
async def test_active_runtime_loader_skips_writable_copies_by_default(minimal_game_dir: Path) -> None:
    """当前运行只读视图默认不构造写入副本。"""
    read_only_game_data = await load_active_runtime_game_data(minimal_game_dir)
    writable_game_data = await load_active_runtime_game_data(
        minimal_game_dir,
        include_writable_copies=True,
    )

    assert read_only_game_data.data
    assert read_only_game_data.plugins_js
    assert read_only_game_data.writable_data == {}
    assert read_only_game_data.writable_plugins_js == []
    assert read_only_game_data.writable_plugin_source_files == {}
    assert writable_game_data.writable_data
    assert writable_game_data.writable_plugins_js
    assert writable_game_data.writable_data["System.json"] is not writable_game_data.data["System.json"]
    assert writable_game_data.writable_plugins_js[0] is not writable_game_data.plugins_js[0]


@pytest.mark.asyncio
async def test_force_full_restore_rewrites_all_runtime_files_from_source_snapshot(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """重建模式必须恢复未发生译文变化但已损坏的当前运行文件。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    system_origin = _read_test_json(minimal_game_dir / "data_origin" / "System.json")
    animation_origin = _read_test_json(minimal_game_dir / "data_origin" / "Animations.json")
    plugins_origin_text = (minimal_game_dir / "js" / "plugins_origin.js").read_text(encoding="utf-8")
    plugin_source_origin_text = (minimal_game_dir / "js" / "plugins_source_origin" / "TestPlugin.js").read_text(
        encoding="utf-8"
    )

    _ = (minimal_game_dir / "data" / "System.json").write_text("{}", encoding="utf-8")
    (minimal_game_dir / "data" / "Animations.json").unlink()
    _ = (minimal_game_dir / "js" / "plugins.js").write_text("var $plugins = [];\n", encoding="utf-8")
    _ = (minimal_game_dir / "js" / "plugins" / "TestPlugin.js").write_text(
        "const broken = true;\n",
        encoding="utf-8",
    )

    game_data = await load_game_data_for_view(
        minimal_game_dir,
        source_view=GameFileView.TRANSLATION_SOURCE,
    )
    write_game_files(
        game_data,
        force_full_restore=True,
    )

    assert _read_test_json(minimal_game_dir / "data" / "System.json") == system_origin
    assert _read_test_json(minimal_game_dir / "data" / "Animations.json") == animation_origin
    assert (minimal_game_dir / "js" / "plugins.js").read_text(encoding="utf-8") == plugins_origin_text
    assert (minimal_game_dir / "js" / "plugins" / "TestPlugin.js").read_text(
        encoding="utf-8"
    ) == plugin_source_origin_text


@pytest.mark.asyncio
async def test_rebuild_active_runtime_uses_native_rebuild_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重建入口必须直接调用 Rust 重建 helper。"""
    captured_calls: list[tuple[str, bool]] = []

    async def fake_rebuild_with_native_plan(
        self: TranslationHandler,
        game_title: str,
        callbacks: tuple[object, object],
        setting_overrides: SettingOverrides | None = None,
        confirm_font_overwrite: bool = False,
    ) -> WriteBackSummary:
        """记录重建入口传入的 native helper 参数。"""
        _ = self
        _ = callbacks
        _ = setting_overrides
        captured_calls.append((game_title, confirm_font_overwrite))
        return WriteBackSummary(
            data_item_count=0,
            plugin_item_count=0,
            terminology_written_count=0,
            target_font_name=None,
            source_font_count=0,
            replaced_font_reference_count=0,
            font_copied=False,
        )

    monkeypatch.setattr(
        TranslationHandler,
        "_rebuild_active_runtime_with_native_plan",
        fake_rebuild_with_native_plan,
    )
    handler = TranslationHandler(GameRegistry(tmp_path / "db"), LLMHandler())
    try:
        _ = await handler.rebuild_active_runtime(
            game_title="テストゲーム",
            callbacks=(lambda _current, _total: None, lambda _count: None),
            confirm_font_overwrite=True,
        )
    finally:
        await handler.close()

    assert captured_calls == [("テストゲーム", True)]


@pytest.mark.asyncio
async def test_mv_write_back_uses_www_active_and_origin_paths(minimal_mv_game_dir: Path) -> None:
    """MV 外层目录写回只触碰 www 内的 data 和 plugins.js。"""
    game_data = await load_game_data(minimal_mv_game_dir)
    reset_writable_copies(game_data)
    system_object = ensure_json_object(game_data.writable_data["System.json"], "System.json")
    system_object["gameTitle"] = "MV测试游戏"
    game_data.writable_data[PLUGINS_FILE_NAME] = "var $plugins = [];\n"

    write_game_files(game_data)

    assert (minimal_mv_game_dir / "www" / "data_origin" / "System.json").exists()
    assert (minimal_mv_game_dir / "www" / "js" / "plugins_origin.js").exists()
    assert not (minimal_mv_game_dir / "data_origin").exists()
    assert not (minimal_mv_game_dir / "js").exists()
    restored_system = ensure_json_object(
        _read_test_json(minimal_mv_game_dir / "www" / "data_origin" / "System.json"),
        "System.json",
    )
    assert restored_system["gameTitle"] == "MVテストゲーム"


@pytest.mark.asyncio
async def test_data_extraction_covers_core_text_sources(minimal_game_dir: Path) -> None:
    """正文提取覆盖正文文本，并排除术语表直接写回字段。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    paths = {item.location_path for data in extracted.values() for item in data.translation_items}

    assert "Map001.json/1/0/0" in paths
    assert "CommonEvents.json/1/0" in paths
    assert "CommonEvents.json/1/2" in paths
    assert "CommonEvents.json/1/3" in paths
    assert "CommonEvents.json/1/4/parameters/3/message" not in paths
    assert "CommonEvents.json/2/0" in paths
    assert "CommonEvents.json/2/4" in paths
    assert "CommonEvents.json/2/5" in paths
    assert "CommonEvents.json/2/8" in paths
    assert "Map001.json/2/0/0" in paths
    assert "Map001.json/2/0/3" in paths
    assert "Map002.json/1/0/0" in paths
    assert "System.json/gameTitle" in paths
    assert "System.json/terms/basic/1" not in paths
    assert "System.json/elements/1" not in paths
    assert "System.json/skillTypes/1" not in paths
    assert "Actors.json/1/name" not in paths
    assert "Actors.json/1/nickname" not in paths
    assert "Actors.json/1/profile" in paths
    assert "Items.json/1/name" not in paths
    assert "Skills.json/1/name" not in paths
    assert "Items.json/1/description" in paths
    assert "Skills.json/1/message1" in paths


@pytest.mark.asyncio
async def test_data_extraction_strips_outer_whitespace_from_core_sources(minimal_game_dir: Path) -> None:
    """标准提取入口保存清理后的玩家可见原文。"""
    system_path = minimal_game_dir / "data" / "System.json"
    system = ensure_json_object(_read_test_json(system_path), "System.json")
    system["gameTitle"] = "　テストゲーム　"
    _rewrite_json(system_path, system)

    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    common_events = ensure_json_array(_read_test_json(common_events_path), "CommonEvents.json")
    common_event = ensure_json_object(common_events[1], "CommonEvents.json[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents.json[1].list")
    choice_command = ensure_json_object(commands[2], "CommonEvents.json[1].list[2]")
    choice_parameters = ensure_json_array(choice_command["parameters"], "CommonEvents.json[1].list[2].parameters")
    choice_parameters[0] = ["　はい　", " いいえ "]
    _rewrite_json(common_events_path, common_events)

    actors_path = minimal_game_dir / "data" / "Actors.json"
    actors = ensure_json_array(_read_test_json(actors_path), "Actors.json")
    actor = ensure_json_object(actors[1], "Actors.json[1]")
    actor["profile"] = "　プロフィール　"
    _rewrite_json(actors_path, actors)

    items_path = minimal_game_dir / "data" / "Items.json"
    items = ensure_json_array(_read_test_json(items_path), "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["description"] = "　体力を回復する。　"
    _rewrite_json(items_path, items)

    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    items_by_path = {item.location_path: item for data in extracted.values() for item in data.translation_items}

    assert items_by_path["System.json/gameTitle"].original_lines == ["テストゲーム"]
    assert items_by_path["CommonEvents.json/1/2"].original_lines == ["はい", "いいえ"]
    assert items_by_path["Actors.json/1/profile"].original_lines == ["プロフィール"]
    assert items_by_path["Items.json/1/description"].original_lines == ["体力を回復する。"]


@pytest.mark.asyncio
async def test_note_tag_rules_extract_and_write_back_only_target_values(minimal_game_dir: Path) -> None:
    """Note 标签只有导入规则后才进入正文提取，回写只替换目标标签值。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = _read_test_json(items_path)
    items = ensure_json_array(raw_items, "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = "<拡張説明:一行目\n二行目>\n<upgrade:1,2,3>\n<ExtendDesc:別説明>"
    _rewrite_json(items_path, raw_items)

    game_data = await load_game_data(minimal_game_dir)
    standard_extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    standard_paths = {
        candidate.location_path for data in standard_extracted.values() for candidate in data.translation_items
    }
    note_extracted = NoteTagTextExtraction(
        game_data=game_data,
        rule_records=[
            NoteTagTextRuleRecord(
                file_name="Items.json",
                tag_names=["拡張説明", "ExtendDesc"],
            )
        ],
        text_rules=get_default_text_rules(),
    ).extract_all_text()
    note_items = note_extracted["Items.json"].translation_items

    assert "Items.json/1/note/拡張説明" not in standard_paths
    assert [candidate.location_path for candidate in note_items] == [
        "Items.json/1/note/拡張説明",
        "Items.json/1/note/ExtendDesc",
    ]
    assert note_items[0].original_lines == ["一行目\n二行目"]

    note_items[0].translation_lines = ["第一行\n第二行"]
    reset_writable_copies(game_data)
    write_data_text(game_data, [note_items[0]])
    writable_items = ensure_json_array(game_data.writable_data["Items.json"], "Items.json")
    writable_item = ensure_json_object(writable_items[1], "Items.json[1]")

    assert writable_item["note"] == "<拡張説明:第一行\n第二行>\n<upgrade:1,2,3>\n<ExtendDesc:別説明>"


@pytest.mark.asyncio
async def test_note_tag_multiline_value_keeps_line_break_structure_before_write_back(minimal_game_dir: Path) -> None:
    """Note 标签单字段写回不再为了切宽新增换行。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = _read_test_json(items_path)
    items = ensure_json_array(raw_items, "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    item["note"] = "<拡張説明:説明\n「原文」>"
    _rewrite_json(items_path, raw_items)
    text_rules = TextRules.from_setting(
        TextRulesSetting(
            long_text_line_width_limit=8,
            line_split_punctuations=["，", "。"],
        )
    )

    game_data = await load_game_data(minimal_game_dir)
    note_items = (
        NoteTagTextExtraction(
            game_data=game_data,
            rule_records=[
                NoteTagTextRuleRecord(
                    file_name="Items.json",
                    tag_names=["拡張説明"],
                )
            ],
            text_rules=text_rules,
        )
        .extract_all_text()["Items.json"]
        .translation_items
    )
    note_items[0].translation_lines = ["说明\n「甲乙，丙丁」"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [note_items[0]], text_rules)

    writable_items = ensure_json_array(game_data.writable_data["Items.json"], "Items.json")
    writable_item = ensure_json_object(writable_items[1], "Items.json[1]")
    assert writable_item["note"] == "<拡張説明:说明\n「甲乙，丙丁」>"


@pytest.mark.asyncio
async def test_note_tag_json_string_leaf_uses_visible_text_protocol(minimal_game_dir: Path) -> None:
    """Note 标签值如果带 JSON 字符串外壳，只翻玩家可见文本并按原结构写回。"""
    items_path = minimal_game_dir / "data" / "Items.json"
    raw_items = _read_test_json(items_path)
    items = ensure_json_array(raw_items, "Items.json")
    item = ensure_json_object(items[1], "Items.json[1]")
    source_note = "\n　" + r"\C[2]詳細説明\C[0]\n次の行" + "　\n"
    item["note"] = f"<拡張説明:{json.dumps(source_note, ensure_ascii=False)}>\n<upgrade:1,2,3>"
    _rewrite_json(items_path, raw_items)

    game_data = await load_game_data(minimal_game_dir)
    candidates = collect_note_tag_candidates(
        game_data=game_data,
        text_rules=get_default_text_rules(),
    )
    candidate = next(
        ensure_json_object(candidate_value, "note_tag_candidate")
        for candidate_value in candidates
        if isinstance(candidate_value, dict)
        and candidate_value.get("file_name") == "Items.json"
        and candidate_value.get("tag_name") == "拡張説明"
    )
    assert candidate["sample_values"] == [source_note.strip()]

    rule_records = build_note_tag_rule_records_from_import(
        game_data=game_data,
        import_file={"Items.json": ["拡張説明"]},
        text_rules=get_default_text_rules(),
    )
    note_items = (
        NoteTagTextExtraction(
            game_data=game_data,
            rule_records=rule_records,
            text_rules=get_default_text_rules(),
        )
        .extract_all_text()["Items.json"]
        .translation_items
    )

    assert note_items[0].original_lines == [source_note.strip()]

    translated_note = "\n　" + r"\C[2]详细说明\C[0]\n下一行" + "　\n"
    note_items[0].translation_lines = [translated_note.strip()]
    reset_writable_copies(game_data)
    write_data_text(game_data, [note_items[0]])

    writable_items = ensure_json_array(game_data.writable_data["Items.json"], "Items.json")
    writable_item = ensure_json_object(writable_items[1], "Items.json[1]")
    writable_note = writable_item["note"]
    assert isinstance(writable_note, str)
    assert writable_note.endswith("\n<upgrade:1,2,3>")
    tag_value = writable_note.removeprefix("<拡張説明:").split(">", maxsplit=1)[0]
    assert json.loads(tag_value) == translated_note.strip()


@pytest.mark.asyncio
async def test_map_event_note_tag_rules_extract_and_write_back(minimal_game_dir: Path) -> None:
    """Note 标签规则覆盖地图事件 note 字段，并支持 Map*.json 文件模式。"""
    map_path = minimal_game_dir / "data" / "Map001.json"
    raw_map = _read_test_json(map_path)
    map_object = ensure_json_object(raw_map, "Map001.json")
    events = ensure_json_array(map_object["events"], "Map001.json.events")
    event = ensure_json_object(events[2], "Map001.json.events[2]")
    event["note"] = "<namePop:導き手>\n<machine:1>"
    _rewrite_json(map_path, raw_map)

    game_data = await load_game_data(minimal_game_dir)
    candidates = collect_note_tag_candidates(
        game_data=game_data,
        text_rules=get_default_text_rules(),
    )
    name_pop_candidate = next(
        ensure_json_object(candidate_value, "note_tag_candidate")
        for candidate_value in candidates
        if isinstance(candidate_value, dict)
        and candidate_value.get("file_name") == "Map*.json"
        and candidate_value.get("tag_name") == "namePop"
    )
    assert name_pop_candidate["translatable_hit_count"] == 1
    assert name_pop_candidate["sample_locations"] == ["Map001.json/events/2/note/namePop"]

    rule_records = build_note_tag_rule_records_from_import(
        game_data=game_data,
        import_file={"Map*.json": ["namePop"]},
        text_rules=get_default_text_rules(),
    )
    note_items = (
        NoteTagTextExtraction(
            game_data=game_data,
            rule_records=rule_records,
            text_rules=get_default_text_rules(),
        )
        .extract_all_text()["Map001.json"]
        .translation_items
    )

    assert [item.location_path for item in note_items] == ["Map001.json/events/2/note/namePop"]
    assert note_items[0].original_lines == ["導き手"]

    note_items[0].translation_lines = ["引导者"]
    reset_writable_copies(game_data)
    write_data_text(game_data, [note_items[0]])
    writable_map = ensure_json_object(game_data.writable_data["Map001.json"], "Map001.json")
    writable_events = ensure_json_array(writable_map["events"], "Map001.json.events")
    writable_event = ensure_json_object(writable_events[2], "Map001.json.events[2]")

    assert writable_event["note"] == "<namePop:引导者>\n<machine:1>"


@pytest.mark.asyncio
async def test_fixture_custom_control_sequences_can_be_protected(minimal_game_dir: Path) -> None:
    """测试夹具里的自定义控制符可通过外部规则保护。"""
    text_rules = TextRules.from_setting(
        TextRulesSetting(),
        custom_placeholder_rules=(CustomPlaceholderRule.create(r"\\F\[[^\]]+\]", "[CUSTOM_FACE_PORTRAIT_{index}]"),),
    )
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, text_rules).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/2/0"
    )

    item.build_placeholders(text_rules)

    assert item.original_lines_with_placeholders[0] == "[CUSTOM_FACE_PORTRAIT_1]テスト一行目です。[RMMZ_WAIT_INPUT]"
    assert item.original_lines_with_placeholders[1] == "[RMMZ_TEXT_COLOR_4]重要語[RMMZ_TEXT_COLOR_0]を含む二行目です。"


@pytest.mark.asyncio
async def test_write_data_text_updates_writable_copy(minimal_game_dir: Path) -> None:
    """正文回写修改可写副本，原始加载数据保持不变。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.translation_lines = ["你好"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    text_command = ensure_json_object(commands[1], "CommonEvents[1].list[1]")
    parameters = ensure_json_array(text_command["parameters"], "CommonEvents[1].list[1].parameters")
    assert parameters[0] == "你好"


@pytest.mark.asyncio
async def test_write_data_text_rejects_internal_placeholder_leak(minimal_game_dir: Path) -> None:
    """正文写回前必须拒绝项目内部占位符。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.translation_lines = ["你好[RMMZ_TEXT_COLOR_0]"]

    reset_writable_copies(game_data)
    with pytest.raises(ValueError, match="译文残留项目内部占位符"):
        write_data_text(game_data, [item])


@pytest.mark.asyncio
async def test_name_text_write_back_uses_real_401_paths(minimal_game_dir: Path) -> None:
    """名字框正文按实际 401 路径写回，不按相邻下标猜测。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_events = _read_test_json(common_events_path)
    events = ensure_json_array(raw_events, "CommonEvents")
    event = ensure_json_object(events[1], "CommonEvents[1]")
    event_commands = ensure_json_array(event["list"], "CommonEvents[1].list")
    event_commands.insert(1, {"code": 401, "parameters": [""]})
    _rewrite_json(common_events_path, raw_events)

    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    assert item.original_lines == ["こんにちは"]
    assert item.source_line_paths == ["CommonEvents.json/1/2"]

    item.translation_lines = ["你好"]
    reset_writable_copies(game_data)
    write_data_text(game_data, [item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    blank_text_command = ensure_json_object(commands[1], "CommonEvents[1].list[1]")
    translated_text_command = ensure_json_object(commands[2], "CommonEvents[1].list[2]")
    blank_parameters = ensure_json_array(blank_text_command["parameters"], "blank.parameters")
    translated_parameters = ensure_json_array(
        translated_text_command["parameters"],
        "translated.parameters",
    )
    assert blank_parameters[0] == ""
    assert translated_parameters[0] == "你好"


@pytest.mark.asyncio
async def test_name_text_write_back_inserts_extra_401_lines(minimal_game_dir: Path) -> None:
    """名字框正文译文行数增加时，在原文本块末尾插入新的 401。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.translation_lines = ["你好", "第二行", "第三行"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    first_text = ensure_json_object(commands[1], "CommonEvents[1].list[1]")
    second_text = ensure_json_object(commands[2], "CommonEvents[1].list[2]")
    third_text = ensure_json_object(commands[3], "CommonEvents[1].list[3]")
    choice_command = ensure_json_object(commands[4], "CommonEvents[1].list[4]")

    assert first_text["code"] == 401
    assert second_text["code"] == 401
    assert third_text["code"] == 401
    assert choice_command["code"] == 102
    assert ensure_json_array(first_text["parameters"], "first.parameters")[0] == "你好"
    assert ensure_json_array(second_text["parameters"], "second.parameters")[0] == "第二行"
    assert ensure_json_array(third_text["parameters"], "third.parameters")[0] == "第三行"


@pytest.mark.asyncio
async def test_write_back_inserts_401_without_shifting_later_name_block(minimal_game_dir: Path) -> None:
    """前一个名字框插入额外 401 时，后一个名字框仍按原始定位正确写回。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_events = _read_test_json(common_events_path)
    events = ensure_json_array(raw_events, "CommonEvents")
    event = ensure_json_object(events[1], "CommonEvents[1]")
    event["list"] = [
        {"code": 101, "parameters": [0, 0, 0, 2, "案内人A"]},
        {"code": 401, "parameters": ["前半一行目"]},
        {"code": 101, "parameters": [0, 0, 0, 2, "案内人B"]},
        {"code": 401, "parameters": ["後半一行目"]},
        {"code": 0, "parameters": []},
    ]
    _rewrite_json(common_events_path, raw_events)

    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    common_items = extracted["CommonEvents.json"].translation_items
    first_item = next(item for item in common_items if item.location_path == "CommonEvents.json/1/0")
    second_item = next(item for item in common_items if item.location_path == "CommonEvents.json/1/2")
    first_item.translation_lines = ["前半译文一", "前半译文二", "前半译文三"]
    second_item.translation_lines = ["后半译文"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [first_item, second_item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")

    assert ensure_json_object(commands[0], "command0")["code"] == 101
    assert (
        ensure_json_array(ensure_json_object(commands[1], "command1")["parameters"], "command1.parameters")[0]
        == "前半译文一"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[2], "command2")["parameters"], "command2.parameters")[0]
        == "前半译文二"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[3], "command3")["parameters"], "command3.parameters")[0]
        == "前半译文三"
    )
    assert ensure_json_object(commands[4], "command4")["code"] == 101
    assert (
        ensure_json_array(ensure_json_object(commands[5], "command5")["parameters"], "command5.parameters")[0]
        == "后半译文"
    )


@pytest.mark.asyncio
async def test_write_back_deletes_401_without_shifting_later_name_block(minimal_game_dir: Path) -> None:
    """前一个名字框删除多余 401 时，后一个名字框仍按原始定位正确写回。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_events = _read_test_json(common_events_path)
    events = ensure_json_array(raw_events, "CommonEvents")
    event = ensure_json_object(events[1], "CommonEvents[1]")
    event["list"] = [
        {"code": 101, "parameters": [0, 0, 0, 2, "案内人A"]},
        {"code": 401, "parameters": ["前半一行目"]},
        {"code": 401, "parameters": ["前半二行目"]},
        {"code": 101, "parameters": [0, 0, 0, 2, "案内人B"]},
        {"code": 401, "parameters": ["後半一行目"]},
        {"code": 0, "parameters": []},
    ]
    _rewrite_json(common_events_path, raw_events)

    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    common_items = extracted["CommonEvents.json"].translation_items
    first_item = next(item for item in common_items if item.location_path == "CommonEvents.json/1/0")
    second_item = next(item for item in common_items if item.location_path == "CommonEvents.json/1/3")
    first_item.translation_lines = ["前半译文"]
    second_item.translation_lines = ["后半译文"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [first_item, second_item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")

    assert ensure_json_object(commands[0], "command0")["code"] == 101
    assert (
        ensure_json_array(ensure_json_object(commands[1], "command1")["parameters"], "command1.parameters")[0]
        == "前半译文"
    )
    assert ensure_json_object(commands[2], "command2")["code"] == 101
    assert (
        ensure_json_array(ensure_json_object(commands[3], "command3")["parameters"], "command3.parameters")[0]
        == "后半译文"
    )
    assert ensure_json_object(commands[4], "command4")["code"] == 0


@pytest.mark.asyncio
async def test_write_data_text_splits_overwide_long_text_before_write_back(minimal_game_dir: Path) -> None:
    """写回阶段按当前行宽配置再次切分已有长译文。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.translation_lines = ["甲乙丙丁戊己庚辛"]
    text_rules = TextRules.from_setting(
        TextRulesSetting(
            long_text_line_width_limit=3,
            line_width_count_pattern=r"\S",
            line_split_punctuations=["，", "。"],
        )
    )

    reset_writable_copies(game_data)
    write_data_text(game_data, [item], text_rules=text_rules)

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")

    assert (
        ensure_json_array(ensure_json_object(commands[1], "command1")["parameters"], "command1.parameters")[0]
        == "甲乙丙"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[2], "command2")["parameters"], "command2.parameters")[0]
        == "丁戊己"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[3], "command3")["parameters"], "command3.parameters")[0] == "庚辛"
    )


@pytest.mark.asyncio
async def test_write_data_text_indents_wrapping_punctuation_continuation_lines(minimal_game_dir: Path) -> None:
    """写回阶段清理译文外层空白，并为跨行引号续行补视觉缩进。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.translation_lines = ["　「甲乙丙。　", "　丁戊己」　"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [item], text_rules=get_default_text_rules())

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")

    assert (
        ensure_json_array(ensure_json_object(commands[1], "command1")["parameters"], "command1.parameters")[0]
        == "「甲乙丙。"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[2], "command2")["parameters"], "command2.parameters")[0]
        == "　丁戊己」"
    )


@pytest.mark.asyncio
async def test_write_data_text_restores_converted_outer_quote_before_indent(minimal_game_dir: Path) -> None:
    """写回阶段先修复被模型改写的外层引号，再补跨行视觉缩进。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.original_lines = ["「甲。", "乙」"]
    item.translation_lines = ["“甲乙丙。", "丁戊己。”"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [item], text_rules=get_default_text_rules())

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")

    assert (
        ensure_json_array(ensure_json_object(commands[1], "command1")["parameters"], "command1.parameters")[0]
        == "「甲乙丙。"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[2], "command2")["parameters"], "command2.parameters")[0]
        == "　丁戊己。」"
    )


@pytest.mark.asyncio
async def test_write_data_text_restores_mismatched_source_quote_slots(minimal_game_dir: Path) -> None:
    """写回阶段按源文真实引号槽位修复错配引号。"""
    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.original_lines = ["これが『秒殺テク」……！"]
    item.translation_lines = ["这就是‘秒杀技术’……！"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [item], text_rules=get_default_text_rules())

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")

    assert (
        ensure_json_array(ensure_json_object(commands[1], "command1")["parameters"], "command1.parameters")[0]
        == "这就是『秒杀技术」……！"
    )


@pytest.mark.asyncio
async def test_scroll_text_commands_are_grouped_by_adjacent_405(minimal_game_dir: Path) -> None:
    """连续 405 滚动文本作为一个翻译单元提取，并支持额外译文行写回。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_events = _read_test_json(common_events_path)
    events = ensure_json_array(raw_events, "CommonEvents")
    event = ensure_json_object(events[1], "CommonEvents[1]")
    event["list"] = [
        {"code": 101, "parameters": [0, 0, 0, 2, "アリス"]},
        {"code": 401, "parameters": ["こんにちは"]},
        {"code": 405, "parameters": ["スクロール一行目"]},
        {"code": 405, "parameters": ["スクロール二行目"]},
        {"code": 405, "parameters": [""]},
        {"code": 405, "parameters": ["別段落"]},
        {"code": 0, "parameters": []},
    ]
    _rewrite_json(common_events_path, raw_events)

    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    first_scroll_item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/2"
    )
    second_scroll_item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/5"
    )
    assert first_scroll_item.original_lines == ["スクロール一行目", "スクロール二行目"]
    assert first_scroll_item.source_line_paths == [
        "CommonEvents.json/1/2",
        "CommonEvents.json/1/3",
    ]
    assert second_scroll_item.original_lines == ["別段落"]

    first_scroll_item.translation_lines = ["滚动第一行", "滚动第二行", "滚动第三行"]
    second_scroll_item.translation_lines = ["另一段"]
    reset_writable_copies(game_data)
    write_data_text(game_data, [first_scroll_item, second_scroll_item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    assert (
        ensure_json_array(ensure_json_object(commands[2], "command2")["parameters"], "command2.parameters")[0]
        == "滚动第一行"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[3], "command3")["parameters"], "command3.parameters")[0]
        == "滚动第二行"
    )
    assert (
        ensure_json_array(ensure_json_object(commands[4], "command4")["parameters"], "command4.parameters")[0]
        == "滚动第三行"
    )
    assert ensure_json_array(ensure_json_object(commands[5], "command5")["parameters"], "command5.parameters")[0] == ""
    assert (
        ensure_json_array(ensure_json_object(commands[6], "command6")["parameters"], "command6.parameters")[0]
        == "另一段"
    )


@pytest.mark.asyncio
async def test_long_text_write_back_deletes_extra_original_lines(minimal_game_dir: Path) -> None:
    """译文行数少于原始 405 行数时，删除多余原始行指令。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_events = _read_test_json(common_events_path)
    events = ensure_json_array(raw_events, "CommonEvents")
    event = ensure_json_object(events[1], "CommonEvents[1]")
    event["list"] = [
        {"code": 405, "parameters": ["スクロール一行目"]},
        {"code": 405, "parameters": ["スクロール二行目"]},
        {"code": 0, "parameters": []},
    ]
    _rewrite_json(common_events_path, raw_events)

    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    scroll_item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    assert scroll_item.original_lines == ["スクロール一行目", "スクロール二行目"]

    scroll_item.translation_lines = ["滚动第一行"]
    reset_writable_copies(game_data)
    write_data_text(game_data, [scroll_item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    assert (
        ensure_json_array(ensure_json_object(commands[0], "command0")["parameters"], "command0.parameters")[0]
        == "滚动第一行"
    )
    assert ensure_json_object(commands[1], "command1")["code"] == 0


@pytest.mark.asyncio
async def test_long_text_write_back_ignores_trailing_empty_translation_lines(minimal_game_dir: Path) -> None:
    """长文本写回忽略译文尾部空行，避免生成空白文本指令。"""
    common_events_path = minimal_game_dir / "data" / "CommonEvents.json"
    raw_events = _read_test_json(common_events_path)
    events = ensure_json_array(raw_events, "CommonEvents")
    event = ensure_json_object(events[1], "CommonEvents[1]")
    event["list"] = [
        {"code": 405, "parameters": ["スクロール一行目"]},
        {"code": 405, "parameters": ["スクロール二行目"]},
        {"code": 0, "parameters": []},
    ]
    _rewrite_json(common_events_path, raw_events)

    game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    scroll_item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    scroll_item.translation_lines = ["滚动第一行", ""]

    reset_writable_copies(game_data)
    write_data_text(game_data, [scroll_item])

    common_events = ensure_json_array(game_data.writable_data["CommonEvents.json"], "CommonEvents")
    common_event = ensure_json_object(common_events[1], "CommonEvents[1]")
    commands = ensure_json_array(common_event["list"], "CommonEvents[1].list")
    assert (
        ensure_json_array(ensure_json_object(commands[0], "command0")["parameters"], "command0.parameters")[0]
        == "滚动第一行"
    )
    assert ensure_json_object(commands[1], "command1")["code"] == 0


@pytest.mark.asyncio
async def test_first_write_back_archives_complete_original_data_snapshot(minimal_game_dir: Path) -> None:
    """首次磁盘回写会准备完整可信源快照。"""
    game_data = await load_game_data(minimal_game_dir)
    active_data_names = sorted(path.name for path in (minimal_game_dir / "data").glob("*.json"))
    extracted = DataTextExtraction(game_data, get_default_text_rules()).extract_all_text()
    item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    item.translation_lines = ["你好"]

    reset_writable_copies(game_data)
    write_data_text(game_data, [item])
    write_game_files(game_data)

    origin_data_dir = minimal_game_dir / "data_origin"
    origin_data_names = sorted(path.name for path in origin_data_dir.glob("*.json"))
    assert origin_data_names == active_data_names
    assert (origin_data_dir / "Animations.json").exists()
    assert (origin_data_dir / "CommonEvents.json").exists()
    assert (origin_data_dir / "MapInfos.json").exists()
    assert (origin_data_dir / "System.json").exists()
    assert (origin_data_dir / "Tilesets.json").exists()
    assert (origin_data_dir / "UnknownPluginData.json").exists()
    assert (minimal_game_dir / "js" / "plugins_origin.js").exists()

    active_common_events = ensure_json_array(
        _read_test_json(minimal_game_dir / "data" / "CommonEvents.json"),
        "CommonEvents",
    )
    active_event = ensure_json_object(active_common_events[1], "CommonEvents[1]")
    active_commands = ensure_json_array(active_event["list"], "CommonEvents[1].list")
    active_text_command = ensure_json_object(active_commands[1], "CommonEvents[1].list[1]")
    active_parameters = ensure_json_array(
        active_text_command["parameters"],
        "CommonEvents[1].list[1].parameters",
    )
    assert active_parameters[0] == "你好"


@pytest.mark.asyncio
async def test_written_game_reads_complete_origin_without_mutating_snapshot(minimal_game_dir: Path) -> None:
    """已有完整原始 data 备份时，后续写回不修改 `data_origin/`。"""
    first_game_data = await load_game_data(minimal_game_dir)
    extracted = DataTextExtraction(first_game_data, get_default_text_rules()).extract_all_text()
    common_item = next(
        candidate
        for candidate in extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    common_item.translation_lines = ["你好"]
    reset_writable_copies(first_game_data)
    write_data_text(first_game_data, [common_item])
    write_game_files(first_game_data)
    origin_data_dir = minimal_game_dir / "data_origin"
    origin_snapshot = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(origin_data_dir.glob("*.json"), key=lambda candidate: candidate.name)
    }

    reloaded_game_data = await load_game_data(minimal_game_dir)
    reloaded_extracted = DataTextExtraction(reloaded_game_data, get_default_text_rules()).extract_all_text()
    reloaded_common_item = next(
        candidate
        for candidate in reloaded_extracted["CommonEvents.json"].translation_items
        if candidate.location_path == "CommonEvents.json/1/0"
    )
    assert reloaded_common_item.original_lines == ["こんにちは"]

    actor_item = next(
        candidate
        for candidate in reloaded_extracted["Actors.json"].translation_items
        if candidate.location_path == "Actors.json/1/profile"
    )
    actor_item.translation_lines = ["角色简介译文"]
    reset_writable_copies(reloaded_game_data)
    write_data_text(reloaded_game_data, [actor_item])
    write_game_files(reloaded_game_data)
    assert {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(origin_data_dir.glob("*.json"), key=lambda candidate: candidate.name)
    } == origin_snapshot

    origin_actors_path = origin_data_dir / "Actors.json"
    assert origin_actors_path.exists()
    origin_actors = ensure_json_array(_read_test_json(origin_actors_path), "data_origin/Actors.json")
    active_actors = ensure_json_array(_read_test_json(minimal_game_dir / "data" / "Actors.json"), "Actors.json")
    origin_actor = ensure_json_object(origin_actors[1], "data_origin/Actors.json[1]")
    active_actor = ensure_json_object(active_actors[1], "Actors.json[1]")
    assert origin_actor["profile"] == "プロフィール"
    assert active_actor["profile"] == "角色简介译文"

    plugin_game_data = await load_game_data(minimal_game_dir)
    reset_writable_copies(plugin_game_data)
    plugin_text = plugin_game_data.writable_data[PLUGINS_FILE_NAME]
    assert isinstance(plugin_text, str)
    plugin_game_data.writable_data[PLUGINS_FILE_NAME] = plugin_text.replace("プラグイン本文", "插件正文")
    write_game_files(plugin_game_data)

    origin_plugins_path = minimal_game_dir / "js" / "plugins_origin.js"
    assert origin_plugins_path.exists()
    assert "プラグイン本文" in origin_plugins_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_loader_rejects_missing_fixed_active_data_file(minimal_game_dir: Path) -> None:
    """激活 data 缺标准文件时禁止加载游戏。"""
    (minimal_game_dir / "data" / "Animations.json").unlink()

    with pytest.raises(FileNotFoundError) as exc_info:
        _ = await load_game_data(minimal_game_dir)

    message = str(exc_info.value)
    assert "激活数据目录" in message
    assert "Animations.json" in message


@pytest.mark.asyncio
async def test_loader_rejects_map_infos_with_missing_map_file(minimal_game_dir: Path) -> None:
    """MapInfos.json 引用不存在的地图文件时禁止加载游戏。"""
    map_infos_path = minimal_game_dir / "data" / "MapInfos.json"
    map_infos = ensure_json_array(_read_test_json(map_infos_path), "MapInfos.json")
    map_infos.append(
        {
            "id": 14,
            "expanded": False,
            "name": "",
            "order": 14,
            "parentId": 0,
            "scrollX": 0,
            "scrollY": 0,
        }
    )
    _rewrite_json(map_infos_path, map_infos)

    with pytest.raises(FileNotFoundError) as exc_info:
        _ = await load_game_data(minimal_game_dir)

    message = str(exc_info.value)
    assert "MapInfos.json" in message
    assert "Map014.json" in message


@pytest.mark.asyncio
async def test_loader_rejects_incomplete_data_origin(minimal_game_dir: Path) -> None:
    """data_origin 必须是完整原始 data 备份。"""
    origin_data_dir = minimal_game_dir / "data_origin"
    origin_data_dir.mkdir()
    _ = shutil.copy2(
        minimal_game_dir / "data" / "CommonEvents.json",
        origin_data_dir / "CommonEvents.json",
    )

    with pytest.raises(FileNotFoundError) as exc_info:
        _ = await load_game_data(minimal_game_dir)

    message = str(exc_info.value)
    assert "原始 data 备份" in message
    assert "Animations.json" in message


@pytest.mark.asyncio
async def test_loader_separates_translation_source_and_active_runtime_data(minimal_game_dir: Path) -> None:
    """翻译源读取完整 data_origin，当前运行视图仍报告激活 data 损坏。"""
    _ = shutil.copytree(minimal_game_dir / "data", minimal_game_dir / "data_origin")
    _ = shutil.copy2(minimal_game_dir / "js" / "plugins.js", minimal_game_dir / "js" / "plugins_origin.js")
    (minimal_game_dir / "data" / "Animations.json").unlink()

    translation_source_data = await load_game_data_for_view(
        minimal_game_dir,
        source_view=GameFileView.TRANSLATION_SOURCE,
        include_plugin_source_files=False,
    )

    assert translation_source_data.system.gameTitle == "テストゲーム"
    with pytest.raises(FileNotFoundError) as exc_info:
        _ = await load_game_data_for_view(
            minimal_game_dir,
            source_view=GameFileView.ACTIVE_RUNTIME,
        )

    message = str(exc_info.value)
    assert "激活数据目录" in message
    assert "Animations.json" in message
