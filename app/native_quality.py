"""Rust 原生质检核心适配层。

本模块负责把 Python 业务对象转换为 Rust 扩展可处理的纯 JSON 数据，并把
Rust 多线程扫描结果恢复成项目现有质量报告明细结构。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.native_runtime import invoke_native
from app.native_runtime import native_thread_count as runtime_native_thread_count
from app.rmmz.schema import (
    COMMON_EVENTS_FILE_NAME,
    MAP_PATTERN,
    PLUGINS_FILE_NAME,
    TROOPS_FILE_NAME,
    SourceResidualRuleRecord,
    TranslationItem,
)
from app.rmmz.text_rules import JsonArray, JsonObject, JsonValue, TextRules, ensure_json_array, ensure_json_object


@dataclass(frozen=True, slots=True)
class NativeQualityDetails:
    """Rust 质检核心返回的问题明细。"""

    source_residual_items: JsonArray
    text_structure_items: JsonArray
    placeholder_risk_items: JsonArray
    overwide_line_items: JsonArray


@dataclass(frozen=True, slots=True)
class NativeQualityCounts:
    """Rust 质检核心返回的问题计数。"""

    source_residual_count: int
    text_structure_count: int
    placeholder_risk_count: int
    overwide_line_count: int


def collect_native_quality_details(
    *,
    items: list[TranslationItem],
    text_rules: TextRules,
    source_residual_rules: list[SourceResidualRuleRecord],
) -> NativeQualityDetails:
    """调用 Rust 多线程核心收集正文质量问题。"""
    payload = _build_quality_payload(
        items=items,
        text_rules=text_rules,
        source_residual_rules=source_residual_rules,
    )
    result = ensure_json_object(
        invoke_native("quality.scan", payload),
        "native_quality_result",
    )
    return NativeQualityDetails(
        source_residual_items=ensure_json_array(
            result["source_residual_items"],
            "native_quality_result.source_residual_items",
        ),
        text_structure_items=ensure_json_array(
            result["text_structure_items"],
            "native_quality_result.text_structure_items",
        ),
        placeholder_risk_items=ensure_json_array(
            result["placeholder_risk_items"],
            "native_quality_result.placeholder_risk_items",
        ),
        overwide_line_items=ensure_json_array(
            result["overwide_line_items"],
            "native_quality_result.overwide_line_items",
        ),
    )


def collect_native_quality_counts(
    *,
    items: list[TranslationItem],
    text_rules: TextRules,
    source_residual_rules: list[SourceResidualRuleRecord],
) -> NativeQualityCounts:
    """调用 Rust 多线程核心收集正文质量问题计数。"""
    payload = _build_quality_payload(
        items=items,
        text_rules=text_rules,
        source_residual_rules=source_residual_rules,
    )
    result = ensure_json_object(
        invoke_native("quality.counts", payload),
        "native_quality_count_result",
    )
    return NativeQualityCounts(
        source_residual_count=_read_non_negative_int(
            result,
            "source_residual_count",
            "native_quality_count_result",
        ),
        text_structure_count=_read_non_negative_int(
            result,
            "text_structure_count",
            "native_quality_count_result",
        ),
        placeholder_risk_count=_read_non_negative_int(
            result,
            "placeholder_risk_count",
            "native_quality_count_result",
        ),
        overwide_line_count=_read_non_negative_int(
            result,
            "overwide_line_count",
            "native_quality_count_result",
        ),
    )


def native_thread_count() -> int:
    """读取 Rust 质检核心当前会使用的线程数。"""
    return runtime_native_thread_count()


def collect_native_write_protocol_details(
    *,
    game_data: JsonObject,
    plugins_js: JsonArray,
    items: list[TranslationItem],
) -> JsonArray:
    """调用 Rust 多线程核心检查写入协议风险。"""
    payload: JsonObject = {"entries": _build_protocol_entries(game_data=game_data, plugins_js=plugins_js, items=items)}
    return ensure_json_array(
        invoke_native("write_protocol.scan", payload),
        "native_write_protocol_result",
    )


def count_native_write_protocol_issues(
    *,
    game_data: JsonObject,
    plugins_js: JsonArray,
    items: list[TranslationItem],
) -> int:
    """调用 Rust 多线程核心检查写入协议风险并只返回数量。"""
    payload: JsonObject = {"entries": _build_protocol_entries(game_data=game_data, plugins_js=plugins_js, items=items)}
    result = ensure_json_object(
        invoke_native("write_protocol.counts", payload),
        "native_write_protocol_count_result",
    )
    return _read_non_negative_int(
        result,
        "write_protocol_count",
        "native_write_protocol_count_result",
    )


def collect_native_note_tag_sources(
    *,
    game_data: JsonObject,
    file_pattern: str | None,
) -> JsonArray:
    """调用 Rust 核心扫描所有可用 Note 标签来源。"""
    payload: JsonObject = {
        "data": game_data,
        "file_pattern": file_pattern,
    }
    return ensure_json_array(
        invoke_native("note_sources.collect", payload),
        "native_note_tag_sources",
    )


def _build_quality_payload(
    *,
    items: list[TranslationItem],
    text_rules: TextRules,
    source_residual_rules: list[SourceResidualRuleRecord],
) -> JsonObject:
    """构造 Rust 质检核心输入载荷。"""
    return {
        "items": [_build_item_payload(item) for item in items],
        "text_rules": build_native_text_rules_payload(text_rules),
        "source_residual_rules": [
            {
                "rule_id": record.rule_id,
                "rule_type": record.rule_type,
                "location_path": record.location_path,
                "pattern_text": record.pattern_text,
                "allowed_terms": list(record.allowed_terms),
                "check_group": record.check_group,
                "reason": record.reason,
            }
            for record in source_residual_rules
        ],
    }


def _build_item_payload(item: TranslationItem) -> JsonObject:
    """把单条译文压成 Rust 只需要读取的最小结构。"""
    return {
        "location_path": item.location_path,
        "item_type": item.item_type,
        "role": item.role,
        "original_lines": [line for line in item.original_lines],
        "translation_lines": [line for line in item.translation_lines],
    }


def _read_non_negative_int(result: JsonObject, key: str, context: str) -> int:
    """读取 Rust 计数结果中的非负整数。"""
    value = result[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"{context}.{key} 必须是非负整数")
    return value


def _build_protocol_entries(
    *,
    game_data: JsonObject,
    plugins_js: JsonArray,
    items: list[TranslationItem],
) -> JsonArray:
    """把写入协议检查压成 Rust 可并行处理的最小目标值列表。"""
    entries: JsonArray = []
    for item in items:
        location_path = item.location_path
        if location_path.startswith(f"{PLUGINS_FILE_NAME}/"):
            entries.append(_build_plugin_protocol_entry(plugins_js=plugins_js, item=item))
            continue
        if "/note/" in location_path:
            entries.append(_build_note_protocol_entry(game_data=game_data, item=item))
            continue
        if "/parameters/" in location_path:
            entries.append(_build_event_parameter_protocol_entry(game_data=game_data, item=item))
    return entries


def _build_plugin_protocol_entry(*, plugins_js: JsonArray, item: TranslationItem) -> JsonObject:
    """构造插件参数写入协议检查条目。"""
    parts = item.location_path.split("/")
    if len(parts) < 3:
        raise ValueError(f"插件参数路径缺少字段段: {item.location_path}")
    plugin = ensure_json_object(plugins_js[int(parts[1])], item.location_path)
    parameters = ensure_json_object(plugin["parameters"], f"{item.location_path}.parameters")
    return {
        "item": _build_item_payload(item),
        "mode": "nested",
        "current_value": parameters[parts[2]],
        "path_parts": [part for part in parts[3:]],
        "note_text": None,
        "tag_name": None,
    }


def _build_event_parameter_protocol_entry(*, game_data: JsonObject, item: TranslationItem) -> JsonObject:
    """构造事件指令参数写入协议检查条目。"""
    parts = item.location_path.split("/")
    command, value_parts = _locate_event_command_for_protocol(
        game_data=game_data, parts=parts, context=item.location_path
    )
    if len(value_parts) < 2 or value_parts[0] != "parameters":
        raise ValueError(f"事件指令路径缺少 parameters 段: {item.location_path}")
    parameters = ensure_json_array(command["parameters"], f"{item.location_path}.parameters")
    return {
        "item": _build_item_payload(item),
        "mode": "nested",
        "current_value": parameters[int(value_parts[1])],
        "path_parts": [part for part in value_parts[2:]],
        "note_text": None,
        "tag_name": None,
    }


def _locate_event_command_for_protocol(
    *,
    game_data: JsonObject,
    parts: list[str],
    context: str,
) -> tuple[JsonObject, list[str]]:
    """按定位路径找到事件指令对象和参数路径尾段。"""
    file_name = parts[0]
    data = game_data[file_name]
    if MAP_PATTERN.fullmatch(file_name):
        map_object = ensure_json_object(data, file_name)
        events = ensure_json_array(map_object["events"], f"{file_name}.events")
        event = ensure_json_object(events[int(parts[1])], f"{context}.event")
        pages = ensure_json_array(event["pages"], f"{context}.pages")
        page = ensure_json_object(pages[int(parts[2])], f"{context}.page")
        commands = ensure_json_array(page["list"], f"{context}.list")
        return ensure_json_object(commands[int(parts[3])], f"{context}.command"), parts[4:]
    if file_name == COMMON_EVENTS_FILE_NAME:
        events = ensure_json_array(data, file_name)
        event = ensure_json_object(events[int(parts[1])], f"{context}.event")
        commands = ensure_json_array(event["list"], f"{context}.list")
        return ensure_json_object(commands[int(parts[2])], f"{context}.command"), parts[3:]
    if file_name == TROOPS_FILE_NAME:
        troops = ensure_json_array(data, file_name)
        troop = ensure_json_object(troops[int(parts[1])], f"{context}.troop")
        pages = ensure_json_array(troop["pages"], f"{context}.pages")
        page = ensure_json_object(pages[int(parts[2])], f"{context}.page")
        commands = ensure_json_array(page["list"], f"{context}.list")
        return ensure_json_object(commands[int(parts[3])], f"{context}.command"), parts[4:]
    raise ValueError(f"无法识别的事件值路径: {context}")


def _build_note_protocol_entry(*, game_data: JsonObject, item: TranslationItem) -> JsonObject:
    """构造 Note 标签写入协议检查条目。"""
    parts = item.location_path.split("/")
    tag_name = parts[-1]
    owner = _locate_note_owner(value=game_data[parts[0]], owner_parts=parts[1:-2], context=item.location_path)
    note_value = owner.get("note")
    if not isinstance(note_value, str):
        raise ValueError(f"Note 字段不是字符串: {item.location_path}")
    return {
        "item": _build_item_payload(item),
        "mode": "note",
        "current_value": None,
        "path_parts": [],
        "note_text": note_value,
        "tag_name": tag_name,
    }


def _locate_note_owner(*, value: JsonValue, owner_parts: list[str], context: str) -> JsonObject:
    """根据 Note 标签路径定位持有 note 字段的 JSON 对象。"""
    current_value = value
    for part in owner_parts:
        if isinstance(current_value, dict):
            current_value = current_value[part]
            continue
        if isinstance(current_value, list):
            index = int(part)
            if index < len(current_value) and current_value[index] is not None:
                current_value = current_value[index]
                continue
            matched_value = next(
                (
                    candidate
                    for candidate in current_value
                    if isinstance(candidate, dict) and candidate.get("id") == index
                ),
                None,
            )
            if matched_value is None:
                raise ValueError(f"Note 路径数组索引不存在: {context}")
            current_value = matched_value
            continue
        raise ValueError(f"Note 路径无法继续定位: {context}")
    return ensure_json_object(current_value, f"{context}.note_owner")


def build_native_text_rules_payload(text_rules: TextRules) -> JsonObject:
    """把文本规则压成 Rust 质检核心的输入结构。"""
    setting = text_rules.setting
    return {
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
                "protected_groups": dict(rule.protected_groups),
            }
            for rule in text_rules.structured_placeholder_rules
        ],
        "source_residual_allowed_chars": [char for char in setting.source_residual_allowed_chars],
        "source_residual_allowed_tail_chars": [char for char in setting.source_residual_allowed_tail_chars],
        "source_residual_segment_pattern": setting.source_residual_segment_pattern,
        "source_residual_label": setting.source_residual_label,
        "allowed_source_residual_terms": [term for term in setting.allowed_source_residual_terms],
        "source_residual_terms_ignore_case": setting.source_residual_terms_ignore_case,
        "line_width_count_pattern": setting.line_width_count_pattern,
        "residual_escape_sequence_pattern": setting.residual_escape_sequence_pattern,
        "long_text_line_width_limit": setting.long_text_line_width_limit,
    }


__all__ = [
    "NativeQualityDetails",
    "NativeQualityCounts",
    "collect_native_note_tag_sources",
    "collect_native_quality_counts",
    "collect_native_quality_details",
    "collect_native_write_protocol_details",
    "count_native_write_protocol_issues",
    "build_native_text_rules_payload",
    "native_thread_count",
]
