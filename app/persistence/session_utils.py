"""数据库会话记录转换与键生成工具。"""

import hashlib
from datetime import datetime
from pathlib import Path

from app.rmmz.schema import (
    ErrorType,
    EventCommandTextRuleRecord,
    LlmFailureCategory,
    SourceResidualRuleType,
    TranslationRunStatus,
)
from app.terminology.schemas import TERMINOLOGY_CATEGORIES, TerminologyCategory


def build_event_command_group_key(rule_record: EventCommandTextRuleRecord) -> str:
    """生成事件指令规则组主键。"""
    filter_text = "|".join(
        f"{parameter_filter.index}={parameter_filter.value}" for parameter_filter in rule_record.parameter_filters
    )
    payload = f"{rule_record.command_code}:{filter_text}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"event_{rule_record.command_code}_{digest}"


def current_timestamp_text() -> str:
    """生成数据库状态记录使用的本地时间文本。"""
    return datetime.now().isoformat(timespec="seconds")


def parse_translation_run_status(value: str, db_path: Path) -> TranslationRunStatus:
    """校验并收窄数据库中的翻译运行状态。"""
    allowed: set[TranslationRunStatus] = {"running", "completed", "blocked", "cancelled", "failed", "stopped"}
    if value in allowed:
        return value
    raise RuntimeError(f"数据库字段 status 不是有效翻译运行状态: {db_path}")


def parse_llm_failure_category(value: str, db_path: Path) -> LlmFailureCategory:
    """校验并收窄数据库中的模型故障分类。"""
    allowed: set[LlmFailureCategory] = {
        "rate_limit",
        "timeout",
        "connection",
        "server",
        "conflict",
        "fatal",
        "unknown",
    }
    if value in allowed:
        return value
    raise RuntimeError(f"数据库字段 category 不是有效模型故障分类: {db_path}")


def parse_error_type(value: str, db_path: Path) -> ErrorType:
    """校验并收窄数据库中的译文检查错误类型。"""
    allowed: set[ErrorType] = {
        "模型返回不可解析",
        "AI漏翻",
        "文本结构不匹配",
        "控制符不匹配",
        "源文残留",
        "选项行数不匹配",
    }
    if value in allowed:
        return value
    raise RuntimeError(f"数据库字段 error_type 不是有效译文检查错误类型: {db_path}")


def parse_source_residual_rule_type(value: str, db_path: Path) -> SourceResidualRuleType:
    """校验并收窄数据库中的源文残留例外规则类型。"""
    if value == "position" or value == "structural":
        return value
    raise RuntimeError(f"数据库字段 rule_type 不是有效源文残留例外规则类型: {db_path}")


def parse_terminology_category(value: str, db_path: Path) -> TerminologyCategory:
    """校验并收窄数据库中的术语类别。"""
    if value in TERMINOLOGY_CATEGORIES:
        return value
    raise RuntimeError(f"数据库字段 category 不是有效术语类别: {db_path}")
