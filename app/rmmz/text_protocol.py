"""游戏文本协议外壳保护工具。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from app.rmmz.text_rules import JsonValue, coerce_json_value

JSON_STRING_SHELL_PATTERN: re.Pattern[str] = re.compile(r'^\s*"(?:[^"\\]|\\.)*"\s*$', re.DOTALL)
DOUBLED_CONTROL_LITERAL_PATTERN: re.Pattern[str] = re.compile(
    r"\\\\(?:[A-Za-z]+\d*(?:\[[^\]\r\n]{0,64}\])?|[{}\\$.\|!><^]|[nrt])"
)


@dataclass(frozen=True, slots=True)
class VisibleTextDecodeResult:
    """原始字符串解壳后的玩家可见文本与外壳层数。"""

    text: str
    json_string_shell_depth: int


@dataclass(frozen=True, slots=True)
class JsonContainerDecodeResult:
    """原始字符串解出的 JSON 容器与容器外的字符串外壳层数。"""

    value: dict[str, JsonValue] | list[JsonValue]
    json_string_shell_depth: int


def decode_visible_text(raw_text: str) -> str:
    """把字符串叶子解成真正给玩家看的文本。"""
    return inspect_visible_text(raw_text).text


def normalize_visible_text_for_extraction(
    raw_text: str,
    *,
    plain_text_normalizer: Callable[[str], str] | None = None,
) -> str:
    """生成提取入库用玩家文本，解开 JSON 字符串外壳后清理首尾空白。"""
    decoded = inspect_visible_text(raw_text)
    if plain_text_normalizer is None:
        return decoded.text.strip()
    return plain_text_normalizer(decoded.text)


def inspect_visible_text(raw_text: str) -> VisibleTextDecodeResult:
    """解析字符串叶子的 JSON 字符串外壳层级。"""
    current_text = raw_text
    shell_depth = 0
    while JSON_STRING_SHELL_PATTERN.fullmatch(current_text) is not None:
        try:
            decoded = cast(object, json.loads(current_text))
        except json.JSONDecodeError:
            break
        if not isinstance(decoded, str):
            break
        shell_depth += 1
        current_text = decoded
    return VisibleTextDecodeResult(
        text=current_text,
        json_string_shell_depth=shell_depth,
    )


def decode_json_container_text(raw_text: str) -> JsonContainerDecodeResult | None:
    """尝试把字符串解析为 JSON 数组或对象，支持容器外的 JSON 字符串外壳。"""
    current_text = raw_text
    shell_depth = 0
    while True:
        try:
            decoded = cast(object, json.loads(current_text))
            parsed = coerce_json_value(decoded)
        except TypeError, json.JSONDecodeError:
            return None

        if isinstance(parsed, dict | list):
            return JsonContainerDecodeResult(
                value=parsed,
                json_string_shell_depth=shell_depth,
            )
        if not isinstance(parsed, str):
            return None
        shell_depth += 1
        current_text = parsed


def encode_visible_text_like(*, original_raw_text: str, translated_visible_text: str) -> str:
    """按原字符串叶子的外壳层级重新封装译文。"""
    shell_depth = inspect_visible_text(original_raw_text).json_string_shell_depth
    encoded_text = translated_visible_text
    for _index in range(shell_depth):
        encoded_text = json.dumps(encoded_text, ensure_ascii=False)
    return encoded_text


def encode_json_container_like(
    *,
    original_raw_text: str,
    updated_value: dict[str, JsonValue] | list[JsonValue],
) -> str:
    """按原字符串容器的外壳层级重新封装 JSON 数组或对象。"""
    container = decode_json_container_text(original_raw_text)
    if container is None:
        raise ValueError("原文本不是可解析的 JSON 容器字符串")
    encoded_text = json.dumps(updated_value, ensure_ascii=False)
    for _index in range(container.json_string_shell_depth):
        encoded_text = json.dumps(encoded_text, ensure_ascii=False)
    return encoded_text


def validate_encoded_text(*, original_raw_text: str, written_raw_text: str) -> list[str]:
    """校验写回后的字符串叶子是否保留原始解析协议。"""
    original = inspect_visible_text(original_raw_text)
    written = inspect_visible_text(written_raw_text)
    errors: list[str] = []
    if original.json_string_shell_depth != written.json_string_shell_depth:
        errors.append(
            f"JSON 字符串外壳层数不一致 (原文: {original.json_string_shell_depth}, 写回: {written.json_string_shell_depth})"
        )
    if original.json_string_shell_depth > 0:
        doubled_literals = _collect_doubled_control_literals(written.text)
        if doubled_literals:
            joined_literals = "、".join(doubled_literals)
            errors.append(f"控制符被写成会直接显示的字面量: {joined_literals}")
    return errors


def ensure_encoded_text_valid(*, original_raw_text: str, written_raw_text: str, context: str) -> None:
    """校验文本协议，失败时抛出带定位的业务错误。"""
    errors = validate_encoded_text(
        original_raw_text=original_raw_text,
        written_raw_text=written_raw_text,
    )
    if errors:
        raise ValueError(f"{context} 文本协议写回失败: {'; '.join(errors)}")


def _collect_doubled_control_literals(text: str) -> list[str]:
    """收集疑似被多写了一层反斜杠的控制符。"""
    literals: list[str] = []
    for match in DOUBLED_CONTROL_LITERAL_PATTERN.finditer(text):
        literals.append(match.group(0))
    return sorted(set(literals))


__all__: list[str] = [
    "JsonContainerDecodeResult",
    "VisibleTextDecodeResult",
    "decode_json_container_text",
    "decode_visible_text",
    "encode_json_container_like",
    "encode_visible_text_like",
    "ensure_encoded_text_valid",
    "inspect_visible_text",
    "normalize_visible_text_for_extraction",
    "validate_encoded_text",
]
