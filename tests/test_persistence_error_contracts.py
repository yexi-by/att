"""持久化业务错误的结构化 CLI 契约测试。"""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from pytest import CaptureFixture, MonkeyPatch

from app.persistence import (
    DatabaseMigrationRequiredError,
    GameRegistrationConflictError,
    GameRegistry,
    RecoveryRequiredError,
    WriteTransactionRecord,
)
from app.rmmz.text_rules import coerce_json_value, ensure_json_array, ensure_json_object
from main import main


async def test_missing_schema_is_explicit_migration_required(tmp_path: Path) -> None:
    """缺少 schema_version 的旧库不得退化成普通结构错误。"""
    db_directory = tmp_path / "db"
    db_directory.mkdir()
    db_path = db_directory / "Legacy.db"
    with sqlite3.connect(db_path) as connection:
        _ = connection.execute("CREATE TABLE legacy_data (value TEXT)")

    with pytest.raises(DatabaseMigrationRequiredError) as raised:
        _ = await GameRegistry(db_directory).open_game("Legacy")

    assert raised.value.code == "database_migration_required"
    assert raised.value.details["db_path"] == str(db_path)
    assert raised.value.details["actual_version"] is None
    assert raised.value.details["required_version"] == 12


def test_cli_reports_v11_database_migration_required(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """list 遇到 v11 数据库时必须输出稳定 JSON code 和版本详情。"""
    db_directory = tmp_path / "db"
    db_directory.mkdir()
    db_path = db_directory / "Legacy.db"
    with sqlite3.connect(db_path) as connection:
        _ = connection.execute("CREATE TABLE schema_version (schema_key TEXT PRIMARY KEY, version INTEGER NOT NULL)")
        _ = connection.execute("INSERT INTO schema_version (schema_key, version) VALUES ('current', 11)")
    monkeypatch.setattr(
        "app.persistence.repository.resolve_default_db_directory",
        lambda: db_directory,
    )

    exit_code = main(["list"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON")
    errors = ensure_json_array(payload["errors"], "CLI JSON errors")
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")
    details = ensure_json_object(payload["details"], "CLI JSON details")
    assert exit_code == 1
    assert first_error["code"] == "database_migration_required"
    assert details["db_path"] == str(db_path)
    assert details["actual_version"] == 11
    assert details["required_version"] == 12


async def test_registration_language_conflict_has_structured_details(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """重复注册不同语言配置时公开现有值和请求值。"""
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(
        minimal_game_dir,
        source_language="ja",
        additional_source_languages=("en",),
    )

    with pytest.raises(GameRegistrationConflictError) as raised:
        _ = await registry.register_game(
            minimal_game_dir,
            source_language="ja",
            additional_source_languages=(),
        )

    assert raised.value.code == "game_registration_conflict"
    assert raised.value.details == {
        "existing_source_language": "ja",
        "existing_additional_source_languages": ["en"],
        "requested_source_language": "ja",
        "requested_additional_source_languages": [],
    }


def test_cli_reports_registration_language_conflict(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """add-game 的不可变语言冲突不得输出 unexpected_error。"""
    db_directory = tmp_path / "db"
    registry = GameRegistry(db_directory)
    _ = asyncio.run(registry.register_game(minimal_game_dir, source_language="ja"))
    monkeypatch.setattr(
        "app.persistence.repository.resolve_default_db_directory",
        lambda: db_directory,
    )

    exit_code = main(
        [
            "add-game",
            "--path",
            str(minimal_game_dir),
            "--source-language",
            "en",
        ]
    )

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON")
    errors = ensure_json_array(payload["errors"], "CLI JSON errors")
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")
    details = ensure_json_object(payload["details"], "CLI JSON details")
    assert exit_code == 1
    assert first_error["code"] == "game_registration_conflict"
    assert details["existing_source_language"] == "ja"
    assert details["requested_source_language"] == "en"


async def test_reregister_refuses_unfinished_write_transaction_before_updates(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """重复注册必须先阻断 preparing 写事务，且不得修改已有绑定。"""
    registry = GameRegistry(tmp_path / "db")
    registered = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game(registered.game_title) as session:
        await session.create_write_transaction(
            WriteTransactionRecord(
                transaction_id="tx-reregister-block",
                operation="write_back",
                game_path=registered.game_path,
                state="preparing",
                journal_path=(registered.content_root / ".att-mz-write-transactions" / "tx-reregister-block.json"),
                payload=None,
                created_at="2026-07-18T00:00:00+00:00",
                updated_at="2026-07-18T00:00:00+00:00",
                error="",
            )
        )

    with pytest.raises(RecoveryRequiredError, match="recover-write-transaction") as raised:
        _ = await registry.register_game(minimal_game_dir, source_language="ja")

    assert raised.value.code == "recovery_required"
    assert raised.value.details["transaction_id"] == "tx-reregister-block"
    assert raised.value.details["state"] == "preparing"
    async with await registry.open_game(registered.game_title) as session:
        assert session.game_id == registered.game_id
        assert session.game_path == registered.game_path
        assert session.source_language == "ja"
        assert session.additional_source_languages == ()
        unfinished = await session.read_unfinished_write_transactions()
        assert [record.transaction_id for record in unfinished] == ["tx-reregister-block"]
