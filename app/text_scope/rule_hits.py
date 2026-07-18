"""统一文本范围服务中的外部规则命中展开。"""

from __future__ import annotations

from app.event_command_text.importer import command_matches_filters
from app.event_command_text.index import EventCommandAnalysisEntry
from app.note_tag_text.sources import NoteTagSource, note_file_pattern_matches
from app.plugin_text.common import expand_rule_to_leaf_paths, jsonpath_to_location_path
from app.plugin_text.index import PluginParameterAnalysisEntry
from app.plugin_text.paths import jsonpath_to_event_command_location_path
from app.rmmz.schema import EventCommandTextRuleRecord, NoteTagTextRuleRecord, PluginTextRuleRecord
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import TextRules

from .models import TextScopeRuleHit


def collect_plugin_rule_hits(
    *,
    plugin_index: tuple[PluginParameterAnalysisEntry, ...],
    plugin_rules: list[PluginTextRuleRecord],
) -> list[TextScopeRuleHit]:
    """展开插件参数规则命中的全部字符串叶子。"""
    hits: list[TextScopeRuleHit] = []
    seen_paths: set[str] = set()
    entries_by_index = {entry.plugin_index: entry for entry in plugin_index}
    for rule in plugin_rules:
        entry = entries_by_index.get(rule.plugin_index)
        if entry is None:
            continue
        resolved_leaves = entry.resolved_leaves
        string_leaf_map = {
            leaf.path: leaf.value
            for leaf in resolved_leaves
            if leaf.value_type == "string" and isinstance(leaf.value, str)
        }
        for path_template in rule.path_templates:
            matched_paths = expand_rule_to_leaf_paths(
                path_template=path_template,
                resolved_leaves=resolved_leaves,
            )
            for leaf_path in matched_paths:
                location_path = jsonpath_to_location_path(
                    json_path=leaf_path,
                    plugin_index=rule.plugin_index,
                )
                if location_path in seen_paths:
                    continue
                seen_paths.add(location_path)
                leaf_value = string_leaf_map.get(leaf_path)
                if leaf_value is None:
                    continue
                hits.append(
                    TextScopeRuleHit(
                        location_path=location_path,
                        source_type="plugin_parameter",
                        rule_source="插件参数规则",
                        original_text=normalize_visible_text_for_extraction(leaf_value),
                    )
                )
    return hits


def collect_event_command_rule_hits(
    *,
    command_index: tuple[EventCommandAnalysisEntry, ...],
    event_rules: list[EventCommandTextRuleRecord],
) -> list[TextScopeRuleHit]:
    """展开事件指令规则命中的全部字符串叶子。"""
    hits: list[TextScopeRuleHit] = []
    seen_paths: set[str] = set()
    for entry in command_index:
        path = entry.location_path
        command = entry.command
        matched_rules = [
            rule
            for rule in event_rules
            if rule.command_code == command.code
            and command_matches_filters(
                parameters=command.parameters,
                filters=rule.parameter_filters,
            )
        ]
        if not matched_rules:
            continue
        command_location_path = "/".join(map(str, path))
        resolved_leaves = entry.resolved_leaves
        string_leaf_map = {
            leaf.path: leaf.value
            for leaf in resolved_leaves
            if leaf.value_type == "string" and isinstance(leaf.value, str)
        }
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
                    if location_path in seen_paths:
                        continue
                    seen_paths.add(location_path)
                    leaf_value = string_leaf_map.get(leaf_path)
                    if leaf_value is None:
                        continue
                    hits.append(
                        TextScopeRuleHit(
                            location_path=location_path,
                            source_type="event_command",
                            rule_source="事件指令规则",
                            original_text=normalize_visible_text_for_extraction(leaf_value),
                        )
                    )
    return hits


def collect_note_tag_rule_hits(
    *,
    note_sources: tuple[NoteTagSource, ...],
    note_tag_rules: list[NoteTagTextRuleRecord],
    text_rules: TextRules,
) -> list[TextScopeRuleHit]:
    """展开 Note 标签规则命中的全部字符串值。"""
    hits: list[TextScopeRuleHit] = []
    seen_paths: set[str] = set()
    for rule in note_tag_rules:
        tag_names = set(rule.tag_names)
        for source in note_sources:
            if not note_file_pattern_matches(file_name=source.file_name, file_pattern=rule.file_name):
                continue
            for match in source.matches:
                if match.tag_name not in tag_names or not match.has_value:
                    continue
                location_path = f"{source.location_prefix}/note/{match.tag_name}"
                if location_path in seen_paths:
                    continue
                seen_paths.add(location_path)
                hits.append(
                    TextScopeRuleHit(
                        location_path=location_path,
                        source_type="note_tag",
                        rule_source="Note 标签规则",
                        original_text=normalize_visible_text_for_extraction(
                            match.value,
                            plain_text_normalizer=text_rules.normalize_extraction_text,
                        ),
                    )
                )
    return hits
