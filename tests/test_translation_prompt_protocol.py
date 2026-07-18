"""正文翻译短 ID、隐私边界与完整请求预算测试。"""

import json
import math
from typing import Literal

import pytest

import app.translation.context as translation_context
from app.application.use_cases.translation_run import build_translation_batches
from app.config.schemas import Setting
from app.llm import ChatMessage
from app.rmmz.schema import TranslationData, TranslationItem
from app.rmmz.text_rules import TextRules, get_default_text_rules
from app.terminology import TerminologyGlossary, TerminologyPromptIndex
from app.translation import TranslationCache, TranslationPromptItemContext
from app.translation.context import (
    PROMPT_BUDGET_SAFETY_RATIO,
    PromptItemTooLargeError,
    iter_translation_context_batches,
)
from app.translation.verify import verify_translation_batch_result


def _build_data(*, item_count: int, text: str = "こんにちは") -> TranslationData:
    """构建包含敏感本地路径的正文范围。"""
    return TranslationData(
        display_name="始まりの町",
        translation_items=[
            TranslationItem(
                location_path=f"C:/Users/tester/private/Map001.json/{index}/0",
                source_line_paths=[f"D:/games/private/data/Map001.json/{index}"],
                item_type="short_text",
                role="村人",
                original_lines=[text],
            )
            for index in range(item_count)
        ],
    )


def _build_setting() -> Setting:
    """构造只包含正文切批所需字段的稳定测试配置。"""
    return Setting.model_validate(
        {
            "llm": {
                "base_url": "https://example.invalid/v1",
                "api_key": "test",
                "model": "test-model",
                "timeout": 30,
            },
            "translation_context": {
                "token_size": 2000,
                "factor": 1.0,
                "max_command_items": 5,
            },
            "text_translation": {
                "worker_count": 1,
                "rpm": None,
                "retry_count": 0,
                "retry_delay": 0,
                "system_prompt_file": "<test>",
                "system_prompt": "系统提示",
            },
            "event_command_text": {
                "default_command_codes": [101],
                "default_command_codes_by_engine": {},
            },
        }
    )


def test_batch_plan_prepares_only_lightweight_metadata_and_defers_prompt_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规划阶段不常驻正文 prompt，Controller 取批时才逐项渲染。"""
    data = _build_data(item_count=3)
    rules = get_default_text_rules()
    placeholder_calls: dict[str, int] = {}
    chat_message_count = 0
    formatted_item_count = 0
    original_build_placeholders = TranslationItem.build_placeholders
    original_chat_message = ChatMessage
    original_format_translation_item = translation_context._format_translation_item  # pyright: ignore[reportPrivateUsage]

    def counted_build_placeholders(
        self: TranslationItem,
        text_rules: TextRules | None = None,
    ) -> None:
        placeholder_calls[self.location_path] = placeholder_calls.get(self.location_path, 0) + 1
        original_build_placeholders(self, text_rules)

    def counted_chat_message(
        *,
        role: Literal["system", "user", "assistant"],
        text: str,
    ) -> ChatMessage:
        nonlocal chat_message_count
        chat_message_count += 1
        return original_chat_message(role=role, text=text)

    def counted_format_translation_item(
        *,
        item: TranslationItem,
        masked_text: str,
        sequence: int,
        request_id: str,
        prompt_context: TranslationPromptItemContext,
    ) -> str:
        nonlocal formatted_item_count
        formatted_item_count += 1
        return original_format_translation_item(
            item=item,
            masked_text=masked_text,
            sequence=sequence,
            request_id=request_id,
            prompt_context=prompt_context,
        )

    monkeypatch.setattr(TranslationItem, "build_placeholders", counted_build_placeholders)
    monkeypatch.setattr(translation_context, "ChatMessage", counted_chat_message)
    monkeypatch.setattr(translation_context, "_format_translation_item", counted_format_translation_item)

    plan = build_translation_batches(
        translation_data_map={"Map001.json": data},
        setting=_build_setting(),
        text_rules=rules,
        terminology_prompt_index=None,
    )

    assert chat_message_count == 0
    assert formatted_item_count == 0
    assert placeholder_calls == {item.location_path: 1 for item in data.translation_items}

    batches = list(plan)

    assert chat_message_count == len(batches) * 2
    assert formatted_item_count == len(data.translation_items)
    assert placeholder_calls == {item.location_path: 1 for item in data.translation_items}


def test_prompt_uses_local_short_id_without_internal_paths() -> None:
    """模型只能看到批次短 ID，本地路径只保留在进程内绑定。"""
    data = _build_data(item_count=1)
    item = data.translation_items[0]

    batch = next(
        iter_translation_context_batches(
            translation_data=data,
            token_size=2000,
            factor=1.0,
            max_command_items=5,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
        )
    )
    complete_prompt = "\n".join(message.text for message in batch.messages)

    assert "id: T000001" in complete_prompt
    assert item.location_path not in complete_prompt
    assert item.source_line_paths[0] not in complete_prompt
    assert "Map001.json" not in complete_prompt
    assert "location_path" not in complete_prompt
    assert "source_line_paths" not in complete_prompt
    assert batch.bindings[0].request_id == "T000001"
    assert batch.bindings[0].item is item


def test_each_short_id_uses_its_own_cached_context_and_terms() -> None:
    """同一 HTTP 中每个短 ID 只能拿到自己的邻居和实际术语。"""
    items = [
        TranslationItem(
            location_path=f"Map001.json/events/1/pages/0/list/{index}",
            item_type="short_text",
            original_lines=[text],
        )
        for index, text in enumerate(["第一项", "第二项", "第三项"])
    ]
    full_data = TranslationData(
        display_name="原始场景",
        translation_items=items,
    )
    prompt_index = TerminologyPromptIndex.from_glossary(
        TerminologyGlossary(
            terms={
                "第一项": "甲",
                "第二项": "乙",
                "第三项": "丙",
                "*": "星号",
            }
        )
    )
    cache = TranslationCache()
    cache.prepare(
        translation_data_map={"Map001.json": full_data},
        terminology_prompt_index=prompt_index,
        source_language="ja",
    )

    batch = next(
        iter_translation_context_batches(
            translation_data=full_data,
            token_size=10000,
            factor=1.0,
            max_command_items=5,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
            terminology_prompt_index=prompt_index,
            translation_cache=cache,
        )
    )
    user_prompt = batch.messages[1].text
    first_block = user_prompt.split("\n## 1\n", maxsplit=1)[1].split("\n## 2\n", maxsplit=1)[0]
    second_block = user_prompt.split("\n## 2\n", maxsplit=1)[1].split("\n## 3\n", maxsplit=1)[0]

    assert len(batch.items) == 3
    assert "各短 ID 块彼此独立" in user_prompt
    assert "第一项 => 甲" in first_block
    assert "第二项 => 乙" not in first_block
    assert "第二项 => 乙" in second_block
    assert "第一项 => 甲" not in second_block
    assert "第三项 => 丙" not in second_block
    assert "* => 星号" not in user_prompt

    key = cache.build_cache_key(items[1])
    prompt_context = cache.build_prompt_context(items[1])
    assert key is not None
    assert prompt_context is not None
    assert prompt_context is key.prompt_context
    assert key.terminology_entries == (("glossary", "第二项", "乙"),)
    assert [neighbor.original_lines for neighbor in prompt_context.previous_items] == [("第一项",)]
    assert [neighbor.original_lines for neighbor in prompt_context.next_items] == [("第三项",)]


def test_cached_item_prompt_context_survives_batch_repartition() -> None:
    """限流或重译重分批不得改变该项的场景、邻居和术语事实。"""
    items = [
        TranslationItem(
            location_path=f"Map001.json/events/1/pages/0/list/{index}",
            item_type="short_text",
            original_lines=[text],
        )
        for index, text in enumerate(["前文", "目标术语", "后文"])
    ]
    full_data = TranslationData(display_name="可信场景", translation_items=items)
    prompt_index = TerminologyPromptIndex.from_glossary(TerminologyGlossary(terms={"目标术语": "目标译名"}))
    cache = TranslationCache()
    cache.prepare(
        translation_data_map={"Map001.json": full_data},
        terminology_prompt_index=prompt_index,
        source_language="ja",
    )
    context_before = cache.build_prompt_context(items[1])

    repartitioned_batch = next(
        iter_translation_context_batches(
            translation_data=TranslationData(
                display_name="不应采用的临时分批场景",
                translation_items=[items[1]],
            ),
            token_size=3000,
            factor=1.0,
            max_command_items=5,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
            terminology_prompt_index=None,
            translation_cache=cache,
        )
    )
    user_prompt = repartitioned_batch.messages[1].text

    assert cache.build_prompt_context(items[1]) is context_before
    assert "地图：可信场景" in user_prompt
    assert "不应采用的临时分批场景" not in user_prompt
    assert "[[前文上下文]]" in user_prompt
    assert "前文" in user_prompt
    assert "[[后文上下文]]" in user_prompt
    assert "后文" in user_prompt
    assert "目标术语 => 目标译名" in user_prompt


def test_prompt_declares_primary_and_additional_source_languages() -> None:
    """日文加英文档案必须明确告诉模型两种源语言都可能出现。"""
    default_rules = get_default_text_rules()
    mixed_rules = TextRules.from_setting(
        default_rules.setting.model_copy(
            update={"additional_source_languages": ("en",)},
        ),
        custom_placeholder_rules=default_rules.custom_placeholder_rules,
        structured_placeholder_rules=default_rules.structured_placeholder_rules,
    )

    batch = next(
        iter_translation_context_batches(
            translation_data=_build_data(item_count=1, text="SOLD OUT / 売り切れ"),
            token_size=2000,
            factor=1.0,
            max_command_items=5,
            system_prompt="系统提示",
            text_rules=mixed_rules,
        )
    )

    assert "本批次允许的源语言：日文、英文。" in batch.messages[0].text


def test_batches_count_complete_request_and_keep_safety_margin() -> None:
    """系统提示、用户正文和预期 JSON 都必须受 15% 安全余量约束。"""
    token_size = 1600
    data = _build_data(item_count=3, text="こんにちは" * 10)

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=token_size,
            factor=1.0,
            max_command_items=5,
            system_prompt="系统提示" * 10,
            text_rules=get_default_text_rules(),
        )
    )

    assert [len(batch.items) for batch in batches] == [2, 1]
    assert sum(len(batch.items) for batch in batches) == 3
    safe_limit = math.floor(token_size * PROMPT_BUDGET_SAFETY_RATIO)
    assert all(batch.estimated_tokens <= safe_limit for batch in batches)
    for batch in batches:
        request_shape = {
            "messages": [message.model_dump(mode="json") for message in batch.messages],
            "expected_response": [
                {
                    "id": binding.request_id,
                    "role": binding.item.role or "",
                    "source_lines": list(binding.item.original_lines_with_placeholders),
                    "translation_lines": list(binding.item.original_lines_with_placeholders),
                }
                for binding in batch.bindings
            ],
        }
        serialized_request = json.dumps(
            request_shape,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert batch.estimated_tokens == math.ceil(len(serialized_request))
    assert [binding.request_id for binding in batches[0].bindings] == [
        "T000001",
        "T000002",
    ]
    assert batches[1].bindings[0].request_id == "T000001"


def test_single_item_over_complete_prompt_budget_fails_explicitly() -> None:
    """单条请求超限时在派发前返回稳定错误码。"""
    data = _build_data(item_count=1, text="こんにちは" * 10)

    with pytest.raises(PromptItemTooLargeError) as raised:
        _ = list(
            iter_translation_context_batches(
                translation_data=data,
                token_size=500,
                factor=1.0,
                max_command_items=5,
                system_prompt="系统提示" * 10,
                text_rules=get_default_text_rules(),
            )
        )

    assert raised.value.code == "prompt_item_too_large"
    assert raised.value.estimated_tokens > raised.value.safe_token_limit


def test_pure_verifier_maps_short_ids_through_batch_bindings() -> None:
    """纯校验接口只能通过批次原始绑定把译文映射回本地条目。"""
    data = _build_data(item_count=2)
    batch = next(
        iter_translation_context_batches(
            translation_data=data,
            token_size=2000,
            factor=1.0,
            max_command_items=5,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
        )
    )
    ai_result = json.dumps(
        [
            {"id": "T000001", "translation_lines": ["你好"]},
            {"id": "T000002", "translation_lines": ["您好"]},
        ],
        ensure_ascii=False,
    )

    verification = verify_translation_batch_result(
        ai_result=ai_result,
        batch=batch,
        text_rules=get_default_text_rules(),
    )

    assert verification.error_items == []
    assert verification.right_items == data.translation_items
    assert [item.translation_lines for item in verification.right_items] == [["你好"], ["您好"]]


@pytest.mark.parametrize(
    ("response_items", "expected_code"),
    [
        (
            [
                {"id": "T999999", "translation_lines": ["你好"]},
                {"id": "T000002", "translation_lines": ["您好"]},
            ],
            "response_unknown_id",
        ),
        (
            [
                {"id": "T000001", "translation_lines": ["你好"]},
                {"id": "T000001", "translation_lines": ["您好"]},
            ],
            "response_duplicate_id",
        ),
        (
            [{"id": "T000001", "translation_lines": ["你好"]}],
            "response_missing_id",
        ),
    ],
)
def test_response_id_protocol_errors_block_whole_batch(
    response_items: list[dict[str, object]],
    expected_code: str,
) -> None:
    """未知、重复和缺失短 ID 都必须给出稳定协议错误并阻止整批保存。"""
    data = _build_data(item_count=2)
    batch = next(
        iter_translation_context_batches(
            translation_data=data,
            token_size=2000,
            factor=1.0,
            max_command_items=5,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
        )
    )

    verification = verify_translation_batch_result(
        ai_result=json.dumps(response_items, ensure_ascii=False),
        batch=batch,
        text_rules=get_default_text_rules(),
    )

    assert verification.right_items == []
    assert len(verification.error_items) == 2
    assert all(item.error_type == "模型返回不可解析" for item in verification.error_items)
    assert all(expected_code in "\n".join(item.error_detail) for item in verification.error_items)
