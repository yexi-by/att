"""上下文安全跨轮译文复用测试。"""

from collections.abc import Sequence

from app.application.handler import translation_record_matches_current_target
from app.persistence.records import TranslationReuseContext
from app.rmmz.schema import TranslationData, TranslationItem
from app.rmmz.text_rules import get_default_text_rules
from app.translation.cache import TranslationCache
from app.translation.reuse import (
    collect_saved_translation_reuse,
    expand_current_run_reuse,
)


def _build_repeated_context() -> tuple[dict[str, TranslationData], list[TranslationItem]]:
    texts = ["前一", "前二", "こんにちは", "前一", "前二", "こんにちは", "前一", "前二", "こんにちは", "前一", "前二"]
    items = [
        TranslationItem(
            location_path=f"Map001.json/1/{index}",
            item_type="short_text",
            original_lines=[text],
        )
        for index, text in enumerate(texts)
    ]
    return {"Map001.json": TranslationData(display_name="场景", translation_items=items)}, items


def test_saved_translation_reuse_requires_unique_translation_and_revalidates() -> None:
    """唯一历史译文可复用，并按目标位置走完整校验链。"""
    data_map, items = _build_repeated_context()
    cache = TranslationCache()
    cache.prepare(
        translation_data_map=data_map,
        terminology_prompt_index=None,
        source_language="ja",
        source_fingerprint="source-v1",
        rule_fingerprint="rules-v1",
    )
    saved = items[2].model_copy(deep=True)
    saved.translation_lines = ["你好"]
    context = cache.build_reuse_context(saved)
    assert context is not None
    assert cache.remember_saved_translation(saved, context)

    result = collect_saved_translation_reuse(
        translation_data_map={
            "Map001.json": TranslationData(
                display_name="场景",
                translation_items=[items[8]],
            )
        },
        translation_cache=cache,
        text_rules=get_default_text_rules(),
        source_residual_rule_set=None,
    )

    assert result.reused_count == 1
    assert result.conflict_count == 0
    assert result.rejected_count == 0
    assert result.reused_items[0].location_path == items[8].location_path
    assert result.reused_items[0].translation_lines == ["你好"]
    assert result.pending_translation_data_map == {}


def test_saved_translation_conflict_is_not_selected_silently() -> None:
    """同一上下文存在不同历史译文时保留为待请求文本。"""
    data_map, items = _build_repeated_context()
    cache = TranslationCache()
    cache.prepare(
        translation_data_map=data_map,
        terminology_prompt_index=None,
        source_language="ja",
        source_fingerprint="source-v1",
        rule_fingerprint="rules-v1",
    )
    first_saved = items[2].model_copy(deep=True)
    first_saved.translation_lines = ["你好"]
    second_saved = items[5].model_copy(deep=True)
    second_saved.translation_lines = ["您好"]
    first_context = cache.build_reuse_context(first_saved)
    second_context = cache.build_reuse_context(second_saved)
    assert first_context is not None
    assert second_context is not None
    assert cache.remember_saved_translation(first_saved, first_context)
    assert cache.remember_saved_translation(second_saved, second_context)

    result = collect_saved_translation_reuse(
        translation_data_map={
            "Map001.json": TranslationData(
                display_name="场景",
                translation_items=[items[8]],
            )
        },
        translation_cache=cache,
        text_rules=get_default_text_rules(),
        source_residual_rule_set=None,
    )

    assert result.reused_count == 0
    assert result.conflict_count == 1
    assert result.pending_translation_data_map["Map001.json"].translation_items == [items[8]]


def test_saved_reuse_batches_all_target_validation_into_one_call() -> None:
    """跨轮候选复验应复用一次批量 native 写回协议检查。"""
    data_map, items = _build_repeated_context()
    cache = TranslationCache()
    cache.prepare(
        translation_data_map=data_map,
        terminology_prompt_index=None,
        source_language="ja",
        source_fingerprint="source-v1",
        rule_fingerprint="rules-v1",
    )
    saved = items[2].model_copy(deep=True, update={"translation_lines": ["你好"]})
    context = cache.build_reuse_context(saved)
    assert context is not None
    assert cache.remember_saved_translation(saved, context)
    validated_paths: list[list[str]] = []

    def validate_candidates(candidates: Sequence[TranslationItem]) -> dict[str, list[str]]:
        validated_paths.append([item.location_path for item in candidates])
        return {}

    result = collect_saved_translation_reuse(
        translation_data_map={
            "Map001.json": TranslationData(
                display_name="场景",
                translation_items=[items[5], items[8]],
            )
        },
        translation_cache=cache,
        text_rules=get_default_text_rules(),
        source_residual_rule_set=None,
        validate_candidates=validate_candidates,
    )

    assert result.reused_count == 2
    assert validated_paths == [[items[5].location_path, items[8].location_path]]


def test_current_run_reuse_retranslates_rejected_target_without_quality_error() -> None:
    """同轮目标复验失败时整条回到模型队列，不复制代表项错误。"""
    data_map, items = _build_repeated_context()
    cache = TranslationCache()
    cache.prepare(
        translation_data_map=data_map,
        terminology_prompt_index=None,
        source_language="ja",
    )
    assert cache.remember_or_defer(items[2])
    assert not cache.remember_or_defer(items[5])
    assert not cache.remember_or_defer(items[8])
    representative = items[2].model_copy(
        deep=True,
        update={"translation_lines": ["你好"]},
    )
    validated_paths: list[list[str]] = []

    def validate_candidates(candidates: Sequence[TranslationItem]) -> dict[str, list[str]]:
        validated_paths.append([item.location_path for item in candidates])
        return {items[5].location_path: ["目标位置写回协议不匹配"]}

    result = expand_current_run_reuse(
        right_items=[representative],
        error_items=[],
        translation_cache=cache,
        text_rules=get_default_text_rules(),
        source_residual_rule_set=None,
        validate_candidates=validate_candidates,
    )

    assert [item.location_path for item in result.right_items] == [
        items[2].location_path,
        items[8].location_path,
    ]
    assert result.error_items == []
    assert [item.location_path for item in result.retranslate_items] == [items[5].location_path]
    assert result.reused_count == 1
    assert result.rejected_count == 1
    assert validated_paths == [[items[5].location_path, items[8].location_path]]


def test_current_path_requires_its_own_context_not_another_matching_key() -> None:
    """查询因另一位置命中旧 key 时，不得把当前路径的旧译文误判为有效。"""
    current = TranslationItem(
        location_path="Map001.json/events/1/name",
        item_type="short_text",
        original_lines=["同文"],
        source_line_paths=["Map001.json/events/1/name"],
    )
    saved = current.model_copy(
        deep=True,
        update={"translation_lines": ["译文"]},
    )
    old_context = TranslationReuseContext(
        context_key_json='{"owner":"old"}',
        context_key_hash="a" * 64,
        source_fingerprint="b" * 64,
        rule_fingerprint="c" * 64,
        terminology_fingerprint="d" * 64,
        language_fingerprint="e" * 64,
        prompt_protocol_version="translation-json-v2",
    )
    current_context = TranslationReuseContext(
        context_key_json='{"owner":"current"}',
        context_key_hash="f" * 64,
        source_fingerprint="b" * 64,
        rule_fingerprint="c" * 64,
        terminology_fingerprint="d" * 64,
        language_fingerprint="e" * 64,
        prompt_protocol_version="translation-json-v2",
    )

    assert not translation_record_matches_current_target(
        saved_item=saved,
        saved_context=old_context,
        current_items_by_path={current.location_path: current},
        current_contexts_by_path={current.location_path: current_context},
    )
