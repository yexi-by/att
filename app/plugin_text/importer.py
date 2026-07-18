"""插件文本规则导入模块。"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, cast

import aiofiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.rmmz.schema import GameData, PluginTextRuleRecord
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import JsonValue, TextRules, coerce_json_value, get_default_text_rules

from .common import (
    build_json_string_leaf_path_hint,
    build_plugin_hash,
    expand_rule_to_leaf_paths,
    extract_plugin_name,
)
from .index import PluginParameterAnalysisEntry, build_plugin_parameter_analysis_index
from .paths import ResolvedLeaf


class StrictPluginRuleModel(BaseModel):
    """插件规则导入文件的严格模型基类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)


class PluginRuleSpec(StrictPluginRuleModel):
    """单个插件参数文本规则。"""

    plugin_index: int = Field(ge=0)
    plugin_name: str
    paths: list[str] = Field(default_factory=list)

    @field_validator("plugin_name")
    @classmethod
    def _validate_plugin_name(cls, value: str) -> str:
        """插件名必须是非空字符串。"""
        normalized_name = value.strip()
        if not normalized_name:
            raise ValueError("plugin_name 不能为空")
        return normalized_name

    @field_validator("paths")
    @classmethod
    def _validate_paths(cls, value: list[str]) -> list[str]:
        """路径数组必须至少包含一条有效 JSONPath。"""
        normalized_paths = normalize_path_templates(value)
        if not normalized_paths:
            raise ValueError("paths 不能为空")
        return normalized_paths


type PluginRuleImportFile = list[PluginRuleSpec]
_PLUGIN_RULE_IMPORT_ADAPTER: TypeAdapter[PluginRuleImportFile] = TypeAdapter(PluginRuleImportFile)


async def load_plugin_rule_import_file(input_path: Path) -> PluginRuleImportFile:
    """读取外部插件规则 JSON 文件。"""
    resolved_path = input_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"插件规则导入文件不存在: {resolved_path}")
    async with aiofiles.open(resolved_path, "r", encoding="utf-8") as file:
        raw_text = await file.read()
    return parse_plugin_rule_import_text(raw_text)


def parse_plugin_rule_import_text(raw_text: str) -> PluginRuleImportFile:
    """解析外部插件规则 JSON 文本。"""
    decoded_raw = cast(object, json.loads(raw_text))
    decoded = coerce_json_value(decoded_raw)
    if not isinstance(decoded, list):
        raise TypeError("插件规则顶层必须是数组，每项包含 plugin_index、plugin_name 和 paths")
    return _PLUGIN_RULE_IMPORT_ADAPTER.validate_python(decoded)


def build_plugin_rule_records_from_import(
    *,
    game_data: GameData,
    import_file: PluginRuleImportFile,
    text_rules: TextRules | None = None,
    plugin_index: tuple[PluginParameterAnalysisEntry, ...] | None = None,
) -> list[PluginTextRuleRecord]:
    """把索引优先的外部插件规则转换成数据库规则记录。"""
    rules = text_rules if text_rules is not None else get_default_text_rules()
    resolved_plugin_index = build_plugin_parameter_analysis_index(game_data) if plugin_index is None else plugin_index
    entries_by_index = {entry.plugin_index: entry for entry in resolved_plugin_index}
    records: list[PluginTextRuleRecord] = []
    seen_plugin_indices: set[int] = set()
    for spec in import_file:
        if spec.plugin_index in seen_plugin_indices:
            raise ValueError(f"插件规则不能重复声明 plugin_index: {spec.plugin_index}")
        seen_plugin_indices.add(spec.plugin_index)
        if spec.plugin_index >= len(game_data.plugins_js):
            raise ValueError(f"插件规则索引超出当前 plugins.js 范围: {spec.plugin_index}")
        plugin = game_data.plugins_js[spec.plugin_index]
        index_entry = entries_by_index.get(spec.plugin_index)
        if index_entry is None:
            raise ValueError(f"插件参数索引缺少 plugin_index: {spec.plugin_index}")
        actual_plugin_name = extract_plugin_name(plugin, spec.plugin_index)
        if spec.plugin_name != actual_plugin_name:
            message = (
                f"插件规则名称与当前 plugins.js 不匹配: plugin_index={spec.plugin_index}, "
                f"规则={spec.plugin_name}, 当前={actual_plugin_name}"
            )
            raise ValueError(message)
        records.append(
            build_plugin_rule_record(
                plugin_index=spec.plugin_index,
                plugin_name=spec.plugin_name,
                plugin=plugin,
                path_templates=spec.paths,
                text_rules=rules,
                resolved_leaves=index_entry.resolved_leaves,
            )
        )
    return records


def build_plugin_rule_record(
    *,
    plugin_index: int,
    plugin_name: str,
    plugin: dict[str, JsonValue],
    path_templates: list[str],
    text_rules: TextRules,
    resolved_leaves: Sequence[ResolvedLeaf],
) -> PluginTextRuleRecord:
    """校验单个插件路径列表并构造数据库记录。"""
    string_leaf_map = {
        leaf.path: leaf.value for leaf in resolved_leaves if leaf.value_type == "string" and isinstance(leaf.value, str)
    }
    accepted_paths: list[str] = []
    for path_template in path_templates:
        matched_paths = expand_rule_to_leaf_paths(
            path_template=path_template,
            resolved_leaves=resolved_leaves,
        )
        if not matched_paths:
            hint = build_json_string_leaf_path_hint(
                path_template=path_template,
                resolved_leaves=resolved_leaves,
            )
            hint_suffix = "" if hint is None else f"。{hint}"
            raise ValueError(f"插件 {plugin_name} 的路径没有命中当前插件字符串叶子: {path_template}{hint_suffix}")
        translatable_hit_found = False
        for leaf_path in matched_paths:
            leaf_value = string_leaf_map.get(leaf_path)
            if leaf_value is None:
                continue
            normalized_value = normalize_visible_text_for_extraction(leaf_value)
            if text_rules.should_translate_source_text(normalized_value):
                translatable_hit_found = True
                break
        if not translatable_hit_found:
            raise ValueError(f"插件 {plugin_name} 的路径没有命中玩家可见可翻译文本: {path_template}")
        accepted_paths.append(path_template)

    return PluginTextRuleRecord(
        plugin_index=plugin_index,
        plugin_name=plugin_name,
        plugin_hash=build_plugin_hash(plugin),
        path_templates=accepted_paths,
    )


def normalize_path_templates(path_templates: list[str]) -> list[str]:
    """清理并去重路径模板。"""
    normalized_paths: list[str] = []
    seen_paths: set[str] = set()
    for path_template in path_templates:
        normalized_path = path_template.strip()
        if not normalized_path or normalized_path in seen_paths:
            continue
        normalized_paths.append(normalized_path)
        seen_paths.add(normalized_path)
    return normalized_paths


__all__: list[str] = [
    "PluginRuleImportFile",
    "PluginRuleSpec",
    "build_plugin_rule_records_from_import",
    "load_plugin_rule_import_file",
    "parse_plugin_rule_import_text",
]
