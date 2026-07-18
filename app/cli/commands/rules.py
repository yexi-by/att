"""规则导入导出与校验命令。

本模块负责插件、事件指令、Note 标签、自定义占位符和源文残留规则的 CLI 适配。
"""

from __future__ import annotations

import argparse

from app.agent_toolkit import AgentIssue, AgentReport, AgentToolkitService
from app.agent_toolkit.reports import issue
from app.cli.arguments import (
    read_bool_arg,
    read_int_set_arg,
    read_optional_int_list_arg,
    read_optional_str_list_arg,
    read_optional_text_source_arg,
    read_required_path_arg,
    read_required_text_source_arg,
    read_text_file,
)
from app.cli.reports import build_sampled_stdout_report, write_report_outputs
from app.cli.runtime import HandlerSession, resolve_optional_target_game_title, resolve_target_game_title
from app.persistence import RecoveryRequiredError
from app.rmmz.text_rules import JsonObject


async def run_export_plugins_json_command(args: argparse.Namespace) -> int:
    """执行 `export-plugins-json` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    async with HandlerSession() as handler:
        summary = await handler.export_plugins_json(game_title=game_title, output_path=output_path)
    report = AgentReport.from_parts(
        errors=[],
        warnings=[],
        summary={
            "game": game_title,
            "output": summary.output_path,
            "plugin_count": summary.plugin_count,
        },
        details={},
    )
    write_report_outputs(report=report, args=args, title="插件配置 JSON 导出报告", write_output_file=False)
    return 0


async def run_import_plugin_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-plugin-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    input_path = read_required_path_arg(args, "input")
    try:
        async with HandlerSession() as handler:
            summary = await handler.import_plugin_rules(
                game_title=game_title,
                input_path=input_path,
                confirm_empty=read_bool_arg(args, "confirm_empty"),
            )
    except RecoveryRequiredError:
        raise
    except Exception as error:
        report = AgentReport.from_parts(
            errors=[issue("plugin_rules_invalid", f"插件规则导入失败: {type(error).__name__}: {error}")],
            warnings=[],
            summary={"game": game_title, "input": str(input_path)},
            details={},
        )
        write_report_outputs(report=report, args=args, title="插件规则导入报告")
        return 1
    warnings = build_deleted_translation_warnings(
        deleted_translation_items=summary.deleted_translation_items,
        backup_path=summary.deleted_translation_backup_path,
        rule_label="插件规则",
    )
    report = AgentReport.from_parts(
        errors=[],
        warnings=warnings,
        summary={
            "game": game_title,
            "input": str(input_path),
            "imported_plugin_count": summary.imported_plugin_count,
            "imported_rule_count": summary.imported_rule_count,
            "deleted_translation_items": summary.deleted_translation_items,
            "deleted_translation_backup_path": summary.deleted_translation_backup_path or "",
        },
        details=build_deleted_translation_backup_details(summary.deleted_translation_backup_path),
    )
    write_report_outputs(report=report, args=args, title="插件规则导入报告")
    return 0


async def run_export_event_commands_json_command(args: argparse.Namespace) -> int:
    """执行 `export-event-commands-json` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    command_codes = read_int_set_arg(args, "codes")
    async with HandlerSession() as handler:
        summary = await handler.export_event_commands_json(
            game_title=game_title,
            output_path=output_path,
            command_codes=command_codes,
            default_command_codes_override=read_optional_int_list_arg(
                args,
                "event_command_default_codes",
            ),
        )
    report = AgentReport.from_parts(
        errors=[],
        warnings=[],
        summary={
            "game": game_title,
            "output": summary.output_path,
            "command_count": summary.command_count,
        },
        details={},
    )
    write_report_outputs(report=report, args=args, title="事件指令参数 JSON 导出报告", write_output_file=False)
    return 0


async def run_import_event_command_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-event-command-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    input_path = read_required_path_arg(args, "input")
    command_codes = read_int_set_arg(args, "codes")
    try:
        async with HandlerSession() as handler:
            summary = await handler.import_event_command_rules(
                game_title=game_title,
                input_path=input_path,
                confirm_empty=read_bool_arg(args, "confirm_empty"),
                command_codes=command_codes,
                default_command_codes_override=read_optional_int_list_arg(
                    args,
                    "event_command_default_codes",
                ),
            )
    except RecoveryRequiredError:
        raise
    except Exception as error:
        report = AgentReport.from_parts(
            errors=[issue("event_command_rules_invalid", f"事件指令规则导入失败: {type(error).__name__}: {error}")],
            warnings=[],
            summary={"game": game_title, "input": str(input_path)},
            details={},
        )
        write_report_outputs(report=report, args=args, title="事件指令规则导入报告")
        return 1
    warnings = build_deleted_translation_warnings(
        deleted_translation_items=summary.deleted_translation_items,
        backup_path=summary.deleted_translation_backup_path,
        rule_label="事件指令规则",
    )
    report = AgentReport.from_parts(
        errors=[],
        warnings=warnings,
        summary={
            "game": game_title,
            "input": str(input_path),
            "imported_rule_group_count": summary.imported_rule_group_count,
            "imported_path_rule_count": summary.imported_path_rule_count,
            "deleted_translation_items": summary.deleted_translation_items,
            "deleted_translation_backup_path": summary.deleted_translation_backup_path or "",
        },
        details=build_deleted_translation_backup_details(summary.deleted_translation_backup_path),
    )
    write_report_outputs(report=report, args=args, title="事件指令规则导入报告")
    return 0


def build_deleted_translation_warnings(
    *,
    deleted_translation_items: int,
    backup_path: str | None,
    rule_label: str,
) -> list[AgentIssue]:
    """为规则导入 JSON 报告生成失效译文清理告警。"""
    if deleted_translation_items <= 0:
        return []
    if backup_path is None:
        return [
            issue(
                "deleted_translations_without_backup",
                f"本次导入{rule_label}已清理 {deleted_translation_items} 条不再属于当前规则范围的已保存译文；没有生成备份文件",
            )
        ]
    return [
        issue(
            "deleted_translations_backed_up",
            f"本次导入{rule_label}已清理 {deleted_translation_items} 条不再属于当前规则范围的已保存译文；已先备份到 {backup_path}。如果发现规则导错，先重新导入正确规则，再用 import-manual-translations 读取该备份文件恢复这些译文",
        )
    ]


def build_deleted_translation_backup_details(backup_path: str | None) -> JsonObject:
    """生成规则导入清理译文后的恢复提示详情。"""
    if backup_path is None:
        return {}
    return {
        "deleted_translation_backup": {
            "path": backup_path,
            "restore_step": "先重新导入正确规则，再运行 import-manual-translations 并把 input 指向该备份文件。",
        }
    }


async def run_export_note_tag_candidates_command(args: argparse.Namespace) -> int:
    """执行 `export-note-tag-candidates` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    service = AgentToolkitService()
    report = await service.export_note_tag_candidates(
        game_title=game_title,
        output_path=output_path,
    )
    write_report_outputs(report=report, args=args, title="Note 标签候选导出报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_validate_note_tag_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-note-tag-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.validate_note_tag_rules(game_title=game_title, rules_text=rules_text)
    write_report_outputs(report=report, args=args, title="Note 标签规则校验报告")
    return 1 if report.status == "error" else 0


async def run_import_note_tag_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-note-tag-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.import_note_tag_rules(
        game_title=game_title,
        rules_text=rules_text,
        confirm_empty=read_bool_arg(args, "confirm_empty"),
    )
    write_report_outputs(report=report, args=args, title="Note 标签规则导入报告")
    return 1 if report.status == "error" else 0


async def run_scan_placeholder_candidates_command(args: argparse.Namespace) -> int:
    """执行 `scan-placeholder-candidates` 命令。"""
    game_title = await resolve_target_game_title(args)
    placeholder_rules_text = await read_optional_text_source_arg(args, "placeholder_rules", "input")
    service = AgentToolkitService()
    report = await service.scan_placeholder_candidates(
        game_title=game_title,
        custom_placeholder_rules_text=placeholder_rules_text,
    )
    write_report_outputs(
        report=report,
        args=args,
        title="自定义控制符候选报告",
        stdout_report=build_sampled_stdout_report(report),
    )
    return 1 if report.status == "error" else 0


async def run_validate_placeholder_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-placeholder-rules` 命令。"""
    game_title = await resolve_optional_target_game_title(args)
    placeholder_rules_text = await read_optional_text_source_arg(args, "placeholder_rules", "input")
    sample_texts = read_optional_str_list_arg(args, "sample") or []
    service = AgentToolkitService()
    report = await service.validate_placeholder_rules(
        game_title=game_title,
        custom_placeholder_rules_text=placeholder_rules_text,
        sample_texts=sample_texts,
    )
    write_report_outputs(report=report, args=args, title="自定义占位符规则校验报告")
    return 1 if report.status == "error" else 0


async def run_build_placeholder_rules_command(args: argparse.Namespace) -> int:
    """执行 `build-placeholder-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    service = AgentToolkitService()
    report = await service.build_placeholder_rules(game_title=game_title, output_path=output_path)
    write_report_outputs(report=report, args=args, title="占位符规则草稿报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_import_placeholder_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-placeholder-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_required_text_source_arg(args, "rules", "input")
    service = AgentToolkitService()
    report = await service.import_placeholder_rules(
        game_title=game_title,
        rules_text=rules_text,
        confirm_empty=read_bool_arg(args, "confirm_empty"),
    )
    write_report_outputs(report=report, args=args, title="自定义占位符规则导入报告")
    return 1 if report.status == "error" else 0


async def run_validate_structured_placeholder_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-structured-placeholder-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    sample_texts = read_optional_str_list_arg(args, "sample") or []
    service = AgentToolkitService()
    report = await service.validate_structured_placeholder_rules(
        game_title=game_title,
        rules_text=rules_text,
        sample_texts=sample_texts,
    )
    write_report_outputs(report=report, args=args, title="结构化占位符规则校验报告")
    return 1 if report.status == "error" else 0


async def run_scan_structured_placeholder_candidates_command(args: argparse.Namespace) -> int:
    """执行 `scan-structured-placeholder-candidates` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.scan_structured_placeholder_candidates(
        game_title=game_title,
        rules_text=rules_text,
    )
    write_report_outputs(
        report=report,
        args=args,
        title="结构化占位符覆盖扫描报告",
        stdout_report=build_sampled_stdout_report(report),
    )
    return 1 if report.status == "error" else 0


async def run_import_structured_placeholder_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-structured-placeholder-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.import_structured_placeholder_rules(
        game_title=game_title,
        rules_text=rules_text,
        confirm_empty=read_bool_arg(args, "confirm_empty"),
    )
    write_report_outputs(report=report, args=args, title="结构化占位符规则导入报告")
    return 1 if report.status == "error" else 0


async def run_validate_plugin_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-plugin-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_required_text_source_arg(args, "rules", "input")
    service = AgentToolkitService()
    report = await service.validate_plugin_rules(game_title=game_title, rules_text=rules_text)
    write_report_outputs(report=report, args=args, title="插件规则校验报告")
    return 1 if report.status == "error" else 0


async def run_validate_plugin_source_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-plugin-source-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.validate_plugin_source_rules(game_title=game_title, rules_text=rules_text)
    write_report_outputs(report=report, args=args, title="插件源码规则校验报告")
    return 1 if report.status == "error" else 0


async def run_import_plugin_source_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-plugin-source-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.import_plugin_source_rules(
        game_title=game_title,
        rules_text=rules_text,
        confirm_empty=read_bool_arg(args, "confirm_empty"),
    )
    write_report_outputs(report=report, args=args, title="插件源码规则导入报告")
    return 1 if report.status == "error" else 0


async def run_validate_event_command_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-event-command-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_required_text_source_arg(args, "rules", "input")
    service = AgentToolkitService()
    report = await service.validate_event_command_rules(game_title=game_title, rules_text=rules_text)
    write_report_outputs(report=report, args=args, title="事件指令规则校验报告")
    return 1 if report.status == "error" else 0


async def run_validate_source_residual_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-source-residual-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_required_text_source_arg(args, "rules", "input")
    service = AgentToolkitService()
    report = await service.validate_source_residual_rules(game_title=game_title, rules_text=rules_text)
    write_report_outputs(report=report, args=args, title="源文残留例外规则校验报告")
    return 1 if report.status == "error" else 0


async def run_import_source_residual_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-source-residual-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_required_text_source_arg(args, "rules", "input")
    service = AgentToolkitService()
    report = await service.import_source_residual_rules(game_title=game_title, rules_text=rules_text)
    write_report_outputs(report=report, args=args, title="源文残留例外规则导入报告")
    return 1 if report.status == "error" else 0


async def run_export_mv_virtual_namebox_candidates_command(args: argparse.Namespace) -> int:
    """执行 `export-mv-virtual-namebox-candidates` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_path = read_required_path_arg(args, "output")
    service = AgentToolkitService()
    report = await service.export_mv_virtual_namebox_candidates(
        game_title=game_title,
        output_path=output_path,
    )
    write_report_outputs(report=report, args=args, title="MV 虚拟名字框候选导出报告", write_output_file=False)
    return 1 if report.status == "error" else 0


async def run_validate_mv_virtual_namebox_rules_command(args: argparse.Namespace) -> int:
    """执行 `validate-mv-virtual-namebox-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.validate_mv_virtual_namebox_rules(game_title=game_title, rules_text=rules_text)
    write_report_outputs(
        report=report,
        args=args,
        title="MV 虚拟名字框规则校验报告",
        stdout_report=build_sampled_stdout_report(report),
    )
    return 1 if report.status == "error" else 0


async def run_import_mv_virtual_namebox_rules_command(args: argparse.Namespace) -> int:
    """执行 `import-mv-virtual-namebox-rules` 命令。"""
    game_title = await resolve_target_game_title(args)
    rules_text = await read_text_file(read_required_path_arg(args, "input"))
    service = AgentToolkitService()
    report = await service.import_mv_virtual_namebox_rules(
        game_title=game_title,
        rules_text=rules_text,
        confirm_empty=read_bool_arg(args, "confirm_empty"),
    )
    write_report_outputs(report=report, args=args, title="MV 虚拟名字框规则导入报告")
    return 1 if report.status == "error" else 0
