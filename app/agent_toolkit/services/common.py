"""Agent 工具箱服务共享依赖与辅助函数。"""

from __future__ import annotations

import json
import platform
import re
import shutil
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Protocol, cast

import aiofiles

from app.agent_toolkit.placeholder_scan import (
    PlaceholderCandidate,
    count_uncovered_candidates,
    placeholder_candidates_to_details,
    scan_placeholder_candidates,
)
from app.agent_toolkit.reports import AgentIssue, AgentReport, issue
from app.application.font_replacement import resolve_replacement_font_path
from app.config import (
    STRUCTURED_PLACEHOLDER_RULES_FILE_NAME,
    SettingOverrides,
    empty_structured_placeholder_rules_payload,
    load_custom_placeholder_rules_text,
    load_structured_placeholder_rules_text,
)
from app.config.environment import load_environment_overrides
from app.event_command_text import (
    EventCommandTextExtraction,
    build_event_command_rule_records_from_import,
    export_event_commands_json_file,
    parse_event_command_rule_import_text,
    resolve_event_command_codes,
)
from app.game_analysis import GameAnalysisContext
from app.language import DEFAULT_SOURCE_LANGUAGE, SourceLanguage
from app.llm import ChatMessage, LLMHandler
from app.native_quality import (
    collect_native_quality_details,
    collect_native_write_protocol_details,
    native_thread_count,
)
from app.note_tag_text import (
    NoteTagTextExtraction,
    build_note_tag_rule_records_from_import,
    export_note_tag_candidates_file,
    parse_note_tag_rule_import_text,
)
from app.note_tag_text.sources import note_file_pattern_matches
from app.persistence import GameRegistry, TargetGameSession, ensure_db_directory
from app.persistence.repository import current_timestamp_text
from app.plugin_text import (
    PluginTextExtraction,
    build_plugin_rule_records_from_import,
    collect_plugin_json_string_leaf_candidates,
    export_plugins_json_file,
    extract_plugin_name,
    parse_plugin_rule_import_text,
)
from app.rmmz.control_codes import (
    REAL_LINE_BREAK_MARKER,
    REAL_LINE_BREAK_PLACEHOLDER,
    ControlSequenceSpan,
    CustomPlaceholderRule,
    StructuredPlaceholderRule,
)
from app.rmmz.game_file_view import GameFileView
from app.rmmz.json_types import coerce_json_value, ensure_json_array, ensure_json_object, ensure_json_string_list
from app.rmmz.loader import load_active_runtime_game_data, load_game_data_for_view
from app.rmmz.placeholder_mapping import (
    OriginalPlaceholderQueues,
    build_original_placeholder_queues,
    consume_original_placeholder,
)
from app.rmmz.schema import (
    PLUGINS_FILE_NAME,
    EventCommandTextRuleRecord,
    GameData,
    LlmFailureRecord,
    NoteTagTextRuleRecord,
    PlaceholderRuleRecord,
    PluginTextRuleRecord,
    SourceResidualRuleRecord,
    StructuredPlaceholderRuleRecord,
    TranslationData,
    TranslationErrorItem,
    TranslationItem,
)
from app.rmmz.text_layout import (
    normalize_translated_wrapping_punctuation,
    split_overwide_lines,
)
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import JsonArray, JsonObject, JsonValue, TextRules
from app.runtime_paths import resolve_app_path
from app.source_residual import (
    SourceResidualRuleSet,
    build_source_residual_rule_records_from_import,
    check_source_residual_for_item,
    parse_source_residual_rule_import_text,
)
from app.terminology import (
    TerminologyCategory,
    TerminologyExtraction,
    TerminologyGlossary,
    TerminologyRegistry,
    export_terminology_artifacts,
    load_terminology_glossary,
    load_terminology_registry,
)
from app.terminology.files import write_field_terms_json, write_glossary_json
from app.text_scope import (
    TextScopeEntry,
    TextScopeResult,
    collect_translation_data_paths,
    read_fresh_plugin_text_rules,
)
from app.translation.text_structure import (
    count_literal_line_breaks,
    count_real_line_breaks,
    validate_translation_text_structure,
)
from app.utils.config_loader_utils import load_setting, resolve_setting_path

type LlmCheckFunc = Callable[[LLMHandler, str], Awaitable[None]]
type QualityProgressCallbacks = tuple[Callable[[int, int], None], Callable[[int], None], Callable[[str], None]]


class AgentServiceContext(Protocol):
    """声明 Agent 工具箱 mixin 方法运行时需要的门面能力。"""

    game_registry: GameRegistry
    llm_handler: LLMHandler
    llm_check: LlmCheckFunc
    setting_path: str | Path | None

    async def _load_game_data_for_view(
        self,
        session: TargetGameSession,
        *,
        source_view: GameFileView,
        include_plugin_source_files: bool = True,
        include_writable_copies: bool = False,
    ) -> GameData:
        """按显式视图加载游戏数据。"""
        ...

    async def _load_translation_source_game_data(
        self,
        session: TargetGameSession,
        *,
        include_plugin_source_files: bool = True,
        include_writable_copies: bool = False,
    ) -> GameData:
        """加载翻译源视图。"""
        ...

    async def _load_active_runtime_game_data(
        self,
        session: TargetGameSession,
        *,
        include_plugin_source_files: bool = True,
        include_writable_copies: bool = False,
    ) -> GameData:
        """加载当前运行视图。"""
        ...

    async def _extract_active_translation_data_map(
        self,
        *,
        session: TargetGameSession,
        game_data: GameData,
        text_rules: TextRules,
    ) -> dict[str, TranslationData]:
        """按当前规则提取本轮可处理文本。"""
        ...

    async def _build_game_analysis_context(
        self,
        *,
        session: TargetGameSession,
        game_data: GameData,
        text_rules: TextRules,
        translated_items: list[TranslationItem] | None = None,
        placeholder_rules: list[PlaceholderRuleRecord] | None = None,
        structured_placeholder_rules: list[StructuredPlaceholderRuleRecord] | None = None,
        include_write_probe: bool = False,
    ) -> GameAnalysisContext:
        """一次建立当前命令共享的游戏分析事实。"""
        ...

    async def _build_source_residual_rule_records(
        self,
        *,
        game_title: str,
        rules_text: str,
    ) -> list[SourceResidualRuleRecord]:
        """解析并校验源文残留例外规则记录。"""
        ...

    async def _read_fresh_plugin_text_rules(
        self,
        *,
        session: TargetGameSession,
        game_data: GameData,
    ) -> tuple[list[PluginTextRuleRecord], int]:
        """读取仍匹配当前插件配置的规则。"""
        ...

    async def _resolve_custom_rules(
        self,
        *,
        session: TargetGameSession,
        custom_placeholder_rules_text: str | None,
    ) -> tuple[CustomPlaceholderRule, ...]:
        """按覆盖优先级解析自定义占位符规则。"""
        ...

    async def _resolve_structured_rules(
        self,
        *,
        session: TargetGameSession,
    ) -> tuple[StructuredPlaceholderRule, ...]:
        """读取当前游戏的结构化占位符规则。"""
        ...

    async def _check_game(
        self,
        *,
        game_title: str,
        setting_available: bool,
        errors: list[AgentIssue],
        warnings: list[AgentIssue],
        summary: JsonObject,
        details: JsonObject,
    ) -> None:
        """检查目标游戏的数据库和文件状态。"""
        ...

    def _check_static_paths(
        self,
        *,
        errors: list[AgentIssue],
        warnings: list[AgentIssue],
        details: JsonObject,
    ) -> None:
        """检查固定目录和运行环境路径。"""
        ...

    async def validate_placeholder_rules(
        self,
        *,
        game_title: str | None,
        custom_placeholder_rules_text: str | None,
        sample_texts: Sequence[str],
        analysis_context: GameAnalysisContext | None = None,
    ) -> AgentReport:
        """校验自定义占位符规则。"""
        ...

    async def scan_placeholder_candidates(
        self,
        *,
        game_title: str,
        custom_placeholder_rules_text: str | None,
    ) -> AgentReport:
        """扫描疑似自定义控制符候选。"""
        ...

    async def validate_structured_placeholder_rules(
        self,
        *,
        game_title: str,
        rules_text: str,
        sample_texts: Sequence[str],
    ) -> AgentReport:
        """校验结构化占位符规则。"""
        ...

    async def scan_structured_placeholder_candidates(
        self,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """扫描结构化占位符规则覆盖情况。"""
        ...

    async def import_structured_placeholder_rules(
        self,
        *,
        game_title: str,
        rules_text: str,
        confirm_empty: bool = False,
    ) -> AgentReport:
        """导入结构化占位符规则。"""
        ...

    async def validate_mv_virtual_namebox_rules(
        self,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """校验 MV 虚拟名字框规则。"""
        ...

    async def validate_note_tag_rules(self, *, game_title: str, rules_text: str) -> AgentReport:
        """校验 Note 标签规则。"""
        ...

    async def validate_plugin_rules(self, *, game_title: str, rules_text: str) -> AgentReport:
        """校验插件文本规则。"""
        ...

    async def validate_plugin_source_rules(self, *, game_title: str, rules_text: str) -> AgentReport:
        """校验插件源码文本规则。"""
        ...

    async def validate_event_command_rules(self, *, game_title: str, rules_text: str) -> AgentReport:
        """校验事件指令文本规则。"""
        ...


def _noop_quality_progress_callbacks() -> QualityProgressCallbacks:
    """返回不输出进度的质量报告回调。"""
    return (_noop_set_progress, _noop_advance_progress, _noop_set_status)


def _noop_set_progress(current: int, total: int) -> None:
    """忽略绝对进度。"""
    _ = (current, total)


def _noop_advance_progress(count: int) -> None:
    """忽略推进进度。"""
    _ = count


def _noop_set_status(status: str) -> None:
    """忽略阶段状态。"""
    _ = status


TERMINOLOGY_SUBTASK_GROUPS: dict[str, tuple[TerminologyCategory, ...]] = {
    "speaker_and_actor_terms": (
        "speaker_names",
        "actor_names",
        "actor_nicknames",
        "class_names",
        "enemy_names",
    ),
    "map_and_system_terms": (
        "map_display_names",
        "system_elements",
        "system_skill_types",
        "system_weapon_types",
        "system_armor_types",
        "system_equip_types",
    ),
    "skill_and_state_terms": (
        "skill_names",
        "state_names",
    ),
    "item_terms": ("item_names",),
    "equipment_terms": (
        "weapon_names",
        "armor_names",
    ),
}


async def run_default_llm_check(llm_handler: LLMHandler, model: str) -> None:
    """执行一次轻量模型连通性检查。"""
    _ = await llm_handler.get_ai_response(
        messages=[
            ChatMessage(role="system", text="你只需要回复 OK。"),
            ChatMessage(role="user", text="OK"),
        ],
        model=model,
        temperature=0,
    )


def _append_check(details: JsonObject, name: str, status: str) -> None:
    """把检查项追加到报告明细。"""
    checks_value = details.get("checks")
    if isinstance(checks_value, list):
        checks: JsonArray = checks_value
    else:
        checks = []
        details["checks"] = checks
    check_item: JsonObject = {"name": name, "status": status}
    checks.append(check_item)


COMMON_ESCAPE_SAMPLES: dict[str, str] = {
    '\\"': '裸 \\" 双引号转义',
    "\\'": "裸 \\' 单引号转义",
    "\\/": "裸 \\/ 斜杠转义",
    "\\?": "裸 \\? 问号转义",
    "\\a": "裸 \\a 响铃转义",
    "\\b": "裸 \\b 退格转义",
    "\\f": "裸 \\f 换页转义",
    "\\n": "裸 \\n 换行标记",
    "\\r": "裸 \\r 回车标记",
    "\\t": "裸 \\t 制表标记",
    "\\v": "裸 \\v 垂直制表转义",
    "\\x41": "裸 \\xHH 十六进制转义",
    "\\u3042": "裸 \\uXXXX Unicode 转义",
    "\\U0001F600": "裸 \\UXXXXXXXX Unicode 转义",
    "\\012": "裸八进制转义",
}
PLAIN_TEXT_RULE_SAMPLES: tuple[str, ...] = (
    "普通中文文本",
    "日本語本文",
    "plain visible text",
)
SUSPICIOUS_CONTROL_BOUNDARY_CHARS: frozenset[str] = frozenset("」』】）〕〉》")


def _append_placeholder_rule_safety_issues(
    *,
    rule: CustomPlaceholderRule,
    errors: list[AgentIssue],
    warnings: list[AgentIssue],
) -> None:
    """检查自定义占位符规则是否误匹配常见正文或裸转义文本。"""
    for sample_text, label in COMMON_ESCAPE_SAMPLES.items():
        if rule.pattern.fullmatch(sample_text) is None and rule.pattern.search(sample_text) is None:
            continue
        errors.append(
            issue(
                "placeholder_rule_matches_common_escape",
                f"规则 {rule.pattern_text} 会匹配{label}，容易把合法文本误判为占位符",
            )
        )
    for sample_text in PLAIN_TEXT_RULE_SAMPLES:
        if rule.pattern.search(sample_text) is None:
            continue
        warnings.append(
            issue(
                "placeholder_rule_matches_plain_text",
                f"规则 {rule.pattern_text} 会匹配普通正文样例 `{sample_text}`，请确认没有过宽吞掉玩家可见文本",
            )
        )
        return


def _build_unprotected_control_warnings(
    sample_texts: Sequence[str],
    text_rules: TextRules,
) -> list[AgentIssue]:
    """根据样本文本提示非 ASCII 括号或未闭合控制片段风险。"""
    suspicious_candidates: list[str] = []
    for sample_text in sample_texts:
        for candidate in text_rules.iter_unprotected_control_sequence_candidates(sample_text):
            if not _is_suspicious_unprotected_control(candidate.original):
                continue
            if candidate.original in suspicious_candidates:
                continue
            suspicious_candidates.append(candidate.original)
            if len(suspicious_candidates) >= 5:
                break
        if len(suspicious_candidates) >= 5:
            break

    if not suspicious_candidates:
        return []

    formatted_candidates = "；".join(
        f"{candidate} ({_format_code_points(candidate)})" for candidate in suspicious_candidates
    )
    return [
        issue(
            "unprotected_control_unicode_boundary",
            f"发现疑似非 ASCII 括号或未闭合控制片段，请核验 Unicode code point 后使用精确规则，禁止猜成 ASCII ]：{formatted_candidates}",
        )
    ]


def _is_suspicious_unprotected_control(candidate: str) -> bool:
    """判断裸露控制符是否包含容易被终端乱码掩盖的边界字符。"""
    if "[" in candidate and "]" not in candidate:
        return True
    return any(char in SUSPICIOUS_CONTROL_BOUNDARY_CHARS for char in candidate)


def _format_code_points(text: str) -> str:
    """把短文本格式化为 Unicode code point 列表。"""
    return " ".join(f"U+{ord(char):04X}" for char in text)


async def _write_json_object(path: Path, payload: JsonObject) -> None:
    """把 Agent 工作区 JSON 对象写成 UTF-8 可读文件。"""
    await _write_json_value(path, payload)


async def _write_json_value(path: Path, payload: JsonValue) -> None:
    """把 Agent 工作区 JSON 值写成 UTF-8 可读文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as file:
        _ = await file.write(f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")


async def _write_terminology_subtask_files(*, field_terms_path: Path, subtasks_dir: Path) -> JsonObject:
    """按字段译名类别生成主代理派发子代理用的独立候选文件。"""
    registry = await load_terminology_registry(field_terms_path=field_terms_path)
    category_map = registry.as_category_map()
    sources_dir = subtasks_dir / "sources"
    candidates_dir = subtasks_dir / "candidates"
    summary: JsonObject = {}
    for group_name, categories in TERMINOLOGY_SUBTASK_GROUPS.items():
        payload: JsonObject = {}
        entry_count = 0
        for category in categories:
            entries = category_map[category]
            entry_count += len(entries)
            category_payload: JsonObject = {}
            for source_text, translated_text in entries.items():
                category_payload[source_text] = translated_text
            payload[category] = category_payload
        source_path = sources_dir / f"{group_name}.json"
        candidate_path = candidates_dir / f"{group_name}.json"
        await _write_json_object(source_path, payload)
        await _write_json_object(candidate_path, payload)
        summary[group_name] = {
            "categories": list(categories),
            "entry_count": entry_count,
            "source": str(source_path),
            "candidate": str(candidate_path),
        }
    return summary


def _agent_workflow_manifest(
    *,
    engine_kind: str,
    terminology_subtask_summary: JsonObject,
) -> JsonObject:
    """生成写入 manifest 的 Agent 工作流说明。"""
    manifest: JsonObject = {
        "subagent_rounds": [
            {
                "round": 1,
                "name": "terminology_candidates",
                "owner": "主代理",
                "description": "主代理按字段译名类别拆分任务，子代理只写候选文件；主代理必须先阅读术语概念和正文术语表清洗规则，再逐项审查、统一译名、亲自修改并合并回 terminology/field-terms.json，同时维护非字段表副本的 terminology/glossary.json 后才能导入数据库。",
                "subtasks": terminology_subtask_summary,
                "final_file": "terminology/field-terms.json",
                "glossary_file": "terminology/glossary.json",
                "import_command": "import-terminology --game <游戏标题> --input <工作区>/terminology/field-terms.json --glossary-input <工作区>/terminology/glossary.json",
            },
            {
                "round": 2,
                "name": "external_text_rules",
                "owner": "主代理",
                "description": "术语表导入后，主代理再派发插件规则、事件指令规则和 Note 标签规则三个子代理，并逐项 validate/import。",
                "subtasks": {
                    "plugin-rules": "plugin-rules.json",
                    "event-command-rules": "event-command-rules.json",
                    "note-tag-rules": "note-tag-rules.json",
                },
            },
        ],
        "placeholder_phase": {
            "owner": "主代理",
            "description": "两轮子代理任务全部完成并导入后，主代理才能亲自生成、审查、覆盖扫描、校验并导入占位符规则。",
        },
    }
    if engine_kind == "mv":
        manifest["main_agent_rounds"] = [
            {
                "round": 0,
                "name": "mv_virtual_namebox_rules",
                "owner": "主代理",
                "description": "MV 游戏必须先由主代理阅读 MV 虚拟名字框规则文档，填写 mv-virtual-namebox-rules.json，运行 validate 后逐条审查 details.newly_matched_candidates，确认非说话人样本已被规则排除，再导入规则。",
                "candidate_file": "mv-virtual-namebox-candidates.json",
                "final_file": "mv-virtual-namebox-rules.json",
                "import_command": "import-mv-virtual-namebox-rules --game <游戏标题> --input <工作区>/mv-virtual-namebox-rules.json",
            }
        ]
    return manifest


def _merge_terminology_registry(
    *,
    exported_registry: TerminologyRegistry,
    stored_registry: TerminologyRegistry,
) -> TerminologyRegistry:
    """把数据库已有译名回填到当前游戏重新导出的术语表键集合。"""
    stored_map = stored_registry.as_category_map()
    merged_map: dict[TerminologyCategory, dict[str, str]] = {
        category: {
            source_text: stored_map[category].get(source_text, translated_text)
            for source_text, translated_text in exported_entries.items()
        }
        for category, exported_entries in exported_registry.as_category_map().items()
    }
    return TerminologyRegistry.from_category_map(merged_map)


def _plugin_rule_records_to_import_json(records: Sequence[PluginTextRuleRecord]) -> JsonArray:
    """把数据库插件规则还原为外部 Agent 可编辑的导入 JSON。"""
    return [
        {
            "plugin_index": record.plugin_index,
            "plugin_name": record.plugin_name,
            "paths": _string_lines_to_json_array(record.path_templates),
        }
        for record in sorted(records, key=lambda item: (item.plugin_index, item.plugin_name))
    ]


def _collect_plugin_json_string_leaf_candidate_details(game_data: GameData) -> JsonArray:
    """生成插件 JSON 字符串参数内部字符串叶子候选。"""
    candidates: JsonArray = []
    for plugin_index, plugin in enumerate(game_data.plugins_js):
        plugin_name = extract_plugin_name(plugin, plugin_index)
        candidates.extend(
            collect_plugin_json_string_leaf_candidates(
                plugin_index=plugin_index,
                plugin_name=plugin_name,
                plugin=plugin,
            )
        )
    return candidates


def _note_tag_rule_records_to_import_json(records: Sequence[NoteTagTextRuleRecord]) -> JsonObject:
    """把数据库 Note 标签规则还原为外部 Agent 可编辑的导入 JSON。"""
    payload: JsonObject = {}
    for record in sorted(records, key=lambda item: item.file_name):
        payload[record.file_name] = _string_lines_to_json_array(record.tag_names)
    return payload


def _event_command_rule_records_to_import_json(records: Sequence[EventCommandTextRuleRecord]) -> JsonObject:
    """把数据库事件指令规则还原为外部 Agent 可编辑的导入 JSON。"""
    payload: JsonObject = {}
    for record in sorted(records, key=lambda item: (item.command_code, _event_rule_filter_sort_key(item))):
        command_key = str(record.command_code)
        specs = payload.get(command_key)
        if not isinstance(specs, list):
            specs = []
            payload[command_key] = specs
        specs.append(
            {
                "match": {
                    str(parameter_filter.index): parameter_filter.value for parameter_filter in record.parameter_filters
                },
                "paths": _string_lines_to_json_array(record.path_templates),
            }
        )
    return payload


def _event_rule_filter_sort_key(record: EventCommandTextRuleRecord) -> tuple[tuple[int, str], ...]:
    """生成事件指令规则回填时的稳定排序键。"""
    return tuple((parameter_filter.index, parameter_filter.value) for parameter_filter in record.parameter_filters)


def _placeholder_rule_records_to_import_json(records: Sequence[PlaceholderRuleRecord]) -> JsonObject:
    """把数据库占位符规则还原为外部 Agent 可编辑的导入 JSON。"""
    return {record.pattern_text: record.placeholder_template for record in records}


def _structured_placeholder_rule_records_to_import_json(
    records: Sequence[StructuredPlaceholderRuleRecord],
) -> JsonObject:
    """把数据库结构化占位符规则还原为外部 Agent 可编辑的导入 JSON。"""
    if not records:
        return empty_structured_placeholder_rules_payload()
    paired_shell_rules: JsonArray = []
    for record in sorted(records, key=lambda item: item.rule_name):
        paired_shell_rules.append(
            {
                "name": record.rule_name,
                "pattern": record.pattern_text,
                "translatable_group": record.translatable_group,
                "protected_groups": {
                    group_name: placeholder_template
                    for group_name, placeholder_template in sorted(record.protected_groups.items())
                },
            }
        )
    return {"paired_shell_rules": paired_shell_rules}


def _collect_active_translation_location_paths(translation_data_items: Iterable[TranslationData]) -> list[str]:
    """按提取顺序收集当前活跃正文定位路径并去重。"""
    location_paths: list[str] = []
    seen_paths: set[str] = set()
    for translation_data in translation_data_items:
        for item in translation_data.translation_items:
            if item.location_path in seen_paths:
                continue
            location_paths.append(item.location_path)
            seen_paths.add(item.location_path)
    return location_paths


async def _read_reset_translation_location_paths(input_path: Path) -> list[str]:
    """读取 reset-translations 的最小 JSON 输入结构。"""
    async with aiofiles.open(input_path, "r", encoding="utf-8-sig") as file:
        raw_payload = cast(object, json.loads(await file.read()))
    payload = ensure_json_object(coerce_json_value(raw_payload), "reset-translations")
    raw_paths = payload.get("location_paths")
    if raw_paths is None:
        raise TypeError("reset-translations.location_paths 必须是字符串数组")
    location_paths = ensure_json_string_list(raw_paths, "reset-translations.location_paths")
    if not location_paths:
        raise ValueError("location_paths 不能为空")
    duplicate_paths = sorted(path for path, count in Counter(location_paths).items() if count > 1)
    if duplicate_paths:
        joined_paths = "、".join(duplicate_paths)
        raise ValueError(f"location_paths 不得重复: {joined_paths}")
    return location_paths


def _build_manual_translation_template_entry(
    *,
    item: TranslationItem,
    text_rules: TextRules,
    translation_lines: list[str],
) -> JsonObject:
    """把当前提取条目转换成手动填写译文表条目。"""
    cloned_item = item.model_copy(deep=True)
    cloned_item.build_placeholders(text_rules)
    restored_translation_lines = _restore_template_translation_lines(
        item=cloned_item,
        translation_lines=translation_lines,
    )
    return {
        "item_type": cloned_item.item_type,
        "role": cloned_item.role,
        "original_lines": _string_lines_to_json_array(cloned_item.original_lines),
        "text_for_model_lines": _string_lines_to_json_array(cloned_item.original_lines_with_placeholders),
        "translation_lines": _string_lines_to_json_array(restored_translation_lines),
        "manual_fill_note": (
            "只改 translation_lines；text_for_model_lines 只供对照。"
            "translation_lines 必须使用 original_lines 里的游戏原始控制符，"
            "不得保留 [RMMZ_...] 或 [CUSTOM_...]。"
        ),
    }


def _restore_template_translation_lines(
    *,
    item: TranslationItem,
    translation_lines: list[str],
) -> list[str]:
    """把修复表预填译文中的程序占位符还原为游戏原始控制符。"""
    if not translation_lines:
        return []
    if not item.placeholder_map:
        return list(translation_lines)

    item.translation_lines_with_placeholders = list(translation_lines)
    item.restore_placeholders()
    return list(item.translation_lines)


def _build_translation_line_break_count_detail(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
) -> JsonObject:
    """生成手工填写译文失败时需要对照的换行数量统计。"""
    cloned_item = item.model_copy(deep=True)
    cloned_item.build_placeholders(text_rules)
    normalized_lines = text_rules.normalize_translation_lines(translation_lines)
    placeholder_queues = build_original_placeholder_queues(
        item=cloned_item,
        text_rules=text_rules,
    )
    translation_lines_with_placeholders = [
        _mask_translation_controls(
            line=line,
            item=cloned_item,
            text_rules=text_rules,
            placeholder_queues=placeholder_queues,
        )
        for line in normalized_lines
    ]
    original_lines = cloned_item.original_lines_with_placeholders or cloned_item.original_lines
    return {
        "expected_real_line_break_count": count_real_line_breaks(original_lines),
        "actual_real_line_break_count": count_real_line_breaks(translation_lines_with_placeholders),
        "expected_literal_line_break_count": count_literal_line_breaks(original_lines),
        "actual_literal_line_break_count": count_literal_line_breaks(translation_lines_with_placeholders),
    }


def _collect_quality_fix_problem_paths(
    *,
    quality_error_items: list[TranslationErrorItem],
    residual_details: JsonArray,
    text_structure_details: JsonArray,
    placeholder_details: JsonArray,
    overwide_details: JsonArray,
    write_back_protocol_details: JsonArray,
    active_paths: set[str],
) -> list[str]:
    """按质量报告优先级收集需要导出的唯一定位路径。"""
    location_paths: list[str] = []
    for item in quality_error_items:
        _append_unique_active_path(location_paths, item.location_path, active_paths)
    for details in (
        residual_details,
        text_structure_details,
        placeholder_details,
        overwide_details,
        write_back_protocol_details,
    ):
        for location_path in _location_paths_from_quality_details(details):
            _append_unique_active_path(location_paths, location_path, active_paths)
    return location_paths


def _build_quality_fix_categories_by_path(
    *,
    quality_error_items: list[TranslationErrorItem],
    residual_details: JsonArray,
    text_structure_details: JsonArray,
    placeholder_details: JsonArray,
    overwide_details: JsonArray,
    write_back_protocol_details: JsonArray,
    active_paths: set[str],
) -> JsonObject:
    """建立质量修复条目到问题类型的映射，方便 Agent 分工处理。"""
    categories: dict[str, list[str]] = {}
    for item in quality_error_items:
        if item.location_path in active_paths:
            categories.setdefault(item.location_path, []).append("quality_error")
    _append_quality_detail_categories(categories, residual_details, active_paths, "source_residual")
    _append_quality_detail_categories(categories, text_structure_details, active_paths, "text_structure")
    _append_quality_detail_categories(categories, placeholder_details, active_paths, "placeholder_risk")
    _append_quality_detail_categories(categories, overwide_details, active_paths, "overwide_line")
    _append_quality_detail_categories(categories, write_back_protocol_details, active_paths, "write_back_protocol")
    return {
        location_path: _string_lines_to_json_array(path_categories)
        for location_path, path_categories in categories.items()
    }


def _build_quality_error_category_counts(quality_error_items: list[TranslationErrorItem]) -> JsonObject:
    """按手工修复视角统计最新翻译运行中的质量错误类别。"""
    category_counts: dict[str, int] = {
        "source_residual": 0,
        "text_structure": 0,
        "placeholder_risk": 0,
        "missing_translation": 0,
        "model_response_error": 0,
        "other": 0,
    }
    for item in quality_error_items:
        category_counts[_quality_error_category(item.error_type)] += 1
    return {key: value for key, value in category_counts.items()}


def _quality_error_category(error_type: str) -> str:
    """把模型检查失败原因归并为稳定摘要类别。"""
    if error_type == "源文残留":
        return "source_residual"
    if error_type in {"文本结构不匹配", "选项行数不匹配"}:
        return "text_structure"
    if error_type == "控制符不匹配":
        return "placeholder_risk"
    if error_type == "AI漏翻":
        return "missing_translation"
    if error_type == "模型返回不可解析":
        return "model_response_error"
    return "other"


def _append_quality_detail_categories(
    categories: dict[str, list[str]],
    details: JsonArray,
    active_paths: set[str],
    category: str,
) -> None:
    """把一组质量明细的问题类型追加到映射中。"""
    for location_path in _location_paths_from_quality_details(details):
        if location_path not in active_paths:
            continue
        path_categories = categories.setdefault(location_path, [])
        if category not in path_categories:
            path_categories.append(category)


def _append_unique_active_path(
    location_paths: list[str],
    location_path: str,
    active_paths: set[str],
) -> None:
    """只把当前有效且未出现过的定位路径加入列表。"""
    if location_path not in active_paths:
        return
    if location_path in location_paths:
        return
    location_paths.append(location_path)


def _location_paths_from_quality_details(details: JsonArray) -> list[str]:
    """从质量明细数组提取定位路径。"""
    location_paths: list[str] = []
    for raw_detail in details:
        if not isinstance(raw_detail, dict):
            continue
        raw_location_path = raw_detail.get("location_path")
        if not isinstance(raw_location_path, str):
            continue
        location_paths.append(raw_location_path)
    return location_paths


def _resolve_quality_fix_translation_lines(
    *,
    location_path: str,
    quality_errors_by_path: dict[str, TranslationErrorItem],
    translated_by_path: dict[str, TranslationItem],
) -> list[str]:
    """决定质量修复模板中应预填的译文行。"""
    quality_error = quality_errors_by_path.get(location_path)
    if quality_error is not None:
        return list(quality_error.translation_lines)
    translated_item = translated_by_path.get(location_path)
    if translated_item is None:
        return []
    return list(translated_item.translation_lines)


def _count_active_quality_details(details: JsonArray, active_paths: set[str]) -> int:
    """统计属于当前提取范围的质量明细数量。"""
    return sum(1 for location_path in _location_paths_from_quality_details(details) if location_path in active_paths)


def _preview_placeholder_sample(text_rules: TextRules, sample_text: str) -> JsonObject:
    """生成单条样本文本的占位符替换和还原预览。"""
    item = TranslationItem(
        location_path="placeholder-preview",
        item_type="short_text",
        original_lines=[sample_text],
    )
    item.build_placeholders(text_rules)
    item.translation_lines_with_placeholders = list(item.original_lines_with_placeholders)
    item.verify_placeholders(text_rules)
    item.restore_placeholders()
    placeholder_map: JsonObject = {placeholder: original for placeholder, original in item.placeholder_map.items()}
    text_for_model = ""
    if item.original_lines_with_placeholders:
        text_for_model = item.original_lines_with_placeholders[0]
    restored_text = ""
    if item.translation_lines:
        restored_text = item.translation_lines[0]
    return {
        "original_text": sample_text,
        "text_for_model": text_for_model,
        "restored_text": restored_text,
        "roundtrip_ok": restored_text == sample_text,
        "placeholder_map": placeholder_map,
    }


def _placeholder_preview_loses_visible_source_text(
    *,
    text_rules: TextRules,
    sample_preview: JsonObject,
) -> bool:
    """判断占位符替换是否把可翻译源语言文本整体遮蔽。"""
    original_text = sample_preview.get("original_text")
    text_for_model = sample_preview.get("text_for_model")
    if not isinstance(original_text, str) or not isinstance(text_for_model, str):
        return False
    detection_rules = TextRules.from_setting(text_rules.setting)
    if not detection_rules.should_translate_source_text(original_text):
        return False
    model_visible_text = detection_rules.placeholder_token_pattern.sub("", text_for_model)
    model_visible_text = detection_rules.strip_rm_control_sequences(model_visible_text)
    return not detection_rules.should_translate_source_text(model_visible_text)


def _build_coverage_report(
    *,
    scope: TextScopeResult,
    translated_items: list[TranslationItem],
    text_rules: TextRules,
) -> AgentReport:
    """根据统一文本清单生成覆盖审计报告。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    translated_paths = {item.location_path for item in translated_items}
    active_paths = scope.active_paths
    writable_paths = scope.writable_paths

    if scope.write_back_probe_error:
        errors.append(issue("write_probe_failed", scope.write_back_probe_error))

    if scope.stale_plugin_rules:
        errors.append(
            issue(
                "stale_plugin_rules", f"发现 {len(scope.stale_plugin_rules)} 个过期插件规则，请重新导出并导入插件规则"
            )
        )

    active_unwritable_items: JsonArray = [
        entry.to_json_object() for entry in scope.entries if entry.enters_translation and not entry.can_write_back
    ]
    if active_unwritable_items:
        errors.append(issue("coverage_unwritable", f"发现 {len(active_unwritable_items)} 条当前文本无法写进游戏文件"))

    unwritable_rule_items: JsonArray = []
    for entry in scope.entries:
        if entry.enters_translation:
            continue
        if entry.source_type == "standard_data":
            continue
        if not text_rules.should_translate_source_lines(entry.original_lines):
            continue
        unwritable_rule_items.append(entry.to_json_object())
    if unwritable_rule_items:
        errors.append(
            issue("rule_hits_unwritable", f"发现 {len(unwritable_rule_items)} 条规则命中文本没有进入当前可写范围")
        )

    missing_translation_paths = sorted(writable_paths - translated_paths)
    if missing_translation_paths:
        errors.append(
            issue(
                "coverage_missing_translation", f"存在 {len(missing_translation_paths)} 条当前可写文本还没成功保存译文"
            )
        )

    stale_translation_paths = sorted(translated_paths - writable_paths)
    if stale_translation_paths:
        errors.append(
            issue("stale_saved_translations", f"发现 {len(stale_translation_paths)} 条已保存译文不在当前可写范围内")
        )

    inactive_rule_hits: JsonArray = [
        entry.to_json_object()
        for entry in scope.entries
        if not entry.enters_translation and entry.source_type != "standard_data"
    ]
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "rule_hit_count": sum(1 for entry in scope.entries if entry.source_type != "standard_data"),
            "extractable_count": len(active_paths),
            "translated_count": len(translated_paths & active_paths),
            "writable_count": len(writable_paths),
            "pending_count": len(missing_translation_paths),
            "unwritable_count": len(active_unwritable_items),
            "unwritable_rule_hit_count": len(unwritable_rule_items),
            "stale_translation_count": len(stale_translation_paths),
            "stale_plugin_rule_count": len(scope.stale_plugin_rules),
            "write_back_probe_failed": bool(scope.write_back_probe_error),
            "write_back_probe_enabled": scope.write_back_probe_enabled,
        },
        details={
            "unwritable_items": active_unwritable_items,
            "unwritable_rule_items": unwritable_rule_items,
            "inactive_rule_hits": inactive_rule_hits,
            "pending_location_paths": _string_lines_to_json_array(missing_translation_paths),
            "stale_translation_paths": _string_lines_to_json_array(stale_translation_paths),
            "stale_plugin_rules": scope.stale_plugin_rules_json(),
            "write_back_probe_error": scope.write_back_probe_error,
            "write_back_probe_enabled": scope.write_back_probe_enabled,
        },
    )


def _validate_source_residual_rule_records(records: Sequence[SourceResidualRuleRecord]) -> list[AgentIssue]:
    """校验数据库中的源文残留例外规则仍可执行。"""
    try:
        _ = SourceResidualRuleSet.from_records(records)
    except ValueError as error:
        return [issue("source_residual_rules_invalid", f"源文残留例外规则已损坏: {error}")]
    return []


def _coverage_hard_stop_errors(report: AgentReport) -> list[AgentIssue]:
    """筛出会让后续质检失去可信写入前提的覆盖审计错误。"""
    hard_stop_codes = {
        "write_probe_failed",
        "stale_plugin_rules",
        "coverage_unwritable",
        "rule_hits_unwritable",
        "stale_saved_translations",
    }
    return [error for error in report.errors if error.code in hard_stop_codes]


def _text_scope_blocking_errors(scope: TextScopeResult) -> list[AgentIssue]:
    """把统一文本范围中的执行阻断项转换成稳定业务错误。"""
    errors: list[AgentIssue] = []
    if scope.write_back_probe_error:
        errors.append(issue("write_probe_failed", scope.write_back_probe_error))
    if scope.stale_plugin_rules:
        errors.append(
            issue(
                "stale_plugin_rules", f"发现 {len(scope.stale_plugin_rules)} 个过期插件规则，请重新导出并导入插件规则"
            )
        )
    if scope.unwritable_entries:
        errors.append(
            issue(
                "coverage_unwritable",
                f"发现 {len(scope.unwritable_entries)} 条当前文本无法写进游戏文件，请先运行 audit-coverage 查看明细",
            )
        )
    return errors


async def _read_feedback_texts(input_path: Path) -> list[str]:
    """读取反馈原文清单，支持字符串数组或包含 texts 字段的对象。"""
    async with aiofiles.open(input_path, "r", encoding="utf-8-sig") as file:
        raw_text = await file.read()
    decoded_raw = cast(object, json.loads(raw_text))
    decoded = coerce_json_value(decoded_raw)
    if isinstance(decoded, list):
        texts = [item for item in decoded if isinstance(item, str) and item.strip()]
    elif isinstance(decoded, dict):
        raw_texts = decoded.get("texts")
        if not isinstance(raw_texts, list):
            raise TypeError("反馈原文清单对象必须包含 texts 字符串数组")
        texts = [item for item in raw_texts if isinstance(item, str) and item.strip()]
    else:
        raise TypeError("反馈原文清单顶层必须是字符串数组或包含 texts 的对象")
    unique_texts: list[str] = []
    seen_texts: set[str] = set()
    for text in texts:
        normalized_text = text.strip()
        if normalized_text in seen_texts:
            continue
        unique_texts.append(normalized_text)
        seen_texts.add(normalized_text)
    if not unique_texts:
        raise ValueError("反馈原文清单不能为空")
    return unique_texts


async def _collect_feedback_text_occurrences(
    *,
    game_data: GameData,
    feedback_texts: list[str],
) -> JsonArray:
    """按游戏文件结构扫描反馈原文残留。"""
    occurrences: JsonArray = []
    for file_name, data in game_data.data.items():
        file_path = game_data.layout.data_dir / file_name
        content = await _read_text_for_line_lookup(file_path)
        for path_parts, raw_text in _iter_json_string_leaves(data):
            visible_text = normalize_visible_text_for_extraction(raw_text)
            for feedback_text in feedback_texts:
                if feedback_text not in visible_text:
                    continue
                occurrences.append(
                    {
                        "text": feedback_text,
                        "file": str(file_path),
                        "line": _line_number_for_structured_text(
                            content=content,
                            raw_text=raw_text,
                            visible_text=visible_text,
                        ),
                        "category": "游戏数据文件仍存在反馈原文",
                        "json_path": _format_json_path(path_parts),
                    }
                )
    plugins_content = await _read_text_for_line_lookup(game_data.layout.plugins_path)
    for plugin_index, plugin in enumerate(game_data.plugins_js):
        for path_parts, raw_text in _iter_json_string_leaves(plugin):
            visible_text = normalize_visible_text_for_extraction(raw_text)
            for feedback_text in feedback_texts:
                if feedback_text not in visible_text:
                    continue
                occurrences.append(
                    {
                        "text": feedback_text,
                        "file": str(game_data.layout.plugins_path),
                        "line": _line_number_for_structured_text(
                            content=plugins_content,
                            raw_text=raw_text,
                            visible_text=visible_text,
                        ),
                        "category": "插件参数或插件配置仍存在反馈原文",
                        "json_path": _format_json_path([plugin_index, *path_parts]),
                    }
                )
    plugin_source_candidates = await _collect_plugin_source_text_candidates(game_data.layout.js_dir)
    for candidate in plugin_source_candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_text = candidate.get("text")
        if not isinstance(candidate_text, str):
            continue
        for feedback_text in feedback_texts:
            if feedback_text not in candidate_text:
                continue
            occurrences.append(
                {
                    "text": feedback_text,
                    "file": candidate.get("file", ""),
                    "line": candidate.get("line", 0),
                    "category": "插件源码硬编码文本候选",
                    "api": candidate.get("api", ""),
                    "structural_flags": candidate.get("structural_flags", []),
                }
            )
    return occurrences


async def _read_text_for_line_lookup(file_path: Path) -> str:
    """读取文本文件内容，供结构化命中补充行号。"""
    if not file_path.is_file():
        return ""
    async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        return await file.read()


def _iter_json_string_leaves(value: JsonValue) -> Iterable[tuple[list[str | int], str]]:
    """遍历 JSON 值里的全部字符串叶子。"""
    if isinstance(value, str):
        yield [], value
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from (([index, *path_parts], text) for path_parts, text in _iter_json_string_leaves(item))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from (([key, *path_parts], text) for path_parts, text in _iter_json_string_leaves(item))


def _line_number_for_structured_text(
    *,
    content: str,
    raw_text: str,
    visible_text: str,
) -> int:
    """根据原始字符串或 JSON 编码字符串尽量定位文件行号。"""
    if not content:
        return 0
    candidates = [
        raw_text,
        visible_text,
        json.dumps(raw_text, ensure_ascii=False),
        json.dumps(visible_text, ensure_ascii=False),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        index = content.find(candidate)
        if index >= 0:
            return content.count("\n", 0, index) + 1
    return 0


def _format_json_path(path_parts: Sequence[str | int]) -> str:
    """把结构化路径格式化成排障用 JSONPath。"""
    path_text = "$"
    for part in path_parts:
        if isinstance(part, int):
            path_text += f"[{part}]"
        else:
            path_text += f"[{json.dumps(part, ensure_ascii=False)}]"
    return path_text


def _classify_feedback_occurrences(
    *,
    occurrences: JsonArray,
    scope: TextScopeResult,
) -> JsonArray:
    """按统一文本清单把反馈反查结果归类为结构性缺口。"""
    classified: JsonArray = []
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        occurrence_object = {key: value for key, value in occurrence.items()}
        feedback_text = occurrence_object.get("text")
        category = occurrence_object.get("category")
        if not isinstance(feedback_text, str):
            occurrence_object["gap_type"] = "rule_gap"
            occurrence_object["gap_label"] = "规则缺口"
            occurrence_object["matching_location_paths"] = []
            classified.append(occurrence_object)
            continue
        if category == "插件源码硬编码文本候选":
            occurrence_object["gap_type"] = "plugin_source_hardcoded"
            occurrence_object["gap_label"] = "插件源码硬编码"
            occurrence_object["matching_location_paths"] = []
            classified.append(occurrence_object)
            continue
        matched_entries = _scope_entries_containing_text(scope=scope, text=feedback_text)
        gap_type, gap_label = _feedback_gap_from_scope_entries(matched_entries)
        occurrence_object["gap_type"] = gap_type
        occurrence_object["gap_label"] = gap_label
        occurrence_object["matching_location_paths"] = [entry.location_path for entry in matched_entries[:10]]
        classified.append(occurrence_object)
    return classified


def _count_feedback_gap_types(occurrences: JsonArray) -> Counter[str]:
    """统计反馈反查结果中的结构性缺口类型。"""
    counter: Counter[str] = Counter()
    for occurrence in occurrences:
        if not isinstance(occurrence, dict):
            continue
        gap_type = occurrence.get("gap_type")
        if isinstance(gap_type, str):
            counter[gap_type] += 1
    return counter


def _scope_entries_containing_text(*, scope: TextScopeResult, text: str) -> list[TextScopeEntry]:
    """查找统一文本清单中包含反馈原文的结构位置。"""
    return [entry for entry in scope.entries if any(text in line for line in entry.original_lines)]


def _feedback_gap_from_scope_entries(entries: list[TextScopeEntry]) -> tuple[str, str]:
    """根据文本清单命中情况判断反馈原文残留的结构性原因。"""
    if not entries:
        return "rule_gap", "规则缺口"
    active_entries = [entry for entry in entries if entry.enters_translation]
    if not active_entries:
        return "rule_gap", "规则缺口"
    if any(not entry.can_write_back for entry in active_entries):
        return "write_gap", "写入缺口"
    if any(not entry.translated for entry in active_entries):
        return "translation_gap", "译文缺口"
    return "write_gap", "写入缺口"


PLUGIN_SOURCE_TEXT_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?P<api>drawText(?:Ex)?|setText|addCommand)\s*\(\s*(?P<quote>['\"])(?P<text>(?:\\.|(?!\2).){2,120})(?P=quote)",
    re.DOTALL,
)


async def _collect_plugin_source_text_candidates(js_dir: Path) -> JsonArray:
    """扫描插件源码中常见 UI 文本 API 的字符串参数候选。"""
    plugin_dir = js_dir / "plugins"
    if not plugin_dir.is_dir():
        return []
    candidates: JsonArray = []
    for file_path in sorted(plugin_dir.glob("*.js"), key=lambda path: path.name):
        async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as file:
            content = await file.read()
        for match in PLUGIN_SOURCE_TEXT_PATTERN.finditer(content):
            text = _unescape_js_candidate_text(match.group("text")).strip()
            if not text:
                continue
            candidates.append(
                {
                    "file": str(file_path),
                    "line": content.count("\n", 0, match.start()) + 1,
                    "api": match.group("api"),
                    "text": text,
                    "structural_flags": _plugin_source_text_structural_flags(text),
                }
            )
    return candidates


def _unescape_js_candidate_text(text: str) -> str:
    """只处理候选展示需要的常见 JavaScript 字符串转义。"""
    return text.replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\'", "'").replace(r"\"", '"').replace(r"\\", "\\")


def _plugin_source_text_structural_flags(text: str) -> JsonArray:
    """给源码字符串候选附加结构提示，不据此丢弃候选。"""
    flags: JsonArray = []
    lowered_text = text.lower()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        flags.append("number_like")
    if re.search(r"\.(?:png|jpg|jpeg|webp|ogg|m4a|mp3|json|js)$", lowered_text):
        flags.append("resource_path_like")
    if re.fullmatch(r"[A-Za-z0-9_./:-]+", text) and ("_" in text or "/" in text):
        flags.append("identifier_or_path_like")
    return flags


def _current_python_major_minor() -> tuple[int, int]:
    """读取当前 Python 主次版本号。"""
    version_parts = platform.python_version_tuple()
    return int(version_parts[0]), int(version_parts[1])


CUSTOM_MARKER_WITH_PARAMS_PATTERN: re.Pattern[str] = re.compile(r"^\\(?P<code>[A-Za-z]+)\d*\[[^\]\r\n]+\]$")
CUSTOM_MARKER_WITHOUT_PARAMS_PATTERN: re.Pattern[str] = re.compile(r"^\\(?P<code>[A-Za-z]+)\d*$")
JOINED_TEXT_CONTROL_BOUNDARY_PATTERN: re.Pattern[str] = re.compile(r"^\\[A-Za-z]*[a-z][A-Za-z]*$")


def _build_custom_placeholder_rule_draft(
    candidates: Sequence[PlaceholderCandidate],
) -> dict[str, str]:
    """把未覆盖候选折叠成适合 Agent 编辑的规则草稿。"""
    draft_rules: dict[str, str] = {}
    for candidate in candidates:
        if candidate.covered:
            continue
        if candidate.coverage_conflict:
            continue
        if _needs_manual_joined_text_boundary(candidate.marker):
            continue
        pattern_text, placeholder_template = _draft_custom_placeholder_rule(candidate.marker)
        _ = draft_rules.setdefault(pattern_text, placeholder_template)
    return draft_rules


def _joined_text_boundary_markers(
    candidates: Sequence[PlaceholderCandidate],
) -> list[str]:
    """列出必须人工确认边界的裸字母控制符候选。"""
    return sorted(
        {
            candidate.marker
            for candidate in candidates
            if not candidate.covered and _needs_manual_joined_text_boundary(candidate.marker)
        },
        key=str.lower,
    )


def _needs_manual_joined_text_boundary(marker: str) -> bool:
    """识别可能由裸控制符紧贴正文组成的字母候选。"""
    return JOINED_TEXT_CONTROL_BOUNDARY_PATTERN.fullmatch(marker) is not None


def _build_joined_text_boundary_warnings(markers: Sequence[str]) -> list[AgentIssue]:
    """提示主代理必须人工确认紧贴正文的控制符边界。"""
    if not markers:
        return []
    preview = "、".join(markers[:5])
    suffix = "" if len(markers) <= 5 else f" 等 {len(markers)} 个"
    return [
        issue(
            "placeholder_boundary_needs_review",
            f"发现疑似控制符紧贴正文，工具不会自动猜边界，请查插件源码后手写精确规则: {preview}{suffix}",
        )
    ]


def _draft_custom_placeholder_rule(marker: str) -> tuple[str, str]:
    """为单个候选生成通用正则和合法语义化占位符模板。"""
    with_params_match = CUSTOM_MARKER_WITH_PARAMS_PATTERN.fullmatch(marker)
    if with_params_match is not None:
        code = with_params_match.group("code").upper()
        pattern_text = rf"(?i)\\{code}\d*\[[^\]\r\n]+\]"
        return pattern_text, _custom_placeholder_template_for_code(code)

    without_params_match = CUSTOM_MARKER_WITHOUT_PARAMS_PATTERN.fullmatch(marker)
    if without_params_match is not None:
        raw_code = without_params_match.group("code")
        semantic_code = raw_code.upper()
        pattern_text = rf"\\{re.escape(raw_code)}\d*(?![A-Za-z\[])"
        return pattern_text, _custom_placeholder_template_for_code(semantic_code)

    return re.escape(marker), "[CUSTOM_UNKNOWN_CONTROL_MARKER_{index}]"


def _custom_placeholder_template_for_code(code: str) -> str:
    """按控制符前缀给出 Agent 可理解的默认占位符名称。"""
    semantic_names: dict[str, str] = {
        "F": "FACE_PORTRAIT",
        "FH": "FACE_PORTRAIT_HIDE",
        "AA": "PLUGIN_AA_MARKER",
        "AC": "PLUGIN_AC_MARKER",
        "AN": "PLUGIN_ACTOR_NAME_MARKER",
        "MT": "PLUGIN_MESSAGE_TAG",
    }
    semantic_name = semantic_names.get(code, f"PLUGIN_{code}_MARKER")
    return f"[CUSTOM_{semantic_name}_{{index}}]"


def _collect_placeholder_preview_samples(
    translation_data_map: dict[str, TranslationData],
    text_rules: TextRules,
) -> list[str]:
    """为占位符校验收集少量当前正文中的控制符样本文本。"""
    samples: list[str] = []
    for translation_data in translation_data_map.values():
        for item in translation_data.translation_items:
            for text in item.original_lines:
                if not text_rules.iter_control_sequence_spans(text):
                    continue
                samples.append(text)
                if len(samples) >= 10:
                    return samples
    return samples


def _collect_unprotected_control_warning_samples(
    translation_data_map: dict[str, TranslationData],
    text_rules: TextRules,
) -> list[str]:
    """收集当前正文中疑似存在裸露控制符边界风险的样本文本。"""
    samples: list[str] = []
    for translation_data in translation_data_map.values():
        for item in translation_data.translation_items:
            for text in item.original_lines:
                if not text_rules.iter_unprotected_control_sequence_candidates(text):
                    continue
                samples.append(text)
                if len(samples) >= 10:
                    return samples
    return samples


def _validate_terminology_registry(registry: TerminologyRegistry) -> list[AgentIssue]:
    """检查字段译名表填写质量。"""
    warnings: list[AgentIssue] = []
    category_map = registry.as_category_map()
    empty_count = registry.total_entry_count() - registry.filled_entry_count()
    if empty_count:
        warnings.append(issue("terminology_empty_translation", f"字段译名表存在 {empty_count} 个空译名"))
    translated_counter = Counter(
        value.strip() for entries in category_map.values() for value in entries.values() if value.strip()
    )
    duplicate_count = sum(1 for count in translated_counter.values() if count > 1)
    if duplicate_count:
        warnings.append(
            issue("terminology_duplicate_translation", f"字段译名表存在 {duplicate_count} 组重复译名，需要确认是否合理")
        )
    variant_mismatch_count = _count_name_variant_mismatches(registry.speaker_names)
    if variant_mismatch_count:
        warnings.append(
            issue("terminology_variant_mismatch", f"说话人变体存在 {variant_mismatch_count} 处译名不一致风险")
        )
    return warnings


def _collect_terminology_duplicate_translation_samples(
    registry: TerminologyRegistry,
    *,
    group_limit: int = 10,
    source_limit: int = 20,
) -> JsonArray:
    """收集字段译名表重复译名样例，方便主代理审查同译是否合理。"""
    translations: dict[str, JsonArray] = {}
    for category, entries in registry.as_category_map().items():
        for source_text, translated_text in entries.items():
            translation = translated_text.strip()
            if not translation:
                continue
            source_detail: JsonObject = {
                "category": category,
                "source_text": source_text,
            }
            translations.setdefault(translation, []).append(source_detail)

    samples: JsonArray = []
    for translation in sorted(translations):
        sources = translations[translation]
        if len(sources) <= 1:
            continue
        samples.append(
            {
                "translation": translation,
                "sources": sources[:source_limit],
            }
        )
        if len(samples) >= group_limit:
            break
    return samples


def _validate_terminology_registry_shape(
    *,
    imported_registry: TerminologyRegistry,
    expected_registry: TerminologyRegistry,
    errors: list[AgentIssue],
) -> None:
    """检查工作区术语表 key 集合是否匹配当前游戏。"""
    imported_map = imported_registry.as_category_map()
    expected_map = expected_registry.as_category_map()
    for category, expected_entries in expected_map.items():
        imported_entries = imported_map[category]
        missing_count = len(set(expected_entries) - set(imported_entries))
        extra_count = len(set(imported_entries) - set(expected_entries))
        if missing_count:
            errors.append(
                issue("terminology_missing_terms", f"字段译名表 {category} 缺少 {missing_count} 个当前游戏词条")
            )
        if extra_count:
            errors.append(
                issue("terminology_extra_terms", f"字段译名表 {category} 多出 {extra_count} 个当前游戏不存在的词条")
            )


def _first_original_line_samples(items: Iterable[TranslationItem], limit: int = 5) -> JsonArray:
    """提取少量首行样例，避免报告输出完整上下文。"""
    samples: JsonArray = []
    for item in items:
        if not item.original_lines:
            continue
        samples.append(item.original_lines[0])
        if len(samples) >= limit:
            break
    return samples


def _build_rule_metric_detail(
    *,
    record_items: Sequence[TranslationItem],
    translated_paths: set[str],
    unwritable_items_by_path: dict[str, JsonArray],
) -> JsonObject:
    """生成单条外部规则的命中、保存和可写统计。"""
    return {
        "hit_count": len(record_items),
        "extractable_count": len(record_items),
        "translated_count": sum(1 for item in record_items if item.location_path in translated_paths),
        "writable_count": sum(1 for item in record_items if item.location_path not in unwritable_items_by_path),
        "unwritable_items": [
            item
            for extracted_item in record_items
            for item in unwritable_items_by_path.get(extracted_item.location_path, [])
        ],
        "samples": _first_original_line_samples(record_items),
    }


def _note_tag_item_matches_rule(*, item: TranslationItem, rule_record: NoteTagTextRuleRecord) -> bool:
    """判断 Note 标签译文条目是否来自指定规则。"""
    parts = item.location_path.split("/")
    if len(parts) < 3 or parts[-2] != "note":
        return False
    file_name = parts[0]
    tag_name = parts[-1]
    return tag_name in rule_record.tag_names and note_file_pattern_matches(
        file_name=file_name, file_pattern=rule_record.file_name
    )


def _preview_event_command_write_back(
    *,
    game_data: GameData,
    extracted_items: list[TranslationItem],
    text_rules: TextRules,
) -> None:
    """用 Rust 写入协议检查规则命中项是否可写。"""
    _ = text_rules
    unwritable_items = _collect_write_protocol_unwritable_items(
        game_data=game_data,
        extracted_items=extracted_items,
    )
    if unwritable_items:
        raise ValueError(f"写入协议检查发现 {len(unwritable_items)} 个不可写命中项")


def _collect_write_protocol_unwritable_items(
    *,
    game_data: GameData,
    extracted_items: list[TranslationItem],
) -> JsonArray:
    """用统一写入协议检查规则命中项是否具备结构性写入位置。"""
    if not extracted_items:
        return []
    probe_items: list[TranslationItem] = []
    for item in extracted_items:
        probe_item = item.model_copy(
            update={"translation_lines": _build_write_back_probe_lines(item)},
            deep=False,
        )
        probe_items.append(probe_item)
    return collect_native_write_protocol_details(
        game_data=game_data.data,
        plugins_js=[plugin for plugin in game_data.plugins_js],
        items=probe_items,
    )


def _json_items_by_location_path(items: JsonArray) -> dict[str, JsonArray]:
    """按定位路径索引 JSON 明细，供规则级报告复用。"""
    indexed: dict[str, JsonArray] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        location_path = item.get("location_path")
        if not isinstance(location_path, str):
            continue
        indexed.setdefault(location_path, []).append(item)
    return indexed


def _build_write_back_probe_lines(item: TranslationItem) -> list[str]:
    """按条目类型生成不会依赖模型结果的回写探针译文。"""
    if item.item_type == "array":
        return ["回写校验" for _line in item.original_lines]
    return ["回写校验"]


def _count_protocol_sensitive_translation_items(
    *,
    items: list[TranslationItem],
    active_paths: set[str],
) -> int:
    """统计需要预演写回协议的译文条目数量。"""
    return sum(
        1
        for item in items
        if item.location_path in active_paths and _is_protocol_sensitive_translation_path(item.location_path)
    )


def _is_protocol_sensitive_translation_path(location_path: str) -> bool:
    """判断路径是否属于需要二次解析或保留标签外壳的文本。"""
    return (
        location_path.startswith(f"{PLUGINS_FILE_NAME}/")
        or "/parameters/" in location_path
        or "/note/" in location_path
    )


def _count_name_variant_mismatches(speaker_names: dict[str, str]) -> int:
    """检查带冒号或声音后缀的名字译名是否延续本体译名。"""
    mismatch_count = 0
    for source_text, translated_text in speaker_names.items():
        base_source = source_text.removesuffix("：").removesuffix(":").removesuffix("の声").strip()
        if base_source == source_text:
            continue
        base_translation = speaker_names.get(base_source)
        if base_translation and base_translation not in translated_text:
            mismatch_count += 1
    return mismatch_count


def _is_path_inside(path: Path, parent: Path) -> bool:
    """判断待删除路径是否位于工作区内部。"""
    try:
        _ = path.relative_to(parent)
        return True
    except ValueError:
        return False


def _mask_translation_controls(
    *,
    line: str,
    item: TranslationItem,
    text_rules: TextRules,
    placeholder_queues: OriginalPlaceholderQueues,
) -> str:
    """把译文中的控制符转换成占位符以便复用数量校验。"""

    def replacer(span: ControlSequenceSpan) -> str:
        """把已知控制符还原成对应占位符，未知控制符标记为风险。"""
        placeholder = consume_original_placeholder(
            queues=placeholder_queues,
            original=span.original,
        )
        if placeholder is not None:
            return placeholder
        return "[CUSTOM_UNEXPECTED_1]"

    masked_line = text_rules.replace_rm_control_sequences(line, replacer)
    if item.placeholder_map.get(REAL_LINE_BREAK_PLACEHOLDER) == REAL_LINE_BREAK_MARKER:
        return masked_line.replace(REAL_LINE_BREAK_MARKER, REAL_LINE_BREAK_PLACEHOLDER)
    return masked_line


def _prepare_manual_translation_item(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
    source_residual_rules: list[SourceResidualRuleRecord] | None = None,
) -> TranslationItem:
    """把手动译文校验成可保存的正文译文条目。"""
    if not translation_lines or not any(line.strip() for line in translation_lines):
        raise ValueError("translation_lines 不能为空")
    if item.item_type == "short_text" and len(translation_lines) != 1:
        raise ValueError("short_text 必须提供 1 行译文")
    if item.item_type == "array" and len(translation_lines) != len(item.original_lines):
        raise ValueError(f"array 必须提供 {len(item.original_lines)} 行译文")
    visible_placeholders = text_rules.collect_placeholder_tokens(translation_lines)
    if visible_placeholders:
        joined_placeholders = "、".join(sorted(visible_placeholders))
        raise ValueError(f"translation_lines 必须使用游戏原始控制符，不得保留程序占位符: {joined_placeholders}")
    normalized_translation_lines = _normalize_manual_translation_lines(
        item=item,
        translation_lines=translation_lines,
        text_rules=text_rules,
    )

    cloned_item = item.model_copy(deep=True)
    cloned_item.build_placeholders(text_rules)
    placeholder_queues = build_original_placeholder_queues(
        item=cloned_item,
        text_rules=text_rules,
    )
    cloned_item.translation_lines_with_placeholders = [
        _mask_translation_controls(
            line=line,
            item=cloned_item,
            text_rules=text_rules,
            placeholder_queues=placeholder_queues,
        )
        for line in normalized_translation_lines
    ]
    validate_translation_text_structure(
        item=cloned_item,
        translation_lines=normalized_translation_lines,
        translation_lines_with_placeholders=cloned_item.translation_lines_with_placeholders,
        text_rules=text_rules,
    )
    cloned_item.verify_placeholders(text_rules)
    cloned_item.translation_lines = list(cloned_item.translation_lines_with_placeholders)
    source_residual_rule_set = SourceResidualRuleSet.from_records(source_residual_rules or [])
    check_source_residual_for_item(
        item=cloned_item,
        text_rules=text_rules,
        rule_set=source_residual_rule_set,
    )
    cloned_item.translation_lines = list(normalized_translation_lines)
    return cloned_item


def _normalize_manual_translation_lines(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
) -> list[str]:
    """手动填写的 long_text 保存前套用与写回一致的行宽兜底。"""
    cleaned_translation_lines = text_rules.normalize_translation_lines(translation_lines)
    normalized_lines = normalize_translated_wrapping_punctuation(
        original_lines=item.original_lines,
        translation_lines=cleaned_translation_lines,
        text_rules=text_rules,
    )
    if item.item_type != "long_text":
        return normalized_lines
    return split_overwide_lines(
        lines=normalized_lines,
        location_path=item.location_path,
        text_rules=text_rules,
    )


def _build_translation_error_quality_detail(item: TranslationErrorItem) -> JsonObject:
    """把没通过项目检查的译文转换为质量报告中可定位、可修复的明细。"""
    return {
        "location_path": item.location_path,
        "item_type": item.item_type,
        "role": item.role,
        "original_lines": _string_lines_to_json_array(item.original_lines),
        "translation_lines": _string_lines_to_json_array(item.translation_lines),
        "error_type": item.error_type,
        "error_detail": _string_lines_to_json_array(item.error_detail),
        "model_response": item.model_response,
    }


def _string_lines_to_json_array(lines: list[str]) -> JsonArray:
    """把字符串行列表收窄为 JSON 数组。"""
    return [line for line in lines]


__all__: list[str] = [
    "annotations",
    "platform",
    "json",
    "re",
    "shutil",
    "Counter",
    "Awaitable",
    "Callable",
    "Iterable",
    "Sequence",
    "Path",
    "cast",
    "aiofiles",
    "PlaceholderCandidate",
    "count_uncovered_candidates",
    "placeholder_candidates_to_details",
    "scan_placeholder_candidates",
    "AgentIssue",
    "AgentReport",
    "issue",
    "resolve_replacement_font_path",
    "SettingOverrides",
    "STRUCTURED_PLACEHOLDER_RULES_FILE_NAME",
    "empty_structured_placeholder_rules_payload",
    "load_custom_placeholder_rules_text",
    "load_structured_placeholder_rules_text",
    "load_environment_overrides",
    "DEFAULT_SOURCE_LANGUAGE",
    "SourceLanguage",
    "ChatMessage",
    "LLMHandler",
    "collect_native_quality_details",
    "collect_native_write_protocol_details",
    "native_thread_count",
    "GameRegistry",
    "TargetGameSession",
    "ensure_db_directory",
    "PluginTextExtraction",
    "build_plugin_rule_records_from_import",
    "collect_plugin_json_string_leaf_candidates",
    "export_plugins_json_file",
    "extract_plugin_name",
    "parse_plugin_rule_import_text",
    "ControlSequenceSpan",
    "CustomPlaceholderRule",
    "StructuredPlaceholderRule",
    "REAL_LINE_BREAK_MARKER",
    "REAL_LINE_BREAK_PLACEHOLDER",
    "GameData",
    "EventCommandTextRuleRecord",
    "LlmFailureRecord",
    "NoteTagTextRuleRecord",
    "PLUGINS_FILE_NAME",
    "PlaceholderRuleRecord",
    "PluginTextRuleRecord",
    "SourceResidualRuleRecord",
    "StructuredPlaceholderRuleRecord",
    "TranslationData",
    "TranslationErrorItem",
    "TranslationItem",
    "JsonArray",
    "JsonObject",
    "JsonValue",
    "TextRules",
    "normalize_visible_text_for_extraction",
    "coerce_json_value",
    "ensure_json_array",
    "ensure_json_object",
    "ensure_json_string_list",
    "GameFileView",
    "load_active_runtime_game_data",
    "load_game_data_for_view",
    "resolve_app_path",
    "normalize_translated_wrapping_punctuation",
    "split_overwide_lines",
    "count_literal_line_breaks",
    "count_real_line_breaks",
    "validate_translation_text_structure",
    "load_setting",
    "resolve_setting_path",
    "EventCommandTextExtraction",
    "build_event_command_rule_records_from_import",
    "export_event_commands_json_file",
    "parse_event_command_rule_import_text",
    "resolve_event_command_codes",
    "TerminologyCategory",
    "TerminologyExtraction",
    "TerminologyGlossary",
    "TerminologyRegistry",
    "export_terminology_artifacts",
    "load_terminology_glossary",
    "load_terminology_registry",
    "write_field_terms_json",
    "write_glossary_json",
    "NoteTagTextExtraction",
    "build_note_tag_rule_records_from_import",
    "export_note_tag_candidates_file",
    "parse_note_tag_rule_import_text",
    "note_file_pattern_matches",
    "current_timestamp_text",
    "SourceResidualRuleSet",
    "build_source_residual_rule_records_from_import",
    "check_source_residual_for_item",
    "parse_source_residual_rule_import_text",
    "TextScopeEntry",
    "TextScopeResult",
    "collect_translation_data_paths",
    "read_fresh_plugin_text_rules",
    "LlmCheckFunc",
    "QualityProgressCallbacks",
    "AgentServiceContext",
    "_noop_quality_progress_callbacks",
    "_noop_set_progress",
    "_noop_advance_progress",
    "_noop_set_status",
    "TERMINOLOGY_SUBTASK_GROUPS",
    "run_default_llm_check",
    "_append_check",
    "COMMON_ESCAPE_SAMPLES",
    "PLAIN_TEXT_RULE_SAMPLES",
    "SUSPICIOUS_CONTROL_BOUNDARY_CHARS",
    "_append_placeholder_rule_safety_issues",
    "_build_unprotected_control_warnings",
    "_is_suspicious_unprotected_control",
    "_format_code_points",
    "_write_json_object",
    "_write_json_value",
    "_write_terminology_subtask_files",
    "_agent_workflow_manifest",
    "_merge_terminology_registry",
    "_plugin_rule_records_to_import_json",
    "_collect_plugin_json_string_leaf_candidate_details",
    "_note_tag_rule_records_to_import_json",
    "_event_command_rule_records_to_import_json",
    "_event_rule_filter_sort_key",
    "_placeholder_rule_records_to_import_json",
    "_structured_placeholder_rule_records_to_import_json",
    "_collect_active_translation_location_paths",
    "_read_reset_translation_location_paths",
    "_build_manual_translation_template_entry",
    "_restore_template_translation_lines",
    "_build_translation_line_break_count_detail",
    "_collect_quality_fix_problem_paths",
    "_build_quality_error_category_counts",
    "_quality_error_category",
    "_build_quality_fix_categories_by_path",
    "_append_quality_detail_categories",
    "_append_unique_active_path",
    "_location_paths_from_quality_details",
    "_resolve_quality_fix_translation_lines",
    "_count_active_quality_details",
    "_preview_placeholder_sample",
    "_placeholder_preview_loses_visible_source_text",
    "_build_coverage_report",
    "_validate_source_residual_rule_records",
    "_coverage_hard_stop_errors",
    "_text_scope_blocking_errors",
    "_read_feedback_texts",
    "_collect_feedback_text_occurrences",
    "_read_text_for_line_lookup",
    "_iter_json_string_leaves",
    "_line_number_for_structured_text",
    "_format_json_path",
    "_classify_feedback_occurrences",
    "_count_feedback_gap_types",
    "_scope_entries_containing_text",
    "_feedback_gap_from_scope_entries",
    "PLUGIN_SOURCE_TEXT_PATTERN",
    "_collect_plugin_source_text_candidates",
    "_unescape_js_candidate_text",
    "_plugin_source_text_structural_flags",
    "_current_python_major_minor",
    "CUSTOM_MARKER_WITH_PARAMS_PATTERN",
    "CUSTOM_MARKER_WITHOUT_PARAMS_PATTERN",
    "JOINED_TEXT_CONTROL_BOUNDARY_PATTERN",
    "_build_custom_placeholder_rule_draft",
    "_joined_text_boundary_markers",
    "_needs_manual_joined_text_boundary",
    "_build_joined_text_boundary_warnings",
    "_draft_custom_placeholder_rule",
    "_custom_placeholder_template_for_code",
    "_collect_placeholder_preview_samples",
    "_collect_unprotected_control_warning_samples",
    "_validate_terminology_registry",
    "_collect_terminology_duplicate_translation_samples",
    "_validate_terminology_registry_shape",
    "_first_original_line_samples",
    "_build_rule_metric_detail",
    "_note_tag_item_matches_rule",
    "_preview_event_command_write_back",
    "_collect_write_protocol_unwritable_items",
    "_json_items_by_location_path",
    "_build_write_back_probe_lines",
    "_count_protocol_sensitive_translation_items",
    "_is_protocol_sensitive_translation_path",
    "_count_name_variant_mismatches",
    "_is_path_inside",
    "_mask_translation_controls",
    "_prepare_manual_translation_item",
    "_normalize_manual_translation_lines",
    "_build_translation_error_quality_detail",
    "_string_lines_to_json_array",
]
