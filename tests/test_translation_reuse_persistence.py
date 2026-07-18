"""上下文安全译文复用的持久化契约测试。"""

import hashlib
import json
from pathlib import Path

import pytest

from app.persistence import GameRegistry, TranslationReuseContext
from app.rmmz.schema import TranslationItem


def _reuse_context(*, source_fingerprint: str = "source-v1") -> TranslationReuseContext:
    key_json = json.dumps(
        {
            "item_type": "short_text",
            "original_lines": ["同じ原文"],
            "owner": "Map001",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return TranslationReuseContext(
        context_key_json=key_json,
        context_key_hash=hashlib.sha256(key_json.encode("utf-8")).hexdigest(),
        source_fingerprint=source_fingerprint,
        rule_fingerprint="rules-v1",
        terminology_fingerprint="terms-v1",
        language_fingerprint="ja+en-to-zh-Hans",
        prompt_protocol_version="translation-json-v2",
    )


def _translation_item(*, location_path: str, translation: str) -> TranslationItem:
    return TranslationItem(
        location_path=location_path,
        item_type="short_text",
        role=None,
        original_lines=["同じ原文"],
        source_line_paths=[f"{location_path}/source"],
        translation_lines=[translation],
    )


async def test_reuse_query_returns_all_exact_context_candidates_and_preserves_conflicts(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """相同完整键的不同历史译文必须全部返回，不能任选其一。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(
        minimal_game_dir,
        source_language="ja",
        additional_source_languages=("en",),
    )
    context = _reuse_context()
    left = _translation_item(location_path="Map001.json/1", translation="译文一")
    right = _translation_item(location_path="Map001.json/2", translation="译文二")

    async with await registry.open_game(record.game_title) as session:
        await session.write_translation_items(
            [left, right],
            reuse_contexts_by_path={
                left.location_path: context,
                right.location_path: context,
            },
        )

        candidates = await session.read_reusable_translations_by_context_keys([context])

    assert [candidate.translation_item.translation_lines for candidate in candidates] == [
        ["译文一"],
        ["译文二"],
    ]
    assert all(candidate.context == context for candidate in candidates)


async def test_reuse_query_rejects_changed_fingerprint_and_contextless_rows(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """源指纹变化或缺失持久化上下文时，旧结果必须自动失去复用资格。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    context = _reuse_context()
    reusable = _translation_item(location_path="Map001.json/1", translation="可复用")
    contextless = _translation_item(location_path="Map001.json/2", translation="仅当前位置")

    async with await registry.open_game(record.game_title) as session:
        await session.write_translation_items(
            [reusable],
            reuse_contexts_by_path={reusable.location_path: context},
        )
        await session.write_translation_items([contextless])

        stale_candidates = await session.read_reusable_translations_by_context_keys(
            [_reuse_context(source_fingerprint="source-v2")]
        )
        current_candidates = await session.read_reusable_translations_by_context_keys([context])

    assert stale_candidates == []
    assert [candidate.translation_item.location_path for candidate in current_candidates] == [reusable.location_path]


async def test_reuse_write_rejects_noncanonical_or_mismatched_context_key(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """持久层必须验证 canonical JSON 与 SHA256，不能接受伪造键。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    item = _translation_item(location_path="Map001.json/1", translation="译文")
    invalid_context = TranslationReuseContext(
        context_key_json='{ "b": 2, "a": 1 }',
        context_key_hash="0" * 64,
        source_fingerprint="source-v1",
        rule_fingerprint="rules-v1",
        terminology_fingerprint="terms-v1",
        language_fingerprint="ja-to-zh-Hans",
        prompt_protocol_version="translation-json-v2",
    )

    async with await registry.open_game(record.game_title) as session:
        with pytest.raises(ValueError, match="canonical JSON"):
            await session.write_translation_items(
                [item],
                reuse_contexts_by_path={item.location_path: invalid_context},
            )
        assert await session.read_translated_items() == []
