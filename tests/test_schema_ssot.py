"""SQLite 唯一 DDL 和完整结构签名测试。"""

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from app.persistence import DatabaseMigrationRequiredError, GameRegistry
from app.persistence.schema_loader import load_current_schema_sql, load_current_schema_table_names
from app.persistence.sql import EXPECTED_STATIC_TABLE_NAMES


def test_expected_table_names_are_derived_from_current_schema() -> None:
    """预期表集合必须由 current.sql 实际执行结果派生。"""
    with sqlite3.connect(":memory:") as connection:
        _ = connection.executescript(load_current_schema_sql())
        rows = cast(
            list[tuple[str]],
            connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall(),
        )
        actual_names = tuple(row[0] for row in rows)

    assert load_current_schema_table_names() == actual_names
    assert EXPECTED_STATIC_TABLE_NAMES == actual_names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_schema_sql",
    [
        "CREATE VIEW undeclared_view AS SELECT game_title FROM metadata",
        """
        CREATE TRIGGER undeclared_trigger
        AFTER INSERT ON text_glossary_terms
        BEGIN
            SELECT NEW.source_text;
        END
        """,
        "CREATE INDEX undeclared_index ON metadata (game_title)",
    ],
    ids=["view", "trigger", "index"],
)
async def test_open_game_rejects_undeclared_schema_objects(
    minimal_game_dir: Path,
    tmp_path: Path,
    extra_schema_sql: str,
) -> None:
    """额外 view、trigger 或 index 都不能绕过完整 schema 签名。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    with sqlite3.connect(record.db_path) as connection:
        _ = connection.executescript(extra_schema_sql)

    with pytest.raises(DatabaseMigrationRequiredError, match="数据库结构不符合当前版本") as raised:
        _ = await registry.open_game(record.game_title)
    assert raised.value.code == "database_migration_required"
    assert raised.value.details["actual_version"] == 12
