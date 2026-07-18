"""统一文本范围构建服务。"""

from __future__ import annotations

import hashlib
import json

from app.event_command_text import EventCommandTextExtraction
from app.note_tag_text import NoteTagTextExtraction
from app.plugin_source_text import PluginSourceTextExtraction, filter_fresh_plugin_source_text_rules
from app.plugin_source_text.models import PluginSourceScan
from app.plugin_text import PluginTextExtraction
from app.rmmz import DataTextExtraction
from app.rmmz.schema import (
    PLUGINS_FILE_NAME,
    EventCommandTextRuleRecord,
    GameData,
    MvVirtualNameboxRuleRecord,
    NoteTagTextRuleRecord,
    PluginSourceTextRuleRecord,
    PluginTextRuleRecord,
    TranslationData,
    TranslationItem,
)
from app.rmmz.text_rules import TextRules

from .indexes import TextScopeAnalysisIndex
from .models import (
    StalePluginRule,
    TextScopeEntry,
    TextScopeResult,
    TextScopeRuleHit,
    TextSourceType,
    WriteBackProbeError,
)
from .rule_hits import collect_event_command_rule_hits, collect_note_tag_rule_hits, collect_plugin_rule_hits
from .write_probe import collect_write_back_probe_reasons


class TextScopeService:
    """构建当前游戏统一文本范围。"""

    def build_from_loaded_rules(
        self,
        *,
        game_data: GameData,
        text_rules: TextRules,
        plugin_rules: list[PluginTextRuleRecord],
        stale_plugin_rules: list[StalePluginRule],
        event_rules: list[EventCommandTextRuleRecord],
        plugin_source_rule_records: list[PluginSourceTextRuleRecord],
        plugin_source_scan: PluginSourceScan,
        note_tag_rules: list[NoteTagTextRuleRecord],
        mv_virtual_namebox_rules: list[MvVirtualNameboxRuleRecord],
        translated_items: list[TranslationItem],
        analysis_index: TextScopeAnalysisIndex,
        include_write_probe: bool = False,
    ) -> TextScopeResult:
        """使用单命令已加载事实构建文本范围，不再读库或扫描 AST。"""
        plugin_source_rules, _stale_plugin_source_rules = filter_fresh_plugin_source_text_rules(
            rule_records=plugin_source_rule_records,
            scan=plugin_source_scan,
        )
        translated_paths = {item.location_path for item in translated_items}

        translation_data_map = build_translation_data_map(
            game_data=game_data,
            text_rules=text_rules,
            plugin_rules=plugin_rules,
            event_rules=event_rules,
            plugin_source_rules=plugin_source_rules,
            plugin_source_scan=plugin_source_scan,
            note_tag_rules=note_tag_rules,
            mv_virtual_namebox_rules=mv_virtual_namebox_rules,
            analysis_index=analysis_index,
        )
        active_items = {
            item.location_path: item
            for translation_data in translation_data_map.values()
            for item in translation_data.translation_items
        }
        write_back_probe_error = ""
        write_back_reasons: dict[str, str] = {}
        if include_write_probe:
            try:
                write_back_reasons = collect_write_back_probe_reasons(
                    game_data=game_data,
                    active_items=list(active_items.values()),
                )
            except WriteBackProbeError as error:
                write_back_reasons = {}
                write_back_probe_error = str(error)
        entries = [
            _active_item_to_scope_entry(
                item=item,
                translated_paths=translated_paths,
                write_back_reason=write_back_reasons.get(item.location_path, ""),
            )
            for item in active_items.values()
        ]

        rule_hits = [
            *collect_plugin_rule_hits(
                plugin_index=analysis_index.plugin_parameters,
                plugin_rules=plugin_rules,
            ),
            *collect_event_command_rule_hits(
                command_index=analysis_index.event_commands,
                event_rules=event_rules,
            ),
            *collect_note_tag_rule_hits(
                note_sources=analysis_index.note_sources,
                note_tag_rules=note_tag_rules,
                text_rules=text_rules,
            ),
        ]
        active_paths = set(active_items)
        for hit in rule_hits:
            if hit.location_path in active_paths:
                continue
            entries.append(
                _rule_hit_to_inactive_scope_entry(
                    hit=hit,
                    translated_paths=translated_paths,
                    text_rules=text_rules,
                )
            )

        entries.sort(key=lambda item: item.location_path)
        return TextScopeResult(
            translation_data_map=translation_data_map,
            entries=entries,
            stale_plugin_rules=stale_plugin_rules,
            write_back_probe_error=write_back_probe_error,
            write_back_probe_enabled=include_write_probe,
            translation_rule_fingerprint=_build_translation_rule_fingerprint(
                text_rules=text_rules,
                plugin_rules=plugin_rules,
                event_rules=event_rules,
                plugin_source_rules=plugin_source_rule_records,
                note_tag_rules=note_tag_rules,
                mv_virtual_namebox_rules=mv_virtual_namebox_rules,
            ),
        )


def _build_translation_rule_fingerprint(
    *,
    text_rules: TextRules,
    plugin_rules: list[PluginTextRuleRecord],
    event_rules: list[EventCommandTextRuleRecord],
    plugin_source_rules: list[PluginSourceTextRuleRecord],
    note_tag_rules: list[NoteTagTextRuleRecord],
    mv_virtual_namebox_rules: list[MvVirtualNameboxRuleRecord],
) -> str:
    """对会影响提取、占位符和提示上下文的规则生成稳定指纹。"""
    payload = {
        "text_rules_setting": text_rules.setting.model_dump(mode="json"),
        "custom_placeholder_rules": [
            {
                "pattern_text": rule.pattern_text,
                "placeholder_template": rule.placeholder_template,
            }
            for rule in text_rules.custom_placeholder_rules
        ],
        "structured_placeholder_rules": [
            {
                "rule_name": rule.rule_name,
                "rule_type": rule.rule_type,
                "pattern_text": rule.pattern_text,
                "translatable_group": rule.translatable_group,
                "protected_groups": rule.protected_groups,
            }
            for rule in text_rules.structured_placeholder_rules
        ],
        "plugin_rules": [record.model_dump(mode="json") for record in plugin_rules],
        "event_rules": [record.model_dump(mode="json") for record in event_rules],
        "plugin_source_rules": [record.model_dump(mode="json") for record in plugin_source_rules],
        "note_tag_rules": [record.model_dump(mode="json") for record in note_tag_rules],
        "mv_virtual_namebox_rules": [record.model_dump(mode="json") for record in mv_virtual_namebox_rules],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_translation_data_map(
    *,
    game_data: GameData,
    text_rules: TextRules,
    plugin_rules: list[PluginTextRuleRecord],
    event_rules: list[EventCommandTextRuleRecord],
    plugin_source_rules: list[PluginSourceTextRuleRecord],
    note_tag_rules: list[NoteTagTextRuleRecord],
    mv_virtual_namebox_rules: list[MvVirtualNameboxRuleRecord],
    plugin_source_scan: PluginSourceScan,
    analysis_index: TextScopeAnalysisIndex,
) -> dict[str, TranslationData]:
    """按同一组规则构建当前可翻译文本集合。"""
    command_snapshots = tuple(
        (entry.location_path, entry.display_name, entry.command) for entry in analysis_index.event_commands
    )
    translation_data_map = DataTextExtraction(
        game_data,
        text_rules,
        mv_virtual_namebox_rule_records=mv_virtual_namebox_rules,
    ).extract_all_text_from_command_snapshots(command_snapshots)
    merge_translation_data_map(
        translation_data_map,
        EventCommandTextExtraction(game_data, event_rules, text_rules).extract_all_text_from_index(
            analysis_index.event_commands
        ),
    )
    merge_translation_data_map(
        translation_data_map,
        PluginTextExtraction(game_data, plugin_rules, text_rules).extract_all_text_from_index(
            analysis_index.plugin_parameters
        ),
    )
    merge_translation_data_map(
        translation_data_map,
        PluginSourceTextExtraction(
            game_data,
            plugin_source_rules,
            text_rules,
            scan=plugin_source_scan,
        ).extract_all_text(),
    )
    merge_translation_data_map(
        translation_data_map,
        NoteTagTextExtraction(game_data, note_tag_rules, text_rules).extract_all_text_from_sources(
            analysis_index.note_sources
        ),
    )
    return translation_data_map


def merge_translation_data_map(
    target: dict[str, TranslationData],
    source: dict[str, TranslationData],
) -> None:
    """合并两个文件维度翻译数据映射。"""
    for file_name, translation_data in source.items():
        existing_data = target.get(file_name)
        if existing_data is None:
            target[file_name] = translation_data
            continue
        existing_data.translation_items.extend(translation_data.translation_items)


def collect_translation_data_paths(translation_data_map: dict[str, TranslationData]) -> set[str]:
    """收集翻译数据中的全部定位路径。"""
    return {
        item.location_path
        for translation_data in translation_data_map.values()
        for item in translation_data.translation_items
    }


def _active_item_to_scope_entry(
    *,
    item: TranslationItem,
    translated_paths: set[str],
    write_back_reason: str,
) -> TextScopeEntry:
    """把当前可翻译条目转换成文本清单记录。"""
    source_type = _source_type_from_location_path(item.location_path)
    can_write_back = not write_back_reason
    return TextScopeEntry(
        location_path=item.location_path,
        source_type=source_type,
        rule_source=_rule_source_label(source_type),
        item_type=item.item_type,
        original_lines=[line for line in item.original_lines],
        role=item.role,
        enters_translation=True,
        can_save_translation=True,
        can_write_back=can_write_back,
        translated=item.location_path in translated_paths,
        cannot_process_reason=write_back_reason,
    )


def _rule_hit_to_inactive_scope_entry(
    *,
    hit: TextScopeRuleHit,
    translated_paths: set[str],
    text_rules: TextRules,
) -> TextScopeEntry:
    """把未进入翻译集合的规则命中项转换成文本清单记录。"""
    normalized_text = text_rules.normalize_extraction_text(hit.original_text)
    if not normalized_text:
        reason = "规则命中的是空文本"
    elif not text_rules.should_translate_source_text(normalized_text):
        reason = "规则命中的字符串不包含当前源语言字符"
    else:
        reason = "规则命中项没有进入统一文本清单"
    return TextScopeEntry(
        location_path=hit.location_path,
        source_type=hit.source_type,
        rule_source=hit.rule_source,
        item_type="short_text",
        original_lines=[normalized_text],
        role=None,
        enters_translation=False,
        can_save_translation=False,
        can_write_back=False,
        translated=hit.location_path in translated_paths,
        cannot_process_reason=reason,
    )


def _source_type_from_location_path(location_path: str) -> TextSourceType:
    """根据定位路径判断文本来源类型。"""
    if location_path.startswith(f"{PLUGINS_FILE_NAME}/"):
        return "plugin_parameter"
    if location_path.startswith("js/plugins/"):
        return "plugin_source"
    if "/note/" in location_path:
        return "note_tag"
    if "/parameters/" in location_path:
        return "event_command"
    return "standard_data"


def _rule_source_label(source_type: TextSourceType) -> str:
    """返回当前来源对应的规则来源说明。"""
    if source_type == "plugin_parameter":
        return "插件参数规则"
    if source_type == "plugin_source":
        return "插件源码规则"
    if source_type == "event_command":
        return "事件指令规则"
    if source_type == "note_tag":
        return "Note 标签规则"
    return "RPG Maker 标准数据结构"
