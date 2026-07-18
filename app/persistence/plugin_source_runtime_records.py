"""插件源码当前运行记录会话能力。"""

import json
from typing import cast

from app.rmmz.schema import (
    PluginSourceRuntimeMappingKind,
    PluginSourceRuntimeScanCacheRecord,
    PluginSourceRuntimeStringLiteralCacheRecord,
    PluginSourceRuntimeWriteMapRecord,
)
from app.rmmz.text_rules import coerce_json_value, ensure_json_array, ensure_json_object

from .rows import row_int, row_str
from .session_base import SessionMixinBase
from .sql import (
    DELETE_ALL_PLUGIN_SOURCE_RUNTIME_SCAN_CACHE,
    DELETE_ALL_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS,
    INSERT_PLUGIN_SOURCE_RUNTIME_SCAN_CACHE,
    INSERT_PLUGIN_SOURCE_RUNTIME_WRITE_MAP,
    SELECT_PLUGIN_SOURCE_RUNTIME_SCAN_CACHE,
    SELECT_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS,
)


class PluginSourceRuntimeRecordSessionMixin(SessionMixinBase):
    """负责插件源码当前运行写回映射的保存、读取与清理。"""

    async def replace_plugin_source_runtime_write_maps(
        self,
        records: list[PluginSourceRuntimeWriteMapRecord],
    ) -> None:
        """替换插件源码写回后可用于确定性反推的映射记录。"""
        _ = await self.connection.execute(DELETE_ALL_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS)
        if records:
            _ = await self.connection.executemany(
                INSERT_PLUGIN_SOURCE_RUNTIME_WRITE_MAP,
                [
                    (
                        record.location_path,
                        record.mapping_kind,
                        record.source_file_name,
                        record.source_selector,
                        record.source_file_hash,
                        record.source_text_hash,
                        record.translation_lines_hash,
                        record.runtime_file_name,
                        record.runtime_selector,
                        record.runtime_file_hash,
                        record.runtime_text_hash,
                        record.runtime_line,
                        record.created_at,
                    )
                    for record in records
                ],
            )
        await self.connection.commit()

    async def read_plugin_source_runtime_write_maps(self) -> list[PluginSourceRuntimeWriteMapRecord]:
        """读取全部插件源码当前运行写回映射。"""
        async with self.connection.execute(SELECT_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS) as cursor:
            rows = await cursor.fetchall()
        return [
            PluginSourceRuntimeWriteMapRecord(
                location_path=row_str(row, "location_path", self.db_path),
                mapping_kind=cast(
                    PluginSourceRuntimeMappingKind,
                    row_str(row, "mapping_kind", self.db_path),
                ),
                source_file_name=row_str(row, "source_file_name", self.db_path),
                source_selector=row_str(row, "source_selector", self.db_path),
                source_file_hash=row_str(row, "source_file_hash", self.db_path),
                source_text_hash=row_str(row, "source_text_hash", self.db_path),
                translation_lines_hash=row_str(row, "translation_lines_hash", self.db_path),
                runtime_file_name=row_str(row, "runtime_file_name", self.db_path),
                runtime_selector=row_str(row, "runtime_selector", self.db_path),
                runtime_file_hash=row_str(row, "runtime_file_hash", self.db_path),
                runtime_text_hash=row_str(row, "runtime_text_hash", self.db_path),
                runtime_line=row_int(row, "runtime_line", self.db_path),
                created_at=row_str(row, "created_at", self.db_path),
            )
            for row in rows
        ]

    async def clear_plugin_source_runtime_write_maps(self) -> None:
        """清空插件源码当前运行写回映射。"""
        _ = await self.connection.execute(DELETE_ALL_PLUGIN_SOURCE_RUNTIME_WRITE_MAPS)
        await self.connection.commit()

    async def replace_plugin_source_runtime_scan_cache(
        self,
        records: list[PluginSourceRuntimeScanCacheRecord],
    ) -> None:
        """替换当前运行插件源码 AST 扫描缓存。"""
        _ = await self.connection.execute(DELETE_ALL_PLUGIN_SOURCE_RUNTIME_SCAN_CACHE)
        if records:
            _ = await self.connection.executemany(
                INSERT_PLUGIN_SOURCE_RUNTIME_SCAN_CACHE,
                [
                    (
                        record.file_name,
                        record.file_hash,
                        record.syntax_error,
                        json.dumps(
                            [literal.model_dump() for literal in record.literals],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        record.created_at,
                    )
                    for record in records
                ],
            )
        await self.connection.commit()

    async def read_plugin_source_runtime_scan_cache(self) -> list[PluginSourceRuntimeScanCacheRecord]:
        """读取当前运行插件源码 AST 扫描缓存。"""
        async with self.connection.execute(SELECT_PLUGIN_SOURCE_RUNTIME_SCAN_CACHE) as cursor:
            rows = await cursor.fetchall()
        records: list[PluginSourceRuntimeScanCacheRecord] = []
        for row in rows:
            literals_json = row_str(row, "literals_json", self.db_path)
            try:
                raw_literals = ensure_json_array(
                    coerce_json_value(cast(object, json.loads(literals_json))),
                    "plugin_source_runtime_scan_cache.literals_json",
                )
                literals = [
                    PluginSourceRuntimeStringLiteralCacheRecord.model_validate(
                        ensure_json_object(raw_literal, "plugin_source_runtime_scan_cache.literal")
                    )
                    for raw_literal in raw_literals
                ]
            except Exception as error:
                raise RuntimeError(f"当前运行插件源码扫描缓存损坏，请重新执行当前运行审计: {self.db_path}") from error
            records.append(
                PluginSourceRuntimeScanCacheRecord(
                    file_name=row_str(row, "file_name", self.db_path),
                    file_hash=row_str(row, "file_hash", self.db_path),
                    syntax_error=row_str(row, "syntax_error", self.db_path),
                    literals=literals,
                    created_at=row_str(row, "created_at", self.db_path),
                )
            )
        return records

    async def clear_plugin_source_runtime_scan_cache(self) -> None:
        """清空当前运行插件源码 AST 扫描缓存。"""
        _ = await self.connection.execute(DELETE_ALL_PLUGIN_SOURCE_RUNTIME_SCAN_CACHE)
        await self.connection.commit()


__all__ = ["PluginSourceRuntimeRecordSessionMixin"]
