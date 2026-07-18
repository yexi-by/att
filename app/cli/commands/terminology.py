"""术语表工程命令。

本模块负责导出外部 Agent 可填写的术语表工程，并导入审查后的术语表结果。
"""

from __future__ import annotations

import argparse

from app.agent_toolkit import AgentReport
from app.agent_toolkit.reports import issue
from app.cli.arguments import read_required_path_arg
from app.cli.reports import write_report_outputs
from app.cli.runtime import HandlerSession, resolve_target_game_title
from app.persistence import RecoveryRequiredError


async def run_export_terminology_command(args: argparse.Namespace) -> int:
    """执行 `export-terminology` 命令。"""
    game_title = await resolve_target_game_title(args)
    output_dir = read_required_path_arg(args, "output_dir")
    async with HandlerSession() as handler:
        summary = await handler.export_terminology(game_title=game_title, output_dir=output_dir)
    report = AgentReport.from_parts(
        errors=[],
        warnings=[],
        summary={
            "game": game_title,
            "field_terms_path": str(summary.field_terms_path),
            "glossary_path": str(summary.glossary_path),
            "contexts_dir": str(summary.contexts_dir),
            "entry_count": summary.entry_count,
            "speaker_entry_count": summary.speaker_entry_count,
            "map_entry_count": summary.map_entry_count,
            "database_entry_count": summary.database_entry_count,
            "sample_file_count": summary.sample_file_count,
        },
        details={
            "speaker_context_dir": str(summary.speaker_context_dir),
            "database_context_path": str(summary.database_context_path),
        },
    )
    write_report_outputs(report=report, args=args, title="术语表工程导出报告")
    return 0


async def run_import_terminology_command(args: argparse.Namespace) -> int:
    """执行 `import-terminology` 命令。"""
    game_title = await resolve_target_game_title(args)
    input_path = read_required_path_arg(args, "input")
    glossary_input_path = read_required_path_arg(args, "glossary_input")
    try:
        async with HandlerSession() as handler:
            summary = await handler.import_terminology(
                game_title=game_title,
                input_path=input_path,
                glossary_input_path=glossary_input_path,
            )
    except RecoveryRequiredError:
        raise
    except Exception as error:
        report = AgentReport.from_parts(
            errors=[issue("terminology_invalid", f"术语表导入失败: {type(error).__name__}: {error}")],
            warnings=[],
            summary={"game": game_title, "input": str(input_path), "glossary_input": str(glossary_input_path)},
            details={},
        )
        write_report_outputs(report=report, args=args, title="术语表导入报告")
        return 1
    report = AgentReport.from_parts(
        errors=[],
        warnings=[],
        summary={
            "game": game_title,
            "input": str(input_path),
            "glossary_input": str(glossary_input_path),
            "imported_entry_count": summary.imported_entry_count,
            "filled_entry_count": summary.filled_entry_count,
            "glossary_term_count": summary.glossary_term_count,
        },
        details={},
    )
    write_report_outputs(report=report, args=args, title="术语表导入报告")
    return 0
