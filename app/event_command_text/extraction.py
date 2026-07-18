"""事件指令文本规则驱动提取模块。"""

from app.event_command_text.importer import command_matches_filters
from app.plugin_text.paths import (
    expand_rule_to_leaf_paths,
    jsonpath_to_event_command_location_path,
)
from app.rmmz.schema import (
    MAP_PATTERN,
    EventCommandTextRuleRecord,
    GameData,
    TranslationData,
    TranslationItem,
)
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import TextRules, get_default_text_rules

from .index import EventCommandAnalysisEntry, build_event_command_analysis_index


class EventCommandTextExtraction:
    """事件指令文本规则驱动提取器。"""

    def __init__(
        self,
        game_data: GameData,
        rule_records: list[EventCommandTextRuleRecord],
        text_rules: TextRules | None = None,
    ) -> None:
        """初始化事件指令文本提取器。"""
        self.game_data: GameData = game_data
        self.rule_records: list[EventCommandTextRuleRecord] = rule_records
        self.text_rules: TextRules = text_rules if text_rules is not None else get_default_text_rules()

    def extract_all_text(self) -> dict[str, TranslationData]:
        """按数据库规则提取事件指令参数中的字符串叶子。"""
        return self.extract_all_text_from_index(build_event_command_analysis_index(self.game_data))

    def extract_all_text_from_index(
        self,
        command_index: tuple[EventCommandAnalysisEntry, ...],
    ) -> dict[str, TranslationData]:
        """从单命令共享索引提取事件指令参数文本。"""
        if not self.rule_records:
            return {}

        translation_data_map: dict[str, TranslationData] = {}
        seen_location_paths: set[str] = set()
        for entry in command_index:
            path = entry.location_path
            display_name = entry.display_name
            command = entry.command
            matched_rules = [
                rule
                for rule in self.rule_records
                if rule.command_code == command.code
                and command_matches_filters(
                    parameters=command.parameters,
                    filters=rule.parameter_filters,
                )
            ]
            if not matched_rules:
                continue

            file_name_value = path[0]
            if not isinstance(file_name_value, str):
                continue

            file_name = file_name_value
            if file_name not in translation_data_map:
                map_display_name = display_name if MAP_PATTERN.fullmatch(file_name) else None
                translation_data_map[file_name] = TranslationData(
                    display_name=map_display_name,
                    translation_items=[],
                )

            command_location_path = "/".join(map(str, path))
            resolved_leaves = entry.resolved_leaves
            string_leaf_map = {leaf.path: leaf.value for leaf in resolved_leaves if leaf.value_type == "string"}
            for rule in matched_rules:
                for path_template in rule.path_templates:
                    matched_paths = expand_rule_to_leaf_paths(
                        path_template=path_template,
                        resolved_leaves=resolved_leaves,
                    )
                    for leaf_path in matched_paths:
                        location_path = jsonpath_to_event_command_location_path(
                            json_path=leaf_path,
                            command_location_path=command_location_path,
                        )
                        if location_path in seen_location_paths:
                            continue
                        leaf_value = string_leaf_map.get(leaf_path)
                        if not isinstance(leaf_value, str):
                            continue
                        normalized_value = normalize_visible_text_for_extraction(leaf_value)
                        if not self.text_rules.should_translate_source_text(normalized_value):
                            continue
                        seen_location_paths.add(location_path)
                        translation_data_map[file_name].translation_items.append(
                            TranslationItem(
                                location_path=location_path,
                                item_type="short_text",
                                original_lines=[normalized_value],
                            )
                        )

        return {file_name: data for file_name, data in translation_data_map.items() if data.translation_items}


__all__: list[str] = ["EventCommandTextExtraction"]
