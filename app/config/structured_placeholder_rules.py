"""结构化占位符规则加载器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.rmmz.control_codes import StructuredPlaceholderRule
from app.rmmz.json_types import JsonObject, coerce_json_value, ensure_json_array, ensure_json_object

STRUCTURED_PLACEHOLDER_RULES_FILE_NAME = "structured-placeholder-rules.json"


def empty_structured_placeholder_rules_payload() -> JsonObject:
    """返回结构化占位符规则的合法空对象。"""
    return {"paired_shell_rules": []}


def load_structured_placeholder_rules_file(
    *,
    rules_path: Path,
) -> tuple[StructuredPlaceholderRule, ...]:
    """从指定 JSON 文件读取结构化占位符规则。"""
    resolved_path = rules_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"结构化占位符规则文件不存在: {resolved_path}")
    raw_value = cast(object, json.loads(resolved_path.read_text(encoding="utf-8-sig")))
    return parse_structured_placeholder_rules(raw_value=raw_value, source_label=str(resolved_path))


def load_structured_placeholder_rules_text(rules_text: str) -> tuple[StructuredPlaceholderRule, ...]:
    """从 JSON 字符串读取结构化占位符规则。"""
    stripped_text = rules_text.strip()
    if not stripped_text:
        raise ValueError("结构化占位符规则 JSON 字符串不能为空")
    raw_value = cast(object, json.loads(stripped_text))
    return parse_structured_placeholder_rules(raw_value=raw_value, source_label="structured-placeholder-rules")


def parse_structured_placeholder_rules(
    *,
    raw_value: object,
    source_label: str,
) -> tuple[StructuredPlaceholderRule, ...]:
    """把 JSON 对象转换成结构化占位符规则集合。"""
    json_value = coerce_json_value(raw_value)
    root = ensure_json_object(json_value, source_label)
    allowed_keys = {"paired_shell_rules"}
    extra_keys = sorted(set(root) - allowed_keys)
    if extra_keys:
        raise ValueError(f"{source_label} 包含不支持的字段: {', '.join(extra_keys)}")
    raw_rules = ensure_json_array(root.get("paired_shell_rules", []), f"{source_label}.paired_shell_rules")
    rules: list[StructuredPlaceholderRule] = []
    seen_names: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        context = f"{source_label}.paired_shell_rules[{index}]"
        rule_object = ensure_json_object(raw_rule, context)
        rule = _parse_paired_shell_rule(rule_object=rule_object, context=context)
        if rule.rule_name in seen_names:
            raise ValueError(f"{source_label} 包含重复结构化规则名: {rule.rule_name}")
        seen_names.add(rule.rule_name)
        rules.append(rule)
    return tuple(rules)


def _parse_paired_shell_rule(
    *,
    rule_object: JsonObject,
    context: str,
) -> StructuredPlaceholderRule:
    """解析单条 paired_shell 规则。"""
    name = _read_required_string(rule_object, "name", context)
    pattern = _read_required_string(rule_object, "pattern", context)
    translatable_group = _read_required_string(rule_object, "translatable_group", context)
    rule_type = _read_optional_string(rule_object, "type", context) or "paired_shell"
    protected_groups_value = rule_object.get("protected_groups")
    if protected_groups_value is None:
        raise ValueError(f"{context}.protected_groups 不能为空")
    protected_groups_json = ensure_json_object(
        coerce_json_value(protected_groups_value),
        f"{context}.protected_groups",
    )
    protected_groups: dict[str, str] = {}
    for group_name, placeholder_template in protected_groups_json.items():
        if not isinstance(placeholder_template, str):
            raise TypeError(f"{context}.protected_groups.{group_name} 必须是字符串")
        protected_groups[group_name] = placeholder_template
    allowed_keys = {"name", "type", "pattern", "translatable_group", "protected_groups"}
    extra_keys = sorted(set(rule_object) - allowed_keys)
    if extra_keys:
        raise ValueError(f"{context} 包含不支持的字段: {', '.join(extra_keys)}")
    return StructuredPlaceholderRule.create(
        rule_name=name,
        rule_type=rule_type,
        pattern_text=pattern,
        translatable_group=translatable_group,
        protected_groups=protected_groups,
    )


def _read_required_string(rule_object: JsonObject, key: str, context: str) -> str:
    """读取必填字符串字段。"""
    value = rule_object.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{context}.{key} 必须是字符串")
    if not value.strip():
        raise ValueError(f"{context}.{key} 不能为空")
    return value


def _read_optional_string(rule_object: JsonObject, key: str, context: str) -> str | None:
    """读取可选字符串字段。"""
    value = rule_object.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{context}.{key} 必须是字符串")
    return value


__all__: list[str] = [
    "STRUCTURED_PLACEHOLDER_RULES_FILE_NAME",
    "empty_structured_placeholder_rules_payload",
    "load_structured_placeholder_rules_file",
    "load_structured_placeholder_rules_text",
    "parse_structured_placeholder_rules",
]
