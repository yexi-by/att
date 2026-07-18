"""写回事务 payload 的不可信数据库边界测试。"""

import json
from pathlib import Path
from typing import cast

import pytest

from app.persistence import GameRegistry


def _valid_payload() -> dict[str, object]:
    return {
        "version": 1,
        "database_committed": False,
        "files": [
            {
                "target_relative_path": "data/Actors.json",
                "staged_relative_path": "data/.Actors.att-mz-write.stage",
                "backup_relative_path": None,
                "existed_before": False,
                "original_sha256": None,
                "target_sha256": "a" * 64,
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["unsupported-version", "invalid-hash", "extra-field"],
)
async def test_read_write_transaction_rejects_invalid_payload(
    minimal_game_dir: Path,
    tmp_path: Path,
    case: str,
) -> None:
    """即使 CHECK 被绕过，记录层也必须拒绝非严格 payload。"""
    registry = GameRegistry(tmp_path / "db")
    game = await registry.register_game(minimal_game_dir, source_language="ja")
    payload = _valid_payload()
    if case == "unsupported-version":
        payload["version"] = 2
    elif case == "invalid-hash":
        files = payload["files"]
        assert isinstance(files, list)
        first_file = cast(list[object], files)[0]
        assert isinstance(first_file, dict)
        first_file["target_sha256"] = "invalid"
    else:
        payload["unexpected"] = True

    async with await registry.open_game(game.game_title) as session:
        _ = await session.connection.execute("PRAGMA ignore_check_constraints = ON")
        _ = await session.connection.execute(
            """
            INSERT INTO write_transactions
            (
                transaction_id, operation, game_path, state, journal_path,
                payload_json, created_at, updated_at, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tx-invalid-payload",
                "write_back",
                str(game.game_path),
                "prepared",
                str(game.content_root / ".att-mz-write-transactions" / "tx-invalid-payload.json"),
                json.dumps(payload),
                "2026-07-18T00:00:00+00:00",
                "2026-07-18T00:00:00+00:00",
                "",
            ),
        )
        await session.connection.commit()

        with pytest.raises(RuntimeError, match="payload_json 非法"):
            _ = await session.read_write_transaction("tx-invalid-payload")
