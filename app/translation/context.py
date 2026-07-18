"""
正文翻译上下文构造模块。

负责把 `TranslationData` 切成适合模型请求的批次，并组装系统提示词与用户正文。
数据库术语表索引由调用方传入。
"""

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import ClassVar

from app.llm.schemas import ChatMessage
from app.rmmz.schema import TranslationData, TranslationItem
from app.rmmz.text_rules import TextRules
from app.terminology.prompt import TerminologyPromptIndex, format_terminology_prompt_section
from app.translation.batch import (
    TranslationBatch,
    bind_translation_items,
)
from app.translation.cache import (
    TranslationCache,
    TranslationNeighbor,
    TranslationPromptItemContext,
    build_translation_prompt_item_context,
)

SCENE_PROMPT_TEMPLATE = "# 场景\n\n地图：{display_name}"
BODY_PROMPT_TEMPLATE = (
    "# 正文\n\n"
    "各短 ID 块彼此独立；翻译某个 ID 时只能使用该块内明确提供的信息，"
    "物理同批的其他 ID 不是它的上下文。\n\n"
    "{unit_text}"
)
LONG_TEXT_CONTEXT_TEMPLATE = "## {sequence}\n\nid: {id}\ntype: {item_type}\nrole: {role}"
ARRAY_CONTEXT_TEMPLATE = "## {sequence}\n\nid: {id}\ntype: {item_type}\nrole: {role}\nline_count: {line_count}"
SHORT_TEXT_CONTEXT_TEMPLATE = "## {sequence}\n\nid: {id}\ntype: {item_type}\nrole: {role}"
NARRATION_ROLE = "旁白"
PROMPT_BUDGET_SAFETY_RATIO = 0.85
SOURCE_LANGUAGE_DISPLAY_NAMES = {
    "ja": "日文",
    "en": "英文",
}


class PromptItemTooLargeError(ValueError):
    """单条正文连同完整请求已超过安全 token 预算。"""

    code: ClassVar[str] = "prompt_item_too_large"

    def __init__(
        self,
        *,
        location_path: str,
        estimated_tokens: int,
        safe_token_limit: int,
    ) -> None:
        """保存可供上层结构化报告的预算信息。"""
        super().__init__(f"{self.code}: 单条正文预计需要 {estimated_tokens} token，安全上限为 {safe_token_limit} token")
        self.location_path: str = location_path
        self.estimated_tokens: int = estimated_tokens
        self.safe_token_limit: int = safe_token_limit


@dataclass(frozen=True, slots=True)
class _PreparedPromptItem:
    """已经完成占位符遮罩、尚未生成模型可见正文的轻量条目。"""

    item: TranslationItem
    prompt_context: TranslationPromptItemContext


@dataclass(frozen=True, slots=True)
class TranslationBatchBlueprint:
    """只保存最终批次边界和上下文引用，不常驻模型 prompt。"""

    prepared_items: tuple[_PreparedPromptItem, ...]
    system_prompt: str
    estimated_tokens: int
    token_limit: int

    @property
    def item_count(self) -> int:
        """返回本蓝图绑定的正文条目数量。"""
        return len(self.prepared_items)

    def materialize(self) -> TranslationBatch:
        """仅在 Controller 取到本批次时创建正文片段和最终消息。"""
        bindings = bind_translation_items([prepared.item for prepared in self.prepared_items])
        body_fragments = tuple(
            _format_translation_item(
                item=prepared.item,
                masked_text=_masked_item_text(prepared.item),
                sequence=index,
                request_id=binding.request_id,
                prompt_context=prepared.prompt_context,
            )
            for index, (prepared, binding) in enumerate(
                zip(self.prepared_items, bindings, strict=True),
                start=1,
            )
        )
        return TranslationBatch(
            bindings=bindings,
            messages=[
                ChatMessage(role="system", text=self.system_prompt),
                ChatMessage(
                    role="user",
                    text=BODY_PROMPT_TEMPLATE.format(unit_text="".join(body_fragments)),
                ),
            ],
            estimated_tokens=self.estimated_tokens,
            token_limit=self.token_limit,
        )


@dataclass(frozen=True, slots=True)
class _TranslationBatchMeasure:
    """规划阶段唯一保留的批次元数据。"""

    item_count: int
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class TranslationContextBatchPlan:
    """保留批次边界整数，迭代时才生成一个批次的 prompt。"""

    translation_data: TranslationData
    measures: tuple[_TranslationBatchMeasure, ...]
    system_prompt: str
    token_limit: int
    terminology_prompt_index: TerminologyPromptIndex | None
    translation_cache: TranslationCache | None

    @property
    def batch_item_counts(self) -> tuple[int, ...]:
        """返回无需构造 prompt 即可读取的准确批次条目数。"""
        return tuple(measure.item_count for measure in self.measures)

    def __iter__(self) -> Iterator[TranslationBatch]:
        """按边界逐批生成正文；未被 Controller 取走的批次不会构造。"""
        items = self.translation_data.translation_items
        start_index = 0
        for measure in self.measures:
            end_index = start_index + measure.item_count
            prepared_items = tuple(
                _PreparedPromptItem(
                    item=items[item_index],
                    prompt_context=_resolve_prompt_context(
                        item=items[item_index],
                        items=items,
                        item_index=item_index,
                        display_name=self.translation_data.display_name or "",
                        terminology_prompt_index=self.terminology_prompt_index,
                        translation_cache=self.translation_cache,
                    ),
                )
                for item_index in range(start_index, end_index)
            )
            blueprint = _finalize_translation_batch_blueprint(
                system_prompt=self.system_prompt,
                prepared_items=prepared_items,
                estimated_tokens=measure.estimated_tokens,
                token_limit=self.token_limit,
            )
            yield blueprint.materialize()
            start_index = end_index
        if start_index != len(items):
            raise RuntimeError("翻译批次边界没有覆盖全部正文条目")


def iter_translation_context_batches(
    translation_data: TranslationData,
    token_size: int,
    factor: float,
    max_command_items: int,
    system_prompt: str,
    text_rules: TextRules,
    terminology_prompt_index: TerminologyPromptIndex | None = None,
    translation_cache: TranslationCache | None = None,
) -> Iterator[TranslationBatch]:
    """规划一次 placeholder 后，按需物化单文件翻译批次。"""
    plan = plan_translation_context_batches(
        translation_data=translation_data,
        token_size=token_size,
        factor=factor,
        max_command_items=max_command_items,
        system_prompt=system_prompt,
        text_rules=text_rules,
        terminology_prompt_index=terminology_prompt_index,
        translation_cache=translation_cache,
    )
    return iter(plan)


def plan_translation_context_batches(
    translation_data: TranslationData,
    token_size: int,
    factor: float,
    max_command_items: int,
    system_prompt: str,
    text_rules: TextRules,
    terminology_prompt_index: TerminologyPromptIndex | None = None,
    translation_cache: TranslationCache | None = None,
) -> TranslationContextBatchPlan:
    """线性规划轻量批次边界，不创建或保留完整模型 prompt。"""
    if token_size <= 0:
        raise ValueError("token_size 必须大于 0")
    if factor <= 0:
        raise ValueError("factor 必须大于 0")
    if max_command_items <= 0:
        raise ValueError("max_command_items 必须大于 0")

    rendered_system_prompt = _render_system_prompt(
        system_prompt=system_prompt,
        text_rules=text_rules,
    )
    safe_token_limit = max(1, math.floor(token_size * PROMPT_BUDGET_SAFETY_RATIO))
    current_items: list[_PreparedPromptItem] = []
    current_body_content_length = 0
    current_expected_items_length = 0
    current_estimated_tokens = 0
    measures: list[_TranslationBatchMeasure] = []
    display_name = translation_data.display_name or ""
    all_items = translation_data.translation_items
    for item_index, item in enumerate(all_items):
        prompt_context = _resolve_prompt_context(
            item=item,
            items=all_items,
            item_index=item_index,
            display_name=display_name,
            terminology_prompt_index=terminology_prompt_index,
            translation_cache=translation_cache,
        )
        prepared_item = _prepare_prompt_item(
            item=item,
            text_rules=text_rules,
            prompt_context=prompt_context,
        )

        if _same_role_run_length(current_items, item.role) >= max_command_items:
            measures.append(
                _TranslationBatchMeasure(
                    item_count=len(current_items),
                    estimated_tokens=current_estimated_tokens,
                )
            )
            current_items = []
            current_body_content_length = 0
            current_expected_items_length = 0
            current_estimated_tokens = 0

        candidate_estimate = _estimate_appended_prompt_item(
            system_prompt=rendered_system_prompt,
            prepared_item=prepared_item,
            sequence=len(current_items) + 1,
            current_body_content_length=current_body_content_length,
            current_expected_items_length=current_expected_items_length,
            current_item_count=len(current_items),
            factor=factor,
        )
        if candidate_estimate.estimated_tokens <= safe_token_limit:
            current_items.append(prepared_item)
            current_body_content_length = candidate_estimate.body_content_length
            current_expected_items_length = candidate_estimate.expected_items_length
            current_estimated_tokens = candidate_estimate.estimated_tokens
            continue

        if current_items:
            measures.append(
                _TranslationBatchMeasure(
                    item_count=len(current_items),
                    estimated_tokens=current_estimated_tokens,
                )
            )
            current_items = []
            current_body_content_length = 0
            current_expected_items_length = 0
            candidate_estimate = _estimate_appended_prompt_item(
                system_prompt=rendered_system_prompt,
                prepared_item=prepared_item,
                sequence=1,
                current_body_content_length=0,
                current_expected_items_length=0,
                current_item_count=0,
                factor=factor,
            )

        if candidate_estimate.estimated_tokens > safe_token_limit:
            raise PromptItemTooLargeError(
                location_path=item.location_path,
                estimated_tokens=candidate_estimate.estimated_tokens,
                safe_token_limit=safe_token_limit,
            )
        current_items = [prepared_item]
        current_body_content_length = candidate_estimate.body_content_length
        current_expected_items_length = candidate_estimate.expected_items_length
        current_estimated_tokens = candidate_estimate.estimated_tokens

    if current_items:
        measures.append(
            _TranslationBatchMeasure(
                item_count=len(current_items),
                estimated_tokens=current_estimated_tokens,
            )
        )
    return TranslationContextBatchPlan(
        translation_data=translation_data,
        measures=tuple(measures),
        system_prompt=rendered_system_prompt,
        token_limit=token_size,
        terminology_prompt_index=terminology_prompt_index,
        translation_cache=translation_cache,
    )


@dataclass(frozen=True, slots=True)
class _PromptSizeEstimate:
    """追加一项后的完整请求序列化长度摘要。"""

    body_content_length: int
    expected_items_length: int
    estimated_tokens: int


def _estimate_appended_prompt_item(
    *,
    system_prompt: str,
    prepared_item: _PreparedPromptItem,
    sequence: int,
    current_body_content_length: int,
    current_expected_items_length: int,
    current_item_count: int,
    factor: float,
) -> _PromptSizeEstimate:
    """只追加当前项的长度，避免每次重渲染整个候选批次。"""
    request_id = f"T{sequence:06d}"
    body_content_length = current_body_content_length + _translation_item_fragment_escaped_length(
        item=prepared_item.item,
        masked_text=_masked_item_text(prepared_item.item),
        sequence=sequence,
        request_id=request_id,
        prompt_context=prepared_item.prompt_context,
    )
    expected_items_length = current_expected_items_length + _expected_response_item_length(
        item=prepared_item.item,
        request_id=request_id,
    )
    item_count = current_item_count + 1
    estimated_tokens = _estimate_accumulated_tokens(
        system_prompt=system_prompt,
        body_content_length=body_content_length,
        expected_items_length=expected_items_length,
        item_count=item_count,
        factor=factor,
    )
    return _PromptSizeEstimate(
        body_content_length=body_content_length,
        expected_items_length=expected_items_length,
        estimated_tokens=estimated_tokens,
    )


def _finalize_translation_batch_blueprint(
    *,
    system_prompt: str,
    prepared_items: Sequence[_PreparedPromptItem],
    estimated_tokens: int,
    token_limit: int,
) -> TranslationBatchBlueprint:
    """冻结轻量边界；正文字符串继续延迟到 Controller 取批时生成。"""
    if not prepared_items:
        raise RuntimeError("不能提交空翻译批次")
    return TranslationBatchBlueprint(
        prepared_items=tuple(prepared_items),
        system_prompt=system_prompt,
        estimated_tokens=estimated_tokens,
        token_limit=token_limit,
    )


def _format_translation_item(
    *,
    item: TranslationItem,
    masked_text: str,
    sequence: int,
    request_id: str,
    prompt_context: TranslationPromptItemContext,
) -> str:
    """将单个 `TranslationItem` 格式化成上下文正文块。"""
    sections = _build_translation_item_sections(
        item=item,
        masked_text=masked_text,
        sequence=sequence,
        request_id=request_id,
        prompt_context=prompt_context,
    )
    return "\n\n".join(sections) + "\n\n"


def _build_translation_item_sections(
    *,
    item: TranslationItem,
    masked_text: str,
    sequence: int,
    request_id: str,
    prompt_context: TranslationPromptItemContext,
) -> list[str]:
    """构造单项的独立段落，供最终渲染和线性预算共用。"""
    if item.item_type == "long_text":
        header = LONG_TEXT_CONTEXT_TEMPLATE.format(
            sequence=sequence,
            id=request_id,
            item_type=item.item_type,
            role=item.role or "",
        )
    elif item.item_type == "array":
        header = ARRAY_CONTEXT_TEMPLATE.format(
            sequence=sequence,
            id=request_id,
            item_type=item.item_type,
            role=item.role or "",
            line_count=len(item.original_lines),
        )
    elif item.item_type == "short_text":
        header = SHORT_TEXT_CONTEXT_TEMPLATE.format(
            sequence=sequence,
            id=request_id,
            item_type=item.item_type,
            role=item.role or "",
        )
    else:
        raise ValueError(f"未知的 item_type: {item.item_type}")

    sections = [
        header,
        SCENE_PROMPT_TEMPLATE.format(display_name=prompt_context.display_name),
    ]
    if prompt_context.previous_items:
        sections.append(
            _format_neighbor_section(
                title="[[前文上下文]]",
                neighbors=prompt_context.previous_items,
            )
        )
    if prompt_context.next_items:
        sections.append(
            _format_neighbor_section(
                title="[[后文上下文]]",
                neighbors=prompt_context.next_items,
            )
        )
    terminology_prompt = format_terminology_prompt_section(prompt_context.terminology_entries)
    if terminology_prompt:
        sections.append(terminology_prompt)
    sections.append(f"[[本项正文]]\n{masked_text}")
    return sections


def _translation_item_fragment_escaped_length(
    *,
    item: TranslationItem,
    masked_text: str,
    sequence: int,
    request_id: str,
    prompt_context: TranslationPromptItemContext,
) -> int:
    """精确计算单项进入 JSON 字符串后的长度，但不组装完整正文。"""
    sections = _build_translation_item_sections(
        item=item,
        masked_text=masked_text,
        sequence=sequence,
        request_id=request_id,
        prompt_context=prompt_context,
    )
    separator_length = _json_escaped_content_length("\n\n")
    return (
        sum(_json_escaped_content_length(section) for section in sections)
        + separator_length * max(len(sections) - 1, 0)
        + separator_length
    )


def _format_neighbor_section(
    *,
    title: str,
    neighbors: Sequence[TranslationNeighbor],
) -> str:
    """把同容器邻居渲染为不含本地路径和内部字段的只读上下文。"""
    fragments = [title]
    for index, neighbor in enumerate(neighbors, start=1):
        fragments.append(
            "\n".join(
                [
                    f"### {index}",
                    f"type: {neighbor.item_type}",
                    f"role: {neighbor.role or ''}",
                    "text:",
                    "\n".join(neighbor.original_lines),
                ]
            )
        )
    return "\n\n".join(fragments)


def _prepare_prompt_item(
    *,
    item: TranslationItem,
    text_rules: TextRules,
    prompt_context: TranslationPromptItemContext,
) -> _PreparedPromptItem:
    """构建一次正文占位符，只保留条目与共享上下文引用。"""
    item.build_placeholders(text_rules)
    return _PreparedPromptItem(
        item=item,
        prompt_context=prompt_context,
    )


def _resolve_prompt_context(
    *,
    item: TranslationItem,
    items: Sequence[TranslationItem],
    item_index: int,
    display_name: str,
    terminology_prompt_index: TerminologyPromptIndex | None,
    translation_cache: TranslationCache | None,
) -> TranslationPromptItemContext:
    """从生产缓存或完整范围读取与批次边界无关的逐项上下文。"""
    if translation_cache is None:
        return build_translation_prompt_item_context(
            item=item,
            items=items,
            item_index=item_index,
            display_name=display_name,
            terminology_prompt_index=terminology_prompt_index,
        )
    prompt_context = translation_cache.build_prompt_context(item)
    if prompt_context is None:
        raise ValueError(f"正文缺少已准备的逐项 Prompt 上下文: {item.location_path}")
    return prompt_context


def _masked_item_text(item: TranslationItem) -> str:
    """从已经构建一次的占位符状态读取模型可见正文。"""
    if item.item_type == "short_text":
        return "".join(item.original_lines_with_placeholders)
    return "\n".join(item.original_lines_with_placeholders)


def _same_role_run_length(
    prepared_items: Sequence[_PreparedPromptItem],
    role: str | None,
) -> int:
    """统计当前批次末尾同角色对白数量，旁白不受连续对白上限影响。"""
    if role is None or role == NARRATION_ROLE:
        return 0
    count = 0
    for prepared in reversed(prepared_items):
        if prepared.item.role != role:
            break
        count += 1
    return count


def _render_system_prompt(*, system_prompt: str, text_rules: TextRules) -> str:
    """只用面向模型的语言名称声明本轮允许出现的全部源语言。"""
    source_languages = (
        text_rules.setting.source_language,
        *text_rules.setting.additional_source_languages,
    )
    display_names = [SOURCE_LANGUAGE_DISPLAY_NAMES.get(language, language) for language in source_languages]
    language_notice = f"本批次允许的源语言：{'、'.join(display_names)}。"
    return f"{system_prompt.rstrip()}\n\n{language_notice}"


def _estimate_accumulated_tokens(
    *,
    system_prompt: str,
    body_content_length: int,
    expected_items_length: int,
    item_count: int,
    factor: float,
) -> int:
    """按已累计的单项长度精确计算完整请求序列化字符数。"""
    if item_count <= 0:
        raise ValueError("item_count 必须大于 0")
    complete_body_content_length = _json_escaped_content_length(BODY_PROMPT_TEMPLATE.format(unit_text=""))
    complete_body_content_length += body_content_length
    user_prompt_json_length = 2 + complete_body_content_length

    serialized_length = len('{"messages":[{"role":"system","text":')
    serialized_length += _json_string_length(system_prompt)
    serialized_length += len('},{"role":"user","text":')
    serialized_length += user_prompt_json_length
    serialized_length += len('}],"expected_response":[')
    serialized_length += expected_items_length
    serialized_length += item_count - 1
    serialized_length += len("]}")
    return max(1, math.ceil(serialized_length / factor))


def _expected_response_item_length(*, item: TranslationItem, request_id: str) -> int:
    """返回单个预期 JSON 结果对象的精确紧凑序列化长度。"""
    source_lines_length = _json_string_array_length(item.original_lines_with_placeholders)
    serialized_length = len('{"id":')
    serialized_length += _json_string_length(request_id)
    serialized_length += len(',"role":')
    serialized_length += _json_string_length(item.role or "")
    serialized_length += len(',"source_lines":')
    serialized_length += source_lines_length
    serialized_length += len(',"translation_lines":')
    serialized_length += source_lines_length
    serialized_length += 1
    return serialized_length


def _json_string_array_length(values: Sequence[str]) -> int:
    """返回 ensure_ascii=False 紧凑 JSON 字符串数组的精确长度。"""
    if not values:
        return 2
    return 2 + len(values) - 1 + sum(_json_string_length(value) for value in values)


def _json_string_length(value: str) -> int:
    """返回 ensure_ascii=False JSON 字符串（含引号）的精确长度。"""
    return 2 + _json_escaped_content_length(value)


def _json_escaped_content_length(value: str) -> int:
    """按 Python JSON 的紧凑非 ASCII 转义规则计算字符串内容长度。"""
    length = 0
    for character in value:
        codepoint = ord(character)
        if character in {'"', "\\"}:
            length += 2
        elif character in {"\b", "\f", "\n", "\r", "\t"}:
            length += 2
        elif codepoint < 0x20:
            length += 6
        else:
            length += 1
    return length


__all__: list[str] = [
    "PROMPT_BUDGET_SAFETY_RATIO",
    "SOURCE_LANGUAGE_DISPLAY_NAMES",
    "PromptItemTooLargeError",
    "TranslationBatchBlueprint",
    "iter_translation_context_batches",
    "plan_translation_context_batches",
]
