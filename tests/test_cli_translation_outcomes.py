"""正文翻译结构化终态报告测试。"""

from app.application.summaries import (
    TextTranslationSummary,
    TranslationOutcome,
    TranslationStopCode,
)
from app.cli.reports import (
    build_run_all_summary_report,
    build_translate_summary_report,
)


def _summary(
    *,
    outcome: TranslationOutcome = "completed",
    stop_code: TranslationStopCode = "none",
    stop_message: str = "",
    success_count: int = 0,
    error_count: int = 0,
    dispatched_batch_count: int = 0,
    completed_batch_count: int = 0,
    undispatched_batch_count: int = 0,
    physical_request_count: int = 0,
    retry_request_count: int = 0,
) -> TextTranslationSummary:
    return TextTranslationSummary(
        total_extracted_items=10,
        pending_count=10,
        deduplicated_count=8,
        batch_count=4,
        success_count=success_count,
        error_count=error_count,
        outcome=outcome,
        stop_code=stop_code,
        stop_message=stop_message,
        dispatched_batch_count=dispatched_batch_count,
        completed_batch_count=completed_batch_count,
        undispatched_batch_count=undispatched_batch_count,
        physical_request_count=physical_request_count,
        retry_request_count=retry_request_count,
    )


def test_stopped_translation_report_preserves_machine_readable_reason_and_counts() -> None:
    summary = _summary(
        outcome="stopped",
        stop_code="quality_error_rate_reached",
        stop_message="检查没通过的译文比例达到停止阈值: 0.5",
        success_count=2,
        error_count=2,
        dispatched_batch_count=2,
        completed_batch_count=2,
        undispatched_batch_count=2,
        physical_request_count=3,
        retry_request_count=1,
    )

    report = build_translate_summary_report(summary)

    assert summary.exit_code == 1
    assert report.status == "error"
    assert report.errors[0].code == "quality_error_rate_reached"
    assert report.summary["outcome"] == "stopped"
    assert report.summary["undispatched_batch_count"] == 2
    assert report.summary["physical_request_count"] == 3
    assert report.summary["retry_request_count"] == 1


def test_quality_errors_are_warning_but_run_all_skips_write_back() -> None:
    summary = _summary(
        outcome="completed_with_quality_errors",
        success_count=8,
        error_count=2,
    )

    translate_report = build_translate_summary_report(summary)
    run_all_report = build_run_all_summary_report(
        text_summary=summary,
        write_back_summary=None,
    )

    assert summary.exit_code == 0
    assert translate_report.status == "warning"
    assert run_all_report.status == "warning"
    assert run_all_report.summary["write_back_performed"] is False
    assert run_all_report.summary["write_back_skipped"] is True


def test_cancelled_translation_uses_exit_code_130() -> None:
    summary = _summary(
        outcome="cancelled",
        stop_code="user_cancelled",
        stop_message="用户取消了正文翻译",
    )

    report = build_translate_summary_report(summary)

    assert summary.exit_code == 130
    assert report.status == "error"
    assert report.errors[0].code == "user_cancelled"


def test_workflow_gate_reports_preserve_actionable_event_and_plugin_codes() -> None:
    """翻译 JSON 不得把具体工作流问题重新泛化成统一错误码。"""
    for stop_code in ("event_command_text_missing", "plugin_source_assessment_missing"):
        summary = _summary(
            outcome="blocked",
            stop_code=stop_code,
            stop_message="检查没通过，不能继续",
        )

        translate_report = build_translate_summary_report(summary)
        run_all_report = build_run_all_summary_report(text_summary=summary, write_back_summary=None)

        assert translate_report.errors[0].code == stop_code
        assert run_all_report.errors[0].code == stop_code
        assert translate_report.summary["stop_code"] == stop_code
