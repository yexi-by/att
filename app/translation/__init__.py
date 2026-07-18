"""
翻译层统一导出入口。
"""

from .batch import TranslationBatch, TranslationBatchPlan, TranslationPromptBinding
from .cache import (
    TranslationCache,
    TranslationContextKey,
    TranslationPromptItemContext,
    TranslationReuseContextData,
    build_translation_prompt_item_context,
    canonical_json_hash,
    prepare_translation_cache_for_scope,
)
from .candidate_validation import (
    TranslationCandidateValidator,
    validate_translation_candidate,
)
from .context import (
    PromptItemTooLargeError,
    TranslationBatchBlueprint,
    iter_translation_context_batches,
    plan_translation_context_batches,
)
from .freshness import (
    TranslationFreshnessResult,
    build_translation_reuse_contexts_by_path,
    evaluate_translation_freshness,
    translation_record_matches_current_target,
    unavailable_translation_freshness,
)
from .reuse import (
    CurrentRunReuseResult,
    SavedTranslationReuseResult,
    collect_saved_translation_reuse,
    expand_current_run_reuse,
)
from .run_controller import (
    BatchExecutionResult,
    PersistedBatchCounts,
    SizedBatchIterable,
    TranslationRunCancelled,
    TranslationRunController,
    TranslationRunResult,
)
from .text_translation import TextTranslation
from .verify import (
    TranslationBatchVerification,
    verify_translation_batch_result,
)

__all__: list[str] = [
    "TranslationCache",
    "TranslationCandidateValidator",
    "TranslationContextKey",
    "TranslationFreshnessResult",
    "TranslationPromptItemContext",
    "TranslationReuseContextData",
    "TranslationBatch",
    "TranslationBatchPlan",
    "BatchExecutionResult",
    "TranslationBatchVerification",
    "TranslationBatchBlueprint",
    "TextTranslation",
    "TranslationPromptBinding",
    "PromptItemTooLargeError",
    "PersistedBatchCounts",
    "CurrentRunReuseResult",
    "SavedTranslationReuseResult",
    "SizedBatchIterable",
    "collect_saved_translation_reuse",
    "build_translation_reuse_contexts_by_path",
    "build_translation_prompt_item_context",
    "canonical_json_hash",
    "prepare_translation_cache_for_scope",
    "evaluate_translation_freshness",
    "expand_current_run_reuse",
    "TranslationRunController",
    "TranslationRunCancelled",
    "TranslationRunResult",
    "iter_translation_context_batches",
    "plan_translation_context_batches",
    "verify_translation_batch_result",
    "validate_translation_candidate",
    "translation_record_matches_current_target",
    "unavailable_translation_freshness",
]
