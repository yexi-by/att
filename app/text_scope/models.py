"""统一文本范围服务的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.rmmz.schema import ItemType, TranslationData, TranslationItem
from app.rmmz.text_rules import JsonArray, JsonObject

type TextSourceType = Literal["standard_data", "plugin_parameter", "plugin_source", "event_command", "note_tag"]


@dataclass(frozen=True, slots=True)
class TextScopeEntry:
    """统一文本清单中的一条文本记录。"""

    location_path: str
    source_type: TextSourceType
    rule_source: str
    item_type: ItemType
    original_lines: list[str]
    role: str | None
    enters_translation: bool
    can_save_translation: bool
    can_write_back: bool
    translated: bool
    cannot_process_reason: str

    def to_json_object(self) -> JsonObject:
        """转换成 CLI 报告使用的 JSON 对象。"""
        return {
            "location_path": self.location_path,
            "source_type": self.source_type,
            "rule_source": self.rule_source,
            "item_type": self.item_type,
            "original_lines": [line for line in self.original_lines],
            "role": self.role or "",
            "enters_translation": self.enters_translation,
            "can_save_translation": self.can_save_translation,
            "can_write_back": self.can_write_back,
            "translated": self.translated,
            "cannot_process_reason": self.cannot_process_reason,
        }


@dataclass(frozen=True, slots=True)
class TextScopeRuleHit:
    """外部规则命中的字符串叶子。"""

    location_path: str
    source_type: TextSourceType
    rule_source: str
    original_text: str


@dataclass(frozen=True, slots=True)
class StalePluginRule:
    """已经不匹配当前插件配置的插件规则。"""

    plugin_index: int
    plugin_name: str
    reason: str

    def to_json_object(self) -> JsonObject:
        """转换成 CLI 报告使用的 JSON 对象。"""
        return {
            "plugin_index": self.plugin_index,
            "plugin_name": self.plugin_name,
            "reason": self.reason,
        }


@dataclass(slots=True)
class TextScopeResult:
    """当前游戏的统一文本范围结果。"""

    translation_data_map: dict[str, TranslationData]
    entries: list[TextScopeEntry]
    stale_plugin_rules: list[StalePluginRule] = field(default_factory=list)
    write_back_probe_error: str = ""
    write_back_probe_enabled: bool = False
    translation_rule_fingerprint: str = ""

    @property
    def active_paths(self) -> set[str]:
        """返回当前会进入翻译集合的定位路径。"""
        return {entry.location_path for entry in self.entries if entry.enters_translation}

    @property
    def writable_paths(self) -> set[str]:
        """返回当前可写进游戏文件的定位路径。"""
        return {entry.location_path for entry in self.entries if entry.can_write_back}

    @property
    def unwritable_entries(self) -> list[TextScopeEntry]:
        """返回已经进入翻译集合但无法写进游戏文件的条目。"""
        return [entry for entry in self.entries if entry.enters_translation and not entry.can_write_back]

    def active_items(self) -> list[TranslationItem]:
        """返回当前会进入翻译集合的条目。"""
        return [
            item
            for translation_data in self.translation_data_map.values()
            for item in translation_data.translation_items
        ]

    def entries_json(self) -> JsonArray:
        """返回全部文本清单记录的 JSON 数组。"""
        return [entry.to_json_object() for entry in self.entries]

    def stale_plugin_rules_json(self) -> JsonArray:
        """返回过期插件规则的 JSON 数组。"""
        return [rule.to_json_object() for rule in self.stale_plugin_rules]


class WriteBackProbeError(RuntimeError):
    """写入协议探针整体不可用。"""
