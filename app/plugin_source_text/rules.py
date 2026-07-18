"""插件源码文本规则新鲜度检查。"""

from __future__ import annotations

from dataclasses import dataclass

from app.rmmz.schema import PluginSourceTextRuleRecord

from .models import PluginSourceCandidate, PluginSourceScan


@dataclass(frozen=True, slots=True)
class StalePluginSourceTextRule:
    """过期插件源码文本规则明细。"""

    file_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class PluginSourceReviewCoverage:
    """插件源码候选被外部审查结果覆盖的统计。"""

    required: bool
    translate_selector_count: int
    excluded_selector_count: int
    reviewed_selector_count: int
    active_candidate_count: int
    unreviewed_candidates: tuple[PluginSourceCandidate, ...]


def filter_fresh_plugin_source_text_rules(
    *,
    rule_records: list[PluginSourceTextRuleRecord],
    scan: PluginSourceScan,
) -> tuple[list[PluginSourceTextRuleRecord], list[StalePluginSourceTextRule]]:
    """按当前源码文件、启用状态、文件哈希和 selector 命中筛出仍有效的源码规则。"""
    if not rule_records:
        return [], []
    file_scans = {file_scan.file_name: file_scan for file_scan in scan.files}
    fresh_rules: list[PluginSourceTextRuleRecord] = []
    stale_rules: list[StalePluginSourceTextRule] = []
    for record in rule_records:
        file_scan = file_scans.get(record.file_name)
        if file_scan is None:
            stale_rules.append(
                StalePluginSourceTextRule(
                    file_name=record.file_name,
                    reason="插件源码文件不存在或不是 js/plugins 直接文件",
                )
            )
            continue
        if not file_scan.active:
            stale_rules.append(
                StalePluginSourceTextRule(
                    file_name=record.file_name,
                    reason="插件源码文件未在 plugins.js 中启用",
                )
            )
            continue
        if file_scan.file_hash != record.file_hash:
            stale_rules.append(
                StalePluginSourceTextRule(
                    file_name=record.file_name,
                    reason="插件源码文件内容已经变化，规则哈希不匹配",
                )
            )
            continue
        available_selectors = {candidate.selector for candidate in file_scan.candidates}
        record_selectors = [*record.selectors, *record.excluded_selectors]
        missing_selectors = [selector for selector in record_selectors if selector not in available_selectors]
        if missing_selectors:
            stale_rules.append(
                StalePluginSourceTextRule(
                    file_name=record.file_name,
                    reason="插件源码 selector 已无法命中当前 AST 地图",
                )
            )
            continue
        fresh_rules.append(record)
    return fresh_rules, stale_rules


def collect_plugin_source_review_coverage(
    *,
    scan: PluginSourceScan,
    rule_records: list[PluginSourceTextRuleRecord],
) -> PluginSourceReviewCoverage:
    """按 AST 地图统计插件源码候选是否已被归入翻译或排除。"""
    reviewed_selectors_by_file: dict[str, set[str]] = {}
    translate_selector_count = 0
    excluded_selector_count = 0
    for record in rule_records:
        reviewed_selectors = reviewed_selectors_by_file.setdefault(record.file_name, set())
        reviewed_selectors.update(record.selectors)
        reviewed_selectors.update(record.excluded_selectors)
        translate_selector_count += len(record.selectors)
        excluded_selector_count += len(record.excluded_selectors)

    active_candidates = tuple(candidate for candidate in scan.candidates if candidate.active)
    required = scan.risk.high_risk or bool(rule_records)
    if required:
        unreviewed_candidates = tuple(
            candidate
            for candidate in active_candidates
            if candidate.selector not in reviewed_selectors_by_file.get(candidate.file_name, set())
        )
    else:
        unreviewed_candidates = ()
    reviewed_selector_count = translate_selector_count + excluded_selector_count
    return PluginSourceReviewCoverage(
        required=required,
        translate_selector_count=translate_selector_count,
        excluded_selector_count=excluded_selector_count,
        reviewed_selector_count=reviewed_selector_count,
        active_candidate_count=len(active_candidates),
        unreviewed_candidates=unreviewed_candidates,
    )


__all__ = [
    "PluginSourceReviewCoverage",
    "StalePluginSourceTextRule",
    "collect_plugin_source_review_coverage",
    "filter_fresh_plugin_source_text_rules",
]
