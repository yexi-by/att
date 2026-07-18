"""翻译去重记录与提示词组装测试。"""

from app.rmmz.control_codes import REAL_LINE_BREAK_PLACEHOLDER
from app.rmmz.schema import TranslationData, TranslationItem
from app.rmmz.text_rules import get_default_text_rules
from app.translation import TranslationCache, iter_translation_context_batches


def test_translation_cache_deduplicates_and_expands_items() -> None:
    """只有完整上下文一致的同轮重复正文才只送模一次。"""
    cache = TranslationCache()
    items = [
        TranslationItem(
            location_path=f"Map001.json/1/{index}",
            item_type="short_text",
            original_lines=[text],
            source_line_paths=[f"Map001.json/events/1/list/{index}/parameters/0"],
        )
        for index, text in enumerate(["前一", "前二", "こんにちは", "前一", "前二", "こんにちは", "前一", "前二"])
    ]
    first = items[2]
    duplicate = items[5]
    cache.prepare(
        translation_data_map={
            "Map001.json": TranslationData(
                display_name="同一场景",
                translation_items=items,
            )
        },
        terminology_prompt_index=None,
        source_language="ja",
    )

    assert cache.remember_or_defer(first)
    assert not cache.remember_or_defer(duplicate)
    assert cache.pop_duplicate_items(first) == [duplicate]


def test_translation_context_key_is_canonical_and_hides_target_paths() -> None:
    """持久化上下文键必须稳定，且不能把目标位置当作语义上下文。"""
    cache = TranslationCache()
    item = TranslationItem(
        location_path="Map001.json/1/2",
        item_type="short_text",
        original_lines=["こんにちは"],
        source_line_paths=["Map001.json/events/1/list/2/parameters/0"],
    )
    cache.prepare(
        translation_data_map={
            "Map001.json": TranslationData(
                display_name="场景",
                translation_items=[item],
            )
        },
        terminology_prompt_index=None,
        source_language="ja",
    )

    key = cache.build_cache_key(item)
    assert key is not None
    assert key.canonical_hash() == key.canonical_hash()
    assert "Map001.json/1/2" not in key.canonical_json()
    assert "events/1/list/2" not in key.canonical_json()


def test_translation_cache_without_prepared_context_never_deduplicates() -> None:
    """缺少场景、邻居和术语上下文时保守地不复用。"""
    cache = TranslationCache()
    first = TranslationItem(location_path="A/1", item_type="short_text", original_lines=["同文"])
    duplicate = TranslationItem(location_path="A/2", item_type="short_text", original_lines=["同文"])

    assert cache.remember_or_defer(first)
    assert cache.remember_or_defer(duplicate)


def test_translation_cache_missing_required_scope_fact_never_deduplicates() -> None:
    """范围索引缺少来源域或规则 owner 时，不得用路径猜测补齐后复用。"""
    first = TranslationItem(location_path="Map001.json/1/0", item_type="short_text", original_lines=["同文"])
    duplicate = TranslationItem(location_path="Map001.json/1/1", item_type="short_text", original_lines=["同文"])
    items = [first, duplicate]

    for data, source_domains, rule_sources in (
        (
            TranslationData(display_name="场景", translation_items=items),
            {first.location_path: "event_command"},
            {item.location_path: "event_rule" for item in items},
        ),
        (
            TranslationData(display_name="场景", translation_items=items),
            {item.location_path: "event_command" for item in items},
            {first.location_path: "event_rule"},
        ),
    ):
        cache = TranslationCache()
        cache.prepare(
            translation_data_map={"Map001.json": data},
            terminology_prompt_index=None,
            source_language="ja",
            source_domains_by_path=source_domains,
            rule_sources_by_path=rule_sources,
        )

        assert cache.remember_or_defer(first)
        assert cache.remember_or_defer(duplicate)


def test_translation_cache_does_not_merge_different_owners() -> None:
    """原文和邻居相同但 owner 不同时仍必须分别请求模型。"""
    cache = TranslationCache()
    items = [
        TranslationItem(
            location_path=f"Map001.json/{owner}/{index}",
            item_type="short_text",
            original_lines=[text],
        )
        for owner, index, text in [
            ("1", 0, "前一"),
            ("1", 1, "前二"),
            ("1", 2, "同文"),
            ("2", 0, "前一"),
            ("2", 1, "前二"),
            ("2", 2, "同文"),
            ("2", 3, "前一"),
            ("2", 4, "前二"),
        ]
    ]
    cache.prepare(
        translation_data_map={"Map001.json": TranslationData(display_name="场景", translation_items=items)},
        terminology_prompt_index=None,
        source_language="ja",
    )

    assert cache.remember_or_defer(items[2])
    assert cache.remember_or_defer(items[5])


def test_translation_cache_separates_real_map_event_owners() -> None:
    """Map 事件路径必须按事件 owner 分组，不能退化为共同的 events 前缀。"""
    items = [
        TranslationItem(
            location_path=f"Map001.json/events/{event_id}/pages/0/list/{index}",
            item_type="short_text",
            original_lines=[text],
        )
        for event_id, index, text in [
            (1, 0, "前一"),
            (1, 1, "前二"),
            (1, 2, "同文"),
            (2, 0, "前一"),
            (2, 1, "前二"),
            (2, 2, "同文"),
            (2, 3, "后一"),
            (2, 4, "后二"),
        ]
    ]
    cache = TranslationCache()
    cache.prepare(
        translation_data_map={"Map001.json": TranslationData(display_name="场景", translation_items=items)},
        terminology_prompt_index=None,
        source_language="ja",
        source_domains_by_path={item.location_path: "event_command" for item in items},
    )

    assert cache.remember_or_defer(items[2])
    assert cache.remember_or_defer(items[5])


def test_translation_context_neighbors_stay_inside_owner_container() -> None:
    """逐项前后文只取同一 owner，不能跨事件或数据库对象串线。"""
    items = [
        TranslationItem(
            location_path=location_path,
            item_type="short_text",
            original_lines=[text],
        )
        for location_path, text in [
            ("Map001.json/events/1/pages/0/list/0", "事件一前文"),
            ("Map001.json/events/2/pages/0/list/0", "事件二文本"),
            ("Map001.json/events/1/pages/0/list/1", "事件一目标"),
            ("Map001.json/events/1/pages/0/list/2", "事件一后文"),
        ]
    ]
    cache = TranslationCache()
    cache.prepare(
        translation_data_map={
            "Map001.json": TranslationData(
                display_name="场景",
                translation_items=items,
            )
        },
        terminology_prompt_index=None,
        source_language="ja",
        source_domains_by_path={item.location_path: "event_command" for item in items},
    )

    context = cache.build_prompt_context(items[2])
    assert context is not None
    assert [neighbor.original_lines for neighbor in context.previous_items] == [("事件一前文",)]
    assert [neighbor.original_lines for neighbor in context.next_items] == [("事件一后文",)]


def test_translation_context_prompt_contains_map_and_body_without_terms() -> None:
    """未传入术语表索引时，提示词包含地图名与正文上下文。"""
    data = TranslationData(
        display_name="始まりの町",
        translation_items=[
            TranslationItem(
                location_path="Map001.json/1/0/0",
                item_type="long_text",
                role="村人",
                original_lines=["こんにちは"],
            )
        ],
    )

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=1000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
        )
    )
    user_prompt = batches[0].messages[1].text

    assert "术语" not in user_prompt
    assert "源语言" not in user_prompt
    assert "[建议换行数]" not in user_prompt
    assert "[[地图名]]" not in user_prompt
    assert "[[需要翻译的正文]]" not in user_prompt
    assert "# 场景" in user_prompt
    assert "# 正文" in user_prompt
    assert "## 1" in user_prompt
    assert "地图：始まりの町" in user_prompt
    assert "id: T000001" in user_prompt
    assert "Map001.json" not in user_prompt
    assert "type: long_text" in user_prompt
    assert "role: 村人" in user_prompt
    assert "こんにちは" in user_prompt


def test_translation_context_keeps_array_output_line_count_hint() -> None:
    """选项数组仍然向模型提供严格输出行数。"""
    data = TranslationData(
        display_name=None,
        translation_items=[
            TranslationItem(
                location_path="Map001.json/1/0/2",
                item_type="array",
                original_lines=["はい", "いいえ"],
            )
        ],
    )

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=1000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
        )
    )
    user_prompt = batches[0].messages[1].text

    assert "line_count: 2" in user_prompt


def test_short_text_real_line_break_is_hidden_from_prompt() -> None:
    """单字段文本送模前必须把真实换行替换为文本标记。"""
    data = TranslationData(
        display_name=None,
        translation_items=[
            TranslationItem(
                location_path="Items.json/1/description",
                item_type="short_text",
                original_lines=["武器スキル\n\\C[14]敵単体"],
            )
        ],
    )

    batches = list(
        iter_translation_context_batches(
            translation_data=data,
            token_size=1000,
            factor=1.0,
            max_command_items=3,
            system_prompt="系统提示",
            text_rules=get_default_text_rules(),
        )
    )
    user_prompt = batches[0].messages[1].text

    assert f"武器スキル{REAL_LINE_BREAK_PLACEHOLDER}[RMMZ_TEXT_COLOR_14]敵単体" in user_prompt
    assert "武器スキル\n[RMMZ_TEXT_COLOR_14]" not in user_prompt
