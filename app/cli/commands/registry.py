"""游戏注册与环境诊断命令。

本模块负责列出、注册游戏，并把环境诊断服务适配为 CLI 子命令。
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import cast

from app.agent_toolkit import AgentReport, AgentToolkitService
from app.agent_toolkit.reports import AgentIssue, issue
from app.cli.arguments import read_bool_arg, read_optional_str_list_arg, read_str_arg
from app.cli.errors import CliBusinessError
from app.cli.reports import write_report_outputs
from app.cli.runtime import HandlerSession, resolve_optional_target_game_title
from app.language import normalize_additional_source_languages, parse_source_language
from app.native_runtime import native_contract, native_thread_count
from app.persistence import GameRegistry
from app.persistence.schema_loader import load_current_schema_sql
from app.persistence.sql import CURRENT_SCHEMA_VERSION
from app.rmmz.text_rules import JsonObject
from app.runtime_paths import resolve_app_path
from app.utils.config_loader_utils import load_setting
from app.version import application_version


async def run_list_command(args: argparse.Namespace) -> int:
    """执行 `list` 命令。"""
    _ = args
    registry = GameRegistry()
    items = await registry.list_games()
    report = AgentReport.from_parts(
        errors=[],
        warnings=[],
        summary={"game_count": len(items)},
        details={
            "games": [
                {
                    "game_id": item.game_id,
                    "game_title": item.game_title,
                    "engine_kind": item.engine_kind,
                    "engine_version": item.engine_version,
                    "source_language": item.source_language,
                    "additional_source_languages": list(item.additional_source_languages),
                    "target_language": item.target_language,
                    "game_path": str(item.game_path),
                    "content_root": str(item.content_root),
                    "db_path": str(item.db_path),
                }
                for item in items
            ]
        },
    )
    print(report.to_json_text())
    return 0


async def run_self_check_command(args: argparse.Namespace) -> int:
    """执行不访问网络的发行运行时自检。"""
    offline = read_bool_arg(args, "offline")
    if not offline:
        raise CliBusinessError("self-check 当前只允许 --offline 模式", code="self_check_mode_required")

    errors: list[AgentIssue] = []
    checks: JsonObject = {"network_accessed": False}
    try:
        _ = load_setting()
        checks["configuration"] = "ok"
    except Exception as error:
        errors.append(issue("self_check_configuration_invalid", f"本地配置或提示词不可用：{error}"))

    required_resources = (
        "setting.toml",
        "prompts/text_translation_ja_to_zh_system.md",
        "prompts/text_translation_en_to_zh_system.md",
        "fonts/NotoSansSC-Regular.ttf",
    )
    missing_resources = [name for name in required_resources if not resolve_app_path(*Path(name).parts).is_file()]
    if missing_resources:
        errors.append(issue("self_check_resource_missing", f"发行资源缺失：{', '.join(missing_resources)}"))
    else:
        checks["resources"] = "ok"

    try:
        with sqlite3.connect(":memory:") as connection:
            _ = connection.executescript(load_current_schema_sql())
            raw_row = cast(
                object,
                connection.execute("SELECT version FROM schema_version WHERE schema_key = 'current'").fetchone(),
            )
            if raw_row is not None and not isinstance(raw_row, tuple):
                raise RuntimeError("schema version 查询返回了非法结果")
            row = cast(tuple[object, ...] | None, raw_row)
        if row != (CURRENT_SCHEMA_VERSION,):
            raise RuntimeError(f"schema version 不是 {CURRENT_SCHEMA_VERSION}")
        checks["schema"] = "ok"
    except Exception as error:
        errors.append(issue("self_check_schema_invalid", f"SQLite schema 不能建立当前数据库：{error}"))

    try:
        contract = native_contract()
        checks["native"] = {
            "abi_version": contract["abi_version"],
            "envelope_version": contract["envelope_version"],
            "threads": native_thread_count(),
        }
    except Exception as error:
        errors.append(issue("self_check_native_invalid", f"Rust 原生扩展不可用或版本不一致：{error}"))

    report = AgentReport.from_parts(
        errors=errors,
        warnings=[],
        summary={
            "version": application_version(),
            "offline": True,
            "schema_version": CURRENT_SCHEMA_VERSION,
        },
        details={"checks": checks},
    )
    print(report.to_json_text())
    return 1 if report.status == "error" else 0


async def run_add_game_command(args: argparse.Namespace) -> int:
    """执行 `add-game` 命令。"""
    game_path = Path(read_str_arg(args, "path"))
    source_language = parse_source_language(read_str_arg(args, "source_language"))
    additional_source_languages = normalize_additional_source_languages(
        source_language=source_language,
        additional_source_languages=read_optional_str_list_arg(
            args,
            "additional_source_language",
        )
        or [],
    )
    async with HandlerSession() as handler:
        try:
            game_title = await handler.add_game(
                game_path,
                source_language=source_language,
                additional_source_languages=additional_source_languages,
            )
        except FileExistsError as error:
            raise CliBusinessError(
                str(error),
                code="game_registration_conflict",
                details={"game_path": str(game_path)},
            ) from error
        report = AgentReport.from_parts(
            errors=[],
            warnings=[],
            summary={
                "game_title": game_title,
                "source_language": source_language,
                "additional_source_languages": list(additional_source_languages),
                "target_language": "zh-Hans",
            },
            details={"next_game_argument": game_title},
        )
        print(report.to_json_text())
    return 0


async def run_doctor_command(args: argparse.Namespace) -> int:
    """执行 `doctor` 命令。"""
    game_title = await resolve_optional_target_game_title(args)
    check_llm = not read_bool_arg(args, "no_check_llm")
    service = AgentToolkitService()
    report = await service.doctor(game_title=game_title, check_llm=check_llm)
    write_report_outputs(report=report, args=args, title="环境诊断报告")
    return 1 if report.status == "error" else 0
