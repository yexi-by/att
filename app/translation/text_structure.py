"""正文译文结构一致性校验。"""

from __future__ import annotations

import re

from app.rmmz.control_codes import (
    LITERAL_LINE_BREAK_MARKER,
    LITERAL_LINE_BREAK_PLACEHOLDER,
    REAL_LINE_BREAK_PLACEHOLDER,
    ControlSequenceSpan,
    RawControlSequenceCandidate,
)
from app.rmmz.schema import TranslationItem
from app.rmmz.text_rules import TextRules

EXPLANATION_PREFIXES: tuple[str, ...] = (
    "译文：",
    "译文:",
    "翻译：",
    "翻译:",
)
EXPLANATION_MARKERS: tuple[str, ...] = ("以下是翻译",)
PROTOCOL_FIELD_PREFIXES: tuple[str, ...] = (
    "id:",
    "id：",
    '"id":',
    "source_lines:",
    "source_lines：",
    '"source_lines":',
    "translation_lines:",
    "translation_lines：",
    '"translation_lines":',
)
SUSPICIOUS_N_PREFIX_PATTERN: re.Pattern[str] = re.compile(r"^n(?=[\u3000-\u303f\u3400-\u9fff\uff00-\uffef])")


def validate_translation_text_structure(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
    translation_lines_with_placeholders: list[str] | None = None,
) -> None:
    """校验译文没有改动单字段结构，也没有混入模型输出协议文本。"""
    errors = collect_translation_text_structure_errors(
        item=item,
        translation_lines=translation_lines,
        text_rules=text_rules,
        translation_lines_with_placeholders=translation_lines_with_placeholders,
    )
    if errors:
        raise ValueError(";\n".join(errors))


def collect_translation_text_structure_errors(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
    translation_lines_with_placeholders: list[str] | None = None,
) -> list[str]:
    """收集译文结构错误，调用方决定是否作为业务失败处理。"""
    errors = _collect_artifact_errors(item=item, translation_lines=translation_lines)
    errors.extend(
        _collect_long_text_artifact_errors(
            item=item,
            translation_lines=translation_lines,
            text_rules=text_rules,
        )
    )
    if item.item_type != "short_text":
        return errors

    if len(translation_lines) != 1:
        errors.append(f"单字段文本必须只提供 1 条中文译文行，当前提供 {len(translation_lines)} 条")
        return errors

    original_real_break_count = count_real_line_breaks(item.original_lines_with_placeholders or item.original_lines)
    placeholder_lines = translation_lines_with_placeholders or translation_lines
    translation_real_break_count = count_real_line_breaks(placeholder_lines)
    if original_real_break_count != translation_real_break_count:
        errors.append(
            f"译文真实换行数量不一致（原文 {original_real_break_count} 个，译文 {translation_real_break_count} 个）"
        )

    original_literal_break_count = count_literal_line_breaks(
        item.original_lines_with_placeholders or item.original_lines
    )
    translation_literal_break_count = count_literal_line_breaks(placeholder_lines)
    if original_literal_break_count != translation_literal_break_count:
        errors.append(
            f"译文字面量换行标记数量不一致（原文 {original_literal_break_count} 个，译文 {translation_literal_break_count} 个）"
        )
    return errors


def _collect_artifact_errors(*, item: TranslationItem, translation_lines: list[str]) -> list[str]:
    """收集模型解释文本、协议字段和内部定位泄漏。"""
    errors: list[str] = []
    joined_text = "\n".join(translation_lines)
    if item.location_path and item.location_path in joined_text:
        errors.append("译文包含文本在游戏里的内部位置，不能写进游戏文件")

    for line in translation_lines:
        stripped_line = line.strip()
        lowered_line = stripped_line.lower()
        if any(stripped_line.startswith(prefix) for prefix in EXPLANATION_PREFIXES):
            errors.append("译文包含明显解释性前缀，不是可写入游戏的正文")
            break
        if any(marker in stripped_line for marker in EXPLANATION_MARKERS):
            errors.append("译文包含明显解释性说明，不是可写入游戏的正文")
            break
        if any(lowered_line.startswith(prefix) for prefix in PROTOCOL_FIELD_PREFIXES):
            errors.append("译文包含模型输出协议字段，不是可写入游戏的正文")
            break
    return errors


def _collect_long_text_artifact_errors(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
) -> list[str]:
    """收集多行正文里的异常空行和转义碎片。"""
    if item.item_type != "long_text":
        return []

    errors: list[str] = []
    errors.extend(
        _collect_unexpected_empty_line_errors(
            item=item,
            translation_lines=translation_lines,
        )
    )
    errors.extend(_collect_suspicious_n_prefix_errors(translation_lines=translation_lines))
    errors.extend(
        _collect_unexpected_escape_fragment_errors(
            translation_lines=translation_lines,
            text_rules=text_rules,
        )
    )
    return errors


def _collect_unexpected_empty_line_errors(
    *,
    item: TranslationItem,
    translation_lines: list[str],
) -> list[str]:
    """原文没有对应空行时，拒绝保存模型生成的局部空行。"""
    original_empty_count = sum(1 for line in item.original_lines if not line.strip())
    translation_empty_line_numbers = [
        line_number for line_number, line in enumerate(translation_lines, start=1) if not line.strip()
    ]
    if not translation_empty_line_numbers:
        return []
    if original_empty_count == 0:
        joined_numbers = "、".join(str(line_number) for line_number in translation_empty_line_numbers)
        return [f"原文没有空行，但译文第 {joined_numbers} 行是空行"]
    if len(translation_empty_line_numbers) > original_empty_count:
        return [
            (
                "译文空行数量超过原文空行数量"
                f"（原文 {original_empty_count} 行，译文 {len(translation_empty_line_numbers)} 行）"
            )
        ]
    return []


def _collect_suspicious_n_prefix_errors(*, translation_lines: list[str]) -> list[str]:
    """识别疑似字面量换行标记被拆坏后残留的行首 n。"""
    errors: list[str] = []
    for line_number, line in enumerate(translation_lines, start=1):
        stripped_line = line.lstrip()
        if SUSPICIOUS_N_PREFIX_PATTERN.match(stripped_line) is None:
            continue
        errors.append(f"译文第 {line_number} 行以异常 n 开头，疑似字面量换行标记 \\n 被拆坏")
    return errors


def _collect_unexpected_escape_fragment_errors(
    *,
    translation_lines: list[str],
    text_rules: TextRules,
) -> list[str]:
    """识别没有被标准控制符或疑似控制符覆盖的裸反斜杠碎片。"""
    errors: list[str] = []
    for line_number, line in enumerate(translation_lines, start=1):
        protected_spans = text_rules.iter_control_sequence_spans(line)
        raw_candidates = text_rules.iter_unprotected_control_sequence_candidates(line)
        for index, char in enumerate(line):
            if char != "\\":
                continue
            if _is_index_inside_control_span(index=index, spans=protected_spans):
                continue
            if _is_index_inside_raw_candidate(index=index, candidates=raw_candidates):
                continue
            errors.append(
                _format_unexpected_escape_fragment_error(
                    line_number=line_number,
                    line=line,
                    index=index,
                )
            )
            break
    return errors


def _is_index_inside_control_span(*, index: int, spans: list[ControlSequenceSpan]) -> bool:
    """判断字符位置是否落在已识别的控制符保护范围内。"""
    return any(span.start_index <= index < span.end_index for span in spans)


def _is_index_inside_raw_candidate(
    *,
    index: int,
    candidates: list[RawControlSequenceCandidate],
) -> bool:
    """判断字符位置是否落在未保护但可识别的疑似控制符范围内。"""
    return any(candidate.start_index <= index < candidate.end_index for candidate in candidates)


def _format_unexpected_escape_fragment_error(*, line_number: int, line: str, index: int) -> str:
    """生成异常反斜杠碎片的中文错误说明。"""
    if index == len(line) - 1:
        return f"译文第 {line_number} 行存在行尾裸反斜杠，疑似转义或换行标记被拆坏"
    fragment = line[index : index + 2]
    return f"译文第 {line_number} 行存在异常反斜杠片段: {fragment}"


def count_real_line_breaks(lines: list[str]) -> int:
    """统计字段内容中的真实换行数量。"""
    if not lines:
        return 0
    return "\n".join(lines).count("\n") + sum(line.count(REAL_LINE_BREAK_PLACEHOLDER) for line in lines)


def count_literal_line_breaks(lines: list[str]) -> int:
    """统计字段内容中的字面量换行标记数量。"""
    return sum(line.count(LITERAL_LINE_BREAK_MARKER) + line.count(LITERAL_LINE_BREAK_PLACEHOLDER) for line in lines)


__all__: list[str] = [
    "collect_translation_text_structure_errors",
    "count_literal_line_breaks",
    "count_real_line_breaks",
    "validate_translation_text_structure",
]
