"""自定义占位符规则加载器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.rmmz.control_codes import CustomPlaceholderRule
from app.rmmz.json_types import coerce_json_value, ensure_json_object


def load_custom_placeholder_rules_file(
    *,
    rules_path: Path,
) -> tuple[CustomPlaceholderRule, ...]:
    """从指定 JSON 文件读取自定义正则占位符规则。"""
    rules_path = rules_path.resolve()
    if not rules_path.exists():
        raise FileNotFoundError(f"自定义占位符规则文件不存在: {rules_path}")

    raw_value = cast(object, json.loads(rules_path.read_text(encoding="utf-8-sig")))
    return parse_custom_placeholder_rules(raw_value=raw_value, source_label=str(rules_path))


def load_custom_placeholder_rules_text(rules_text: str) -> tuple[CustomPlaceholderRule, ...]:
    """从命令行 JSON 字符串读取自定义正则占位符规则。"""
    stripped_text = rules_text.strip()
    if not stripped_text:
        raise ValueError("自定义占位符规则 JSON 字符串不能为空")
    raw_value = cast(object, json.loads(stripped_text))
    return parse_custom_placeholder_rules(raw_value=raw_value, source_label="--placeholder-rules")


def parse_custom_placeholder_rules(
    *,
    raw_value: object,
    source_label: str,
) -> tuple[CustomPlaceholderRule, ...]:
    """把 JSON 对象转换成自定义占位符规则集合。"""
    json_value = coerce_json_value(raw_value)
    raw_rules = ensure_json_object(json_value, source_label)

    rules: list[CustomPlaceholderRule] = []
    for pattern_text, placeholder_template in raw_rules.items():
        if not isinstance(placeholder_template, str):
            raise TypeError(f"{source_label} 中 {pattern_text} 的值必须是字符串")
        try:
            rule = CustomPlaceholderRule.create(
                pattern_text=pattern_text,
                placeholder_template=placeholder_template,
            )
        except ValueError as error:
            raise ValueError(f"{source_label} 中规则 {pattern_text} -> {placeholder_template} 无效: {error}") from error
        rules.append(rule)
    return tuple(rules)


__all__: list[str] = [
    "load_custom_placeholder_rules_file",
    "load_custom_placeholder_rules_text",
    "parse_custom_placeholder_rules",
]
