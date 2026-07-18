"""命令行报告输出工具。

本模块负责把业务摘要转换为稳定 JSON 报告。
"""

from __future__ import annotations

import argparse

from app.agent_toolkit import AgentIssue, AgentReport
from app.agent_toolkit.reports import issue
from app.application.handler import (
    FontRestoreSummary,
    TerminologyWriteSummary,
    TextTranslationSummary,
    WriteBackSummary,
    WriteTransactionRecoverySummary,
)
from app.cli.arguments import read_optional_path_arg
from app.observability import logger
from app.rmmz.json_types import JsonArray, JsonObject, JsonValue

REPORT_STDOUT_SAMPLE_LIMIT = 20


def build_translate_summary_report(summary: TextTranslationSummary) -> AgentReport:
    """把正文翻译摘要转换为稳定 JSON 报告。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    if summary.is_blocked:
        error_code = "translation_blocked" if summary.stop_code == "none" else summary.stop_code
        errors.append(
            issue(
                error_code,
                summary.stop_message or summary.blocked_reason or "正文翻译未完成",
            )
        )
    if summary.has_errors:
        warnings.append(
            issue(
                "translation_quality_errors",
                f"本轮翻译有 {summary.error_count} 条模型翻了但项目检查没通过的译文；可以继续运行 translate，或导出手动填写译文表修复",
            )
        )
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary=_build_translation_summary_object(summary),
        details={},
    )


def build_write_back_summary_report(summary: WriteBackSummary) -> AgentReport:
    """把游戏文件回写摘要转换为稳定 JSON 报告。"""
    return AgentReport.from_parts(
        errors=[],
        warnings=[],
        summary=_build_write_back_summary_object(summary),
        details={},
    )


def build_terminology_write_summary_report(summary: TerminologyWriteSummary) -> AgentReport:
    """把术语专用写入摘要转换为稳定 JSON 报告。"""
    return AgentReport.from_parts(
        errors=[],
        warnings=[],
        summary={
            "written_count": summary.written_count,
            "preserved_translation_count": summary.preserved_translation_count,
        },
        details={},
    )


def build_run_all_summary_report(
    *,
    text_summary: TextTranslationSummary,
    write_back_summary: WriteBackSummary | None,
) -> AgentReport:
    """把 `run-all` 翻译和写文件结果转换为稳定 JSON 报告。"""
    write_back_performed = write_back_summary is not None
    summary: JsonObject = {
        **_build_translation_summary_object(text_summary),
        "write_back_performed": write_back_performed,
        "write_back_skipped": not write_back_performed,
        "write_back_planned_file_count": 0,
        "write_back_skipped_file_count": 0,
        "write_back_data_item_count": 0,
        "write_back_plugin_item_count": 0,
        "write_back_terminology_written_count": 0,
    }
    details: JsonObject = {
        "translation": _build_translation_summary_object(text_summary),
        "write_back": None,
    }
    if write_back_summary is not None:
        write_summary = _build_write_back_summary_object(write_back_summary)
        summary.update(
            {
                "write_back_planned_file_count": write_back_summary.planned_file_count,
                "write_back_skipped_file_count": write_back_summary.skipped_file_count,
                "write_back_data_item_count": write_back_summary.data_item_count,
                "write_back_plugin_item_count": write_back_summary.plugin_item_count,
                "write_back_terminology_written_count": write_back_summary.terminology_written_count,
            }
        )
        details["write_back"] = write_summary
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    if text_summary.is_blocked:
        message = (
            text_summary.stop_message or text_summary.blocked_reason or "正文翻译没有干净完成，因此没有执行 write-back"
        )
        error_code = "translation_blocked" if text_summary.stop_code == "none" else text_summary.stop_code
        errors.append(issue(error_code, message))
    elif text_summary.has_errors:
        warnings.append(
            issue(
                "translation_quality_errors",
                f"本轮有 {text_summary.error_count} 条译文未通过检查，已跳过 write-back",
            )
        )
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary=summary,
        details=details,
    )


def build_font_restore_summary_report(summary: FontRestoreSummary) -> AgentReport:
    """把字体还原摘要转换为稳定 JSON 报告。"""
    warnings: list[AgentIssue] = []
    if summary.target_font_name is None:
        warnings.append(issue("font_restore", "没有候选覆盖字体名称，无法判断需要还原哪个新字体引用"))
    elif summary.restored_reference_count == 0:
        warnings.append(issue("font_restore", "没有找到需要还原的覆盖字体引用"))
    return AgentReport.from_parts(
        errors=[],
        warnings=warnings,
        summary={
            "restored_field_count": summary.restored_field_count,
            "restored_reference_count": summary.restored_reference_count,
            "target_font_name": summary.target_font_name or "",
        },
        details={},
    )


def build_write_transaction_recovery_summary_report(
    summary: WriteTransactionRecoverySummary,
) -> AgentReport:
    """把文件写事务恢复结果转换为稳定 JSON 报告。"""
    warnings: list[AgentIssue] = []
    if summary.final_state == "none":
        warnings.append(issue("write_transaction_not_found", "当前游戏没有未完成写事务"))
    return AgentReport.from_parts(
        errors=[],
        warnings=warnings,
        summary={
            "transaction_id": summary.transaction_id or "",
            "previous_state": summary.previous_state or "",
            "final_state": summary.final_state,
            "restored_file_count": summary.restored_file_count,
            "finalized_committed_file_count": summary.finalized_committed_file_count,
        },
        details={},
    )


def build_sampled_stdout_report(
    report: AgentReport,
    *,
    sample_limit: int = REPORT_STDOUT_SAMPLE_LIMIT,
) -> AgentReport:
    """把大报告明细裁剪为适合 stdout 的摘要报告。"""
    return AgentReport(
        status=report.status,
        errors=list(report.errors),
        warnings=list(report.warnings),
        summary=dict(report.summary),
        details=_summarize_json_object(report.details, sample_limit=sample_limit),
        diagnostics=report.diagnostics,
    )


def _summarize_json_object(value: JsonObject, *, sample_limit: int) -> JsonObject:
    """递归裁剪 JSON 对象里的数组明细。"""
    summary: JsonObject = {}
    for key, child in value.items():
        summary[key] = _summarize_json_value(child, sample_limit=sample_limit)
    return summary


def _summarize_json_value(value: JsonValue, *, sample_limit: int) -> JsonValue:
    """把 JSON 值转换为摘要值。"""
    if isinstance(value, list):
        return _summarize_json_array(value, sample_limit=sample_limit)
    if isinstance(value, dict):
        return _summarize_json_object(value, sample_limit=sample_limit)
    return value


def _summarize_json_array(value: JsonArray, *, sample_limit: int) -> JsonObject:
    """把数组转换为计数、样例和省略数量。"""
    effective_limit = max(0, sample_limit)
    samples: JsonArray = [_summarize_json_value(item, sample_limit=sample_limit) for item in value[:effective_limit]]
    return {
        "count": len(value),
        "samples": samples,
        "omitted_count": max(0, len(value) - effective_limit),
    }


def write_report_outputs(
    *,
    report: AgentReport,
    args: argparse.Namespace,
    title: str,
    write_output_file: bool = True,
    stdout_report: AgentReport | None = None,
) -> None:
    """输出 Agent 工具包报告。"""
    _ = title
    output_path = read_optional_path_arg(args, "output") if write_output_file else None
    json_text = report.to_json_text()
    display_report = stdout_report or report
    display_json_text = display_report.to_json_text()
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _ = output_path.write_text(f"{json_text}\n", encoding="utf-8")

    print(display_json_text)
    if output_path is not None:
        logger.success(f"[tag.success]JSON 报告已写出[/tag.success] 文件 [tag.path]{output_path}[/tag.path]")


def _build_translation_summary_object(summary: TextTranslationSummary) -> JsonObject:
    """构建正文翻译摘要 JSON 对象。"""
    return {
        "run_id": summary.run_id,
        "outcome": summary.outcome,
        "stop_code": summary.stop_code,
        "stop_message": summary.stop_message,
        "total_extracted_items": summary.total_extracted_items,
        "pending_count": summary.pending_count,
        "selected_count": summary.selected_count,
        "remaining_count": summary.remaining_count,
        "limit_reason": summary.limit_reason,
        "deduplicated_count": summary.deduplicated_count,
        "batch_count": summary.batch_count,
        "planned_batch_count": summary.batch_count,
        "success_count": summary.success_count,
        "quality_error_count": summary.error_count,
        "llm_failure_count": summary.llm_failure_count,
        "dispatched_batch_count": summary.dispatched_batch_count,
        "completed_batch_count": summary.completed_batch_count,
        "undispatched_batch_count": summary.undispatched_batch_count,
        "cancelled_batch_count": summary.cancelled_batch_count,
        "waiting_permission_cancelled_count": summary.waiting_permission_cancelled_count,
        "inflight_cancelled_count": summary.inflight_cancelled_count,
        "completed_after_stop_count": summary.completed_after_stop_count,
        "reused_current_run_count": summary.reused_current_run_count,
        "reused_saved_count": summary.reused_saved_count,
        "context_conflict_count": summary.context_conflict_count,
        "rejected_reuse_count": summary.rejected_reuse_count,
        "physical_request_count": summary.physical_request_count,
        "retry_request_count": summary.retry_request_count,
        "elapsed_ms": summary.elapsed_ms,
    }


def _build_write_back_summary_object(summary: WriteBackSummary) -> JsonObject:
    """构建游戏文件写入摘要 JSON 对象。"""
    return {
        "data_item_count": summary.data_item_count,
        "plugin_item_count": summary.plugin_item_count,
        "terminology_written_count": summary.terminology_written_count,
        "target_font_name": summary.target_font_name or "",
        "source_font_count": summary.source_font_count,
        "replaced_font_reference_count": summary.replaced_font_reference_count,
        "font_copied": summary.font_copied,
        "planned_file_count": summary.planned_file_count,
        "skipped_file_count": summary.skipped_file_count,
        "plugin_source_ast_source_scan_file_count": summary.plugin_source_ast_source_scan_file_count,
        "plugin_source_ast_runtime_scan_file_count": summary.plugin_source_ast_runtime_scan_file_count,
        "plugin_source_runtime_map_count": summary.plugin_source_runtime_map_count,
        "pre_write_check_ms": summary.pre_write_check_ms,
        "rust_plan_ms": summary.rust_plan_ms,
        "file_replacement_ms": summary.file_replacement_ms,
        "post_write_audit_ms": summary.post_write_audit_ms,
    }


__all__ = [
    "build_font_restore_summary_report",
    "build_run_all_summary_report",
    "build_sampled_stdout_report",
    "build_terminology_write_summary_report",
    "build_translate_summary_report",
    "build_write_back_summary_report",
    "build_write_transaction_recovery_summary_report",
    "REPORT_STDOUT_SAMPLE_LIMIT",
    "write_report_outputs",
]
