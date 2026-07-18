"""
正文翻译校验模块。

负责解析模型返回的 JSON，按批次短 ID 映射回本地条目，并执行漏翻、
占位符和源文残留校验。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from json_repair import repair_json
from pydantic import BaseModel, RootModel

from app.rmmz.placeholder_mapping import (
    build_original_placeholder_queues,
    consume_original_placeholder,
)
from app.rmmz.schema import ErrorType, TranslationErrorItem, TranslationItem
from app.rmmz.text_layout import (
    align_long_text_lines,
    normalize_translated_wrapping_punctuation,
)
from app.rmmz.text_rules import ControlSequenceSpan, TextRules
from app.source_residual import SourceResidualRuleSet, check_source_residual_for_item
from app.translation.batch import (
    TranslationBatch,
    TranslationPromptBinding,
    bind_translation_items,
)
from app.translation.text_structure import validate_translation_text_structure

ERR_PARSE_FAILED: ErrorType = "模型返回不可解析"
ERR_TEXT_STRUCTURE: ErrorType = "文本结构不匹配"
ERR_PLACEHOLDER_MISMATCH: ErrorType = "控制符不匹配"
ERR_SOURCE_RESIDUAL: ErrorType = "源文残留"
ERR_ARRAY_LINE_COUNT: ErrorType = "选项行数不匹配"
ERR_EMPTY_TRANSLATION: ErrorType = "AI漏翻"


class TranslationResponseItem(BaseModel):
    """模型返回的单条对照译文。"""

    id: str
    translation_lines: list[str]


class TranslationResponse(RootModel[list[TranslationResponseItem]]):
    """正文翻译返回结果模型。"""


class TranslationResponseProtocolError(ValueError):
    """模型返回的批次短 ID 不满足一一对应协议。"""

    def __init__(self, *, code: str, detail: str) -> None:
        """保存稳定错误码和可读详情。"""
        super().__init__(f"{code}: {detail}")
        self.code: str = code
        self.detail: str = detail


@dataclass(frozen=True, slots=True)
class TranslationBatchVerification:
    """一个批次完成本地协议与质量检查后的纯结果。"""

    right_items: list[TranslationItem]
    error_items: list[TranslationErrorItem]


type ValidateTranslationCandidates = Callable[
    [Sequence[TranslationItem]],
    dict[str, list[str]],
]


def verify_translation_batch_result(
    *,
    ai_result: str,
    batch: TranslationBatch,
    text_rules: TextRules,
    source_residual_rule_set: SourceResidualRuleSet | None = None,
    validate_candidates: ValidateTranslationCandidates | None = None,
) -> TranslationBatchVerification:
    """使用批次原始短 ID 绑定返回不依赖队列的校验结果。"""
    return _verify_translation_items_result(
        ai_result=ai_result,
        items=batch.items,
        bindings=batch.bindings,
        text_rules=text_rules,
        source_residual_rule_set=source_residual_rule_set,
        validate_candidates=validate_candidates,
    )


def _verify_translation_items_result(
    *,
    ai_result: str,
    items: Sequence[TranslationItem],
    text_rules: TextRules,
    source_residual_rule_set: SourceResidualRuleSet | None,
    bindings: Sequence[TranslationPromptBinding] | None,
    validate_candidates: ValidateTranslationCandidates | None,
) -> TranslationBatchVerification:
    """执行与队列和调度器无关的批次校验。"""
    right_items: list[TranslationItem] = []
    error_items: list[TranslationErrorItem] = []
    resolved_bindings = _resolve_bindings(items=items, bindings=bindings)

    try:
        clean_result = repair_json(ai_result, return_objects=False)

        response_items = TranslationResponse.model_validate_json(clean_result).root
        translation_map = _build_translation_line_map(
            response_items=response_items,
            bindings=resolved_bindings,
        )
    except TranslationResponseProtocolError as error:
        error_items.extend(
            _build_batch_protocol_error_items(
                items=items,
                ai_result=ai_result,
                detail=[
                    "模型返回的短 ID 与当前批次不一致",
                    f"协议错误 [{error.code}]: {error.detail}",
                ],
            )
        )
        return TranslationBatchVerification(
            right_items=right_items,
            error_items=error_items,
        )
    except Exception as error:
        error_items.extend(
            _build_batch_protocol_error_items(
                items=items,
                ai_result=ai_result,
                detail=["模型返回无法解析为 JSON 数组", f"详细错误: {error}"],
            )
        )
        return TranslationBatchVerification(
            right_items=right_items,
            error_items=error_items,
        )

    for binding in resolved_bindings:
        item = binding.item
        model_translation_lines = translation_map[binding.request_id]
        if _is_empty_translation_lines(model_translation_lines):
            error_items.append(
                TranslationErrorItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=list(item.original_lines),
                    translation_lines=list(model_translation_lines),
                    error_type=ERR_EMPTY_TRANSLATION,
                    error_detail=["AI漏翻: 模型返回空译文"],
                    model_response=ai_result,
                )
            )
            continue
        normalized_model_translation_lines = text_rules.normalize_translation_lines(model_translation_lines)

        if item.item_type == "long_text":
            translation_lines = align_long_text_lines(
                text="\n".join(normalized_model_translation_lines),
                target_lines=len(item.original_lines),
                location_path=item.location_path,
                text_rules=text_rules,
                original_lines=item.original_lines,
            )
        elif item.item_type == "array":
            translation_lines = list(normalized_model_translation_lines)
            translation_lines = normalize_translated_wrapping_punctuation(
                original_lines=item.original_lines,
                translation_lines=translation_lines,
                text_rules=text_rules,
            )
            if len(translation_lines) != len(item.original_lines):
                error_items.append(
                    TranslationErrorItem(
                        location_path=item.location_path,
                        item_type=item.item_type,
                        role=item.role,
                        original_lines=list(item.original_lines),
                        translation_lines=list(translation_lines),
                        error_type=ERR_ARRAY_LINE_COUNT,
                        error_detail=[
                            f"选项行数不匹配: 期望 {len(item.original_lines)} 行, 实际 {len(translation_lines)} 行"
                        ],
                        model_response=ai_result,
                    )
                )
                continue
        else:
            translation_lines = list(normalized_model_translation_lines)
            translation_lines = normalize_translated_wrapping_punctuation(
                original_lines=item.original_lines,
                translation_lines=translation_lines,
                text_rules=text_rules,
            )

        item.translation_lines_with_placeholders = _mask_known_translation_controls(
            item=item,
            translation_lines=translation_lines,
            text_rules=text_rules,
        )
        item.translation_lines = []

        try:
            validate_translation_text_structure(
                item=item,
                translation_lines=translation_lines,
                translation_lines_with_placeholders=item.translation_lines_with_placeholders,
                text_rules=text_rules,
            )
        except ValueError as error:
            error_items.append(
                TranslationErrorItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=list(item.original_lines),
                    translation_lines=list(translation_lines),
                    error_type=ERR_TEXT_STRUCTURE,
                    error_detail=str(error).split(";\n"),
                    model_response=ai_result,
                )
            )
            continue

        try:
            item.verify_placeholders(text_rules)
            item.translation_lines = list(item.translation_lines_with_placeholders)
        except ValueError as error:
            error_items.append(
                TranslationErrorItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=list(item.original_lines),
                    translation_lines=list(item.translation_lines_with_placeholders),
                    error_type=ERR_PLACEHOLDER_MISMATCH,
                    error_detail=str(error).split(";\n"),
                    model_response=ai_result,
                )
            )
            continue

        try:
            check_source_residual_for_item(
                item=item,
                text_rules=text_rules,
                rule_set=source_residual_rule_set,
            )
        except ValueError as error:
            error_items.append(
                TranslationErrorItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=list(item.original_lines),
                    translation_lines=list(item.translation_lines),
                    error_type=ERR_SOURCE_RESIDUAL,
                    error_detail=[str(error)],
                    model_response=ai_result,
                )
            )
            continue

        item.restore_placeholders()
        right_items.append(item)

    if validate_candidates is not None and right_items:
        validation_errors = validate_candidates(right_items)
        validated_right_items: list[TranslationItem] = []
        for item in right_items:
            details = validation_errors.get(item.location_path, [])
            if not details:
                validated_right_items.append(item)
                continue
            error_items.append(
                TranslationErrorItem(
                    location_path=item.location_path,
                    item_type=item.item_type,
                    role=item.role,
                    original_lines=list(item.original_lines),
                    translation_lines=list(item.translation_lines),
                    error_type=ERR_TEXT_STRUCTURE,
                    error_detail=list(details),
                    model_response=ai_result,
                )
            )
        right_items = validated_right_items

    return TranslationBatchVerification(
        right_items=right_items,
        error_items=error_items,
    )


def _build_translation_line_map(
    *,
    response_items: list[TranslationResponseItem],
    bindings: Sequence[TranslationPromptBinding],
) -> dict[str, list[str]]:
    """严格校验模型短 ID 与本地批次绑定一一对应。"""
    valid_ids = {binding.request_id for binding in bindings}
    translation_map: dict[str, list[str]] = {}
    for response_item in response_items:
        if response_item.id not in valid_ids:
            raise TranslationResponseProtocolError(
                code="response_unknown_id",
                detail=f"模型返回了当前批次不存在的 ID: {response_item.id}",
            )
        if response_item.id in translation_map:
            raise TranslationResponseProtocolError(
                code="response_duplicate_id",
                detail=f"模型重复返回 ID: {response_item.id}",
            )
        translation_map[response_item.id] = list(response_item.translation_lines)
    missing_ids = [binding.request_id for binding in bindings if binding.request_id not in translation_map]
    if missing_ids:
        raise TranslationResponseProtocolError(
            code="response_missing_id",
            detail=f"模型漏掉了当前批次 ID: {', '.join(missing_ids)}",
        )
    return translation_map


def _build_batch_protocol_error_items(
    *,
    items: Sequence[TranslationItem],
    ai_result: str,
    detail: list[str],
) -> list[TranslationErrorItem]:
    """把批次级模型协议错误映射到所有本地条目。"""
    return [
        TranslationErrorItem(
            location_path=item.location_path,
            item_type=item.item_type,
            role=item.role,
            original_lines=list(item.original_lines),
            translation_lines=[],
            error_type=ERR_PARSE_FAILED,
            error_detail=list(detail),
            model_response=ai_result,
        )
        for item in items
    ]


def _resolve_bindings(
    *,
    items: Sequence[TranslationItem],
    bindings: Sequence[TranslationPromptBinding] | None,
) -> tuple[TranslationPromptBinding, ...]:
    """优先使用批次原始绑定，并校验调用方没有替换或重排条目。"""
    if bindings is None:
        return bind_translation_items(items)
    resolved_bindings = tuple(bindings)
    if len(resolved_bindings) != len(items) or any(
        binding.item is not item for binding, item in zip(resolved_bindings, items, strict=False)
    ):
        raise ValueError("翻译批次绑定与待校验条目不一致")
    return resolved_bindings


def _is_empty_translation_lines(translation_lines: list[str]) -> bool:
    """判断模型是否返回了空数组或全空白译文。"""
    return not translation_lines or not any(line.strip() for line in translation_lines)


def _mask_known_translation_controls(
    *,
    item: TranslationItem,
    translation_lines: list[str],
    text_rules: TextRules,
) -> list[str]:
    """把模型返回的原始控制符修回本条原文对应的占位符。"""
    placeholder_queues = build_original_placeholder_queues(
        item=item,
        text_rules=text_rules,
    )
    known_originals = set(placeholder_queues)

    def replacer(span: ControlSequenceSpan) -> str:
        """只修回原文已有的控制符，未知控制符继续交给后续校验。"""
        placeholder = consume_original_placeholder(
            queues=placeholder_queues,
            original=span.original,
        )
        if placeholder is not None:
            return placeholder
        if span.original in known_originals:
            return "[CUSTOM_UNEXPECTED_1]"
        return span.original

    return [text_rules.replace_rm_control_sequences(line, replacer) for line in translation_lines]


__all__: list[str] = [
    "TranslationBatchVerification",
    "ValidateTranslationCandidates",
    "verify_translation_batch_result",
]
