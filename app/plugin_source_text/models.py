"""插件源码文本扫描、规则和候选数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.rmmz.text_rules import JsonArray, JsonObject, JsonValue

type PluginSourceEnabledFileStatus = Literal["present", "read_error", "missing"]


@dataclass(frozen=True, slots=True)
class PluginSourceEnabledFileState:
    """一个启用插件在翻译源源码集合中的完整读取状态。"""

    file_name: str
    status: PluginSourceEnabledFileStatus
    file_hash: str = ""
    read_error: str = ""

    def __post_init__(self) -> None:
        """拒绝状态与附带事实不一致的插件源码记录。"""
        if not self.file_name:
            raise ValueError("启用插件源码文件名不能为空")
        if self.status == "present":
            if not self.file_hash or self.read_error:
                raise ValueError(f"present 插件源码状态必须只包含文件哈希: {self.file_name}")
            return
        if self.status == "read_error":
            if self.file_hash or not self.read_error:
                raise ValueError(f"read_error 插件源码状态必须只包含读取错误: {self.file_name}")
            return
        if self.file_hash or self.read_error:
            raise ValueError(f"missing 插件源码状态不能包含文件哈希或读取错误: {self.file_name}")

    def to_json_object(self) -> JsonObject:
        """转换成风险报告使用的稳定 JSON 对象。"""
        payload: JsonObject = {
            "file": self.file_name,
            "status": self.status,
        }
        if self.file_hash:
            payload["file_hash"] = self.file_hash
        if self.read_error:
            payload["read_error"] = self.read_error
        return payload


@dataclass(frozen=True, slots=True)
class PluginSourceCandidate:
    """插件源码中一个 AST 字符串候选。"""

    file_name: str
    selector: str
    text: str
    raw_text: str
    quote: str
    line: int
    start_index: int
    end_index: int
    content_start_index: int
    content_end_index: int
    context: str
    api: str
    key: str
    ast_context: JsonObject
    active: bool
    confidence: str
    structural_flags: tuple[str, ...]

    def to_json_object(self) -> JsonObject:
        """转换成 Agent 可读 JSON 对象。"""
        return {
            "file": self.file_name,
            "line": self.line,
            "selector": self.selector,
            "text": self.text,
            "context": self.context,
            "api": self.api,
            "key": self.key,
            "ast_context": {key: value for key, value in self.ast_context.items()},
            "active": self.active,
            "confidence": self.confidence,
            "structural_flags": [flag for flag in self.structural_flags],
        }


@dataclass(frozen=True, slots=True)
class PluginSourceFileScan:
    """单个插件源码文件的扫描结果。"""

    file_name: str
    file_hash: str
    active: bool
    candidates: tuple[PluginSourceCandidate, ...]
    strong_context_text_count: int
    medium_confidence_text_count: int
    file_score: int

    def to_json_object(self) -> JsonObject:
        """转换成 AST 地图文件中的单文件对象。"""
        return {
            "file": self.file_name,
            "file_hash": self.file_hash,
            "active": self.active,
            "strong_context_text_count": self.strong_context_text_count,
            "medium_confidence_text_count": self.medium_confidence_text_count,
            "file_score": self.file_score,
            "candidates": [candidate.to_json_object() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class PluginSourceRisk:
    """插件源码文本风险摘要。"""

    high_risk: bool
    risk_score: int
    strong_context_text_count: int
    medium_confidence_text_count: int
    scanned_file_count: int
    ignored_file_count: int
    read_error_file_count: int
    files_score_ge_250: int
    max_file_score: int

    def to_json_object(self) -> JsonObject:
        """转换成风险报告 JSON 对象。"""
        return {
            "high_risk": self.high_risk,
            "risk_score": self.risk_score,
            "strong_context_text_count": self.strong_context_text_count,
            "medium_confidence_text_count": self.medium_confidence_text_count,
            "scanned_file_count": self.scanned_file_count,
            "ignored_file_count": self.ignored_file_count,
            "read_error_file_count": self.read_error_file_count,
            "files_score_ge_250": self.files_score_ge_250,
            "max_file_score": self.max_file_score,
            "thresholds": {
                "strong_context_text_count": 300,
                "risk_score": 2000,
                "files_score_ge_250": 3,
                "single_file_score": 300,
                "single_file_strong_context_text_count": 80,
            },
        }


@dataclass(frozen=True, slots=True)
class PluginSourceScan:
    """插件源码扫描总结果。"""

    risk: PluginSourceRisk
    files: tuple[PluginSourceFileScan, ...]
    candidates: tuple[PluginSourceCandidate, ...]
    enabled_file_states: tuple[PluginSourceEnabledFileState, ...]

    @property
    def enabled_plugin_files(self) -> frozenset[str]:
        """从单一状态事实派生完整启用插件源码文件集合。"""
        return frozenset(state.file_name for state in self.enabled_file_states)

    @property
    def missing_enabled_file_count(self) -> int:
        """返回翻译源中缺失的启用插件源码文件数量。"""
        return sum(1 for state in self.enabled_file_states if state.status == "missing")

    @property
    def unreadable_enabled_file_count(self) -> int:
        """返回翻译源中存在但读取失败的启用插件源码文件数量。"""
        return sum(1 for state in self.enabled_file_states if state.status == "read_error")

    def to_json_object(self) -> JsonObject:
        """转换成完整 AST 地图 JSON 对象。"""
        enabled_plugin_files: list[JsonValue] = [
            cast(JsonValue, file_name) for file_name in sorted(self.enabled_plugin_files)
        ]
        return {
            "risk": self.risk.to_json_object(),
            "enabled_plugin_files": enabled_plugin_files,
            "enabled_plugin_file_states": [state.to_json_object() for state in self.enabled_file_states],
            "candidate_count": len(self.candidates),
            "files": [file_scan.to_json_object() for file_scan in self.files],
        }

    def risk_report_json(self) -> JsonObject:
        """转换成默认工作区使用的轻量风险报告。"""
        enabled_plugin_files: list[JsonValue] = [
            cast(JsonValue, file_name) for file_name in sorted(self.enabled_plugin_files)
        ]
        return {
            "risk": self.risk.to_json_object(),
            "enabled_plugin_files": enabled_plugin_files,
            "enabled_plugin_file_states": [state.to_json_object() for state in self.enabled_file_states],
            "candidate_count": len(self.candidates),
            "active_candidate_count": sum(1 for candidate in self.candidates if candidate.active),
        }

    def candidates_json(self) -> JsonArray:
        """返回插件源码候选数组。"""
        return [candidate.to_json_object() for candidate in self.candidates]


class PluginSourceRuleImportEntry(BaseModel):
    """插件源码规则导入文件中的单文件规则。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    file: str
    selectors: list[str] = Field(default_factory=list)
    excluded_selectors: list[str] = Field(default_factory=list)


class PluginSourceRuleImportFile(BaseModel):
    """插件源码规则导入文件。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    rules: list[PluginSourceRuleImportEntry] = Field(default_factory=list)


__all__ = [
    "PluginSourceCandidate",
    "PluginSourceFileScan",
    "PluginSourceRisk",
    "PluginSourceRuleImportEntry",
    "PluginSourceRuleImportFile",
    "PluginSourceScan",
]
