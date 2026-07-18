"""自定义占位符候选扫描服务。"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from app.diagnostics import record_scan_counts
from app.native_placeholder_candidates import NativePlaceholderScanText, scan_native_placeholder_occurrences
from app.rmmz.schema import TranslationData, TranslationItem
from app.rmmz.text_rules import (
    ControlSequenceCoverageKind,
    JsonObject,
    JsonValue,
    TextRules,
)


@dataclass(frozen=True, slots=True)
class PlaceholderOccurrence:
    """单次疑似控制符在原文中的覆盖事实。"""

    location_path: str
    line_number: int
    start_index: int
    end_index: int
    raw_marker: str
    coverage_kind: ControlSequenceCoverageKind
    matched_rule_ids: tuple[str, ...]

    @property
    def source(self) -> str:
        """返回与旧报告兼容的条目行标识。"""
        return f"{self.location_path}#{self.line_number - 1}"


@dataclass(frozen=True, slots=True)
class PlaceholderCandidate:
    """按展示 marker 聚合的疑似控制符候选。"""

    marker: str
    count: int
    sources: frozenset[str]
    standard_covered: bool
    custom_covered: bool
    structured_covered: bool
    uncovered_count: int
    matched_rule_ids: frozenset[str]
    occurrences: tuple[PlaceholderOccurrence, ...]

    @property
    def covered(self) -> bool:
        """只有聚合项中每一次 occurrence 都已覆盖才返回真。"""
        return self.count > 0 and self.uncovered_count == 0

    @property
    def coverage_conflict(self) -> bool:
        """返回同一 marker 是否同时存在已覆盖和未覆盖实例。"""
        return self.uncovered_count > 0 and self.uncovered_count < self.count


@dataclass(slots=True)
class _PlaceholderCandidateAccumulator:
    """扫描期间临时归并 occurrence，完成后冻结为不可变候选。"""

    marker: str
    sources: set[str] = field(default_factory=set)
    matched_rule_ids: set[str] = field(default_factory=set)
    occurrences: list[PlaceholderOccurrence] = field(default_factory=list)

    def add_occurrence(self, occurrence: PlaceholderOccurrence) -> None:
        """归并一次 occurrence，保留每个位置的覆盖事实。"""
        self.sources.add(occurrence.source)
        self.matched_rule_ids.update(occurrence.matched_rule_ids)
        self.occurrences.append(occurrence)

    def freeze(self) -> PlaceholderCandidate:
        """返回可在验证、哈希和报告之间安全共享的事实。"""
        occurrences = tuple(self.occurrences)
        coverage_kinds = {occurrence.coverage_kind for occurrence in occurrences}
        return PlaceholderCandidate(
            marker=self.marker,
            count=len(occurrences),
            sources=frozenset(self.sources),
            standard_covered="standard" in coverage_kinds,
            custom_covered="custom" in coverage_kinds,
            structured_covered="structured" in coverage_kinds,
            uncovered_count=sum(1 for occurrence in occurrences if occurrence.coverage_kind == "uncovered"),
            matched_rule_ids=frozenset(self.matched_rule_ids),
            occurrences=occurrences,
        )


@dataclass(frozen=True, slots=True)
class PlaceholderCandidateAnalysis:
    """一次整批普通占位符扫描的不可变结果。"""

    candidates: tuple[PlaceholderCandidate, ...]

    @property
    def details(self) -> list[JsonValue]:
        """生成对外 JSON 明细，不重新扫描源文。"""
        return placeholder_candidates_to_details(self.candidates)


@dataclass(frozen=True, slots=True)
class StructuredPlaceholderCandidate:
    """单个结构化协议外壳 occurrence 的覆盖事实。"""

    location_path: str
    line_number: int
    start_index: int
    end_index: int
    raw_candidate: str
    matching_rule_ids: tuple[str, ...]

    @property
    def covered(self) -> bool:
        """返回该完整外壳是否由已验证规则精确覆盖。"""
        return bool(self.matching_rule_ids)

    def to_detail(self) -> JsonObject:
        """转换为稳定报告明细。"""
        return {
            "location_path": self.location_path,
            "line_number": self.line_number,
            "candidate": self.raw_candidate,
            "covered": self.covered,
            "matching_rules": list(self.matching_rule_ids),
        }


@dataclass(frozen=True, slots=True)
class StructuredPlaceholderCandidateAnalysis:
    """一次整批结构化外壳扫描的不可变结果。"""

    candidates: tuple[StructuredPlaceholderCandidate, ...]

    @property
    def details(self) -> list[JsonValue]:
        """生成对外 JSON 明细，不重新扫描源文。"""
        return [candidate.to_detail() for candidate in self.candidates]

    @property
    def uncovered_count(self) -> int:
        """返回未被完整结构化规则覆盖的 occurrence 数。"""
        return sum(1 for candidate in self.candidates if not candidate.covered)


STRUCTURED_SHELL_CANDIDATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<[^<>\r\n]{1,160}(?:[:：=])[^<>\r\n]{0,240}>"),
    re.compile(r"◆<[^<>\r\n]{1,160}>[^\s<>\r\n]?"),
    re.compile(r"【[^】\r\n]{1,160}[:：][^】\r\n]{0,240}】"),
)


def scan_placeholder_candidates(
    translation_data_map: dict[str, TranslationData],
    text_rules: TextRules,
) -> list[PlaceholderCandidate]:
    """扫描当前会进入正文翻译的文本中的反斜杠控制符候选。"""
    return list(analyze_placeholder_candidates(translation_data_map, text_rules).candidates)


def analyze_placeholder_candidates(
    translation_data_map: dict[str, TranslationData],
    text_rules: TextRules,
) -> PlaceholderCandidateAnalysis:
    """执行一次整批普通占位符扫描并返回可复用事实。"""
    record_scan_counts({"placeholder_candidate_scan_count": 1})
    candidates: dict[str, _PlaceholderCandidateAccumulator] = {}
    scan_texts = [
        NativePlaceholderScanText(
            location_path=location_path,
            line_number=line_number,
            text=text,
        )
        for location_path, line_number, text in _iter_scan_texts(translation_data_map)
    ]
    for native_occurrence in scan_native_placeholder_occurrences(texts=scan_texts, text_rules=text_rules):
        occurrence = PlaceholderOccurrence(
            location_path=native_occurrence.location_path,
            line_number=native_occurrence.line_number,
            start_index=native_occurrence.start_index,
            end_index=native_occurrence.end_index,
            raw_marker=native_occurrence.raw_marker,
            coverage_kind=native_occurrence.coverage_kind,
            matched_rule_ids=native_occurrence.matched_rule_ids,
        )
        candidate = candidates.get(native_occurrence.marker)
        if candidate is None:
            candidate = _PlaceholderCandidateAccumulator(marker=native_occurrence.marker)
            candidates[native_occurrence.marker] = candidate
        candidate.add_occurrence(occurrence)

    frozen_candidates = tuple(candidate.freeze() for candidate in candidates.values())
    return PlaceholderCandidateAnalysis(
        candidates=tuple(
            sorted(
                frozen_candidates,
                key=lambda item: (item.covered, item.marker.lower()),
            )
        )
    )


def analyze_structured_placeholder_candidates(
    translation_data_map: dict[str, TranslationData],
    text_rules: TextRules,
) -> StructuredPlaceholderCandidateAnalysis:
    """执行一次整批结构化外壳扫描，只认可已验证的完整命中。"""
    record_scan_counts({"structured_placeholder_candidate_scan_count": 1})
    candidates: list[StructuredPlaceholderCandidate] = []
    seen_candidates: set[tuple[str, int, int, int, str]] = set()
    for item in _iter_translation_items_from_map(translation_data_map):
        for line_index, line in enumerate(item.original_lines):
            structured_matches = text_rules.iter_structured_placeholder_matches(line)
            for start, end, raw_candidate in _iter_structured_shell_candidate_matches(line):
                key = (item.location_path, line_index, start, end, raw_candidate)
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                matching_rule_ids = tuple(
                    sorted(
                        structured_match.rule_name
                        for structured_match in structured_matches
                        if structured_match.start_index == start and structured_match.end_index == end
                    )
                )
                candidates.append(
                    StructuredPlaceholderCandidate(
                        location_path=item.location_path,
                        line_number=line_index + 1,
                        start_index=start,
                        end_index=end,
                        raw_candidate=raw_candidate,
                        matching_rule_ids=matching_rule_ids,
                    )
                )
    return StructuredPlaceholderCandidateAnalysis(candidates=tuple(candidates))


def _iter_structured_shell_candidate_matches(text: str) -> list[tuple[int, int, str]]:
    """扫描常见结构化协议外壳候选，对重叠候选只保留最完整外壳。"""
    matches: list[tuple[int, int, str]] = []
    for pattern in STRUCTURED_SHELL_CANDIDATE_PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.start(), match.end(), match.group(0)))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

    selected: list[tuple[int, int, str]] = []
    protected_until = -1
    for start, end, raw_candidate in matches:
        if start < protected_until:
            continue
        selected.append((start, end, raw_candidate))
        protected_until = end
    return selected


def placeholder_candidates_to_details(candidates: Sequence[PlaceholderCandidate]) -> list[JsonValue]:
    """把候选聚合和逐 occurrence 覆盖事实转换成报告 JSON。"""
    details: list[JsonValue] = []
    for candidate in candidates:
        sources: list[JsonValue] = list(sorted(candidate.sources))
        occurrences: list[JsonValue] = [
            {
                "location_path": occurrence.location_path,
                "line_number": occurrence.line_number,
                "start_index": occurrence.start_index,
                "end_index": occurrence.end_index,
                "raw_marker": occurrence.raw_marker,
                "coverage_kind": occurrence.coverage_kind,
                "matched_rule_ids": list(occurrence.matched_rule_ids),
            }
            for occurrence in candidate.occurrences
        ]
        item: dict[str, JsonValue] = {
            "marker": candidate.marker,
            "count": candidate.count,
            "source_count": len(candidate.sources),
            "sources": sources,
            "standard_covered": candidate.standard_covered,
            "custom_covered": candidate.custom_covered,
            "structured_covered": candidate.structured_covered,
            "uncovered_count": candidate.uncovered_count,
            "covered": candidate.covered,
            "coverage_conflict": candidate.coverage_conflict,
            "matched_rule_ids": list(sorted(candidate.matched_rule_ids)),
            "occurrences": occurrences,
        }
        if candidate.coverage_conflict:
            item["conflict_reason"] = "同一聚合标记同时存在已覆盖和未覆盖 occurrence"
        details.append(item)
    return details


def count_uncovered_candidates(candidates: Sequence[PlaceholderCandidate]) -> int:
    """统计至少包含一次未覆盖 occurrence 的聚合候选数量。"""
    return sum(1 for candidate in candidates if not candidate.covered)


def count_uncovered_occurrences(candidates: Sequence[PlaceholderCandidate]) -> int:
    """统计未被任何完整规则覆盖的 occurrence 数量。"""
    return sum(candidate.uncovered_count for candidate in candidates)


def _iter_scan_texts(
    translation_data_map: dict[str, TranslationData],
) -> Iterable[tuple[str, int, str]]:
    """遍历当前提取规则确认会进入模型正文的原文行。"""
    for translation_data in translation_data_map.values():
        for item in translation_data.translation_items:
            yield from _iter_item_scan_texts(item)


def _iter_item_scan_texts(item: TranslationItem) -> Iterable[tuple[str, int, str]]:
    """逐行返回单个正文条目的原文和精确行位置。"""
    for line_index, text in enumerate(item.original_lines):
        yield item.location_path, line_index + 1, text


def _iter_translation_items_from_map(translation_data_map: dict[str, TranslationData]) -> Iterable[TranslationItem]:
    """以稳定顺序遍历当前命令上下文中的正文条目。"""
    for translation_data in translation_data_map.values():
        yield from translation_data.translation_items


__all__: list[str] = [
    "PlaceholderCandidate",
    "PlaceholderCandidateAnalysis",
    "PlaceholderOccurrence",
    "StructuredPlaceholderCandidate",
    "StructuredPlaceholderCandidateAnalysis",
    "analyze_placeholder_candidates",
    "analyze_structured_placeholder_candidates",
    "count_uncovered_candidates",
    "count_uncovered_occurrences",
    "placeholder_candidates_to_details",
    "scan_placeholder_candidates",
]
