"""Agent 工具包报告模型。"""

import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.diagnostics import current_diagnostic_snapshot
from app.rmmz.text_rules import JsonObject

type AgentReportStatus = Literal["ok", "warning", "error"]


class AgentIssue(BaseModel):
    """诊断报告中的单条问题。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    code: str
    message: str


class AgentDiagnostics(BaseModel):
    """仅在 `--debug` 报告中出现的阶段诊断。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    timings: dict[str, int] = Field(default_factory=dict)
    scan_counts: dict[str, int] = Field(default_factory=dict)


class AgentReport(BaseModel):
    """供终端和外部 Agent 使用的统一报告结构。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    status: AgentReportStatus
    errors: list[AgentIssue] = Field(default_factory=list)
    warnings: list[AgentIssue] = Field(default_factory=list)
    summary: JsonObject = Field(default_factory=dict)
    details: JsonObject = Field(default_factory=dict)
    diagnostics: AgentDiagnostics | None = None

    @classmethod
    def from_parts(
        cls,
        *,
        errors: list[AgentIssue],
        warnings: list[AgentIssue],
        summary: JsonObject,
        details: JsonObject,
    ) -> "AgentReport":
        """根据错误和告警集合构造报告状态。"""
        if errors:
            status: AgentReportStatus = "error"
        elif warnings:
            status = "warning"
        else:
            status = "ok"
        return cls(
            status=status,
            errors=errors,
            warnings=warnings,
            summary=summary,
            details=details,
        )

    def to_json_text(self) -> str:
        """序列化为稳定的 UTF-8 JSON 文本。"""
        payload = self.model_dump(mode="json")
        if self.diagnostics is None:
            diagnostic_snapshot = current_diagnostic_snapshot()
            if diagnostic_snapshot is None:
                if "diagnostics" in payload:
                    del payload["diagnostics"]
            else:
                payload["diagnostics"] = AgentDiagnostics(
                    timings=diagnostic_snapshot.timings,
                    scan_counts=diagnostic_snapshot.scan_counts,
                ).model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=False, indent=2)


def issue(code: str, message: str) -> AgentIssue:
    """创建报告问题对象。"""
    return AgentIssue(code=code, message=message)


__all__: list[str] = [
    "AgentDiagnostics",
    "AgentIssue",
    "AgentReport",
    "AgentReportStatus",
    "issue",
]
