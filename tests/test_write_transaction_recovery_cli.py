"""文件写事务恢复命令测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import TracebackType
from typing import cast

from pytest import CaptureFixture, MonkeyPatch

from app.application.summaries import WriteTransactionRecoverySummary
from app.persistence import GameRegistry, RecoveryRequiredError, WriteTransactionRecord
from app.rmmz.json_types import coerce_json_value, ensure_json_object
from main import main


def test_recover_write_transaction_outputs_machine_readable_summary(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """恢复命令必须经过 handler，并稳定输出事务状态和文件计数。"""

    class FakeHandler:
        """返回固定恢复摘要。"""

        async def recover_write_transaction(
            self,
            *,
            game_title: str,
        ) -> WriteTransactionRecoverySummary:
            assert game_title == "demo"
            return WriteTransactionRecoverySummary(
                transaction_id="txrecover",
                previous_state="prepared",
                final_state="rolled_back",
                restored_file_count=3,
                finalized_committed_file_count=0,
            )

    class FakeHandlerSession:
        """替换真实 handler 生命周期。"""

        async def __aenter__(self) -> FakeHandler:
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            _ = exc_type
            _ = exc
            _ = traceback

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)

    exit_code = main(["recover-write-transaction", "--game", "demo"])

    captured = capsys.readouterr()
    raw_payload = cast(object, json.loads(captured.out))
    payload = ensure_json_object(coerce_json_value(raw_payload), "CLI JSON 输出")
    summary = ensure_json_object(payload["summary"], "CLI JSON summary")
    assert exit_code == 0
    assert summary == {
        "transaction_id": "txrecover",
        "previous_state": "prepared",
        "final_state": "rolled_back",
        "restored_file_count": 3,
        "finalized_committed_file_count": 0,
    }


def test_mutating_commands_report_unfinished_transaction_as_recovery_required(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
    app_home_with_example_setting: Path,
) -> None:
    """代表性修改命令不得吞掉统一事务阻断错误。"""
    _ = app_home_with_example_setting
    db_directory = tmp_path / "db"
    registry = GameRegistry(db_directory)
    registered = asyncio.run(registry.register_game(minimal_game_dir, source_language="ja"))

    async def create_unfinished_transaction() -> None:
        async with await registry.open_game(registered.game_title) as session:
            await session.create_write_transaction(
                WriteTransactionRecord(
                    transaction_id="tx-cli-blocked",
                    operation="write_back",
                    game_path=registered.game_path,
                    state="preparing",
                    journal_path=(registered.content_root / ".att-mz-write-transactions" / "tx-cli-blocked.json"),
                    payload=None,
                    created_at="2026-07-18T00:00:00+00:00",
                    updated_at="2026-07-18T00:00:00+00:00",
                    error="",
                )
            )

    asyncio.run(create_unfinished_transaction())
    monkeypatch.setattr(
        "app.persistence.repository.resolve_default_db_directory",
        lambda: db_directory,
    )
    plugin_source_rules_path = tmp_path / "plugin-source-rules.json"
    _ = plugin_source_rules_path.write_text("[]\n", encoding="utf-8")
    placeholder_rules = json.dumps(
        {
            r"(?i)\\F\d*\[[^\]\r\n]+\]": "[CUSTOM_FACE_PORTRAIT_{index}]",
        },
        ensure_ascii=False,
    )
    commands = (
        [
            "add-game",
            "--path",
            str(minimal_game_dir),
            "--source-language",
            "ja",
        ],
        [
            "import-placeholder-rules",
            "--game",
            registered.game_title,
            "--rules",
            placeholder_rules,
        ],
        [
            "import-plugin-source-rules",
            "--game",
            registered.game_title,
            "--input",
            str(plugin_source_rules_path),
            "--confirm-empty",
        ],
        ["write-back", "--game", registered.game_title],
    )

    for command in commands:
        exit_code = main(command)
        captured = capsys.readouterr()
        payload = ensure_json_object(
            coerce_json_value(cast(object, json.loads(captured.out))),
            "CLI JSON 输出",
        )
        errors = payload["errors"]
        assert isinstance(errors, list)
        first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")
        details = ensure_json_object(payload["details"], "CLI JSON details")

        assert exit_code == 1
        assert first_error["code"] == "recovery_required"
        assert details["transaction_id"] == "tx-cli-blocked"
        assert details["state"] == "preparing"


def test_recover_cli_reports_manual_recovery_as_recovery_required(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """recover 发现不能安全覆盖现场时必须返回稳定业务错误码。"""

    class FakeHandler:
        """模拟恢复阶段发现外部改写。"""

        async def recover_write_transaction(self, *, game_title: str) -> WriteTransactionRecoverySummary:
            assert game_title == "demo"
            raise RecoveryRequiredError(
                "写事务 tx-external 恢复失败，游戏文件保持阻断状态：目标已被外部改写",
                transaction_id="tx-external",
                state="recovery_required",
                details={"operation": "write_back"},
            )

    class FakeHandlerSession:
        """替换真实 handler 生命周期。"""

        async def __aenter__(self) -> FakeHandler:
            return FakeHandler()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            _ = (exc_type, exc, traceback)

    monkeypatch.setattr("app.cli.commands.write_back.HandlerSession", FakeHandlerSession)

    exit_code = main(["recover-write-transaction", "--game", "demo"])

    captured = capsys.readouterr()
    payload = ensure_json_object(
        coerce_json_value(cast(object, json.loads(captured.out))),
        "CLI JSON 输出",
    )
    errors = payload["errors"]
    assert isinstance(errors, list)
    first_error = ensure_json_object(errors[0], "CLI JSON errors[0]")
    details = ensure_json_object(payload["details"], "CLI JSON details")
    assert exit_code == 1
    assert first_error["code"] == "recovery_required"
    assert details == {
        "operation": "write_back",
        "transaction_id": "tx-external",
        "state": "recovery_required",
    }
