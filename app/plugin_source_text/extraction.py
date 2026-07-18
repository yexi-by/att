"""插件源码文本规则驱动提取。"""

from __future__ import annotations

from app.rmmz.schema import GameData, PluginSourceTextRuleRecord, TranslationData, TranslationItem
from app.rmmz.text_rules import TextRules

from .models import PluginSourceScan
from .scanner import PluginSourceCandidateIndex


class PluginSourceTextExtraction:
    """从已确认插件源码 AST selector 提取正文文本。"""

    def __init__(
        self,
        game_data: GameData,
        rule_records: list[PluginSourceTextRuleRecord],
        text_rules: TextRules,
        scan: PluginSourceScan,
    ) -> None:
        """初始化插件源码提取器。"""
        self.game_data: GameData = game_data
        self.rule_records: list[PluginSourceTextRuleRecord] = rule_records
        self.text_rules: TextRules = text_rules
        self.scan: PluginSourceScan = scan

    def extract_all_text(self) -> dict[str, TranslationData]:
        """按规则全量提取插件源码文本。"""
        result: dict[str, TranslationData] = {}
        records_by_file: dict[str, list[PluginSourceTextRuleRecord]] = {}
        for record in self.rule_records:
            records_by_file.setdefault(record.file_name, []).append(record)
        for file_name, records in records_by_file.items():
            source = self.game_data.plugin_source_files.get(file_name)
            if source is None:
                continue
            file_key = plugin_source_file_key(file_name)
            candidate_index = self._candidate_index_for_file(
                file_name=file_name,
            )
            items: list[TranslationItem] = []
            for record in records:
                items.extend(self._extract_file_items(record=record, candidate_index=candidate_index))
            if not items:
                continue
            result[file_key] = TranslationData(display_name=None, translation_items=items)
        return result

    def _extract_file_items(
        self,
        *,
        record: PluginSourceTextRuleRecord,
        candidate_index: PluginSourceCandidateIndex,
    ) -> list[TranslationItem]:
        """提取单个源码文件的规则命中项。"""
        items: list[TranslationItem] = []
        for selector in record.selectors:
            candidate = candidate_index.by_selector.get(selector)
            if candidate is None:
                continue
            if not self.text_rules.should_translate_source_text(candidate.text):
                continue
            items.append(
                TranslationItem(
                    location_path=plugin_source_location_path(
                        file_name=record.file_name,
                        selector=selector,
                    ),
                    item_type="short_text",
                    original_lines=[candidate.text],
                    source_line_paths=[plugin_source_location_path(file_name=record.file_name, selector=selector)],
                )
            )
        return items

    def _candidate_index_for_file(self, *, file_name: str) -> PluginSourceCandidateIndex:
        """从调用者提供的唯一 AST 扫描读取单文件候选索引。"""
        for file_scan in self.scan.files:
            if file_scan.file_name != file_name:
                continue
            candidates = file_scan.candidates
            return PluginSourceCandidateIndex(
                candidates=candidates,
                by_selector={candidate.selector: candidate for candidate in candidates},
            )
        return PluginSourceCandidateIndex(candidates=(), by_selector={})


def plugin_source_file_key(file_name: str) -> str:
    """返回统一文本范围中的插件源码文件键。"""
    return f"js/plugins/{file_name}"


def plugin_source_location_path(*, file_name: str, selector: str) -> str:
    """返回插件源码文本内部定位键。"""
    return f"{plugin_source_file_key(file_name)}/{selector}"


def parse_plugin_source_location_path(location_path: str) -> tuple[str, str] | None:
    """从内部定位键解析插件源码文件名和 selector。"""
    prefix = "js/plugins/"
    if not location_path.startswith(prefix):
        return None
    remain = location_path[len(prefix) :]
    parts = remain.split("/", 1)
    if len(parts) != 2:
        return None
    file_name, selector = parts
    if not file_name.endswith(".js") or not selector:
        return None
    return file_name, selector


__all__ = [
    "PluginSourceTextExtraction",
    "parse_plugin_source_location_path",
    "plugin_source_file_key",
    "plugin_source_location_path",
]
