"""字体引用文本替换与还原算法。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.rmmz.schema import FontReplacementRecord
from app.rmmz.text_rules import JsonObject, JsonValue, coerce_json_value

from .constants import BARE_FONT_REFERENCE_PATTERN, FONT_FILE_REFERENCE_PATTERN, FONT_FILE_SUFFIXES


def build_font_reference_tokens(old_font_names: list[str]) -> list[str]:
    """生成字体文件名和不带扩展名的字体引用候选。"""
    token_set: set[str] = set()
    for old_font_name in old_font_names:
        token_set.add(old_font_name)
        font_stem = Path(old_font_name).stem
        if font_stem:
            token_set.add(font_stem)
    return sorted(token_set, key=len, reverse=True)


def replace_font_names_in_json_value(
    *,
    value: JsonValue,
    old_font_names: list[str],
    replacement_font_name: str,
) -> tuple[JsonValue, int]:
    """递归替换 JSON 值中的旧字体文件名。"""
    if isinstance(value, str):
        return replace_font_names_in_text(
            text=value,
            old_font_names=old_font_names,
            replacement_font_name=replacement_font_name,
        )

    if isinstance(value, list):
        replaced_items: list[JsonValue] = []
        replaced_count = 0
        for item in value:
            replaced_item, count = replace_font_names_in_json_value(
                value=item,
                old_font_names=old_font_names,
                replacement_font_name=replacement_font_name,
            )
            replaced_items.append(replaced_item)
            replaced_count += count
        return replaced_items, replaced_count

    if isinstance(value, dict):
        replaced_object: JsonObject = {}
        replaced_count = 0
        for key, item in value.items():
            replaced_item, count = replace_font_names_in_json_value(
                value=item,
                old_font_names=old_font_names,
                replacement_font_name=replacement_font_name,
            )
            replaced_object[key] = replaced_item
            replaced_count += count
        return replaced_object, replaced_count

    return value, 0


def replace_font_names_in_text(
    *,
    text: str,
    old_font_names: list[str],
    replacement_font_name: str,
) -> tuple[str, int]:
    """只替换完整字体引用，避免误改玩家可见正文。"""
    if not any(old_font_name in text for old_font_name in old_font_names):
        return text, 0
    replaced_text, replaced_count = replace_complete_font_reference_text(
        text=text,
        old_font_names=old_font_names,
        replacement_font_name=replacement_font_name,
    )
    if replaced_count:
        return replaced_text, replaced_count
    return replace_font_references_in_encoded_json_text(
        text=text,
        old_font_names=old_font_names,
        replacement_font_name=replacement_font_name,
    )


def replace_complete_font_reference_text(
    *,
    text: str,
    old_font_names: list[str],
    replacement_font_name: str,
) -> tuple[str, int]:
    """替换整个字符串就是旧字体引用的值。"""
    stripped_text = text.strip()
    if not stripped_text:
        return text, 0

    leading_text = text[: len(text) - len(text.lstrip())]
    trailing_text = text[len(text.rstrip()) :]
    for old_font_name in old_font_names:
        replaced_reference = replace_complete_font_reference_core(
            text=stripped_text,
            old_font_name=old_font_name,
            replacement_font_name=replacement_font_name,
        )
        if replaced_reference is not None:
            return f"{leading_text}{replaced_reference}{trailing_text}", 1
    return text, 0


def replace_complete_font_reference_core(
    *,
    text: str,
    old_font_name: str,
    replacement_font_name: str,
) -> str | None:
    """替换完整文件名、完整 stem 或带目录前缀的完整字体引用。"""
    if text == old_font_name:
        return replacement_font_name

    separator_index = max(text.rfind("/"), text.rfind("\\"))
    if separator_index < 0:
        return None
    reference_name = text[separator_index + 1 :]
    if reference_name != old_font_name:
        return None
    return f"{text[: separator_index + 1]}{replacement_font_name}"


def replace_font_references_in_encoded_json_text(
    *,
    text: str,
    old_font_names: list[str],
    replacement_font_name: str,
) -> tuple[str, int]:
    """解析插件参数里的 JSON 字符串，并只替换其中的完整字体引用。"""
    try:
        raw_value = cast(object, json.loads(text))
        json_value = coerce_json_value(raw_value)
    except json.JSONDecodeError, TypeError:
        return text, 0

    if not isinstance(json_value, list | dict):
        return text, 0

    replaced_value, replaced_count = replace_font_names_in_json_value(
        value=json_value,
        old_font_names=old_font_names,
        replacement_font_name=replacement_font_name,
    )
    if replaced_count == 0:
        return text, 0
    return json.dumps(replaced_value, ensure_ascii=False), replaced_count


def collect_replacement_font_names(
    *,
    replacement_font_path: str | None,
    records: list[FontReplacementRecord],
) -> list[str]:
    """收集本次字体还原应识别的新字体文件名。"""
    font_names: list[str] = []
    if replacement_font_path is not None and replacement_font_path.strip():
        font_names.append(Path(replacement_font_path).name)
    font_names.extend(record.replacement_font_name for record in records)
    return normalize_font_name_list(font_names)


def restore_font_references_in_json_value_by_origin(
    *,
    active_value: JsonValue,
    origin_value: JsonValue,
    target_font_names: list[str],
) -> tuple[JsonValue, int, int]:
    """递归对比同路径 JSON 值，并只还原字符串里的字体引用。"""
    if isinstance(active_value, str) and isinstance(origin_value, str):
        restored_text, reference_count = restore_font_references_in_text_by_origin(
            active_text=active_value,
            origin_text=origin_value,
            target_font_names=target_font_names,
        )
        return restored_text, 1 if reference_count > 0 else 0, reference_count

    if isinstance(active_value, list) and isinstance(origin_value, list):
        restored_items: list[JsonValue] = []
        restored_field_count = 0
        restored_reference_count = 0
        for index, active_item in enumerate(active_value):
            if index >= len(origin_value):
                restored_items.append(active_item)
                continue
            restored_item, field_count, reference_count = restore_font_references_in_json_value_by_origin(
                active_value=active_item,
                origin_value=origin_value[index],
                target_font_names=target_font_names,
            )
            restored_items.append(restored_item)
            restored_field_count += field_count
            restored_reference_count += reference_count
        return restored_items, restored_field_count, restored_reference_count

    if isinstance(active_value, dict) and isinstance(origin_value, dict):
        restored_object: JsonObject = {}
        restored_field_count = 0
        restored_reference_count = 0
        for key, active_item in active_value.items():
            if key not in origin_value:
                restored_object[key] = active_item
                continue
            restored_item, field_count, reference_count = restore_font_references_in_json_value_by_origin(
                active_value=active_item,
                origin_value=origin_value[key],
                target_font_names=target_font_names,
            )
            restored_object[key] = restored_item
            restored_field_count += field_count
            restored_reference_count += reference_count
        return restored_object, restored_field_count, restored_reference_count

    return active_value, 0, 0


def restore_font_references_in_text_by_origin(
    *,
    active_text: str,
    origin_text: str,
    target_font_names: list[str],
) -> tuple[str, int]:
    """只在完整字体引用位置按原始备份还原字体名。"""
    restored_text, reference_count = restore_complete_font_reference_text(
        active_text=active_text,
        origin_text=origin_text,
        target_font_names=target_font_names,
    )
    if reference_count:
        return restored_text, reference_count
    return restore_font_references_in_encoded_json_text(
        active_text=active_text,
        origin_text=origin_text,
        target_font_names=target_font_names,
    )


def restore_complete_font_reference_text(
    *,
    active_text: str,
    origin_text: str,
    target_font_names: list[str],
) -> tuple[str, int]:
    """还原整个字符串就是覆盖字体引用的字段。"""
    stripped_active_text = active_text.strip()
    if not stripped_active_text:
        return active_text, 0
    origin_font_reference = collect_origin_font_reference(origin_text)
    if origin_font_reference is None:
        return active_text, 0

    leading_text = active_text[: len(active_text) - len(active_text.lstrip())]
    trailing_text = active_text[len(active_text.rstrip()) :]
    for target_font_name in target_font_names:
        if not is_complete_reference_to_font(
            text=stripped_active_text,
            font_name=target_font_name,
        ):
            continue
        return f"{leading_text}{origin_font_reference}{trailing_text}", 1
    return active_text, 0


def restore_font_references_in_encoded_json_text(
    *,
    active_text: str,
    origin_text: str,
    target_font_names: list[str],
) -> tuple[str, int]:
    """还原插件参数 JSON 字符串内部的完整字体引用。"""
    try:
        active_raw_value = cast(object, json.loads(active_text))
        origin_raw_value = cast(object, json.loads(origin_text))
        active_value = coerce_json_value(active_raw_value)
        origin_value = coerce_json_value(origin_raw_value)
    except json.JSONDecodeError, TypeError:
        return active_text, 0

    if not isinstance(active_value, list | dict) or not isinstance(origin_value, list | dict):
        return active_text, 0

    restored_value, _field_count, reference_count = restore_font_references_in_json_value_by_origin(
        active_value=active_value,
        origin_value=origin_value,
        target_font_names=target_font_names,
    )
    if reference_count == 0:
        return active_text, 0
    return json.dumps(restored_value, ensure_ascii=False), reference_count


def collect_origin_font_reference(text: str) -> str | None:
    """从原文本字段中读取完整旧字体引用。"""
    stripped_text = text.strip()
    if not stripped_text:
        return None
    if is_complete_font_file_reference(stripped_text):
        return stripped_text
    if is_complete_bare_font_reference(stripped_text):
        return stripped_text
    return None


def is_complete_reference_to_font(*, text: str, font_name: str) -> bool:
    """判断当前字符串是否完整指向指定字体名。"""
    if text == font_name:
        return True
    reference_name = extract_font_reference_name(text)
    return reference_name == font_name


def is_complete_font_file_reference(text: str) -> bool:
    """判断字符串是否是完整字体文件引用。"""
    reference_name = extract_font_reference_name(text)
    return FONT_FILE_REFERENCE_PATTERN.fullmatch(reference_name) is not None


def is_complete_bare_font_reference(text: str) -> bool:
    """判断字符串是否是完整无扩展名字体引用。"""
    reference_name = extract_font_reference_name(text)
    return BARE_FONT_REFERENCE_PATTERN.fullmatch(reference_name) is not None


def extract_font_reference_name(text: str) -> str:
    """取出带目录字体引用末尾的字体名。"""
    separator_index = max(text.rfind("/"), text.rfind("\\"))
    if separator_index < 0:
        return text
    return text[separator_index + 1 :]


def is_supported_font_file_name(file_name: str) -> bool:
    """判断路径末尾是否是项目支持替换的字体文件。"""
    return Path(file_name).suffix.lower() in FONT_FILE_SUFFIXES


def is_target_font_reference(*, text: str, target_font_names: list[str]) -> bool:
    """判断 CSS URL 是否指向候选覆盖字体。"""
    for target_font_name in target_font_names:
        if is_complete_reference_to_font(text=text, font_name=target_font_name):
            return True
    return False


def normalize_font_name_list(font_names: list[str]) -> list[str]:
    """清理字体名列表并保持稳定去重顺序。"""
    normalized_names: list[str] = []
    seen_names: set[str] = set()
    for font_name in font_names:
        normalized_name = font_name.strip()
        if not normalized_name or normalized_name in seen_names:
            continue
        normalized_names.append(normalized_name)
        seen_names.add(normalized_name)
    return normalized_names
