"""主翻译表读写会话能力。"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import aiosqlite

from app.rmmz.schema import TranslationItem

from .records import TranslationReuseContext, TranslationReuseRecord
from .rows import decode_string_list, row_item_type, row_optional_str, row_str
from .session_base import SessionMixinBase
from .sql import (
    DELETE_TRANSLATION_ITEM_BY_PATH,
    DELETE_TRANSLATION_ITEMS_BY_PREFIX,
    INSERT_TRANSLATION,
    SELECT_TRANSLATED_ITEM_BY_PATH,
    SELECT_TRANSLATED_ITEMS,
    SELECT_TRANSLATED_ITEMS_BY_PREFIX,
    SELECT_TRANSLATION_PATHS,
    TRANSLATION_TABLE_NAME,
)

_TRANSLATION_REUSE_COLUMNS = """
    location_path, item_type, role, original_lines, source_line_paths, translation_lines,
    context_key_json, context_key_hash, source_fingerprint, rule_fingerprint,
    terminology_fingerprint, language_fingerprint, prompt_protocol_version
"""
_CONTEXT_QUERY_CHUNK_SIZE = 500


class TranslationRecordSessionMixin(SessionMixinBase):
    """负责已保存译文记录的读写与清理。"""

    async def write_translation_items(
        self,
        items: Sequence[TranslationItem],
        reuse_contexts_by_path: Mapping[str, TranslationReuseContext] | None = None,
    ) -> None:
        """批量写入已完成译文到主翻译表。"""
        contexts_by_path = reuse_contexts_by_path or {}
        item_paths = {item.location_path for item in items}
        unknown_context_paths = sorted(set(contexts_by_path) - item_paths)
        if unknown_context_paths:
            raise ValueError(f"复用上下文包含本批次之外的位置: {unknown_context_paths[0]}")
        if items:
            serialized_items = [
                serialize_translation_item(
                    translation_item,
                    contexts_by_path.get(translation_item.location_path),
                )
                for translation_item in items
            ]
            _ = await self.connection.executemany(INSERT_TRANSLATION, serialized_items)
        await self.connection.commit()

    async def read_translation_location_paths(self) -> set[str]:
        """读取主翻译表中的全部已完成路径。"""
        async with self.connection.execute(SELECT_TRANSLATION_PATHS) as cursor:
            rows = await cursor.fetchall()
        return {row_str(row, "location_path", self.db_path) for row in rows}

    async def read_translated_items(self) -> list[TranslationItem]:
        """读取主翻译表中的全部正文译文。"""
        async with self.connection.execute(SELECT_TRANSLATED_ITEMS) as cursor:
            rows = await cursor.fetchall()

        return [self._translation_item_from_row(row) for row in rows]

    async def read_reusable_translations_by_context_keys(
        self,
        contexts: Sequence[TranslationReuseContext],
    ) -> list[TranslationReuseRecord]:
        """按完整持久化上下文读取全部历史候选，保留冲突供调用方处理。"""
        if not contexts:
            return []
        expected_contexts = set(contexts)
        for context in expected_contexts:
            validate_translation_reuse_context(context)
        context_hashes = sorted({context.context_key_hash for context in expected_contexts})
        records: list[TranslationReuseRecord] = []
        for start in range(0, len(context_hashes), _CONTEXT_QUERY_CHUNK_SIZE):
            chunk = context_hashes[start : start + _CONTEXT_QUERY_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            async with self.connection.execute(
                f"""
                SELECT {_TRANSLATION_REUSE_COLUMNS}
                FROM [{TRANSLATION_TABLE_NAME}]
                WHERE context_key_hash IN ({placeholders})
                ORDER BY context_key_hash, location_path
                """,
                tuple(chunk),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                context = translation_reuse_context_from_row(row=row, db_path=self.db_path)
                if context is None or context not in expected_contexts:
                    continue
                records.append(
                    TranslationReuseRecord(
                        translation_item=self._translation_item_from_row(row),
                        context=context,
                    )
                )
        return records

    async def read_translated_items_by_prefixes(
        self,
        prefixes: Sequence[str],
    ) -> list[TranslationItem]:
        """按路径前缀读取即将受规则变更影响的译文记录。"""
        items_by_path: dict[str, TranslationItem] = {}
        for prefix in prefixes:
            async with self.connection.execute(
                SELECT_TRANSLATED_ITEMS_BY_PREFIX,
                (f"{prefix}%",),
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                item = self._translation_item_from_row(row)
                items_by_path[item.location_path] = item
        return [items_by_path[path] for path in sorted(items_by_path)]

    async def read_translated_items_by_paths(
        self,
        location_paths: Sequence[str],
    ) -> list[TranslationItem]:
        """按精确定位路径读取即将受规则变更影响的译文记录。"""
        items_by_path: dict[str, TranslationItem] = {}
        for location_path in location_paths:
            async with self.connection.execute(
                SELECT_TRANSLATED_ITEM_BY_PATH,
                (location_path,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                continue
            item = self._translation_item_from_row(row)
            items_by_path[item.location_path] = item
        return [items_by_path[path] for path in sorted(items_by_path)]

    async def delete_translation_items_by_prefixes(self, prefixes: list[str]) -> int:
        """按路径前缀批量删除主翻译表中的记录。"""
        deleted_rows = 0
        for prefix in prefixes:
            cursor = await self.connection.execute(
                DELETE_TRANSLATION_ITEMS_BY_PREFIX,
                (f"{prefix}%",),
            )
            if cursor.rowcount > 0:
                deleted_rows += cursor.rowcount
        await self.connection.commit()
        return deleted_rows

    async def delete_translation_items_except_paths(
        self,
        allowed_paths: set[str],
    ) -> int:
        """删除当前提取规则之外的主翻译表记录。"""
        async with self.connection.execute(SELECT_TRANSLATION_PATHS) as cursor:
            rows = await cursor.fetchall()

        stored_paths = {row_str(row, "location_path", self.db_path) for row in rows}
        stale_paths = sorted(stored_paths - allowed_paths)
        if not stale_paths:
            return 0

        _ = await self.connection.executemany(
            DELETE_TRANSLATION_ITEM_BY_PATH,
            [(path,) for path in stale_paths],
        )
        await self.connection.commit()
        return len(stale_paths)

    async def delete_translation_items_by_paths(
        self,
        location_paths: Sequence[str],
    ) -> int:
        """按精确定位路径批量删除主翻译表记录。"""
        deleted_rows = 0
        for location_path in location_paths:
            cursor = await self.connection.execute(
                DELETE_TRANSLATION_ITEM_BY_PATH,
                (location_path,),
            )
            if cursor.rowcount > 0:
                deleted_rows += cursor.rowcount
        await self.connection.commit()
        return deleted_rows

    def _translation_item_from_row(self, row: aiosqlite.Row) -> TranslationItem:
        """把数据库行还原为已保存译文对象。"""
        original_lines = decode_string_list(row_str(row, "original_lines", self.db_path), "original_lines")
        source_line_paths = decode_string_list(
            row_str(row, "source_line_paths", self.db_path),
            "source_line_paths",
        )
        translation_lines = decode_string_list(
            row_str(row, "translation_lines", self.db_path),
            "translation_lines",
        )
        return TranslationItem(
            location_path=row_str(row, "location_path", self.db_path),
            item_type=row_item_type(row, "item_type", self.db_path),
            role=row_optional_str(row, "role", self.db_path),
            original_lines=original_lines,
            source_line_paths=source_line_paths,
            translation_lines=translation_lines,
        )


def serialize_translation_item(
    item: TranslationItem,
    context: TranslationReuseContext | None,
) -> tuple[object, ...]:
    """把译文及可选复用上下文序列化为统一 INSERT 参数。"""
    context_values: tuple[object, ...]
    if context is None:
        context_values = (None, None, None, None, None, None, None)
    else:
        validate_translation_reuse_context(context)
        context_values = (
            context.context_key_json,
            context.context_key_hash,
            context.source_fingerprint,
            context.rule_fingerprint,
            context.terminology_fingerprint,
            context.language_fingerprint,
            context.prompt_protocol_version,
        )
    return (
        item.location_path,
        item.item_type,
        item.role,
        json.dumps(item.original_lines, ensure_ascii=False),
        json.dumps(item.source_line_paths, ensure_ascii=False),
        json.dumps(item.translation_lines, ensure_ascii=False),
        *context_values,
    )


def validate_translation_reuse_context(context: TranslationReuseContext) -> None:
    """拒绝非 canonical JSON、错误哈希和不完整指纹。"""
    try:
        decoded_key = cast(object, json.loads(context.context_key_json))
    except json.JSONDecodeError as error:
        raise ValueError("translation context key 不是有效 JSON") from error
    if not isinstance(decoded_key, dict):
        raise ValueError("translation context key 必须是 JSON 对象")
    canonical_json = json.dumps(
        decoded_key,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if canonical_json != context.context_key_json:
        raise ValueError("translation context key 必须使用 canonical JSON")
    expected_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    if context.context_key_hash != expected_hash:
        raise ValueError("translation context key hash 与 canonical JSON 不一致")
    fingerprints = {
        "source_fingerprint": context.source_fingerprint,
        "rule_fingerprint": context.rule_fingerprint,
        "terminology_fingerprint": context.terminology_fingerprint,
        "language_fingerprint": context.language_fingerprint,
        "prompt_protocol_version": context.prompt_protocol_version,
    }
    for field_name, value in fingerprints.items():
        if not value.strip():
            raise ValueError(f"translation reuse context 缺少 {field_name}")


def translation_reuse_context_from_row(
    *,
    row: aiosqlite.Row,
    db_path: Path,
) -> TranslationReuseContext | None:
    """从数据库行严格恢复成组可空的译文复用上下文。"""
    field_names = (
        "context_key_json",
        "context_key_hash",
        "source_fingerprint",
        "rule_fingerprint",
        "terminology_fingerprint",
        "language_fingerprint",
        "prompt_protocol_version",
    )
    values = tuple(row_optional_str(row, field_name, db_path) for field_name in field_names)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise RuntimeError(f"数据库译文复用上下文字段不完整: {db_path}")
    context = TranslationReuseContext(
        context_key_json=cast(str, values[0]),
        context_key_hash=cast(str, values[1]),
        source_fingerprint=cast(str, values[2]),
        rule_fingerprint=cast(str, values[3]),
        terminology_fingerprint=cast(str, values[4]),
        language_fingerprint=cast(str, values[5]),
        prompt_protocol_version=cast(str, values[6]),
    )
    try:
        validate_translation_reuse_context(context)
    except ValueError as error:
        raise RuntimeError(f"数据库译文复用上下文非法: {db_path}") from error
    return context


__all__ = [
    "TranslationRecordSessionMixin",
    "serialize_translation_item",
    "translation_reuse_context_from_row",
    "validate_translation_reuse_context",
]
