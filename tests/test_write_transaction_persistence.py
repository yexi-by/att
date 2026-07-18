"""文件写事务数据库状态机测试。"""

from __future__ import annotations

from pathlib import Path
from typing import override

import aiosqlite
import pytest

from app.persistence.records import (
    WriteTransactionFileRecord,
    WriteTransactionPayload,
    WriteTransactionRecord,
)
from app.persistence.write_transaction_records import WriteTransactionSessionMixin
from app.rmmz.schema import FontReplacementRecord, PluginSourceRuntimeWriteMapRecord


class WriteTransactionTestSession(WriteTransactionSessionMixin):
    """只装配写事务能力的最小测试会话。"""

    def __init__(self, *, connection: aiosqlite.Connection, db_path: Path) -> None:
        self.connection: aiosqlite.Connection = connection
        self._db_path: Path = db_path

    @property
    @override
    def db_path(self) -> Path:
        """返回测试数据库路径。"""
        return self._db_path


async def open_write_transaction_session(tmp_path: Path) -> WriteTransactionTestSession:
    """按当前正式 schema 创建测试会话。"""
    db_path = tmp_path / "write-transactions.db"
    connection = await aiosqlite.connect(db_path)
    connection.row_factory = aiosqlite.Row
    schema_path = Path(__file__).parents[1] / "app" / "persistence" / "schema" / "current.sql"
    _ = await connection.executescript(schema_path.read_text(encoding="utf-8"))
    await connection.commit()
    return WriteTransactionTestSession(connection=connection, db_path=db_path)


def build_transaction_record(tmp_path: Path, transaction_id: str) -> WriteTransactionRecord:
    """构造 preparing 写事务记录。"""
    return WriteTransactionRecord(
        transaction_id=transaction_id,
        operation="write_back",
        game_path=tmp_path / "game",
        state="preparing",
        journal_path=tmp_path / "game" / ".att-mz-write-transactions" / f"{transaction_id}.json",
        payload=None,
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
        error="",
    )


async def mark_empty_transaction_prepared(
    session: WriteTransactionTestSession,
    transaction_id: str,
) -> None:
    """为数据库原子提交测试保存空文件清单。"""
    await session.mark_write_transaction_prepared(
        transaction_id,
        WriteTransactionPayload(version=1, database_committed=False, files=()),
    )


def build_runtime_map(location_path: str) -> PluginSourceRuntimeWriteMapRecord:
    """构造插件运行映射。"""
    return PluginSourceRuntimeWriteMapRecord(
        location_path=location_path,
        mapping_kind="translated",
        source_file_name="Plugin.js",
        source_selector="$.source",
        source_file_hash="source-file",
        source_text_hash="source-text",
        translation_lines_hash="translation",
        runtime_file_name="Plugin.js",
        runtime_selector="$.runtime",
        runtime_file_hash="runtime-file",
        runtime_text_hash="runtime-text",
        runtime_line=1,
        created_at="2026-07-18T00:00:00+00:00",
    )


def build_font_record(file_name: str) -> FontReplacementRecord:
    """构造字体替换诊断记录。"""
    return FontReplacementRecord(
        file_name=file_name,
        value_path="$.fontFace",
        original_text="GameFont",
        replaced_text="ReplacementFont",
        replacement_font_name="Replacement.ttf",
    )


@pytest.mark.asyncio
async def test_write_transaction_commit_atomically_persists_diagnostics(tmp_path: Path) -> None:
    """runtime map、字体记录和 committed 状态必须一次提交。"""
    session = await open_write_transaction_session(tmp_path)
    try:
        record = build_transaction_record(tmp_path, "txatomic")
        await session.create_write_transaction(record)
        await mark_empty_transaction_prepared(session, "txatomic")

        unfinished = await session.read_unfinished_write_transactions()
        assert [item.transaction_id for item in unfinished] == ["txatomic"]
        await session.finalize_write_transaction_commit(
            transaction_id="txatomic",
            runtime_maps=[build_runtime_map("plugins.Plugin.source")],
            font_records=[build_font_record("System.json")],
        )

        committed = await session.read_write_transaction("txatomic")
        assert committed is not None
        assert committed.state == "committed"
        assert committed.payload is not None
        assert committed.payload.database_committed
        async with session.connection.execute("SELECT COUNT(*) FROM plugin_source_runtime_write_map") as cursor:
            runtime_count_row = await cursor.fetchone()
            assert runtime_count_row is not None
            assert runtime_count_row[0] == 1
        async with session.connection.execute("SELECT COUNT(*) FROM font_replacement_records") as cursor:
            font_count_row = await cursor.fetchone()
            assert font_count_row is not None
            assert font_count_row[0] == 1

        await session.mark_write_transaction_finalized("txatomic")
        assert await session.read_unfinished_write_transactions() == []
    finally:
        await session.connection.close()


@pytest.mark.asyncio
async def test_write_transaction_diagnostic_failure_rolls_back_entire_database_commit(
    tmp_path: Path,
) -> None:
    """任一诊断记录失败时，不得留下清空后的表或 committed 状态。"""
    session = await open_write_transaction_session(tmp_path)
    try:
        first = build_transaction_record(tmp_path, "txfirst")
        await session.create_write_transaction(first)
        await mark_empty_transaction_prepared(session, "txfirst")
        await session.finalize_write_transaction_commit(
            transaction_id="txfirst",
            runtime_maps=[build_runtime_map("old")],
            font_records=[build_font_record("old.json")],
        )
        await session.mark_write_transaction_finalized("txfirst")

        second = build_transaction_record(tmp_path, "txsecond")
        await session.create_write_transaction(second)
        await mark_empty_transaction_prepared(session, "txsecond")
        _ = await session.connection.execute(
            """
            CREATE TRIGGER reject_injected_runtime_map
            BEFORE INSERT ON plugin_source_runtime_write_map
            WHEN NEW.location_path = 'reject'
            BEGIN
                SELECT RAISE(FAIL, 'injected runtime map failure');
            END
            """
        )
        await session.connection.commit()

        with pytest.raises(aiosqlite.IntegrityError, match="injected"):
            await session.finalize_write_transaction_commit(
                transaction_id="txsecond",
                runtime_maps=[build_runtime_map("reject")],
                font_records=[build_font_record("new.json")],
            )

        pending = await session.read_write_transaction("txsecond")
        assert pending is not None
        assert pending.state == "prepared"
        assert pending.payload is not None
        assert not pending.payload.database_committed
        async with session.connection.execute("SELECT location_path FROM plugin_source_runtime_write_map") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["old"]
        async with session.connection.execute("SELECT file_name FROM font_replacement_records") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["old.json"]
    finally:
        await session.connection.close()


@pytest.mark.asyncio
async def test_unfinished_write_transaction_blocks_second_modification(tmp_path: Path) -> None:
    """唯一未完成索引和应用层 gate 都必须阻止叠加修改。"""
    session = await open_write_transaction_session(tmp_path)
    try:
        await session.create_write_transaction(build_transaction_record(tmp_path, "txone"))
        with pytest.raises(RuntimeError, match="recover-write-transaction"):
            await session.assert_no_unfinished_write_transaction()
        with pytest.raises(RuntimeError, match="recover-write-transaction"):
            await session.create_write_transaction(build_transaction_record(tmp_path, "txtwo"))

        await session.mark_write_transaction_rolled_back("txone")
        await session.create_write_transaction(build_transaction_record(tmp_path, "txtwo"))
    finally:
        await session.connection.close()


@pytest.mark.asyncio
async def test_prepared_transaction_roundtrips_strict_file_manifest(tmp_path: Path) -> None:
    """暂存后的原文件与目标哈希必须完整留在数据库。"""
    session = await open_write_transaction_session(tmp_path)
    try:
        await session.create_write_transaction(build_transaction_record(tmp_path, "txmanifest"))
        payload = WriteTransactionPayload(
            version=1,
            database_committed=False,
            files=(
                WriteTransactionFileRecord(
                    target_relative_path="data/Actors.json",
                    staged_relative_path="data/.Actors.json.att-mz-write-txmanifest.stage",
                    backup_relative_path="data/.Actors.json.att-mz-write-txmanifest.backup",
                    existed_before=True,
                    original_sha256="1" * 64,
                    target_sha256="2" * 64,
                ),
            ),
        )

        await session.mark_write_transaction_prepared("txmanifest", payload)

        loaded = await session.read_write_transaction("txmanifest")
        assert loaded is not None
        assert loaded.state == "prepared"
        assert loaded.payload == payload
    finally:
        await session.connection.close()
