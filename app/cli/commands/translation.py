"""正文翻译与修复辅助命令。

本模块负责质量报告、翻译运行、状态查询、手动译文导入导出和译文重置。
"""

from __future__ import annotations

import argparse

from app.agent_toolkit import AgentToolkitService
from app.cli.arguments import (
    read_bool_arg,
    read_optional_int_arg,
    read_optional_path_arg,
    read_optional_str_arg,
    read_required_path_arg,
)
from app.cli.progress import build_progress_reporter
from app.cli.reports import build_translate_summary_report, write_report_outputs
from app.cli.runtime import (
    HandlerSession,
    build_setting_overrides,
    build_translation_run_limits,
    resolve_target_game_title,
    translate_text_for_handler,
)


async def run_quality_report_command(args: argparse.Namespace) -> int:
    """执行 `quality-report` 命令。"""
    game_title = await resolve_target_game_title(args)
    service = AgentToolkitService()
    with build_progress_reporter("质量报告") as progress:
        report = await service.quality_report(
            game_title=game_title,
            callbacks=progress.status_callbacks(),
            include_write_probe=read_bool_arg(args, "include_write_probe"),
        )
    write_report_outputs(report=report, args=args, title="翻译质量报告")
    return 1 if report.status == "error" else 0


async def run_text_scope_command(args: argparse.Namespace) -> int:
    """执行 `text-scope` 命令。"""
    game_title = await resolve_target_game_title(args)
    service = AgentToolkitService()
    report = await service.text_scope(
        game_title=game_title,
        include_write_probe=read_bool_arg(args, "include_write_probe"),
    )
    write_report_outputs(report=report, args=args, title="统一文本清单")
    return 1 if report.status == "error" else 0


async def run_audit_coverage_command(args: argparse.Namespace) -> int:
    """执行 `audit-coverage` 命令。"""
    game_title = await resolve_target_game_title(args)
    service = AgentToolkitService()
    report = await service.audit_coverage(
        game_title=game_title,
        include_write_probe=read_bool_arg(args, "include_write_probe"),
    )
    write_report_outputs(report=report, args=args, title="覆盖审计报告")
    return 1 if report.status == "error" else 0


async def run_audit_active_runtime_command(args: argparse.Namespace) -> int:
    """执行 `audit-active-runtime` 命令。"""
    game_title = await resolve_target_game_title(args)
    service = AgentToolkitService()
    report = await service.audit_active_runtime(game_title=game_title)
    write_report_outputs(report=report, args=args, title="当前运行文件审计报告")
    return 1 if report.status == "error" else 0


async def run_diagnose_active_runtime_command(args: argparse.Namespace) -> int:
    """执行 `diagnose-active-runtime` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    service = AgentToolkitService()
    report = await service.diagnose_active_runtime(game_title=game_title, output_path=output_path)
    write_report_outputs(report=report, args=args, title="当前运行文件反推诊断报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_verify_feedback_text_command(args: argparse.Namespace) -> int:
    """执行 `verify-feedback-text` 命令。"""
    game_title = await resolve_target_game_title(args)
    input_path = read_required_path_arg(args, "input")
    service = AgentToolkitService()
    report = await service.verify_feedback_text(game_title=game_title, input_path=input_path)
    write_report_outputs(report=report, args=args, title="反馈原文反查报告")
    return 1 if report.status == "error" else 0


async def run_scan_plugin_source_text_command(args: argparse.Namespace) -> int:
    """执行 `scan-plugin-source-text` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    source_view = read_optional_str_arg(args, "view") or "translation-source"
    service = AgentToolkitService()
    report = await service.scan_plugin_source_text(
        game_title=game_title,
        output_path=output_path,
        source_view=source_view,
    )
    write_report_outputs(report=report, args=args, title="插件源码风险扫描报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_export_plugin_source_ast_map_command(args: argparse.Namespace) -> int:
    """执行 `export-plugin-source-ast-map` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    source_view = read_optional_str_arg(args, "view") or "translation-source"
    service = AgentToolkitService()
    report = await service.export_plugin_source_ast_map(
        game_title=game_title,
        output_path=output_path,
        source_view=source_view,
    )
    write_report_outputs(report=report, args=args, title="插件源码 AST 地图导出报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_export_pending_translations_command(args: argparse.Namespace) -> int:
    """执行 `export-pending-translations` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    limit = read_optional_int_arg(args, "limit")
    service = AgentToolkitService()
    with build_progress_reporter("手动填写译文表导出") as progress:
        report = await service.export_pending_translations(
            game_title=game_title,
            output_path=output_path,
            limit=limit,
            include_write_probe=read_bool_arg(args, "include_write_probe"),
            callbacks=progress.status_callbacks(),
        )
    write_report_outputs(report=report, args=args, title="手动填写译文表导出报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_export_quality_fix_template_command(args: argparse.Namespace) -> int:
    """执行 `export-quality-fix-template` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    service = AgentToolkitService()
    with build_progress_reporter("质量修复模板导出") as progress:
        report = await service.export_quality_fix_template(
            game_title=game_title,
            output_path=output_path,
            include_write_probe=read_bool_arg(args, "include_write_probe"),
            callbacks=progress.status_callbacks(),
        )
    write_report_outputs(report=report, args=args, title="质量修复模板导出报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_import_manual_translations_command(args: argparse.Namespace) -> int:
    """执行 `import-manual-translations` 命令。"""
    game_title = await resolve_target_game_title(args)
    input_path = read_required_path_arg(args, "input")
    service = AgentToolkitService()
    report = await service.import_manual_translations(game_title=game_title, input_path=input_path)
    write_report_outputs(report=report, args=args, title="手动填写译文表导入报告")
    return 1 if report.status == "error" else 0


async def run_reset_translations_command(args: argparse.Namespace) -> int:
    """执行 `reset-translations` 命令。"""
    game_title = await resolve_target_game_title(args)
    input_path = read_optional_path_arg(args, "input")
    reset_all = read_bool_arg(args, "reset_all")
    service = AgentToolkitService()
    report = await service.reset_translations(game_title=game_title, input_path=input_path, reset_all=reset_all)
    write_report_outputs(report=report, args=args, title="译文重置报告")
    return 1 if report.status == "error" else 0


async def run_translation_status_command(args: argparse.Namespace) -> int:
    """执行 `translation-status` 命令。"""
    game_title = await resolve_target_game_title(args)
    service = AgentToolkitService()
    with build_progress_reporter("正文翻译状态") as progress:
        report = await service.translation_status(
            game_title=game_title,
            refresh_scope=read_bool_arg(args, "refresh_scope"),
            callbacks=progress.status_callbacks(),
        )
    write_report_outputs(report=report, args=args, title="正文翻译状态")
    return 1 if report.status == "error" else 0


async def run_translate_command(args: argparse.Namespace) -> int:
    """执行 `translate` 命令。"""
    game_title = await resolve_target_game_title(args)
    placeholder_rules_text = read_optional_str_arg(args, "placeholder_rules")
    setting_overrides = build_setting_overrides(args)
    async with HandlerSession() as handler:
        summary = await translate_text_for_handler(
            handler=handler,
            game_title=game_title,
            setting_overrides=setting_overrides,
            placeholder_rules_text=placeholder_rules_text,
            run_limits=build_translation_run_limits(args),
        )
    report = build_translate_summary_report(summary)
    print(report.to_json_text())
    return summary.exit_code
