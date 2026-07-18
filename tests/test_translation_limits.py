"""正文翻译运行上限的上下文组选择测试。"""

from app.application.use_cases.translation_run import limit_translation_data
from app.rmmz.schema import TranslationData, TranslationItem
from app.translation import TranslationCache


def test_max_items_never_splits_same_context_group() -> None:
    items = [
        TranslationItem(
            location_path=f"Map001.json/1/{index}",
            item_type="short_text",
            original_lines=[text],
        )
        for index, text in enumerate(["前一", "前二", "同文", "前一", "前二", "同文", "前一", "前二"])
    ]
    data_map = {"Map001.json": TranslationData(display_name="场景", translation_items=items)}
    cache = TranslationCache()
    cache.prepare(
        translation_data_map=data_map,
        terminology_prompt_index=None,
        source_language="ja",
    )
    duplicate_only_map = {
        "Map001.json": TranslationData(
            display_name="场景",
            translation_items=[items[2], items[5]],
        )
    }

    limited = limit_translation_data(
        translation_data_map=duplicate_only_map,
        max_items=1,
        translation_cache=cache,
    )

    assert limited == {}


def test_max_items_selects_whole_context_group_when_it_fits() -> None:
    items = [
        TranslationItem(
            location_path=f"Map001.json/1/{index}",
            item_type="short_text",
            original_lines=[text],
        )
        for index, text in enumerate(["前一", "前二", "同文", "前一", "前二", "同文", "前一", "前二"])
    ]
    data_map = {"Map001.json": TranslationData(display_name="场景", translation_items=items)}
    cache = TranslationCache()
    cache.prepare(
        translation_data_map=data_map,
        terminology_prompt_index=None,
        source_language="ja",
    )
    duplicate_only_map = {
        "Map001.json": TranslationData(
            display_name="场景",
            translation_items=[items[2], items[5]],
        )
    }

    limited = limit_translation_data(
        translation_data_map=duplicate_only_map,
        max_items=2,
        translation_cache=cache,
    )

    assert limited["Map001.json"].translation_items == [items[2], items[5]]
