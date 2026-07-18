"""写入游戏文件相关命令。

本模块负责正文写入、术语写入、字体还原和 run-all 流水线的 CLI 适配。
"""

from __future__ import annotations

import argparse

from app.application.summaries import WriteBackSummary
from app.cli.arguments import read_bool_arg, read_optional_str_arg
from app.cli.progress import build_progress_reporter
from app.cli.reports import (
    build_font_restore_summary_report,
    build_run_all_summary_report,
    build_terminology_write_summary_report,
    build_write_back_summary_report,
    build_write_transaction_recovery_summary_report,
)
from app.cli.runtime import (
    HandlerSession,
    build_setting_overrides,
    build_translation_run_limits,
    resolve_target_game_title,
    translate_text_for_handler,
    write_back_for_handler,
)
from app.observability import logger


async def run_write_back_command(args: argparse.Namespace) -> int:
    """执行 `write-back` 命令。"""
    game_title = await resolve_target_game_title(args)
    setting_overrides = build_setting_overrides(args)
    async with HandlerSession() as handler:
        summary = await write_back_for_handler(
            handler=handler,
            game_title=game_title,
            setting_overrides=setting_overrides,
            confirm_font_overwrite=read_bool_arg(args, "confirm_font_overwrite"),
        )
    report = build_write_back_summary_report(summary)
    print(report.to_json_text())
    return 0


async def run_rebuild_active_runtime_command(args: argparse.Namespace) -> int:
    """执行 `rebuild-active-runtime` 命令。"""
    game_title = await resolve_target_game_title(args)
    setting_overrides = build_setting_overrides(args)
    async with HandlerSession() as handler:
        with build_progress_reporter("重建运行文件") as progress:
            summary = await handler.rebuild_active_runtime(
                game_title=game_title,
                callbacks=progress.status_callbacks(),
                setting_overrides=setting_overrides,
                confirm_font_overwrite=read_bool_arg(args, "confirm_font_overwrite"),
            )
    report = build_write_back_summary_report(summary)
    print(report.to_json_text())
    return 0


async def run_restore_font_command(args: argparse.Namespace) -> int:
    """执行 `restore-font` 命令。"""
    game_title = await resolve_target_game_title(args)
    setting_overrides = build_setting_overrides(args)
    async with HandlerSession() as handler:
        summary = await handler.restore_font_replacement(
            game_title=game_title,
            setting_overrides=setting_overrides,
        )
    report = build_font_restore_summary_report(summary)
    print(report.to_json_text())
    return 0


async def run_recover_write_transaction_command(args: argparse.Namespace) -> int:
    """执行 `recover-write-transaction` 命令。"""
    game_title = await resolve_target_game_title(args)
    async with HandlerSession() as handler:
        summary = await handler.recover_write_transaction(game_title=game_title)
    report = build_write_transaction_recovery_summary_report(summary)
    print(report.to_json_text())
    return 0


async def run_write_terminology_command(args: argparse.Namespace) -> int:
    """执行 `write-terminology` 命令。"""
    game_title = await resolve_target_game_title(args)
    setting_overrides = build_setting_overrides(args)
    async with HandlerSession() as handler:
        with build_progress_reporter("术语写回") as progress:
            summary = await handler.write_terminology(
                game_title=game_title,
                callbacks=progress.status_callbacks(),
                setting_overrides=setting_overrides,
                confirm_font_overwrite=read_bool_arg(args, "confirm_font_overwrite"),
            )
    report = build_terminology_write_summary_report(summary)
    print(report.to_json_text())
    return 0


async def run_all_command(args: argparse.Namespace) -> int:
    """执行 `run-all` 命令。"""
    game_title = await resolve_target_game_title(args)
    placeholder_rules_text = read_optional_str_arg(args, "placeholder_rules")
    setting_overrides = build_setting_overrides(args)
    skip_write_back = read_bool_arg(args, "skip_write_back")

    async with HandlerSession() as handler:
        logger.info(f"[tag.phase]run-all 开始[/tag.phase] 游戏 [tag.count]{game_title}[/tag.count]")
        text_summary = await translate_text_for_handler(
            handler=handler,
            game_title=game_title,
            setting_overrides=setting_overrides,
            placeholder_rules_text=placeholder_rules_text,
            run_limits=build_translation_run_limits(args),
        )
        write_back_summary: WriteBackSummary | None = None
        if not text_summary.is_clean_completion:
            logger.warning(
                f"[tag.warning]正文翻译没有干净完成，已跳过回写[/tag.warning] 游戏 [tag.count]{game_title}[/tag.count]"
            )
        elif skip_write_back:
            logger.warning(f"[tag.warning]已按参数跳过回写[/tag.warning] 游戏 [tag.count]{game_title}[/tag.count]")
        else:
            write_back_summary = await write_back_for_handler(
                handler=handler,
                game_title=game_title,
                setting_overrides=setting_overrides,
                confirm_font_overwrite=read_bool_arg(args, "confirm_font_overwrite"),
            )
        logger.success(f"[tag.success]run-all 完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count]")
    report = build_run_all_summary_report(
        text_summary=text_summary,
        write_back_summary=write_back_summary,
    )
    print(report.to_json_text())
    return text_summary.exit_code
