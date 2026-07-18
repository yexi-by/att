"""应用层任务摘要模型。"""

from dataclasses import dataclass
from typing import Literal

type TranslationOutcome = Literal[
    "completed",
    "completed_with_quality_errors",
    "stopped",
    "blocked",
    "failed",
    "cancelled",
]
# 调度终态使用稳定字符串，工作流检查失败时还必须原样携带 WorkflowGateIssue.code。
type TranslationStopCode = str
type WriteTransactionRecoveryState = Literal["none", "rolled_back", "finalized"]


@dataclass(slots=True)
class PluginRuleImportSummary:
    """外部插件规则导入任务摘要。"""

    imported_plugin_count: int
    imported_rule_count: int
    deleted_translation_items: int
    deleted_translation_backup_path: str | None = None


@dataclass(slots=True)
class PluginJsonExportSummary:
    """插件配置 JSON 导出任务摘要。"""

    output_path: str
    plugin_count: int


@dataclass(slots=True)
class EventCommandJsonExportSummary:
    """事件指令参数 JSON 导出任务摘要。"""

    output_path: str
    command_count: int


@dataclass(slots=True)
class EventCommandRuleImportSummary:
    """事件指令规则导入任务摘要。"""

    imported_rule_group_count: int
    imported_path_rule_count: int
    deleted_translation_items: int
    deleted_translation_backup_path: str | None = None


@dataclass(slots=True)
class NoteTagJsonExportSummary:
    """Note 标签候选 JSON 导出任务摘要。"""

    output_path: str
    candidate_tag_count: int
    translatable_value_count: int


@dataclass(slots=True)
class NoteTagRuleImportSummary:
    """Note 标签规则导入任务摘要。"""

    imported_file_count: int
    imported_tag_count: int
    deleted_translation_items: int
    deleted_translation_backup_path: str | None = None


@dataclass(slots=True)
class TerminologyImportSummary:
    """外部字段译名表和正文术语表导入任务摘要。"""

    imported_entry_count: int
    filled_entry_count: int
    glossary_term_count: int


@dataclass(slots=True)
class TextTranslationSummary:
    """正文翻译任务摘要。"""

    total_extracted_items: int
    pending_count: int
    deduplicated_count: int
    batch_count: int
    success_count: int
    error_count: int
    llm_failure_count: int = 0
    run_id: str = ""
    blocked_reason: str | None = None
    outcome: TranslationOutcome = "completed"
    stop_code: TranslationStopCode = "none"
    stop_message: str = ""
    dispatched_batch_count: int = 0
    completed_batch_count: int = 0
    undispatched_batch_count: int = 0
    cancelled_batch_count: int = 0
    waiting_permission_cancelled_count: int = 0
    inflight_cancelled_count: int = 0
    completed_after_stop_count: int = 0
    reused_current_run_count: int = 0
    reused_saved_count: int = 0
    context_conflict_count: int = 0
    rejected_reuse_count: int = 0
    physical_request_count: int = 0
    retry_request_count: int = 0
    elapsed_ms: int = 0
    selected_count: int = 0
    remaining_count: int = 0
    limit_reason: str = ""

    @property
    def is_blocked(self) -> bool:
        """判断正文翻译是否因为业务前置条件无法继续。"""
        return self.blocked_reason is not None or self.outcome in {
            "blocked",
            "stopped",
            "failed",
            "cancelled",
        }

    @property
    def has_errors(self) -> bool:
        """判断正文翻译是否产生错误条目。"""
        return self.error_count > 0

    @property
    def is_clean_completion(self) -> bool:
        """判断流水线是否可以继续进入写回。"""
        return self.outcome == "completed" and self.error_count == 0

    @property
    def exit_code(self) -> int:
        """返回 CLI 对当前运行结果使用的稳定退出码。"""
        if self.outcome == "cancelled":
            return 130
        if self.is_blocked:
            return 1
        return 0


@dataclass(slots=True)
class TerminologyWriteSummary:
    """数据库术语表写回任务摘要。"""

    written_count: int
    preserved_translation_count: int


@dataclass(slots=True)
class WriteBackSummary:
    """游戏文件回写任务摘要。"""

    data_item_count: int
    plugin_item_count: int
    terminology_written_count: int
    target_font_name: str | None
    source_font_count: int
    replaced_font_reference_count: int
    font_copied: bool
    planned_file_count: int = 0
    skipped_file_count: int = 0
    plugin_source_ast_source_scan_file_count: int = 0
    plugin_source_ast_runtime_scan_file_count: int = 0
    plugin_source_runtime_map_count: int = 0
    pre_write_check_ms: int = 0
    rust_plan_ms: int = 0
    file_replacement_ms: int = 0
    post_write_audit_ms: int = 0


@dataclass(slots=True)
class FontRestoreSummary:
    """字体引用还原任务摘要。"""

    restored_field_count: int
    restored_reference_count: int
    target_font_name: str | None


@dataclass(slots=True)
class WriteTransactionRecoverySummary:
    """未完成文件写事务的恢复结果。"""

    transaction_id: str | None
    previous_state: str | None
    final_state: WriteTransactionRecoveryState
    restored_file_count: int
    finalized_committed_file_count: int


__all__: list[str] = [
    "EventCommandJsonExportSummary",
    "EventCommandRuleImportSummary",
    "FontRestoreSummary",
    "NoteTagJsonExportSummary",
    "NoteTagRuleImportSummary",
    "PluginJsonExportSummary",
    "PluginRuleImportSummary",
    "TerminologyImportSummary",
    "TerminologyWriteSummary",
    "TextTranslationSummary",
    "TranslationOutcome",
    "TranslationStopCode",
    "WriteBackSummary",
    "WriteTransactionRecoveryState",
    "WriteTransactionRecoverySummary",
]
