"""当前 SQLite schema 唯一事实源加载器。"""

import sqlite3
from functools import cache
from pathlib import Path
from typing import cast

CURRENT_SCHEMA_PATH = Path(__file__).with_name("schema") / "current.sql"


def load_current_schema_sql() -> str:
    """读取随程序发布的当前 schema DDL。"""
    return CURRENT_SCHEMA_PATH.read_text(encoding="utf-8")


@cache
def load_current_schema_table_names() -> tuple[str, ...]:
    """执行唯一 DDL，并从 SQLite 自身缓存当前业务表集合。"""
    connection = sqlite3.connect(":memory:")
    try:
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
    finally:
        connection.close()
    return tuple(row[0] for row in rows)


__all__ = ["CURRENT_SCHEMA_PATH", "load_current_schema_sql", "load_current_schema_table_names"]
