"""未完成写事务对 CLI 与应用层修改入口的阻断契约。"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast, final

import pytest

import app.cli.dispatch as dispatch_module
from app.application.handler import TranslationHandler
from app.cli.dispatch import COMMAND_HANDLERS, MUTATING_COMMAND_NAMES, MUTATION_PREFLIGHT_COMMAND_NAMES
from app.llm import LLMHandler
from app.persistence import GameRegistry, RecoveryRequiredError


@final
class _BlockedMutationSession:
    """始终报告未完成写事务的最小会话。"""

    def __init__(self) -> None:
        self.lease_call_count = 0
        self.guard_call_count = 0
        self.close_call_count = 0

    def acquire_mutation_lease(self) -> None:
        self.lease_call_count += 1

    async def assert_no_unfinished_write_transaction(self) -> None:
        self.guard_call_count += 1
        raise RecoveryRequiredError(
            "当前游戏存在未完成写事务 tx-blocked，请先执行 recover-write-transaction",
            transaction_id="tx-blocked",
            state="prepared",
        )

    async def close(self) -> None:
        self.close_call_count += 1


@final
class _BlockedMutationRegistry:
    """为修改入口返回阻断会话的注册表。"""

    def __init__(self, session: _BlockedMutationSession) -> None:
        self.session = session

    async def open_game(self, game_title: str) -> _BlockedMutationSession:
        assert game_title == "demo"
        return self.session

    async def open_game_with_mutation_lease(self, game_title: str) -> _BlockedMutationSession:
        assert game_title == "demo"
        self.session.acquire_mutation_lease()
        return self.session


EXPECTED_MUTATING_COMMAND_NAMES = frozenset(
    {
        "add-game",
        "audit-active-runtime",
        "diagnose-active-runtime",
        "export-plugin-source-ast-map",
        "import-event-command-rules",
        "import-manual-translations",
        "import-mv-virtual-namebox-rules",
        "import-note-tag-rules",
        "import-placeholder-rules",
        "import-plugin-rules",
        "import-plugin-source-rules",
        "import-source-residual-rules",
        "import-structured-placeholder-rules",
        "import-terminology",
        "prepare-agent-workspace",
        "rebuild-active-runtime",
        "reset-translations",
        "restore-font",
        "run-all",
        "scan-plugin-source-text",
        "translate",
        "write-back",
        "write-terminology",
    }
)


def test_cli_mutation_contract_enumerates_every_modifying_command() -> None:
    """CLI 契约清单需要在新增修改命令时显式更新。"""
    assert MUTATING_COMMAND_NAMES == EXPECTED_MUTATING_COMMAND_NAMES
    assert MUTATION_PREFLIGHT_COMMAND_NAMES == MUTATING_COMMAND_NAMES - {"add-game"}
    assert MUTATING_COMMAND_NAMES <= COMMAND_HANDLERS.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", sorted(MUTATION_PREFLIGHT_COMMAND_NAMES))
async def test_dispatch_preflights_every_existing_game_mutation_before_handler(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """现有游戏的每个修改命令必须在进入命令处理器前执行统一事务预检。"""
    calls: list[tuple[GameRegistry, str]] = []

    async def blocked_preflight(game_registry: GameRegistry, game_title: str) -> None:
        calls.append((game_registry, game_title))
        raise RecoveryRequiredError(
            "当前游戏存在未完成写事务 tx-dispatch，请先执行 recover-write-transaction",
            transaction_id="tx-dispatch",
            state="prepared",
        )

    monkeypatch.setattr(dispatch_module, "assert_game_mutation_allowed", blocked_preflight)
    args = argparse.Namespace(command=command, game="demo", game_path=None)

    with pytest.raises(RecoveryRequiredError, match="tx-dispatch"):
        _ = await dispatch_module.dispatch_command(args)

    assert len(calls) == 1
    assert calls[0][1] == "demo"


MUTATING_APPLICATION_ENTRYPOINTS = (
    ("app/application/handler.py", "import_plugin_rules"),
    ("app/application/handler.py", "import_event_command_rules"),
    ("app/application/handler.py", "import_note_tag_rules"),
    ("app/application/handler.py", "translate_text"),
    ("app/application/handler.py", "_write_back_with_native_fast_gate"),
    ("app/application/handler.py", "_rebuild_active_runtime_with_native_plan"),
    ("app/application/handler.py", "import_terminology"),
    ("app/application/handler.py", "write_terminology"),
    ("app/application/handler.py", "restore_font_replacement"),
    ("app/agent_toolkit/services/manual_translation.py", "import_manual_translations"),
    ("app/agent_toolkit/services/quality.py", "audit_active_runtime"),
    ("app/agent_toolkit/services/quality.py", "diagnose_active_runtime"),
    ("app/agent_toolkit/services/quality.py", "reset_translations"),
    ("app/agent_toolkit/services/rule_validation.py", "import_mv_virtual_namebox_rules"),
    ("app/agent_toolkit/services/rule_validation.py", "import_note_tag_rules"),
    ("app/agent_toolkit/services/rule_validation.py", "import_source_residual_rules"),
    ("app/agent_toolkit/services/rule_validation.py", "import_plugin_source_rules"),
    ("app/agent_toolkit/services/placeholder_rules.py", "import_placeholder_rules"),
    ("app/agent_toolkit/services/placeholder_rules.py", "import_structured_placeholder_rules"),
    ("app/agent_toolkit/services/workspace.py", "scan_plugin_source_text"),
    ("app/agent_toolkit/services/workspace.py", "export_plugin_source_ast_map"),
    ("app/agent_toolkit/services/workspace.py", "prepare_agent_workspace"),
)


@pytest.mark.parametrize(
    ("relative_path", "method_name"),
    MUTATING_APPLICATION_ENTRYPOINTS,
)
def test_modifying_application_entrypoint_uses_single_high_level_guard(
    relative_path: str,
    method_name: str,
) -> None:
    """真正修改状态的入口自身必须打开 mutation session。"""
    source_path = Path(__file__).parents[1] / Path(relative_path)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name]
    assert len(methods) == 1
    guard_calls = [
        node
        for node in ast.walk(methods[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open_game_for_mutation"
    ]
    assert len(guard_calls) == 1


type HandlerInvocation = Callable[[TranslationHandler], Awaitable[object]]


def _set_progress(_current: int, _total: int) -> None:
    return


def _advance_progress(_count: int) -> None:
    return


def _set_status(_status: str) -> None:
    return


async def _invoke_plugin_rules(handler: TranslationHandler) -> object:
    return await handler.import_plugin_rules("demo", Path("rules.json"))


async def _invoke_event_rules(handler: TranslationHandler) -> object:
    return await handler.import_event_command_rules("demo", Path("rules.json"))


async def _invoke_note_rules(handler: TranslationHandler) -> object:
    return await handler.import_note_tag_rules("demo", Path("rules.json"))


async def _invoke_translate(handler: TranslationHandler) -> object:
    return await handler.translate_text(
        "demo",
        None,
        None,
        None,
        (_set_progress, _advance_progress, _set_status),
    )


async def _invoke_write_back(handler: TranslationHandler) -> object:
    return await handler.write_back("demo", (_set_progress, _advance_progress))


async def _invoke_rebuild(handler: TranslationHandler) -> object:
    return await handler.rebuild_active_runtime(
        "demo",
        (_set_progress, _advance_progress),
    )


async def _invoke_terminology_import(handler: TranslationHandler) -> object:
    return await handler.import_terminology(
        "demo",
        Path("terms.json"),
        Path("glossary.json"),
    )


async def _invoke_terminology_write(handler: TranslationHandler) -> object:
    return await handler.write_terminology(
        "demo",
        (_set_progress, _advance_progress),
    )


async def _invoke_font_restore(handler: TranslationHandler) -> object:
    return await handler.restore_font_replacement("demo")


HANDLER_INVOCATIONS: tuple[HandlerInvocation, ...] = (
    _invoke_plugin_rules,
    _invoke_event_rules,
    _invoke_note_rules,
    _invoke_translate,
    _invoke_write_back,
    _invoke_rebuild,
    _invoke_terminology_import,
    _invoke_terminology_write,
    _invoke_font_restore,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    HANDLER_INVOCATIONS,
    ids=(
        "plugin-rules",
        "event-rules",
        "note-rules",
        "translate",
        "write-back",
        "rebuild",
        "terminology-import",
        "terminology-write",
        "font-restore",
    ),
)
async def test_application_mutation_entrypoints_share_the_high_level_guard(
    invoke: HandlerInvocation,
) -> None:
    """直接调用应用层时也不能绕过 CLI guard。"""
    session = _BlockedMutationSession()
    registry = cast(GameRegistry, cast(object, _BlockedMutationRegistry(session)))
    handler = TranslationHandler(registry, cast(LLMHandler, object()))

    with pytest.raises(RecoveryRequiredError, match="recover-write-transaction") as raised:
        _ = await invoke(handler)

    assert raised.value.code == "recovery_required"
    assert raised.value.details == {
        "transaction_id": "tx-blocked",
        "state": "prepared",
    }
    assert session.guard_call_count == 1
    assert session.lease_call_count == 1
    assert session.close_call_count == 1
