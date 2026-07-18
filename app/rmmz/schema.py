"""
核心业务数据模型定义模块。

本模块定义 CLI 翻译流程使用的业务模型：翻译条目、插件文本规则和
标准 RPG Maker MV/MZ 文件名常量。
"""

import re
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.rmmz.control_codes import (
    LITERAL_LINE_BREAK_PLACEHOLDER,
    REAL_LINE_BREAK_MARKER,
    REAL_LINE_BREAK_PLACEHOLDER,
)
from app.rmmz.engine import EngineKind
from app.rmmz.game_data import BaseItem, CommonEvent, MapData, System, Troop
from app.rmmz.text_rules import ControlSequenceSpan, JsonValue, TextRules, get_default_text_rules

type ItemType = Literal["long_text", "array", "short_text"]
type ErrorType = Literal["模型返回不可解析", "AI漏翻", "文本结构不匹配", "控制符不匹配", "源文残留", "选项行数不匹配"]
type TranslationRunStatus = Literal["running", "completed", "blocked", "cancelled", "failed", "stopped"]
type SourceResidualRuleType = Literal["position", "structural"]
type MvVirtualNameboxSpeakerPolicy = Literal["translate", "preserve", "actor_name"]
type PluginSourceRuntimeMappingKind = Literal["translated", "excluded"]
type LlmFailureCategory = Literal[
    "rate_limit",
    "timeout",
    "connection",
    "server",
    "conflict",
    "fatal",
    "unknown",
]


class Code(IntEnum):
    """RPG Maker 常用事件指令代码枚举。"""

    NAME = 101
    TEXT = 401
    CHOICES = 102
    SCROLL_TEXT = 405
    MV_PLUGIN_COMMAND = 356
    MZ_PLUGIN_COMMAND = 357
    PLUGIN_TEXT = 357


class TranslationItem(BaseModel):
    """单个翻译条目，负责维护控制符占位符与译文恢复。"""

    role: str | None = None
    location_path: str
    item_type: ItemType
    original_lines: list[str] = Field(default_factory=list)
    source_line_paths: list[str] = Field(default_factory=list)
    original_lines_with_placeholders: list[str] = Field(default_factory=list)
    translation_lines_with_placeholders: list[str] = Field(default_factory=list)
    translation_lines: list[str] = Field(default_factory=list)
    placeholder_map: dict[str, str] = Field(default_factory=dict)
    placeholder_counts: dict[str, int] = Field(default_factory=dict)

    def build_placeholders(self, text_rules: TextRules | None = None) -> None:
        """为原文中的 RM 控制符构建语义化占位符。"""
        rules = text_rules or get_default_text_rules()
        self.original_lines_with_placeholders.clear()
        self.placeholder_map.clear()
        self.placeholder_counts.clear()
        placeholder_sources: dict[str, str] = {}
        custom_placeholder_counter = 0
        custom_placeholder_map: dict[str, str] = {}
        custom_index_by_key: dict[str, int] = {}

        def replace_func(span: ControlSequenceSpan) -> str:
            """把单个控制符替换为结构化占位符。"""
            nonlocal custom_placeholder_counter
            original = span.original
            if span.placeholder is not None:
                placeholder = span.placeholder
            elif span.custom_template is not None:
                if span.custom_index_key is not None:
                    existing_index = custom_index_by_key.get(span.custom_index_key)
                    if existing_index is None:
                        custom_placeholder_counter += 1
                        existing_index = custom_placeholder_counter
                        custom_index_by_key[span.custom_index_key] = existing_index
                    placeholder = rules.format_custom_placeholder(
                        template=span.custom_template,
                        index=existing_index,
                    )
                else:
                    existing_placeholder = custom_placeholder_map.get(original)
                    if existing_placeholder is not None:
                        placeholder = existing_placeholder
                    else:
                        custom_placeholder_counter += 1
                        placeholder = rules.format_custom_placeholder(
                            template=span.custom_template,
                            index=custom_placeholder_counter,
                        )
                        custom_placeholder_map[original] = placeholder
            else:
                raise ValueError(f"无法为控制符生成占位符: {original}")

            existing_original = self.placeholder_map.get(placeholder)
            existing_source = placeholder_sources.get(placeholder)
            if (
                existing_original is not None
                and existing_original != original
                and (existing_source in {"custom", "structured"} or span.source in {"custom", "structured"})
            ):
                detail = f"{existing_original} / {original}"
                raise ValueError(f"自定义占位符 {placeholder} 同时匹配了多个不同片段: {detail}")

            if existing_original is None:
                self.placeholder_map[placeholder] = original
                placeholder_sources[placeholder] = span.source
            self.placeholder_counts[placeholder] = self.placeholder_counts.get(placeholder, 0) + 1
            return placeholder

        def replace_real_line_breaks(line: str) -> str:
            """把字段内部真实换行纳入标准占位符计数。"""
            real_break_count = line.count(REAL_LINE_BREAK_MARKER)
            if real_break_count == 0:
                return line

            existing_original = self.placeholder_map.get(REAL_LINE_BREAK_PLACEHOLDER)
            if existing_original is None:
                self.placeholder_map[REAL_LINE_BREAK_PLACEHOLDER] = REAL_LINE_BREAK_MARKER
                placeholder_sources[REAL_LINE_BREAK_PLACEHOLDER] = "standard"
            elif existing_original != REAL_LINE_BREAK_MARKER:
                raise ValueError(f"占位符 {REAL_LINE_BREAK_PLACEHOLDER} 同时匹配了多个不同片段")
            self.placeholder_counts[REAL_LINE_BREAK_PLACEHOLDER] = (
                self.placeholder_counts.get(REAL_LINE_BREAK_PLACEHOLDER, 0) + real_break_count
            )
            return line.replace(REAL_LINE_BREAK_MARKER, REAL_LINE_BREAK_PLACEHOLDER)

        def replace_line(line: str) -> str:
            """按标准控制符、自定义规则、结构化规则和真实换行顺序构建模型输入文本。"""
            masked_line = rules.replace_rm_control_sequences(line, replace_func)
            return replace_real_line_breaks(masked_line)

        self.original_lines_with_placeholders = [replace_line(line) for line in self.original_lines]

    def verify_placeholders(self, text_rules: TextRules | None = None) -> None:
        """校验模型返回的占位符数量是否与原文一致。"""
        rules = text_rules or get_default_text_rules()
        errors: list[str] = []
        original_placeholders = rules.collect_placeholder_tokens(self.original_lines_with_placeholders)
        translated_placeholders = rules.collect_placeholder_tokens(self.translation_lines_with_placeholders)

        extra_placeholders = translated_placeholders - original_placeholders
        if extra_placeholders:
            joined_placeholders = "、".join(sorted(extra_placeholders))
            if not original_placeholders:
                errors.append(f"原文不包含任何占位符，但译文新增了以下占位符: {joined_placeholders}")
            else:
                errors.append(f"译文新增了原文没有的占位符: {joined_placeholders}")

        if self.placeholder_map:
            combined_text = "".join(self.translation_lines_with_placeholders).lower()
            for placeholder, expected_count in self.placeholder_counts.items():
                actual_count = combined_text.count(placeholder.lower())
                if placeholder.lower() == LITERAL_LINE_BREAK_PLACEHOLDER.lower():
                    if actual_count < expected_count:
                        errors.append(
                            f"占位符 {placeholder} 数量不足 (至少需要: {expected_count}, 实际: {actual_count})"
                        )
                    continue
                if actual_count != expected_count:
                    errors.append(f"占位符 {placeholder} 数量错误 (期望: {expected_count}, 实际: {actual_count})")

        original_raw_controls = rules.collect_unprotected_control_sequences(self.original_lines)
        translated_raw_controls = rules.collect_unprotected_control_sequences(self.translation_lines_with_placeholders)
        if original_raw_controls != translated_raw_controls:
            control_error = (
                "疑似控制符不一致，未被占位符规则覆盖的反斜杠控制片段必须原样保留 "
                f"(原文: {_format_control_counts(original_raw_controls)}; "
                f"译文: {_format_control_counts(translated_raw_controls)})"
            )
            errors.append(control_error)

        if errors:
            raise ValueError(";\n".join(errors))

    def restore_placeholders(self) -> None:
        """将占位符恢复成 RPG Maker 原始控制符。"""
        if not self.translation_lines_with_placeholders:
            self.translation_lines = []
            return

        if not self.placeholder_map:
            self.translation_lines = list(self.translation_lines_with_placeholders)
            return

        sorted_placeholders = sorted(self.placeholder_map.keys(), key=len, reverse=True)
        new_translation_lines: list[str] = []
        for line in self.translation_lines_with_placeholders:
            restored_line = line
            for placeholder in sorted_placeholders:
                original_code = self.placeholder_map[placeholder]
                pattern = re.compile(re.escape(placeholder), re.IGNORECASE)
                restored_line = pattern.sub(lambda _match: original_code, restored_line)
            new_translation_lines.append(restored_line)
        self.translation_lines = new_translation_lines


def _format_control_counts(counts: dict[str, int]) -> str:
    """把控制符计数格式化为可读错误信息。"""
    if not counts:
        return "无"
    parts: list[str] = []
    for marker in sorted(counts):
        parts.append(f"{marker}×{counts[marker]}")
    return "、".join(parts)


class TranslationErrorItem(BaseModel):
    """正文翻译错误记录。"""

    location_path: str
    item_type: ItemType
    role: str | None
    original_lines: list[str] = Field(default_factory=list)
    translation_lines: list[str] = Field(default_factory=list)
    error_type: ErrorType
    error_detail: list[str] = Field(default_factory=list)
    model_response: str = ""


class PlaceholderRuleRecord(BaseModel):
    """当前游戏导入的自定义占位符规则。"""

    pattern_text: str
    placeholder_template: str


class StructuredPlaceholderRuleRecord(BaseModel):
    """当前游戏导入的结构化占位符规则。"""

    rule_name: str
    rule_type: str
    pattern_text: str
    translatable_group: str
    protected_groups: dict[str, str] = Field(default_factory=dict)


class SourceResidualRuleRecord(BaseModel):
    """当前游戏导入的源文残留例外规则。"""

    rule_id: str = ""
    rule_type: SourceResidualRuleType = "position"
    location_path: str = ""
    pattern_text: str = ""
    allowed_terms: list[str] = Field(default_factory=list)
    check_group: str = ""
    reason: str


class MvVirtualNameboxRuleRecord(BaseModel):
    """当前游戏导入的 MV 虚拟名字框规则。"""

    rule_order: int = Field(ge=0)
    rule_name: str
    pattern_text: str
    speaker_group: str
    body_group: str = ""
    speaker_policy: MvVirtualNameboxSpeakerPolicy
    render_template: str


class FontReplacementRecord(BaseModel):
    """一次字体覆盖写回中被替换的字段记录。"""

    file_name: str
    value_path: str
    original_text: str
    replaced_text: str
    replacement_font_name: str


class TranslationRunRecord(BaseModel):
    """正文翻译运行状态快照。"""

    run_id: str
    status: TranslationRunStatus
    total_extracted: int = 0
    pending_count: int = 0
    deduplicated_count: int = 0
    batch_count: int = 0
    success_count: int = 0
    quality_error_count: int = 0
    llm_failure_count: int = 0
    physical_request_count: int = Field(default=0, ge=0)
    retry_request_count: int = Field(default=0, ge=0)
    started_at: str
    updated_at: str
    finished_at: str | None = None
    stop_reason: str = ""
    last_error: str = ""


class LlmFailureRecord(BaseModel):
    """正文翻译运行级模型故障记录。"""

    run_id: str
    category: LlmFailureCategory
    error_type: str
    error_message: str
    retryable: bool
    attempt_count: int
    created_at: str


class TranslationData(BaseModel):
    """单个文件维度的翻译数据集合。"""

    display_name: str | None
    translation_items: list[TranslationItem] = Field(default_factory=list)


class PluginTextRuleRecord(BaseModel):
    """单个插件的最新文本路径规则快照。"""

    plugin_index: int
    plugin_name: str
    plugin_hash: str
    path_templates: list[str] = Field(default_factory=list)


class PluginSourceTextRuleRecord(BaseModel):
    """单个插件源码文件的 AST 文本规则快照。"""

    file_name: str
    file_hash: str
    selectors: list[str] = Field(default_factory=list)
    excluded_selectors: list[str] = Field(default_factory=list)


class PluginSourceRuntimeWriteMapRecord(BaseModel):
    """插件源码写回后从当前运行字符串反推翻译源条目的可选诊断映射。"""

    mapping_kind: PluginSourceRuntimeMappingKind = "translated"
    location_path: str
    source_file_name: str
    source_selector: str
    source_file_hash: str
    source_text_hash: str
    translation_lines_hash: str
    runtime_file_name: str
    runtime_selector: str
    runtime_file_hash: str
    runtime_text_hash: str
    runtime_line: int = Field(ge=1)
    created_at: str


class PluginSourceRuntimeStringLiteralCacheRecord(BaseModel):
    """当前运行插件源码 AST 字符串字面量缓存。"""

    selector: str
    text: str
    raw_text: str
    line: int = Field(ge=1)
    start_index: int = Field(ge=0)
    end_index: int = Field(ge=0)
    context: str


class PluginSourceRuntimeScanCacheRecord(BaseModel):
    """当前运行插件源码按文件 hash 保存的 AST 扫描缓存。"""

    file_name: str
    file_hash: str
    syntax_error: str = ""
    literals: list[PluginSourceRuntimeStringLiteralCacheRecord] = Field(default_factory=list)
    created_at: str


class NoteTagTextRuleRecord(BaseModel):
    """单个 data 文件或文件模式的 Note 标签文本规则快照。"""

    file_name: str
    tag_names: list[str] = Field(default_factory=list)


class EventCommandParameterFilter(BaseModel):
    """事件指令参数匹配条件。"""

    index: int = Field(ge=0)
    value: str


class EventCommandTextRuleRecord(BaseModel):
    """事件指令文本路径规则快照。"""

    command_code: int = Field(ge=0)
    parameter_filters: list[EventCommandParameterFilter] = Field(default_factory=list)
    path_templates: list[str] = Field(default_factory=list)


DATA_DIRECTORY_NAME = "data"
DATA_ORIGIN_DIRECTORY_NAME = "data_origin"
JS_DIRECTORY_NAME = "js"
SYSTEM_FILE_NAME = "System.json"
PLUGINS_FILE_NAME = "plugins.js"
PLUGINS_ORIGIN_FILE_NAME = "plugins_origin.js"
PLUGIN_SOURCE_ORIGIN_DIRECTORY_NAME = "plugins_source_origin"
COMMON_EVENTS_FILE_NAME = "CommonEvents.json"
TROOPS_FILE_NAME = "Troops.json"
MAP_INFOS_FILE_NAME = "MapInfos.json"

PLUGINS_JS_PATTERN: re.Pattern[str] = re.compile(
    r"var\s+\$plugins\s*=\s*(\[.*?\])\s*;\s*$",
    re.DOTALL | re.MULTILINE,
)
MAP_PATTERN: re.Pattern[str] = re.compile(r"Map\d+\.json")
FIXED_FILE_NAMES: set[str] = {
    "Actors.json",
    "Animations.json",
    "Armors.json",
    "Classes.json",
    "Enemies.json",
    "Items.json",
    "Skills.json",
    "States.json",
    "Weapons.json",
    "Tilesets.json",
    MAP_INFOS_FILE_NAME,
    COMMON_EVENTS_FILE_NAME,
    TROOPS_FILE_NAME,
    SYSTEM_FILE_NAME,
}


@dataclass(frozen=True, slots=True)
class GameLayout:
    """描述用户目录与真实游戏内容目录的对应关系。"""

    game_root: Path
    content_root: Path
    data_dir: Path
    data_origin_dir: Path
    js_dir: Path
    plugins_path: Path
    plugins_origin_path: Path
    plugin_source_origin_dir: Path
    package_path: Path
    engine_kind: EngineKind
    engine_version: str
    is_www_layout: bool

    @property
    def engine_label(self) -> str:
        """返回面向用户展示的引擎名称。"""
        if self.engine_kind == "mv":
            return "RPG Maker MV"
        return "RPG Maker MZ"

    @property
    def source_plugins_path(self) -> Path:
        """返回本轮读取插件配置时应优先使用的文件。"""
        if self.plugins_origin_path.exists():
            return self.plugins_origin_path
        return self.plugins_path

    @property
    def has_origin_backup(self) -> bool:
        """判断当前游戏是否已经存在原始备份。"""
        return self.data_origin_dir.exists() or self.plugins_origin_path.exists()


@dataclass(slots=True)
class GameData:
    """游戏数据聚合模型。"""

    layout: GameLayout
    data: dict[str, JsonValue]
    writable_data: dict[str, JsonValue]
    map_data: dict[str, MapData]
    system: System
    common_events: list[CommonEvent | None]
    troops: list[Troop | None]
    base_data: dict[str, list[BaseItem | None]]
    plugins_js: list[dict[str, JsonValue]]
    writable_plugins_js: list[dict[str, JsonValue]]
    plugin_source_files: dict[str, str]
    plugin_source_read_errors: dict[str, str]
    writable_plugin_source_files: dict[str, str]

    def __post_init__(self) -> None:
        """确保标准核心文件已经加载。"""
        required_files = FIXED_FILE_NAMES
        missing_files = sorted(required_files.difference(self.data))
        if missing_files:
            raise ValueError(f"游戏缺少必要标准文件: {', '.join(missing_files)}")


__all__: list[str] = [
    "Code",
    "COMMON_EVENTS_FILE_NAME",
    "DATA_DIRECTORY_NAME",
    "DATA_ORIGIN_DIRECTORY_NAME",
    "EngineKind",
    "ErrorType",
    "EventCommandParameterFilter",
    "EventCommandTextRuleRecord",
    "FIXED_FILE_NAMES",
    "FontReplacementRecord",
    "GameData",
    "GameLayout",
    "ItemType",
    "JS_DIRECTORY_NAME",
    "MAP_INFOS_FILE_NAME",
    "MAP_PATTERN",
    "MvVirtualNameboxRuleRecord",
    "MvVirtualNameboxSpeakerPolicy",
    "NoteTagTextRuleRecord",
    "PluginTextRuleRecord",
    "PluginSourceRuntimeScanCacheRecord",
    "PluginSourceRuntimeStringLiteralCacheRecord",
    "PluginSourceRuntimeWriteMapRecord",
    "PluginSourceTextRuleRecord",
    "PlaceholderRuleRecord",
    "PLUGINS_FILE_NAME",
    "PLUGINS_JS_PATTERN",
    "PLUGINS_ORIGIN_FILE_NAME",
    "PLUGIN_SOURCE_ORIGIN_DIRECTORY_NAME",
    "SYSTEM_FILE_NAME",
    "TROOPS_FILE_NAME",
    "LlmFailureCategory",
    "LlmFailureRecord",
    "SourceResidualRuleRecord",
    "SourceResidualRuleType",
    "TranslationData",
    "TranslationErrorItem",
    "TranslationItem",
    "TranslationRunRecord",
    "TranslationRunStatus",
]
