"""模型新译文与复用译文共享的最终候选校验。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.game_analysis import GameAnalysisContext
from app.native_quality import collect_native_write_protocol_details
from app.plugin_source_text.extraction import parse_plugin_source_location_path
from app.rmmz.schema import TranslationItem

from .cache import TranslationCache


def validate_translation_candidate(
    *,
    item: TranslationItem,
    terminology_entries: Sequence[tuple[str, str, str]],
    write_protocol_reasons: Sequence[str],
) -> list[str]:
    """校验实际选中术语和目标位置写回协议，返回稳定问题明细。"""
    errors: list[str] = []
    original_text = "\n".join(item.original_lines)
    translated_text = "\n".join(item.translation_lines)
    for _category, source_text, translated_term in terminology_entries:
        if source_text not in original_text or translated_term in translated_text:
            continue
        errors.append(f"terminology_mismatch: 原文术语 {source_text!r} 必须使用译名 {translated_term!r}")
    errors.extend(f"write_protocol_mismatch: {reason}" for reason in write_protocol_reasons)
    return errors


@dataclass(frozen=True, slots=True)
class TranslationCandidateValidator:
    """复用单命令分析事实批量执行最终候选校验。"""

    analysis_context: GameAnalysisContext
    translation_cache: TranslationCache

    def __call__(
        self,
        items: Sequence[TranslationItem],
    ) -> dict[str, list[str]]:
        """一次 native 调用检查一个已完成批次，并补充术语与插件源码协议。"""
        if not items:
            return {}
        reasons_by_path = _native_write_protocol_reasons(
            analysis_context=self.analysis_context,
            items=list(items),
        )
        _append_plugin_source_protocol_reasons(
            analysis_context=self.analysis_context,
            items=items,
            reasons_by_path=reasons_by_path,
        )

        errors_by_path: dict[str, list[str]] = {}
        for item in items:
            scope_entry = self.analysis_context.write_target_index.get(item.location_path)
            target_reasons = list(reasons_by_path.get(item.location_path, ()))
            if scope_entry is None:
                target_reasons.append("目标位置不在当前可写文本索引中")
            elif not scope_entry.can_write_back:
                target_reasons.append(scope_entry.cannot_process_reason or "目标位置当前不可写回")
            cache_key = self.translation_cache.build_cache_key(item)
            terminology_entries = () if cache_key is None else cache_key.terminology_entries
            errors = validate_translation_candidate(
                item=item,
                terminology_entries=terminology_entries,
                write_protocol_reasons=target_reasons,
            )
            if errors:
                errors_by_path[item.location_path] = errors
        return errors_by_path


def _native_write_protocol_reasons(
    *,
    analysis_context: GameAnalysisContext,
    items: list[TranslationItem],
) -> dict[str, list[str]]:
    details = collect_native_write_protocol_details(
        game_data=analysis_context.game_data.data,
        plugins_js=[plugin for plugin in analysis_context.game_data.plugins_js],
        items=items,
    )
    reasons: dict[str, list[str]] = {}
    for detail in details:
        if not isinstance(detail, dict):
            continue
        location_path = detail.get("location_path")
        if not isinstance(location_path, str):
            continue
        message = detail.get("reason")
        if not isinstance(message, str) or not message.strip():
            message = detail.get("message")
        if not isinstance(message, str) or not message.strip():
            message = "写入协议预演失败"
        reasons.setdefault(location_path, []).append(message)
    return reasons


def _append_plugin_source_protocol_reasons(
    *,
    analysis_context: GameAnalysisContext,
    items: Sequence[TranslationItem],
    reasons_by_path: dict[str, list[str]],
) -> None:
    candidates = {
        (candidate.file_name, candidate.selector): candidate
        for candidate in analysis_context.plugin_source_scan.candidates
    }
    for item in items:
        parsed = parse_plugin_source_location_path(item.location_path)
        if parsed is None:
            continue
        candidate = candidates.get(parsed)
        if candidate is None:
            reasons_by_path.setdefault(item.location_path, []).append("插件源码 selector 已失效")
            continue
        if item.original_lines != [candidate.text]:
            reasons_by_path.setdefault(item.location_path, []).append("插件源码原文已变化")
        if len(item.translation_lines) != 1:
            reasons_by_path.setdefault(item.location_path, []).append("插件源码短文本只能写入 1 行译文")


__all__ = [
    "TranslationCandidateValidator",
    "validate_translation_candidate",
]
