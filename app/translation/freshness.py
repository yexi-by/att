"""已保存译文相对当前翻译上下文的新鲜度判定。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.persistence.records import TranslationReuseContext, TranslationReuseRecord
from app.rmmz.schema import SourceResidualRuleRecord, TranslationItem
from app.rmmz.source_snapshot import SourceSnapshotFileRecord
from app.terminology import TerminologyPromptIndex
from app.text_scope import TextScopeResult

from .cache import (
    TranslationCache,
    TranslationReuseContextData,
    prepare_translation_cache_for_scope,
)


class TranslationReuseReader(Protocol):
    """读取完整上下文完全一致的历史译文候选。"""

    async def read_reusable_translations_by_context_keys(
        self,
        contexts: Sequence[TranslationReuseContext],
    ) -> list[TranslationReuseRecord]:
        """返回全部精确匹配候选，保留冲突供调用方判定。"""
        ...


@dataclass(frozen=True, slots=True)
class TranslationFreshnessResult:
    """一次当前范围计算出的译文新鲜度事实。"""

    current_items_by_path: dict[str, TranslationItem]
    current_contexts_by_path: dict[str, TranslationReuseContext]
    reusable_records: tuple[TranslationReuseRecord, ...]
    conflicted_contexts: frozenset[TranslationReuseContext]
    valid_translated_paths: frozenset[str]
    stale_current_paths: tuple[str, ...]
    valid_translated_items: tuple[TranslationItem, ...]


def translation_reuse_context_data(
    context: TranslationReuseContext,
) -> TranslationReuseContextData:
    """把持久层复用上下文转换成翻译层不可变值。"""
    return TranslationReuseContextData(
        context_key_json=context.context_key_json,
        context_key_hash=context.context_key_hash,
        source_fingerprint=context.source_fingerprint,
        rule_fingerprint=context.rule_fingerprint,
        terminology_fingerprint=context.terminology_fingerprint,
        language_fingerprint=context.language_fingerprint,
        prompt_protocol_version=context.prompt_protocol_version,
    )


def persistence_translation_reuse_context(
    context: TranslationReuseContextData,
) -> TranslationReuseContext:
    """把翻译层上下文转换成持久层冻结契约。"""
    return TranslationReuseContext(
        context_key_json=context.context_key_json,
        context_key_hash=context.context_key_hash,
        source_fingerprint=context.source_fingerprint,
        rule_fingerprint=context.rule_fingerprint,
        terminology_fingerprint=context.terminology_fingerprint,
        language_fingerprint=context.language_fingerprint,
        prompt_protocol_version=context.prompt_protocol_version,
    )


def build_translation_reuse_contexts_by_path(
    *,
    translation_cache: TranslationCache,
    items: Sequence[TranslationItem],
) -> dict[str, TranslationReuseContext]:
    """为将要成功保存的每个目标位置生成完整复用上下文。"""
    contexts: dict[str, TranslationReuseContext] = {}
    for item in items:
        context = translation_cache.build_reuse_context(item)
        if context is None:
            raise RuntimeError(f"正文缺少可持久化复用上下文: {item.location_path}")
        contexts[item.location_path] = persistence_translation_reuse_context(context)
    return contexts


def translation_record_matches_current_target(
    *,
    saved_item: TranslationItem,
    saved_context: TranslationReuseContext,
    current_items_by_path: dict[str, TranslationItem],
    current_contexts_by_path: dict[str, TranslationReuseContext],
) -> bool:
    """确认历史记录仍代表同一目标位置，而不只是跨位置候选。"""
    current_item = current_items_by_path.get(saved_item.location_path)
    return (
        current_item is not None
        and current_contexts_by_path.get(saved_item.location_path) == saved_context
        and bool(saved_item.translation_lines)
        and saved_item.item_type == current_item.item_type
        and saved_item.role == current_item.role
        and saved_item.original_lines == current_item.original_lines
        and saved_item.source_line_paths == current_item.source_line_paths
    )


async def evaluate_translation_freshness(
    *,
    reuse_reader: TranslationReuseReader,
    translation_cache: TranslationCache,
    scope: TextScopeResult,
    translated_items: Sequence[TranslationItem],
    terminology_prompt_index: TerminologyPromptIndex,
    source_language: str,
    additional_source_languages: Sequence[str],
    target_language: str,
    source_snapshot_records: Sequence[SourceSnapshotFileRecord],
    source_residual_records: Sequence[SourceResidualRuleRecord],
) -> TranslationFreshnessResult:
    """以完整上下文和全部运行指纹判定当前已保存译文是否仍有效。"""
    prepare_translation_cache_for_scope(
        translation_cache=translation_cache,
        scope=scope,
        terminology_prompt_index=terminology_prompt_index,
        source_language=source_language,
        additional_source_languages=additional_source_languages,
        target_language=target_language,
        source_snapshot_records=source_snapshot_records,
        source_residual_records=source_residual_records,
    )
    current_items = [
        item for translation_data in scope.translation_data_map.values() for item in translation_data.translation_items
    ]
    current_items_by_path = {item.location_path: item for item in current_items}
    current_contexts_by_path = build_translation_reuse_contexts_by_path(
        translation_cache=translation_cache,
        items=current_items,
    )
    reusable_records = await reuse_reader.read_reusable_translations_by_context_keys(
        list(set(current_contexts_by_path.values()))
    )

    translations_by_context: dict[TranslationReuseContext, set[tuple[str, ...]]] = {}
    for record in reusable_records:
        translations_by_context.setdefault(record.context, set()).add(tuple(record.translation_item.translation_lines))
        _ = translation_cache.remember_saved_translation(
            record.translation_item,
            translation_reuse_context_data(record.context),
        )
    conflicted_contexts = frozenset(
        context for context, translations in translations_by_context.items() if len(translations) > 1
    )
    valid_translated_paths = frozenset(
        record.translation_item.location_path
        for record in reusable_records
        if record.context not in conflicted_contexts
        and translation_record_matches_current_target(
            saved_item=record.translation_item,
            saved_context=record.context,
            current_items_by_path=current_items_by_path,
            current_contexts_by_path=current_contexts_by_path,
        )
    )
    stored_translated_paths = {item.location_path for item in translated_items}
    stale_current_paths = tuple(sorted((stored_translated_paths & set(current_items_by_path)) - valid_translated_paths))
    return TranslationFreshnessResult(
        current_items_by_path=current_items_by_path,
        current_contexts_by_path=current_contexts_by_path,
        reusable_records=tuple(reusable_records),
        conflicted_contexts=conflicted_contexts,
        valid_translated_paths=valid_translated_paths,
        stale_current_paths=stale_current_paths,
        valid_translated_items=tuple(item for item in translated_items if item.location_path in valid_translated_paths),
    )


def unavailable_translation_freshness(
    *,
    scope: TextScopeResult,
    translated_items: Sequence[TranslationItem],
) -> TranslationFreshnessResult:
    """缺少完整术语上下文时保守地令当前范围内旧译文全部失效。"""
    current_items = [
        item for translation_data in scope.translation_data_map.values() for item in translation_data.translation_items
    ]
    current_items_by_path = {item.location_path: item for item in current_items}
    stored_translated_paths = {item.location_path for item in translated_items}
    return TranslationFreshnessResult(
        current_items_by_path=current_items_by_path,
        current_contexts_by_path={},
        reusable_records=(),
        conflicted_contexts=frozenset(),
        valid_translated_paths=frozenset(),
        stale_current_paths=tuple(sorted(stored_translated_paths & set(current_items_by_path))),
        valid_translated_items=(),
    )


__all__ = [
    "TranslationFreshnessResult",
    "TranslationReuseReader",
    "build_translation_reuse_contexts_by_path",
    "evaluate_translation_freshness",
    "persistence_translation_reuse_context",
    "translation_record_matches_current_target",
    "translation_reuse_context_data",
    "unavailable_translation_freshness",
]
