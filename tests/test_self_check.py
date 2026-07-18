"""发行版版本与离线自检契约测试。"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import cast

import pytest
from pytest import CaptureFixture, MonkeyPatch

from app.cli import build_parser
from app.cli.commands.registry import run_self_check_command


def test_version_flag_reports_unified_application_version(capsys: CaptureFixture[str]) -> None:
    """全局版本开关必须在加载任何子命令前返回统一版本。"""
    with pytest.raises(SystemExit) as raised:
        _ = build_parser().parse_args(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == "att-mz 0.1.15"


@pytest.mark.asyncio
async def test_offline_self_check_never_accesses_network(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """离线自检只验证本地配置、资源、schema 和 native 契约。"""
    for relative_path in (
        "setting.toml",
        "prompts/text_translation_ja_to_zh_system.md",
        "prompts/text_translation_en_to_zh_system.md",
        "fonts/NotoSansSC-Regular.ttf",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("test\n", encoding="utf-8")

    monkeypatch.setenv("ATT_MZ_HOME", str(tmp_path))
    monkeypatch.setattr("app.cli.commands.registry.load_setting", lambda: object())
    monkeypatch.setattr(
        "app.cli.commands.registry.native_contract",
        lambda: {"abi_version": 1, "envelope_version": 1},
    )
    native_operations: list[str] = []

    def invoke_native(operation: str, payload: object) -> dict[str, int]:
        """记录离线自检使用的版本化线程数操作。"""
        assert payload == {}
        native_operations.append(operation)
        return {"thread_count": 2}

    monkeypatch.setattr("app.native_runtime.invoke_native", invoke_native)

    exit_code = await run_self_check_command(Namespace(offline=True))

    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["summary"] == {"version": "0.1.15", "offline": True, "schema_version": 12}
    details = cast(dict[str, object], payload["details"])
    checks = cast(dict[str, object], details["checks"])
    assert checks["network_accessed"] is False
    assert native_operations == ["runtime.thread_count"]


@pytest.mark.asyncio
async def test_self_check_reports_native_contract_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """原生 ABI 漂移必须以稳定错误码明确失败。"""
    for relative_path in (
        "setting.toml",
        "prompts/text_translation_ja_to_zh_system.md",
        "prompts/text_translation_en_to_zh_system.md",
        "fonts/NotoSansSC-Regular.ttf",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("test\n", encoding="utf-8")

    monkeypatch.setenv("ATT_MZ_HOME", str(tmp_path))
    monkeypatch.setattr("app.cli.commands.registry.load_setting", lambda: object())

    def reject_native() -> object:
        raise RuntimeError("ABI version mismatch")

    monkeypatch.setattr("app.cli.commands.registry.native_contract", reject_native)

    exit_code = await run_self_check_command(Namespace(offline=True))

    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert exit_code == 1
    errors = cast(list[dict[str, object]], payload["errors"])
    first_error = errors[0]
    assert first_error["code"] == "self_check_native_invalid"
