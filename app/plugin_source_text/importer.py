"""插件源码文本规则导入与校验。"""

from __future__ import annotations

import json
from collections import Counter
from typing import cast

from pydantic import TypeAdapter

from app.rmmz.schema import PluginSourceTextRuleRecord
from app.rmmz.text_rules import JsonValue, coerce_json_value

from .models import PluginSourceRuleImportEntry, PluginSourceRuleImportFile, PluginSourceScan

RULE_IMPORT_ADAPTER: TypeAdapter[list[PluginSourceRuleImportEntry]] = TypeAdapter(list[PluginSourceRuleImportEntry])


def parse_plugin_source_rule_import_text(text: str) -> PluginSourceRuleImportFile:
    """解析插件源码规则 JSON 文本。"""
    raw_value = cast(object, json.loads(text))
    value = coerce_json_value(raw_value)
    if not isinstance(value, list):
        raise TypeError("插件源码规则顶层必须是数组")
    entries = RULE_IMPORT_ADAPTER.validate_python(value)
    return PluginSourceRuleImportFile(rules=entries)


def build_plugin_source_rule_records_from_import(
    *,
    import_file: PluginSourceRuleImportFile,
    scan: PluginSourceScan,
) -> list[PluginSourceTextRuleRecord]:
    """按当前游戏源码校验外部插件源码规则并转换为数据库记录。"""
    if not import_file.rules:
        return []
    file_scans = {file_scan.file_name: file_scan for file_scan in scan.files}
    selectors_by_file = {
        file_scan.file_name: {candidate.selector for candidate in file_scan.candidates} for file_scan in scan.files
    }
    file_names = [entry.file for entry in import_file.rules]
    duplicate_files = sorted(file_name for file_name, count in Counter(file_names).items() if count > 1)
    if duplicate_files:
        raise ValueError(f"插件源码规则文件重复: {'、'.join(duplicate_files)}")

    records: list[PluginSourceTextRuleRecord] = []
    for entry in import_file.rules:
        file_name = _validate_plugin_source_file_name(entry.file)
        file_scan = file_scans.get(file_name)
        if file_scan is None:
            raise ValueError(f"插件源码文件不存在或不是 js/plugins 直接文件: {file_name}")
        if not file_scan.active:
            raise ValueError(f"插件源码文件未在 plugins.js 中启用，不能导入规则: {file_name}")
        selectors = _validate_selectors(
            file_name=file_name,
            selector_label="selector",
            selectors=entry.selectors,
            available_selectors=selectors_by_file[file_name],
            allow_empty=True,
        )
        excluded_selectors = _validate_selectors(
            file_name=file_name,
            selector_label="排除 selector",
            selectors=entry.excluded_selectors,
            available_selectors=selectors_by_file[file_name],
            allow_empty=True,
        )
        overlap_selectors = sorted(set(selectors) & set(excluded_selectors))
        if overlap_selectors:
            raise ValueError(f"插件源码 selector 不能同时翻译和排除: {file_name}: {'、'.join(overlap_selectors[:5])}")
        if not selectors and not excluded_selectors:
            raise ValueError(f"插件源码规则缺少 selector: {file_name}")
        records.append(
            PluginSourceTextRuleRecord(
                file_name=file_name,
                file_hash=file_scan.file_hash,
                selectors=selectors,
                excluded_selectors=excluded_selectors,
            )
        )
    return records


def plugin_source_rule_records_to_import_json(records: list[PluginSourceTextRuleRecord]) -> JsonValue:
    """把数据库记录还原为外部可编辑 JSON。"""
    return [
        {
            "file": record.file_name,
            "selectors": [selector for selector in record.selectors],
            "excluded_selectors": [selector for selector in record.excluded_selectors],
        }
        for record in sorted(records, key=lambda item: item.file_name)
    ]


def _validate_plugin_source_file_name(file_name: str) -> str:
    """校验规则中的插件源码文件名只能是直接 `.js` 文件。"""
    normalized_name = file_name.strip()
    if not normalized_name:
        raise ValueError("插件源码文件名不能为空")
    if "/" in normalized_name or "\\" in normalized_name:
        raise ValueError(f"插件源码规则只允许直接文件名: {file_name}")
    if normalized_name in {".", ".."} or not normalized_name.endswith(".js"):
        raise ValueError(f"插件源码规则文件名必须是 .js 文件: {file_name}")
    return normalized_name


def _validate_selectors(
    *,
    file_name: str,
    selector_label: str,
    selectors: list[str],
    available_selectors: set[str],
    allow_empty: bool = False,
) -> list[str]:
    """校验 selector 非空、无重复且能命中当前 AST 地图。"""
    cleaned_selectors = [selector.strip() for selector in selectors if selector.strip()]
    if not cleaned_selectors and not allow_empty:
        raise ValueError(f"插件源码规则缺少 selector: {file_name}")
    duplicate_selectors = sorted(selector for selector, count in Counter(cleaned_selectors).items() if count > 1)
    if duplicate_selectors:
        raise ValueError(f"插件源码 {selector_label} 重复: {file_name}: {'、'.join(duplicate_selectors)}")
    missing_selectors = sorted(selector for selector in cleaned_selectors if selector not in available_selectors)
    if missing_selectors:
        raise ValueError(
            f"插件源码 {selector_label} 未命中当前 AST 地图: {file_name}: {'、'.join(missing_selectors[:5])}"
        )
    return cleaned_selectors


__all__ = [
    "build_plugin_source_rule_records_from_import",
    "parse_plugin_source_rule_import_text",
    "plugin_source_rule_records_to_import_json",
]
