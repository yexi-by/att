"""Rust 原生疑似控制符 occurrence 批量扫描适配层。"""

from __future__ import annotations

from dataclasses import dataclass

from app.native_quality import build_native_text_rules_payload
from app.native_runtime import invoke_native
from app.rmmz.text_rules import (
    ControlSequenceCoverageKind,
    JsonValue,
    TextRules,
    ensure_json_array,
    ensure_json_object,
)


@dataclass(frozen=True, slots=True)
class NativePlaceholderScanText:
    """交给 Rust 扫描的一行正文及其稳定位置。"""

    location_path: str
    line_number: int
    text: str


@dataclass(frozen=True, slots=True)
class NativePlaceholderOccurrence:
    """Rust 返回的单次疑似控制符覆盖事实。"""

    location_path: str
    line_number: int
    start_index: int
    end_index: int
    raw_marker: str
    marker: str
    coverage_kind: ControlSequenceCoverageKind
    matched_rule_ids: tuple[str, ...]


def scan_native_placeholder_occurrences(
    *,
    texts: list[NativePlaceholderScanText],
    text_rules: TextRules,
) -> list[NativePlaceholderOccurrence]:
    """一次调用 Rust 核心扫描全部正文行，不提供 Python 正则回退。"""
    result = ensure_json_object(
        invoke_native(
            "placeholder_candidates.scan",
            {
                "texts": [
                    {
                        "location_path": item.location_path,
                        "line_number": item.line_number,
                        "text": item.text,
                    }
                    for item in texts
                ],
                "text_rules": build_native_text_rules_payload(text_rules),
            },
        ),
        "native_placeholder_candidates_result",
    )
    _require_exact_keys(result, {"occurrences"}, "native_placeholder_candidates_result")
    source_texts: dict[tuple[str, int], list[str]] = {}
    for item in texts:
        source_texts.setdefault((item.location_path, item.line_number), []).append(item.text)
    return [
        _parse_occurrence(value, index=index, source_texts=source_texts)
        for index, value in enumerate(
            ensure_json_array(
                result["occurrences"],
                "native_placeholder_candidates_result.occurrences",
            )
        )
    ]


def _parse_occurrence(
    value: JsonValue,
    *,
    index: int,
    source_texts: dict[tuple[str, int], list[str]],
) -> NativePlaceholderOccurrence:
    """严格收窄单个 occurrence，并核对字符范围绑定的原文。"""
    label = f"native_placeholder_candidates_result.occurrences[{index}]"
    item = ensure_json_object(value, label)
    _require_exact_keys(
        item,
        {
            "location_path",
            "line_number",
            "start_index",
            "end_index",
            "raw_marker",
            "marker",
            "coverage_kind",
            "matched_rule_ids",
        },
        label,
    )
    location_path = _read_string(item, "location_path", label)
    line_number = _read_non_negative_int(item, "line_number", label, allow_zero=False)
    start_index = _read_non_negative_int(item, "start_index", label)
    end_index = _read_non_negative_int(item, "end_index", label)
    raw_marker = _read_string(item, "raw_marker", label)
    marker = _read_string(item, "marker", label)
    coverage_kind = _read_coverage_kind(item, label)
    matched_rule_ids = tuple(
        _read_list_string(rule_id, f"{label}.matched_rule_ids[{rule_index}]")
        for rule_index, rule_id in enumerate(ensure_json_array(item["matched_rule_ids"], f"{label}.matched_rule_ids"))
    )
    if tuple(sorted(set(matched_rule_ids))) != matched_rule_ids:
        raise RuntimeError(f"{label}.matched_rule_ids 必须已排序且不得重复")
    possible_texts = source_texts.get((location_path, line_number))
    if possible_texts is None:
        raise RuntimeError(f"{label} 返回了未请求的位置")
    if end_index <= start_index or not any(
        end_index <= len(text) and text[start_index:end_index] == raw_marker for text in possible_texts
    ):
        raise RuntimeError(f"{label} 的字符范围与原文不一致")
    return NativePlaceholderOccurrence(
        location_path=location_path,
        line_number=line_number,
        start_index=start_index,
        end_index=end_index,
        raw_marker=raw_marker,
        marker=marker,
        coverage_kind=coverage_kind,
        matched_rule_ids=matched_rule_ids,
    )


def _read_coverage_kind(item: dict[str, JsonValue], label: str) -> ControlSequenceCoverageKind:
    value = _read_string(item, "coverage_kind", label)
    if value == "standard":
        return "standard"
    if value == "custom":
        return "custom"
    if value == "structured":
        return "structured"
    if value == "uncovered":
        return "uncovered"
    raise RuntimeError(f"{label}.coverage_kind 无效: {value}")


def _read_string(item: dict[str, JsonValue], key: str, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{label}.{key} 必须是字符串")
    return value


def _read_list_string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} 必须是字符串")
    return value


def _read_non_negative_int(
    item: dict[str, JsonValue],
    key: str,
    label: str,
    *,
    allow_zero: bool = True,
) -> int:
    value = item.get(key)
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise TypeError(f"{label}.{key} 必须是大于等于 {minimum} 的整数")
    return value


def _require_exact_keys(item: dict[str, JsonValue], expected: set[str], label: str) -> None:
    actual = set(item)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise RuntimeError(f"{label} 字段不匹配：缺少={missing}，未知={unexpected}")


__all__ = [
    "NativePlaceholderOccurrence",
    "NativePlaceholderScanText",
    "scan_native_placeholder_occurrences",
]
