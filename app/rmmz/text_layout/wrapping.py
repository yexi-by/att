"""RPG Maker 长文本包裹标点修复服务。"""

from __future__ import annotations

from app.rmmz.text_rules import TextRules

from .models import BoundaryChar, WrappingSpan
from .protected import collect_protected_spans, find_containing_span

TRANSLATED_WRAPPING_PUNCTUATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("“", "”"),
    ("‘", "’"),
    ('"', '"'),
    ("'", "'"),
    ("＂", "＂"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("〈", "〉"),
    ("（", "）"),
    ("(", ")"),
)


def normalize_translated_wrapping_punctuation(
    *,
    original_lines: list[str],
    translation_lines: list[str],
    text_rules: TextRules,
) -> list[str]:
    """把源文包裹标点被模型改写的译文修回源文标点。

    源文中的每一组保留包裹标点形成一个槽位；译文中可识别的替代引号按
    可见顺序填入这些槽位。多出的译文引号保持原样，避免删除模型正常新增
    的中文表达。
    """
    if not _has_preserved_wrapping_chars(lines=original_lines, text_rules=text_rules):
        return list(translation_lines)
    source_spans = _collect_source_wrapping_spans(
        lines=original_lines,
        text_rules=text_rules,
    )
    if not source_spans:
        return list(translation_lines)
    translated_spans = _collect_translated_wrapping_spans(
        lines=translation_lines,
        text_rules=text_rules,
    )
    if not translated_spans:
        return list(translation_lines)

    normalized_lines = list(translation_lines)
    for source_span, translated_span in zip(source_spans, translated_spans, strict=False):
        source_left, source_right = source_span.pair
        if translated_span.left.char != source_left:
            normalized_lines[translated_span.left.line_index] = _replace_char_at(
                text=normalized_lines[translated_span.left.line_index],
                index=translated_span.left.char_index,
                char=source_left,
            )
        if translated_span.right.char != source_right:
            normalized_lines[translated_span.right.line_index] = _replace_char_at(
                text=normalized_lines[translated_span.right.line_index],
                index=translated_span.right.char_index,
                char=source_right,
            )
    return normalized_lines


def find_opening_wrapping_pair(*, line: str, text_rules: TextRules) -> tuple[str, str] | None:
    """返回当前行开头命中的包裹标点配置。"""
    stripped_line = build_wrapping_check_line(line=line, text_rules=text_rules)
    for left, right in text_rules.setting.preserve_wrapping_punctuation_pairs:
        if stripped_line.startswith(left):
            return left, right
    return None


def closes_wrapping_pair(
    *,
    line: str,
    wrapping_pair: tuple[str, str],
    text_rules: TextRules,
) -> bool:
    """判断当前逻辑行是否结束了跨行包裹标点块。"""
    _, right = wrapping_pair
    stripped_line = build_wrapping_check_line(line=line, text_rules=text_rules)
    return stripped_line.endswith(right)


def build_wrapping_check_line(*, line: str, text_rules: TextRules) -> str:
    """去掉控制符后生成包裹标点状态判定用文本。"""
    return text_rules.strip_rm_control_sequences(line).strip()


def prepend_continuation_prefix(*, line: str, prefix: str) -> str:
    """给包裹标点续行补视觉缩进，避免重复添加已有空白。"""
    if not prefix or not line:
        return line
    if line.startswith(prefix):
        return line
    first_char = line[0]
    if first_char.isspace():
        return line
    return f"{prefix}{line}"


def _has_preserved_wrapping_chars(*, lines: list[str], text_rules: TextRules) -> bool:
    """快速判断源文是否可能需要包裹标点修复。"""
    wrapping_chars = {char for pair in text_rules.setting.preserve_wrapping_punctuation_pairs for char in pair}
    if not wrapping_chars:
        return False
    return any(char in line for line in lines for char in wrapping_chars)


def _collect_source_wrapping_spans(
    *,
    text_rules: TextRules,
    lines: list[str],
) -> list[WrappingSpan]:
    """按源文实际字符收集需要保留的包裹标点槽位。"""
    return _collect_wrapping_spans(
        lines=lines,
        pair_definitions=tuple(text_rules.setting.preserve_wrapping_punctuation_pairs),
        text_rules=text_rules,
        allow_mismatched_right=True,
    )


def _collect_translated_wrapping_spans(
    *,
    text_rules: TextRules,
    lines: list[str],
) -> list[WrappingSpan]:
    """收集译文中可被源文槽位接管的替代包裹标点对。"""
    source_pairs = tuple(text_rules.setting.preserve_wrapping_punctuation_pairs)
    return _collect_flexible_wrapping_spans(
        visible_chars=_collect_visible_chars(lines=lines, text_rules=text_rules),
        pair_definitions=_build_translated_wrapping_pairs(source_pairs),
    )


def _build_translated_wrapping_pairs(source_pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """生成译文中允许被源文槽位接管的包裹标点对。"""
    translated_pairs: list[tuple[str, str]] = []
    for pair in (*source_pairs, *TRANSLATED_WRAPPING_PUNCTUATION_PAIRS):
        if pair in translated_pairs:
            continue
        translated_pairs.append(pair)
    return tuple(translated_pairs)


def _collect_wrapping_spans(
    *,
    lines: list[str],
    pair_definitions: tuple[tuple[str, str], ...],
    text_rules: TextRules,
    allow_mismatched_right: bool,
) -> list[WrappingSpan]:
    """按可见字符顺序收集已配对的包裹标点。"""
    visible_chars = _collect_visible_chars(lines=lines, text_rules=text_rules)
    different_char_pairs = tuple(pair for pair in pair_definitions if pair[0] != pair[1])
    same_char_pairs = tuple(pair for pair in pair_definitions if pair[0] == pair[1])
    spans = _collect_different_char_wrapping_spans(
        visible_chars=visible_chars,
        pair_definitions=different_char_pairs,
        allow_mismatched_right=allow_mismatched_right,
    )
    spans.extend(
        _collect_same_char_wrapping_spans(
            visible_chars=visible_chars,
            pair_definitions=same_char_pairs,
        )
    )
    return sorted(
        spans,
        key=lambda span: (
            span.left.line_index,
            span.left.char_index,
            span.right.line_index,
            span.right.char_index,
        ),
    )


def _collect_different_char_wrapping_spans(
    *,
    visible_chars: list[BoundaryChar],
    pair_definitions: tuple[tuple[str, str], ...],
    allow_mismatched_right: bool,
) -> list[WrappingSpan]:
    """收集左右字符不同的包裹标点对。"""
    left_to_pair = {left: (left, right) for left, right in pair_definitions}
    right_chars = {right for _left, right in pair_definitions}
    spans: list[WrappingSpan] = []
    stack: list[tuple[BoundaryChar, tuple[str, str]]] = []
    for boundary in visible_chars:
        pair = left_to_pair.get(boundary.char)
        if pair is not None:
            stack.append((boundary, pair))
            continue
        if boundary.char not in right_chars:
            continue
        if not stack:
            continue
        left_boundary, expected_pair = stack[-1]
        if expected_pair[1] != boundary.char and not allow_mismatched_right:
            continue
        _ = stack.pop()
        source_pair = expected_pair if expected_pair[1] == boundary.char else (left_boundary.char, boundary.char)
        spans.append(WrappingSpan(left=left_boundary, right=boundary, pair=source_pair))
    return spans


def _collect_flexible_wrapping_spans(
    *,
    visible_chars: list[BoundaryChar],
    pair_definitions: tuple[tuple[str, str], ...],
) -> list[WrappingSpan]:
    """收集译文中左右边界可能来自不同标点体系的包裹槽位。"""
    left_to_pair = {left: (left, right) for left, right in pair_definitions}
    right_chars = {right for _left, right in pair_definitions}
    spans: list[WrappingSpan] = []
    stack: list[tuple[BoundaryChar, tuple[str, str]]] = []
    for boundary in visible_chars:
        if stack and boundary.char in right_chars:
            left_boundary, expected_pair = stack.pop()
            span_pair = expected_pair if expected_pair[1] == boundary.char else (left_boundary.char, boundary.char)
            spans.append(WrappingSpan(left=left_boundary, right=boundary, pair=span_pair))
            continue
        pair = left_to_pair.get(boundary.char)
        if pair is not None:
            stack.append((boundary, pair))
    return sorted(
        spans,
        key=lambda span: (
            span.left.line_index,
            span.left.char_index,
            span.right.line_index,
            span.right.char_index,
        ),
    )


def _collect_same_char_wrapping_spans(
    *,
    visible_chars: list[BoundaryChar],
    pair_definitions: tuple[tuple[str, str], ...],
) -> list[WrappingSpan]:
    """收集左右字符相同的直引号包裹对。"""
    quote_chars = {left for left, _right in pair_definitions}
    open_boundaries: dict[str, BoundaryChar] = {}
    spans: list[WrappingSpan] = []
    for boundary in visible_chars:
        if boundary.char not in quote_chars:
            continue
        open_boundary = open_boundaries.get(boundary.char)
        if open_boundary is None:
            open_boundaries[boundary.char] = boundary
            continue
        spans.append(WrappingSpan(left=open_boundary, right=boundary, pair=(boundary.char, boundary.char)))
        del open_boundaries[boundary.char]
    return spans


def _collect_visible_chars(
    *,
    lines: list[str],
    text_rules: TextRules,
) -> list[BoundaryChar]:
    """收集多行文本中不属于控制符且非空白的可见字符位置。"""
    visible_chars: list[BoundaryChar] = []
    for line_index, line in enumerate(lines):
        protected_spans = collect_protected_spans(text=line, text_rules=text_rules)
        index = 0
        while index < len(line):
            containing_span = find_containing_span(index=index, protected_spans=protected_spans)
            if containing_span is not None:
                index = containing_span.end_index
                continue
            char = line[index]
            if not char.isspace():
                visible_chars.append(
                    BoundaryChar(
                        line_index=line_index,
                        char_index=index,
                        char=char,
                    )
                )
            index += 1
    return visible_chars


def _replace_char_at(*, text: str, index: int, char: str) -> str:
    """替换文本指定下标处的单个字符。"""
    return f"{text[:index]}{char}{text[index + 1 :]}"
