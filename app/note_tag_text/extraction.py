"""Note 标签规则驱动提取模块。"""

from app.note_tag_text.sources import NoteTagSource, collect_note_tag_sources, note_file_pattern_matches
from app.rmmz.schema import (
    GameData,
    NoteTagTextRuleRecord,
    TranslationData,
    TranslationItem,
)
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import TextRules, get_default_text_rules


class NoteTagTextExtraction:
    """从标准 `data/*.json` 的 `note` 字段提取已授权标签文本。"""

    def __init__(
        self,
        game_data: GameData,
        rule_records: list[NoteTagTextRuleRecord],
        text_rules: TextRules | None = None,
    ) -> None:
        """初始化 Note 标签文本提取器。"""
        self.game_data: GameData = game_data
        self.rule_records: list[NoteTagTextRuleRecord] = rule_records
        self.text_rules: TextRules = text_rules if text_rules is not None else get_default_text_rules()

    def extract_all_text(self) -> dict[str, TranslationData]:
        """按数据库规则全量提取 Note 标签值。"""
        return self.extract_all_text_from_sources(collect_note_tag_sources(game_data=self.game_data))

    def extract_all_text_from_sources(
        self,
        sources: list[NoteTagSource] | tuple[NoteTagSource, ...],
    ) -> dict[str, TranslationData]:
        """从单命令已建立的 Note 索引提取标签值。"""
        translation_data_map: dict[str, TranslationData] = {}
        seen_location_paths: set[str] = set()
        for rule_record in self.rule_records:
            tag_names = set(rule_record.tag_names)
            for source in sources:
                if not note_file_pattern_matches(file_name=source.file_name, file_pattern=rule_record.file_name):
                    continue
                matches_by_tag: dict[str, list[str]] = {}
                for match in source.matches:
                    if match.tag_name not in tag_names or not match.has_value:
                        continue
                    matches_by_tag.setdefault(match.tag_name, []).append(match.value)

                for tag_name in rule_record.tag_names:
                    values = matches_by_tag.get(tag_name, [])
                    if not values:
                        continue
                    if len(values) > 1:
                        raise ValueError(f"{source.location_prefix}/note/{tag_name} 标签重复，无法生成唯一定位路径")
                    normalized_value = normalize_visible_text_for_extraction(
                        values[0],
                        plain_text_normalizer=self.text_rules.normalize_extraction_text,
                    )
                    if not self.text_rules.should_translate_source_text(normalized_value):
                        continue
                    location_path = f"{source.location_prefix}/note/{tag_name}"
                    if location_path in seen_location_paths:
                        continue
                    seen_location_paths.add(location_path)
                    translation_data = translation_data_map.setdefault(
                        source.file_name,
                        TranslationData(display_name=None, translation_items=[]),
                    )
                    translation_data.translation_items.append(
                        TranslationItem(
                            location_path=location_path,
                            item_type="short_text",
                            original_lines=[normalized_value],
                        )
                    )
        return translation_data_map


__all__: list[str] = ["NoteTagTextExtraction"]
