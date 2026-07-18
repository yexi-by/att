"""正文翻译运行用例的状态模型与纯业务辅助函数。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from app.config.schemas import Setting
from app.llm import LLMRequestFailure
from app.persistence.repository import current_timestamp_text
from app.rmmz.schema import LlmFailureRecord, TranslationData
from app.rmmz.text_rules import TextRules
from app.terminology import TerminologyPromptIndex
from app.translation import (
    TranslationBatch,
    TranslationBatchPlan,
    TranslationCache,
    plan_translation_context_batches,
)


@dataclass(frozen=True, slots=True)
class TranslationRunLimits:
    """正文翻译单次运行控制参数。"""

    max_items: int | None = None
    max_batches: int | None = None
    time_limit_seconds: int | None = None
    stop_on_error_rate: float | None = None


@dataclass(slots=True)
class TranslationProgressState:
    """正文翻译运行期间共享的保存计数。"""

    success_count: int = 0
    quality_error_count: int = 0


def filter_pending_translation_data(
    *,
    translation_data_map: dict[str, TranslationData],
    translated_paths: set[str],
) -> dict[str, TranslationData]:
    """过滤掉数据库中已经存在译文的条目。"""
    pending_translation_data_map: dict[str, TranslationData] = {}
    for file_name, translation_data in translation_data_map.items():
        pending_items = [
            item for item in translation_data.translation_items if item.location_path not in translated_paths
        ]
        if not pending_items:
            continue
        pending_translation_data_map[file_name] = TranslationData(
            display_name=translation_data.display_name,
            translation_items=pending_items,
        )
    return pending_translation_data_map


def deduplicate_translation_data(
    *,
    translation_data_map: dict[str, TranslationData],
    translation_cache: TranslationCache,
) -> dict[str, TranslationData]:
    """按正文内容执行请求级去重。"""
    deduplicated_translation_data_map: dict[str, TranslationData] = {}
    for file_name, translation_data in translation_data_map.items():
        deduplicated_items = [
            item for item in translation_data.translation_items if translation_cache.remember_or_defer(item)
        ]
        if not deduplicated_items:
            continue
        deduplicated_translation_data_map[file_name] = TranslationData(
            display_name=translation_data.display_name,
            translation_items=deduplicated_items,
        )
    return deduplicated_translation_data_map


def limit_translation_data(
    *,
    translation_data_map: dict[str, TranslationData],
    max_items: int | None,
    translation_cache: TranslationCache | None = None,
) -> dict[str, TranslationData]:
    """按本轮上限选择完整上下文组，避免只覆盖同组的一部分位置。"""
    if max_items is None:
        return translation_data_map
    if max_items <= 0:
        raise ValueError("max_items 必须是正整数")

    if translation_cache is not None:
        return _limit_translation_data_by_context_group(
            translation_data_map=translation_data_map,
            max_items=max_items,
            translation_cache=translation_cache,
        )

    remaining_count = max_items
    limited_data_map: dict[str, TranslationData] = {}
    for file_name, translation_data in translation_data_map.items():
        if remaining_count <= 0:
            break
        selected_items = translation_data.translation_items[:remaining_count]
        if selected_items:
            limited_data_map[file_name] = TranslationData(
                display_name=translation_data.display_name,
                translation_items=selected_items,
            )
            remaining_count -= len(selected_items)
    return limited_data_map


def _limit_translation_data_by_context_group(
    *,
    translation_data_map: dict[str, TranslationData],
    max_items: int,
    translation_cache: TranslationCache,
) -> dict[str, TranslationData]:
    """仅选择能够整体放入上限的上下文组。"""
    grouped_locations: dict[object, list[str]] = {}
    ordered_group_keys: list[object] = []
    for translation_data in translation_data_map.values():
        for item in translation_data.translation_items:
            context_key = translation_cache.build_cache_key(item)
            group_key: object = context_key if context_key is not None else ("unprepared_location", item.location_path)
            if group_key not in grouped_locations:
                grouped_locations[group_key] = []
                ordered_group_keys.append(group_key)
            grouped_locations[group_key].append(item.location_path)

    selected_paths: set[str] = set()
    remaining_count = max_items
    for group_key in ordered_group_keys:
        locations = grouped_locations[group_key]
        if len(locations) > remaining_count:
            continue
        selected_paths.update(locations)
        remaining_count -= len(locations)
        if remaining_count == 0:
            break

    limited_data_map: dict[str, TranslationData] = {}
    for file_name, translation_data in translation_data_map.items():
        selected_items = [item for item in translation_data.translation_items if item.location_path in selected_paths]
        if selected_items:
            limited_data_map[file_name] = TranslationData(
                display_name=translation_data.display_name,
                translation_items=selected_items,
            )
    return limited_data_map


def count_translation_items(translation_data_map: dict[str, TranslationData]) -> int:
    """统计翻译数据中的条目数量。"""
    return sum(len(data.translation_items) for data in translation_data_map.values())


def build_translation_batches(
    *,
    translation_data_map: dict[str, TranslationData],
    setting: Setting,
    text_rules: TextRules,
    terminology_prompt_index: TerminologyPromptIndex | None,
    translation_cache: TranslationCache | None = None,
) -> TranslationBatchPlan:
    """构建不驻留完整 prompt 的可重复惰性正文翻译计划。"""
    context_plans = tuple(
        plan_translation_context_batches(
            translation_data=translation_data,
            token_size=setting.translation_context.token_size,
            factor=setting.translation_context.factor,
            max_command_items=setting.translation_context.max_command_items,
            system_prompt=setting.text_translation.system_prompt,
            text_rules=text_rules,
            terminology_prompt_index=terminology_prompt_index,
            translation_cache=translation_cache,
        )
        for translation_data in translation_data_map.values()
    )

    def iter_batches() -> Iterator[TranslationBatch]:
        return (batch for context_plan in context_plans for batch in context_plan)

    batch_item_counts = tuple(
        item_count for context_plan in context_plans for item_count in context_plan.batch_item_counts
    )
    return TranslationBatchPlan(
        iterator_factory=iter_batches,
        batch_item_counts=batch_item_counts,
    )


def build_llm_failure_record(
    *,
    run_id: str,
    failure: LLMRequestFailure,
) -> LlmFailureRecord:
    """把模型请求异常转换成数据库运行级故障记录。"""
    return LlmFailureRecord(
        run_id=run_id,
        category=failure.info.category,
        error_type=failure.info.error_type,
        error_message=failure.info.message,
        retryable=failure.info.retryable,
        attempt_count=failure.attempt_count,
        created_at=current_timestamp_text(),
    )


def format_exception_summary(error: Exception) -> str:
    """将异常压缩为适合日志首行展示的稳定摘要。"""
    message = str(error).strip()
    if message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__
