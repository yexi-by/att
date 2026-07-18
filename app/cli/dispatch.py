"""命令行子命令分发器。

本模块维护子命令到处理函数的显式映射，保证解析器新增命令时能被测试发现。
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable

from app.application.mutation_guard import assert_game_mutation_allowed
from app.cli.arguments import read_str_arg
from app.cli.commands.registry import (
    run_add_game_command,
    run_doctor_command,
    run_list_command,
    run_self_check_command,
)
from app.cli.commands.rules import (
    run_build_placeholder_rules_command,
    run_export_event_commands_json_command,
    run_export_mv_virtual_namebox_candidates_command,
    run_export_note_tag_candidates_command,
    run_export_plugins_json_command,
    run_import_event_command_rules_command,
    run_import_mv_virtual_namebox_rules_command,
    run_import_note_tag_rules_command,
    run_import_placeholder_rules_command,
    run_import_plugin_rules_command,
    run_import_plugin_source_rules_command,
    run_import_source_residual_rules_command,
    run_import_structured_placeholder_rules_command,
    run_scan_placeholder_candidates_command,
    run_scan_structured_placeholder_candidates_command,
    run_validate_event_command_rules_command,
    run_validate_mv_virtual_namebox_rules_command,
    run_validate_note_tag_rules_command,
    run_validate_placeholder_rules_command,
    run_validate_plugin_rules_command,
    run_validate_plugin_source_rules_command,
    run_validate_source_residual_rules_command,
    run_validate_structured_placeholder_rules_command,
)
from app.cli.commands.terminology import run_export_terminology_command, run_import_terminology_command
from app.cli.commands.translation import (
    run_audit_active_runtime_command,
    run_audit_coverage_command,
    run_diagnose_active_runtime_command,
    run_export_pending_translations_command,
    run_export_plugin_source_ast_map_command,
    run_export_quality_fix_template_command,
    run_import_manual_translations_command,
    run_quality_report_command,
    run_reset_translations_command,
    run_scan_plugin_source_text_command,
    run_text_scope_command,
    run_translate_command,
    run_translation_status_command,
    run_verify_feedback_text_command,
)
from app.cli.commands.workspace import (
    run_cleanup_agent_workspace_command,
    run_prepare_agent_workspace_command,
    run_validate_agent_workspace_command,
)
from app.cli.commands.write_back import (
    run_all_command,
    run_rebuild_active_runtime_command,
    run_recover_write_transaction_command,
    run_restore_font_command,
    run_write_back_command,
    run_write_terminology_command,
)
from app.cli.errors import CliBusinessError
from app.cli.runtime import resolve_target_game_title
from app.diagnostics import diagnostic_stage
from app.persistence import GameRegistry

CommandHandler = Callable[[argparse.Namespace], Awaitable[int]]

MUTATING_COMMAND_NAMES = frozenset(
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

# 新注册没有可恢复的旧事务；重复注册由 GameRegistry.register_game 在首次写入前
# 对既有数据库执行同一事务检查。其余修改命令必须在读取规则或写目标前统一预检。
MUTATION_PREFLIGHT_COMMAND_NAMES = MUTATING_COMMAND_NAMES - {"add-game"}

COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "list": run_list_command,
    "self-check": run_self_check_command,
    "doctor": run_doctor_command,
    "add-game": run_add_game_command,
    "export-plugins-json": run_export_plugins_json_command,
    "import-plugin-rules": run_import_plugin_rules_command,
    "export-event-commands-json": run_export_event_commands_json_command,
    "import-event-command-rules": run_import_event_command_rules_command,
    "export-note-tag-candidates": run_export_note_tag_candidates_command,
    "validate-note-tag-rules": run_validate_note_tag_rules_command,
    "import-note-tag-rules": run_import_note_tag_rules_command,
    "scan-placeholder-candidates": run_scan_placeholder_candidates_command,
    "validate-placeholder-rules": run_validate_placeholder_rules_command,
    "build-placeholder-rules": run_build_placeholder_rules_command,
    "import-placeholder-rules": run_import_placeholder_rules_command,
    "validate-structured-placeholder-rules": run_validate_structured_placeholder_rules_command,
    "scan-structured-placeholder-candidates": run_scan_structured_placeholder_candidates_command,
    "import-structured-placeholder-rules": run_import_structured_placeholder_rules_command,
    "validate-plugin-rules": run_validate_plugin_rules_command,
    "validate-plugin-source-rules": run_validate_plugin_source_rules_command,
    "import-plugin-source-rules": run_import_plugin_source_rules_command,
    "validate-event-command-rules": run_validate_event_command_rules_command,
    "prepare-agent-workspace": run_prepare_agent_workspace_command,
    "validate-agent-workspace": run_validate_agent_workspace_command,
    "cleanup-agent-workspace": run_cleanup_agent_workspace_command,
    "quality-report": run_quality_report_command,
    "text-scope": run_text_scope_command,
    "audit-coverage": run_audit_coverage_command,
    "audit-active-runtime": run_audit_active_runtime_command,
    "diagnose-active-runtime": run_diagnose_active_runtime_command,
    "verify-feedback-text": run_verify_feedback_text_command,
    "scan-plugin-source-text": run_scan_plugin_source_text_command,
    "export-plugin-source-ast-map": run_export_plugin_source_ast_map_command,
    "export-pending-translations": run_export_pending_translations_command,
    "export-quality-fix-template": run_export_quality_fix_template_command,
    "import-manual-translations": run_import_manual_translations_command,
    "reset-translations": run_reset_translations_command,
    "validate-source-residual-rules": run_validate_source_residual_rules_command,
    "import-source-residual-rules": run_import_source_residual_rules_command,
    "export-mv-virtual-namebox-candidates": run_export_mv_virtual_namebox_candidates_command,
    "validate-mv-virtual-namebox-rules": run_validate_mv_virtual_namebox_rules_command,
    "import-mv-virtual-namebox-rules": run_import_mv_virtual_namebox_rules_command,
    "translation-status": run_translation_status_command,
    "translate": run_translate_command,
    "write-back": run_write_back_command,
    "rebuild-active-runtime": run_rebuild_active_runtime_command,
    "restore-font": run_restore_font_command,
    "recover-write-transaction": run_recover_write_transaction_command,
    "export-terminology": run_export_terminology_command,
    "import-terminology": run_import_terminology_command,
    "write-terminology": run_write_terminology_command,
    "run-all": run_all_command,
}


def registered_command_names() -> frozenset[str]:
    """返回分发器当前支持的子命令集合。"""
    return frozenset(COMMAND_HANDLERS)


async def dispatch_command(args: argparse.Namespace) -> int:
    """分发并执行用户选择的子命令。"""
    command = read_str_arg(args, "command")
    handler = COMMAND_HANDLERS.get(command)
    if handler is None:
        raise CliBusinessError(f"未知命令：{command}")
    if command in MUTATION_PREFLIGHT_COMMAND_NAMES:
        with diagnostic_stage("mutation_preflight"):
            game_title = await resolve_target_game_title(args)
            await assert_game_mutation_allowed(GameRegistry(), game_title)
    return await handler(args)


__all__ = [
    "MUTATING_COMMAND_NAMES",
    "MUTATION_PREFLIGHT_COMMAND_NAMES",
    "dispatch_command",
    "registered_command_names",
]
