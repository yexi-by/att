"""
文本规则服务模块。

本模块把 RPG Maker 标准控制符保护、自定义正则占位符、源文残留检查和提取阶段
文本正规化统一收敛到 `TextRules`。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.config.schemas import TextRulesSetting
from app.language import SourceLanguage
from app.language_profiles import language_profile
from app.rmmz.control_codes import (
    ALL_PLACEHOLDER_PATTERN,
    ControlSequenceSpan,
    CustomPlaceholderRule,
    RawControlSequenceCandidate,
    StructuredPlaceholderRule,
    format_placeholder_template,
    iter_raw_control_sequence_candidates,
    iter_standard_control_spans,
    select_non_overlapping_spans,
)
from app.rmmz.json_types import (
    JsonArray,
    JsonObject,
    JsonPrimitive,
    JsonValue,
    coerce_json_value,
    ensure_json_array,
    ensure_json_object,
    ensure_json_string_list,
)

type ControlSequenceCoverageKind = Literal["standard", "custom", "structured", "uncovered"]


@dataclass(frozen=True, slots=True)
class ControlSequenceCandidateCoverage:
    """记录单次疑似控制符在当前文本中的实际覆盖结果。"""

    candidate: RawControlSequenceCandidate
    marker: str
    coverage_kind: ControlSequenceCoverageKind
    matched_rule_ids: tuple[str, ...] = ()

    @property
    def covered(self) -> bool:
        """返回该 occurrence 是否已由完整规则覆盖。"""
        return self.coverage_kind != "uncovered"


@dataclass(frozen=True, slots=True)
class StructuredPlaceholderMatch:
    """记录一次已验证完整外壳的结构化规则命中。"""

    start_index: int
    end_index: int
    rule_name: str
    translatable_range: tuple[int, int]
    protected_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class TextRules:
    """运行时文本规则集合。"""

    setting: TextRulesSetting
    custom_placeholder_rules: tuple[CustomPlaceholderRule, ...]
    structured_placeholder_rules: tuple[StructuredPlaceholderRule, ...]
    placeholder_token_pattern: re.Pattern[str]
    source_text_required_pattern: re.Pattern[str]
    source_detection_patterns: tuple[tuple[SourceLanguage, re.Pattern[str]], ...]
    custom_source_detection_pattern: re.Pattern[str] | None
    source_residual_segment_pattern: re.Pattern[str]
    line_width_count_pattern: re.Pattern[str]
    residual_escape_sequence_pattern: re.Pattern[str]

    @classmethod
    def from_setting(
        cls,
        setting: TextRulesSetting,
        custom_placeholder_rules: tuple[CustomPlaceholderRule, ...] = (),
        structured_placeholder_rules: tuple[StructuredPlaceholderRule, ...] = (),
    ) -> "TextRules":
        """根据配置构建并预编译全部正则规则。"""
        source_languages: tuple[SourceLanguage, ...] = (
            setting.source_language,
            *setting.additional_source_languages,
        )
        builtin_pattern_text = "|".join(
            f"(?:{language_profile(language).source_text_required_pattern})" for language in source_languages
        )
        single_language_pattern_text = language_profile(setting.source_language).source_text_required_pattern
        custom_source_detection_pattern = (
            None
            if setting.source_text_required_pattern in {builtin_pattern_text, single_language_pattern_text}
            else re.compile(setting.source_text_required_pattern)
        )
        detection_patterns: tuple[tuple[SourceLanguage, re.Pattern[str]], ...] = tuple(
            (
                language,
                re.compile(language_profile(language).source_text_required_pattern),
            )
            for language in source_languages
        )
        return cls(
            setting=setting,
            custom_placeholder_rules=custom_placeholder_rules,
            structured_placeholder_rules=structured_placeholder_rules,
            placeholder_token_pattern=ALL_PLACEHOLDER_PATTERN,
            source_text_required_pattern=re.compile(setting.source_text_required_pattern),
            source_detection_patterns=detection_patterns,
            custom_source_detection_pattern=custom_source_detection_pattern,
            source_residual_segment_pattern=re.compile(setting.source_residual_segment_pattern),
            line_width_count_pattern=re.compile(setting.line_width_count_pattern),
            residual_escape_sequence_pattern=re.compile(setting.residual_escape_sequence_pattern),
        )

    def normalize_extraction_text(self, text: str) -> str:
        """按配置清理提取阶段的包裹标点并去除首尾空白。"""
        normalized_text = text.strip()
        for left, right in self.setting.strip_wrapping_punctuation_pairs:
            if normalized_text.startswith(left) and normalized_text.endswith(right):
                normalized_text = normalized_text[len(left) : len(normalized_text) - len(right)]
        return normalized_text.strip()

    def normalize_translation_lines(self, lines: list[str]) -> list[str]:
        """清理模型或人工译文行的意外首尾空白，保留行内空白。"""
        return [line.strip() for line in lines]

    def replace_rm_control_sequences(
        self,
        text: str,
        replacer: Callable[[ControlSequenceSpan], str],
    ) -> str:
        """按顺序替换文本中的 RPG Maker 控制符。"""
        spans = self.iter_control_sequence_spans(text)
        if not spans:
            return text

        parts: list[str] = []
        last_end = 0
        for span in spans:
            parts.append(text[last_end : span.start_index])
            parts.append(replacer(span))
            last_end = span.end_index
        parts.append(text[last_end:])
        return "".join(parts)

    def strip_rm_control_sequences(self, text: str) -> str:
        """从文本中剥离 RPG Maker 控制符。"""
        return self.replace_rm_control_sequences(text, lambda _span: "")

    def iter_control_sequence_spans(self, text: str) -> list[ControlSequenceSpan]:
        """顺序扫描一行文本，识别标准控制符和自定义保护片段。"""
        return self._scan_control_sequences(text).spans

    def iter_control_sequence_candidate_coverages(
        self,
        text: str,
    ) -> list[ControlSequenceCandidateCoverage]:
        """逐 occurrence 判断疑似控制符由哪类规则完整覆盖。"""
        scan_result = self._scan_control_sequences(text)
        return [
            _classify_control_sequence_candidate(
                candidate=candidate,
                scan_result=scan_result,
            )
            for candidate in iter_raw_control_sequence_candidates(text)
        ]

    def iter_structured_placeholder_matches(self, text: str) -> list[StructuredPlaceholderMatch]:
        """返回已验证命名分组和完整外壳的结构化命中。"""
        return self._scan_control_sequences(text).structured_matches

    def _scan_control_sequences(self, text: str) -> "_ControlSequenceScanResult":
        """一次扫描产生替换、候选覆盖和结构化覆盖共用的事实。"""
        standard_spans = _filter_standard_prefix_conflicts(
            text=text,
            spans=iter_standard_control_spans(text),
        )
        custom_result = self._iter_custom_placeholder_spans(text)
        structured_result = self._iter_structured_placeholder_spans(text)
        self._validate_structured_placeholder_conflicts(
            base_spans=[*standard_spans, *custom_result.spans],
            structured_spans=structured_result.spans,
            translatable_ranges=structured_result.translatable_ranges,
        )
        spans = [*standard_spans, *custom_result.spans]
        spans.extend(structured_result.spans)
        return _ControlSequenceScanResult(
            spans=select_non_overlapping_spans(spans),
            custom_matches=custom_result.matches,
            structured_matches=structured_result.matches,
        )

    def format_custom_placeholder(self, *, template: str, index: int) -> str:
        """按外部 JSON 模板格式化自定义占位符。"""
        return format_placeholder_template(
            template=template,
            code="",
            param="",
            index=index,
        )

    def count_line_width_chars(self, text: str) -> int:
        """按配置统计长文本切行时计入长度的字符数量。"""
        return len(self.line_width_count_pattern.findall(text))

    def should_translate_source_text(self, text: str) -> bool:
        """判断原文是否包含需要交给模型处理的源语言字符。"""
        normalized_text = self.normalize_extraction_text(text)
        if not normalized_text:
            return False
        detection_text = self.strip_rm_control_sequences(normalized_text)
        if not detection_text:
            return False
        if self.custom_source_detection_pattern is not None:
            return self.custom_source_detection_pattern.search(detection_text) is not None
        for language, pattern in self.source_detection_patterns:
            if pattern.search(detection_text) is None:
                continue
            if language == "en" and self._is_english_protocol_noise_text(detection_text):
                continue
            return True
        return False

    def should_translate_source_lines(self, lines: list[str]) -> bool:
        """判断多行原文是否至少包含一处需要翻译的源语言字符。"""
        return any(self.should_translate_source_text(line) for line in lines)

    def is_line_width_counted_char(self, char: str) -> bool:
        """判断单个字符是否计入长文本切行长度。"""
        return self.line_width_count_pattern.fullmatch(char) is not None

    def collect_placeholder_tokens(self, lines: list[str]) -> set[str]:
        """收集文本行中的翻译占位符集合。"""
        placeholders: set[str] = set()
        for line in lines:
            placeholders.update(self.placeholder_token_pattern.findall(line))
        return placeholders

    def collect_unprotected_control_sequences(self, lines: list[str]) -> dict[str, int]:
        """统计未被标准、自定义或结构化规则覆盖的疑似控制符。"""
        counts: dict[str, int] = {}
        for line in lines:
            for candidate in self.iter_unprotected_control_sequence_candidates(line):
                counts[candidate.original] = counts.get(candidate.original, 0) + 1
        return counts

    def iter_unprotected_control_sequence_candidates(
        self,
        text: str,
    ) -> list[RawControlSequenceCandidate]:
        """找出一行文本中仍裸露的反斜杠控制符候选。"""
        return [
            coverage.candidate
            for coverage in self.iter_control_sequence_candidate_coverages(text)
            if not coverage.covered
        ]

    def _iter_custom_placeholder_spans(self, text: str) -> "_CustomPlaceholderScanResult":
        """扫描外部 JSON 中定义的自定义占位符规则。"""
        spans: list[ControlSequenceSpan] = []
        matches: list[_RuleRange] = []
        for rule in self.custom_placeholder_rules:
            for match in rule.pattern.finditer(text):
                matches.append(
                    _RuleRange(
                        start=match.start(),
                        end=match.end(),
                        rule_id=rule.pattern_text,
                    )
                )
                spans.append(
                    ControlSequenceSpan(
                        start_index=match.start(),
                        end_index=match.end(),
                        original=match.group(0),
                        source="custom",
                        placeholder=None,
                        custom_template=rule.placeholder_template,
                        priority=1,
                    )
                )
        return _CustomPlaceholderScanResult(spans=spans, matches=matches)

    def _iter_structured_placeholder_spans(self, text: str) -> "_StructuredPlaceholderScanResult":
        """扫描外部 JSON 中定义的结构化占位符规则。"""
        spans: list[ControlSequenceSpan] = []
        translatable_ranges: list[_ProtectedRange] = []
        structured_matches: list[StructuredPlaceholderMatch] = []
        for rule in self.structured_placeholder_rules:
            for match in rule.pattern.finditer(text):
                translatable_range = _match_group_range(
                    match=match,
                    group_name=rule.translatable_group,
                    rule_name=rule.rule_name,
                )
                match_key = f"structured:{rule.rule_name}:{match.start()}:{match.end()}:{match.group(0)}"
                group_ranges: list[_ProtectedRange] = []
                group_spans: list[ControlSequenceSpan] = []
                for group_name, placeholder_template in rule.protected_groups.items():
                    protected_range = _match_group_range(
                        match=match,
                        group_name=group_name,
                        rule_name=rule.rule_name,
                    )
                    if protected_range.start == protected_range.end:
                        raise ValueError(f"结构化占位符规则 {rule.rule_name} 的保护分组 {group_name} 命中了空文本")
                    if _ranges_overlap(protected_range, translatable_range):
                        raise ValueError(
                            f"结构化占位符规则 {rule.rule_name} 的保护分组 {group_name} 覆盖了可翻译文本分组"
                        )
                    for existing_range in group_ranges:
                        if _ranges_overlap(protected_range, existing_range):
                            raise ValueError(f"结构化占位符规则 {rule.rule_name} 的保护分组互相重叠")
                    group_ranges.append(protected_range)
                    group_spans.append(
                        ControlSequenceSpan(
                            start_index=protected_range.start,
                            end_index=protected_range.end,
                            original=text[protected_range.start : protected_range.end],
                            source="structured",
                            placeholder=None,
                            custom_template=placeholder_template,
                            priority=2,
                            custom_index_key=match_key,
                        )
                    )
                _validate_structured_match_shape(
                    rule_name=rule.rule_name,
                    match_range=_ProtectedRange(start=match.start(), end=match.end()),
                    translatable_range=translatable_range,
                    protected_ranges=group_ranges,
                )
                translatable_ranges.append(translatable_range)
                spans.extend(group_spans)
                structured_matches.append(
                    StructuredPlaceholderMatch(
                        start_index=match.start(),
                        end_index=match.end(),
                        rule_name=rule.rule_name,
                        translatable_range=(translatable_range.start, translatable_range.end),
                        protected_ranges=tuple(
                            (protected_range.start, protected_range.end) for protected_range in group_ranges
                        ),
                    )
                )
        return _StructuredPlaceholderScanResult(
            spans=spans,
            translatable_ranges=translatable_ranges,
            matches=structured_matches,
        )

    def _validate_structured_placeholder_conflicts(
        self,
        *,
        base_spans: list[ControlSequenceSpan],
        structured_spans: list[ControlSequenceSpan],
        translatable_ranges: list["_ProtectedRange"],
    ) -> None:
        """校验结构化规则与普通保护规则没有抢占同一段文本。"""
        for structured_span in structured_spans:
            structured_range = _ProtectedRange(
                start=structured_span.start_index,
                end=structured_span.end_index,
            )
            for base_span in base_spans:
                base_range = _ProtectedRange(start=base_span.start_index, end=base_span.end_index)
                if _ranges_overlap(structured_range, base_range):
                    raise ValueError(
                        f"结构化占位符保护片段与已有控制符规则重叠: {structured_span.original} / {base_span.original}"
                    )
        for index, left_span in enumerate(structured_spans):
            left_range = _ProtectedRange(start=left_span.start_index, end=left_span.end_index)
            for right_span in structured_spans[index + 1 :]:
                right_range = _ProtectedRange(start=right_span.start_index, end=right_span.end_index)
                if _ranges_overlap(left_range, right_range):
                    raise ValueError(f"结构化占位符保护片段互相重叠: {left_span.original} / {right_span.original}")
        for translatable_range in translatable_ranges:
            for span in [*base_spans, *structured_spans]:
                span_range = _ProtectedRange(start=span.start_index, end=span.end_index)
                if _ranges_overlap(translatable_range, span_range):
                    if span.source == "standard":
                        continue
                    raise ValueError(f"结构化占位符可翻译文本分组被保护规则覆盖: {span.original}")

    def check_source_residual(
        self,
        translation_lines: list[str],
        *,
        allowed_terms: Sequence[str] = (),
    ) -> None:
        """检查译文中是否残留当前源语言文本。"""
        allowed_chars = set(self.setting.source_residual_allowed_chars)
        allowed_tail_chars = set(self.setting.source_residual_allowed_tail_chars)
        masked_lines = self.mask_source_residual_terms(
            translation_lines,
            [*allowed_terms, *self.setting.allowed_source_residual_terms],
        )
        for index, line in enumerate(translation_lines, start=1):
            line = masked_lines[index - 1]
            cleaned_line = self._strip_non_content_for_residual(line)
            segments = [match.group(0) for match in self.source_residual_segment_pattern.finditer(cleaned_line)]
            if not segments:
                continue

            has_non_source_content = self._has_non_source_content(cleaned_line)
            real_residual_segments: list[str] = []
            for segment in segments:
                filtered_segment = [char for char in segment if char not in allowed_chars]
                if not filtered_segment:
                    if not has_non_source_content:
                        real_residual_segments.append(segment)
                    continue
                if has_non_source_content and all(char in allowed_tail_chars for char in filtered_segment):
                    continue
                real_residual_segments.append(segment)

            if real_residual_segments:
                raise ValueError(
                    f"发现{self.setting.source_residual_label}残留(第 {index} 行): {real_residual_segments}"
                )

    def _strip_non_content_for_residual(self, text: str) -> str:
        """在残留校验前剥离控制符和占位符噪音。"""
        cleaned_text = self.strip_rm_control_sequences(text)
        cleaned_text = self.placeholder_token_pattern.sub("", cleaned_text)
        return self.residual_escape_sequence_pattern.sub(" ", cleaned_text)

    def _has_non_source_content(self, text: str) -> bool:
        """判断残留检查文本中是否存在源语言片段之外的正文内容。"""
        text_without_source = self.source_residual_segment_pattern.sub("", text)
        return any(char.isalnum() for char in text_without_source)

    def mask_source_residual_terms(
        self,
        lines: list[str],
        allowed_terms: Sequence[str],
    ) -> list[str]:
        """遮蔽允许保留的源语言片段，供源文残留检测复用。"""
        allowed_terms = [term for term in allowed_terms if term]
        if not allowed_terms:
            return list(lines)
        sorted_terms = sorted(allowed_terms, key=len, reverse=True)
        masked_lines: list[str] = []
        for line in lines:
            masked_line = line
            for term in sorted_terms:
                if self.setting.source_residual_terms_ignore_case:
                    masked_line = re.sub(
                        rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                        " ",
                        masked_line,
                        flags=re.IGNORECASE,
                    )
                else:
                    masked_line = masked_line.replace(term, " ")
            masked_lines.append(masked_line)
        return masked_lines

    def _is_english_protocol_noise_text(self, text: str) -> bool:
        """排除英文游戏中常见的资源路径、脚本片段和机器协议值。"""
        stripped_text = self.strip_rm_control_sequences(text).strip()
        if not stripped_text:
            return True
        lowered_text = stripped_text.lower()
        if lowered_text in {
            "true",
            "false",
            "null",
            "undefined",
            "gamefont",
        }:
            return True
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", stripped_text):
            return True
        if re.search(r"(?:^|[\\/])(?:img|audio|fonts|icon|js|data)[\\/]", lowered_text):
            return True
        if re.search(
            r"\.(?:png|jpe?g|webp|gif|ogg|m4a|mp3|wav|webm|json|js|css|html|ttf|otf|woff2?|rpgmvp|rpgmvo|rpgmvm)$",
            lowered_text,
        ):
            return True
        if self._looks_like_english_script_punctuation(stripped_text):
            return True
        if re.search(r"\bthis\s*(?:\.[A-Za-z_$]|\[)", stripped_text, flags=re.IGNORECASE):
            return True
        if re.search(r"\b(?:console|math)\s*\.", stripped_text, flags=re.IGNORECASE):
            return True
        if re.search(r"\b(?:var|let|const)\s+[A-Za-z_$][A-Za-z0-9_$]*\s*=", stripped_text):
            return True
        if re.search(r"\bfunction(?:\s+[A-Za-z_$][A-Za-z0-9_$]*)?\s*\(", stripped_text):
            return True
        if re.search(r"\breturn\b.*(?:[;=<>+\-*/]|\b(?:true|false|null|undefined)\b)", stripped_text):
            return True
        if re.search(r"\b[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+\s*\(", stripped_text):
            return True
        if re.search(r"[+\-*/<>=]=?|&&|\|\|", stripped_text) and len(re.findall(r"[A-Za-z]{2,}", stripped_text)) < 2:
            return True
        if re.search(r"[/\\]", stripped_text) and re.fullmatch(r"[A-Za-z0-9_./\\:-]+", stripped_text):
            return True
        if re.fullmatch(r"[a-z][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*", stripped_text):
            return True
        return False

    def _looks_like_english_script_punctuation(self, text: str) -> bool:
        """只在符号呈现明确脚本结构时排除，避免误伤自然英文说明。"""
        if re.search(r"\$\{[^}]+\}", text):
            return True
        if re.search(r"\$[A-Za-z_$][A-Za-z0-9_$]*(?:\s*(?:\.|\[|\())", text):
            return True
        if re.search(
            r"(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>\s*(?:[{(]|[A-Za-z_$][A-Za-z0-9_$]*\s*[+*/<>=])",
            text,
        ):
            return True
        if re.search(
            r"\{[^{}]*(?:\b(?:var|let|const|return|function|if|for|while)\b|[A-Za-z_$][A-Za-z0-9_$]*\s*:|[A-Za-z_$][A-Za-z0-9_$]*\s*=|;)[^{}]*\}",
            text,
        ):
            return True
        if re.search(
            r"(?:\b(?:return|var|let|const|throw|break|continue)\b|[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*|\[[^\]]+\])*\s*(?:[-+*/]?=)|\b[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+\s*\([^)]*\))[^.;!?]*;",
            text,
        ):
            return True
        return False


_DEFAULT_TEXT_RULES = TextRules.from_setting(TextRulesSetting())


def get_default_text_rules() -> TextRules:
    """返回配置缺省值构建的文本规则。"""
    return _DEFAULT_TEXT_RULES


def _filter_standard_prefix_conflicts(
    *,
    text: str,
    spans: list[ControlSequenceSpan],
) -> list[ControlSequenceSpan]:
    """移除只覆盖更长疑似控制符前缀的标准片段。"""
    candidates = iter_raw_control_sequence_candidates(text)
    filtered_spans: list[ControlSequenceSpan] = []
    for span in spans:
        if span.source != "standard":
            filtered_spans.append(span)
            continue
        if _is_standard_prefix_of_longer_candidate(span, candidates):
            continue
        filtered_spans.append(span)
    return filtered_spans


def _is_standard_prefix_of_longer_candidate(
    span: ControlSequenceSpan,
    candidates: list[RawControlSequenceCandidate],
) -> bool:
    """判断标准片段是否只是某个更长候选的前缀。"""
    return any(
        candidate.start_index == span.start_index and candidate.end_index > span.end_index for candidate in candidates
    )


def _classify_control_sequence_candidate(
    *,
    candidate: RawControlSequenceCandidate,
    scan_result: "_ControlSequenceScanResult",
) -> ControlSequenceCandidateCoverage:
    """依据实际选中保护片段和完整外壳命中归类单次候选。"""
    custom_spans = [
        span for span in scan_result.spans if span.source == "custom" and _span_contains_candidate(span, candidate)
    ]
    if custom_spans:
        covering_span = custom_spans[0]
        matching_rule_ids = tuple(
            sorted(
                {
                    match.rule_id
                    for match in scan_result.custom_matches
                    if match.start <= candidate.start_index and match.end >= candidate.end_index
                }
            )
        )
        return ControlSequenceCandidateCoverage(
            candidate=candidate,
            marker=covering_span.original,
            coverage_kind="custom",
            matched_rule_ids=matching_rule_ids,
        )

    standard_spans = [
        span for span in scan_result.spans if span.source == "standard" and _span_contains_candidate(span, candidate)
    ]
    if standard_spans:
        return ControlSequenceCandidateCoverage(
            candidate=candidate,
            marker=standard_spans[0].original,
            coverage_kind="standard",
            matched_rule_ids=("standard",),
        )

    structured_matches = [
        match
        for match in scan_result.structured_matches
        if match.start_index == candidate.start_index and match.end_index == candidate.end_index
    ]
    if structured_matches:
        return ControlSequenceCandidateCoverage(
            candidate=candidate,
            marker=candidate.original,
            coverage_kind="structured",
            matched_rule_ids=tuple(sorted({match.rule_name for match in structured_matches})),
        )

    return ControlSequenceCandidateCoverage(
        candidate=candidate,
        marker=candidate.original,
        coverage_kind="uncovered",
    )


def _span_contains_candidate(
    span: ControlSequenceSpan,
    candidate: RawControlSequenceCandidate,
) -> bool:
    """只有完整包含候选的保护片段才能证明该 occurrence 已覆盖。"""
    return span.start_index <= candidate.start_index and span.end_index >= candidate.end_index


@dataclass(frozen=True, slots=True)
class _ProtectedRange:
    """记录单个受保护或可翻译文本范围。"""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _RuleRange:
    """记录自定义规则单次命中的范围和稳定标识。"""

    start: int
    end: int
    rule_id: str


@dataclass(frozen=True, slots=True)
class _CustomPlaceholderScanResult:
    """自定义占位符扫描结果。"""

    spans: list[ControlSequenceSpan]
    matches: list[_RuleRange]


@dataclass(frozen=True, slots=True)
class _StructuredPlaceholderScanResult:
    """结构化占位符扫描结果。"""

    spans: list[ControlSequenceSpan]
    translatable_ranges: list[_ProtectedRange]
    matches: list[StructuredPlaceholderMatch]


@dataclass(frozen=True, slots=True)
class _ControlSequenceScanResult:
    """占位符替换与候选覆盖共用的单行扫描结果。"""

    spans: list[ControlSequenceSpan]
    custom_matches: list[_RuleRange]
    structured_matches: list[StructuredPlaceholderMatch]


def _validate_structured_match_shape(
    *,
    rule_name: str,
    match_range: _ProtectedRange,
    translatable_range: _ProtectedRange,
    protected_ranges: list[_ProtectedRange],
) -> None:
    """确保 paired_shell 的命名分组完整、连续地覆盖本次正则命中。"""
    has_opening_shell = any(protected_range.end <= translatable_range.start for protected_range in protected_ranges)
    has_closing_shell = any(protected_range.start >= translatable_range.end for protected_range in protected_ranges)
    if not has_opening_shell or not has_closing_shell:
        raise ValueError(f"结构化占位符规则 {rule_name} 的保护分组必须成对包围可翻译分组")

    named_ranges = sorted(
        [translatable_range, *protected_ranges],
        key=lambda item: (item.start, item.end),
    )
    if named_ranges[0].start != match_range.start or named_ranges[-1].end != match_range.end:
        raise ValueError(f"结构化占位符规则 {rule_name} 的命名分组没有完整覆盖外壳命中")
    for left_range, right_range in zip(named_ranges, named_ranges[1:]):
        if left_range.end != right_range.start:
            raise ValueError(f"结构化占位符规则 {rule_name} 的命名分组没有连续覆盖外壳命中")


def _match_group_range(
    *,
    match: re.Match[str],
    group_name: str,
    rule_name: str,
) -> _ProtectedRange:
    """读取命名分组范围并把未命中情况转成业务错误。"""
    try:
        start, end = match.span(group_name)
    except IndexError as error:
        raise ValueError(f"结构化占位符规则 {rule_name} 缺少命名分组: {group_name}") from error
    if start < 0 or end < 0:
        raise ValueError(f"结构化占位符规则 {rule_name} 的命名分组未命中: {group_name}")
    return _ProtectedRange(start=start, end=end)


def _ranges_overlap(left: _ProtectedRange, right: _ProtectedRange) -> bool:
    """判断两个半开范围是否重叠。"""
    return left.start < right.end and left.end > right.start


__all__: list[str] = [
    "ControlSequenceCandidateCoverage",
    "ControlSequenceCoverageKind",
    "ControlSequenceSpan",
    "CustomPlaceholderRule",
    "JsonArray",
    "JsonObject",
    "JsonPrimitive",
    "JsonValue",
    "StructuredPlaceholderRule",
    "StructuredPlaceholderMatch",
    "TextRules",
    "coerce_json_value",
    "ensure_json_array",
    "ensure_json_object",
    "ensure_json_string_list",
    "get_default_text_rules",
]
