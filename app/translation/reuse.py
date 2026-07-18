"""上下文安全译文复用与目标位置复验。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.rmmz.schema import TranslationData, TranslationErrorItem, TranslationItem
from app.rmmz.text_rules import TextRules
from app.source_residual import SourceResidualRuleSet

from .batch import TranslationBatch, bind_translation_items
from .cache import TranslationCache
from .verify import (
    TranslationBatchVerification,
    ValidateTranslationCandidates,
    verify_translation_batch_result,
)


@dataclass(frozen=True, slots=True)
class SavedTranslationReuseResult:
    """跨轮复用筛选后的待请求范围和已复验译文。"""

    pending_translation_data_map: dict[str, TranslationData]
    reused_items: list[TranslationItem]
    reused_count: int
    conflict_count: int
    rejected_count: int


@dataclass(frozen=True, slots=True)
class CurrentRunReuseResult:
    """同轮代表译文向重复目标展开后的结果。"""

    right_items: list[TranslationItem]
    error_items: list[TranslationErrorItem]
    retranslate_items: list[TranslationItem]
    reused_count: int
    rejected_count: int


def collect_saved_translation_reuse(
    *,
    translation_data_map: dict[str, TranslationData],
    translation_cache: TranslationCache,
    text_rules: TextRules,
    source_residual_rule_set: SourceResidualRuleSet | None,
    validate_candidates: ValidateTranslationCandidates | None = None,
) -> SavedTranslationReuseResult:
    """仅复用唯一上下文键下唯一且能在目标位置重新通过检查的历史译文。"""
    reusable_lines_by_path: dict[str, list[str]] = {}
    conflict_count = 0

    for translation_data in translation_data_map.values():
        for item in translation_data.translation_items:
            translation_lines, conflicted = translation_cache.reusable_saved_translation(item)
            if conflicted:
                conflict_count += 1
                continue
            if translation_lines is not None:
                reusable_lines_by_path[item.location_path] = translation_lines

    verification = verify_reused_translations(
        candidates=[
            (item, reusable_lines_by_path[item.location_path])
            for translation_data in translation_data_map.values()
            for item in translation_data.translation_items
            if item.location_path in reusable_lines_by_path
        ],
        text_rules=text_rules,
        source_residual_rule_set=source_residual_rule_set,
        validate_candidates=validate_candidates,
    )
    reused_by_path = {item.location_path: item for item in verification.right_items}
    rejected_paths = {item.location_path for item in verification.error_items}
    pending_map: dict[str, TranslationData] = {}
    reused_items: list[TranslationItem] = []
    for rule_source, translation_data in translation_data_map.items():
        pending_items: list[TranslationItem] = []
        for item in translation_data.translation_items:
            reused_item = reused_by_path.get(item.location_path)
            if reused_item is None:
                pending_items.append(item)
            else:
                reused_items.append(reused_item)

        if pending_items:
            pending_map[rule_source] = TranslationData(
                display_name=translation_data.display_name,
                translation_items=pending_items,
            )

    return SavedTranslationReuseResult(
        pending_translation_data_map=pending_map,
        reused_items=reused_items,
        reused_count=len(reused_items),
        conflict_count=conflict_count,
        rejected_count=len(rejected_paths),
    )


def expand_current_run_reuse(
    *,
    right_items: list[TranslationItem],
    error_items: list[TranslationErrorItem],
    translation_cache: TranslationCache,
    text_rules: TextRules,
    source_residual_rule_set: SourceResidualRuleSet | None,
    validate_candidates: ValidateTranslationCandidates | None = None,
) -> CurrentRunReuseResult:
    """把同轮代表结果展开到重复目标，并在每个目标位置重新执行全部检查。"""
    expanded_right_items: list[TranslationItem] = []
    expanded_error_items: list[TranslationErrorItem] = []
    retranslate_items: list[TranslationItem] = []
    reused_count = 0
    rejected_count = 0

    duplicate_groups = [(item, translation_cache.pop_duplicate_items(item)) for item in right_items]
    duplicate_verification = verify_reused_translations(
        candidates=[
            (duplicate_item, item.translation_lines)
            for item, duplicate_items in duplicate_groups
            for duplicate_item in duplicate_items
        ],
        text_rules=text_rules,
        source_residual_rule_set=source_residual_rule_set,
        validate_candidates=validate_candidates,
    )
    reused_duplicates_by_path = {item.location_path: item for item in duplicate_verification.right_items}

    for item, duplicate_items in duplicate_groups:
        expanded_right_items.append(item)
        for duplicate_item in duplicate_items:
            reused_item = reused_duplicates_by_path.get(duplicate_item.location_path)
            if reused_item is not None:
                expanded_right_items.append(reused_item)
                reused_count += 1
            else:
                retranslate_items.append(duplicate_item)
                rejected_count += 1

    for error_item in error_items:
        expanded_error_items.append(error_item)
        deferred_items = translation_cache.pop_duplicate_items_by_location(error_item.location_path)
        retranslate_items.extend(deferred_items)
        rejected_count += len(deferred_items)

    return CurrentRunReuseResult(
        right_items=expanded_right_items,
        error_items=expanded_error_items,
        retranslate_items=retranslate_items,
        reused_count=reused_count,
        rejected_count=rejected_count,
    )


def verify_reused_translation(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
    source_residual_rule_set: SourceResidualRuleSet | None,
    validate_candidates: ValidateTranslationCandidates | None = None,
) -> TranslationBatchVerification:
    """使用与模型返回完全相同的协议和质量链复验一条复用译文。"""
    return verify_reused_translations(
        candidates=[(item, translation_lines)],
        text_rules=text_rules,
        source_residual_rule_set=source_residual_rule_set,
        validate_candidates=validate_candidates,
    )


def verify_reused_translations(
    *,
    candidates: Sequence[tuple[TranslationItem, list[str]]],
    text_rules: TextRules,
    source_residual_rule_set: SourceResidualRuleSet | None,
    validate_candidates: ValidateTranslationCandidates | None = None,
) -> TranslationBatchVerification:
    """批量复验多个目标位置，使 native 写回协议检查每组只执行一次。"""
    if not candidates:
        return TranslationBatchVerification(right_items=[], error_items=[])
    candidate_items = [item.model_copy(deep=True) for item, _translation_lines in candidates]
    for candidate_item in candidate_items:
        candidate_item.build_placeholders(text_rules)
    bindings = bind_translation_items(candidate_items)
    batch = TranslationBatch(
        bindings=bindings,
        messages=[],
        estimated_tokens=0,
        token_limit=0,
    )
    model_response = json.dumps(
        [
            {"id": binding.request_id, "translation_lines": translation_lines}
            for binding, (_item, translation_lines) in zip(
                bindings,
                candidates,
                strict=True,
            )
        ],
        ensure_ascii=False,
    )
    return verify_translation_batch_result(
        ai_result=model_response,
        batch=batch,
        text_rules=text_rules,
        source_residual_rule_set=source_residual_rule_set,
        validate_candidates=validate_candidates,
    )


__all__: list[str] = [
    "CurrentRunReuseResult",
    "SavedTranslationReuseResult",
    "collect_saved_translation_reuse",
    "expand_current_run_reuse",
    "verify_reused_translation",
    "verify_reused_translations",
]
