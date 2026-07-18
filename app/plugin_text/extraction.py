"""
插件文本规则驱动提取模块。

本模块按照数据库中的外部导入规则，从 `plugins.js` 展开命中的字符串叶子。
外部规则文件是插件参数文本提取的唯一来源。
"""

from __future__ import annotations

from app.plugin_text.common import (
    expand_rule_to_leaf_paths,
    jsonpath_to_location_path,
)
from app.rmmz.schema import (
    PLUGINS_FILE_NAME,
    GameData,
    PluginTextRuleRecord,
    TranslationData,
    TranslationItem,
)
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import TextRules, get_default_text_rules

from .index import PluginParameterAnalysisEntry, build_plugin_parameter_analysis_index


class PluginTextExtraction:
    """插件文本规则驱动提取器。"""

    def __init__(
        self,
        game_data: GameData,
        plugin_rule_records: list[PluginTextRuleRecord],
        text_rules: TextRules | None = None,
    ) -> None:
        """初始化插件文本提取器。"""
        self.game_data: GameData = game_data
        self.plugin_rule_records: list[PluginTextRuleRecord] = plugin_rule_records
        self.text_rules: TextRules = text_rules if text_rules is not None else get_default_text_rules()

    def extract_all_text(self) -> dict[str, TranslationData]:
        """按规则全量提取 `plugins.js` 中的可翻译文本。"""
        return self.extract_all_text_from_index(build_plugin_parameter_analysis_index(self.game_data))

    def extract_all_text_from_index(
        self,
        plugin_index: tuple[PluginParameterAnalysisEntry, ...],
    ) -> dict[str, TranslationData]:
        """从单命令共享索引提取插件参数文本。"""
        entries_by_index = {entry.plugin_index: entry for entry in plugin_index}
        translation_items: list[TranslationItem] = []
        for rule_record in self.plugin_rule_records:
            if not rule_record.path_templates:
                continue
            index_entry = entries_by_index.get(rule_record.plugin_index)
            if index_entry is None:
                continue
            translation_items.extend(
                self._extract_plugin_items(
                    rule_record=rule_record,
                    index_entry=index_entry,
                )
            )

        if not translation_items:
            return {}

        return {
            PLUGINS_FILE_NAME: TranslationData(
                display_name=None,
                translation_items=translation_items,
            )
        }

    def _extract_plugin_items(
        self,
        *,
        rule_record: PluginTextRuleRecord,
        index_entry: PluginParameterAnalysisEntry,
    ) -> list[TranslationItem]:
        """根据单个插件规则快照提取正文条目。"""
        resolved_leaves = index_entry.resolved_leaves
        string_leaf_map = {leaf.path: leaf.value for leaf in resolved_leaves if leaf.value_type == "string"}
        translation_items: list[TranslationItem] = []
        seen_leaf_paths: set[str] = set()

        for path_template in rule_record.path_templates:
            matched_paths = expand_rule_to_leaf_paths(
                path_template=path_template,
                resolved_leaves=resolved_leaves,
            )
            for leaf_path in matched_paths:
                if leaf_path in seen_leaf_paths:
                    continue
                seen_leaf_paths.add(leaf_path)

                leaf_value = string_leaf_map.get(leaf_path)
                if not isinstance(leaf_value, str):
                    continue

                normalized_value = normalize_visible_text_for_extraction(leaf_value)
                if not self.text_rules.should_translate_source_text(normalized_value):
                    continue

                translation_items.append(
                    TranslationItem(
                        location_path=jsonpath_to_location_path(
                            json_path=leaf_path,
                            plugin_index=rule_record.plugin_index,
                        ),
                        item_type="short_text",
                        original_lines=[normalized_value],
                    )
                )

        return translation_items


__all__: list[str] = ["PluginTextExtraction"]
