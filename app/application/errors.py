"""应用层可预期业务失败类型。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar


class ApplicationBusinessError(RuntimeError):
    """表示应用层已经识别且应直接展示给用户的业务失败。"""

    default_code: ClassVar[str] = "application_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """保存稳定错误码与结构化诊断字段。"""
        super().__init__(message)
        normalized_code = (code or self.default_code).strip()
        if not normalized_code:
            raise ValueError("应用错误码不能为空")
        self.code: str = normalized_code
        self.details: dict[str, object] = dict(details or {})


class WorkflowGateError(ApplicationBusinessError):
    """表示翻译或写文件前置流程检查未通过。"""

    default_code: ClassVar[str] = "workflow_blocked"


class WriteBackGateError(ApplicationBusinessError):
    """表示写入游戏文件前质量或写入条件检查未通过。"""

    default_code: ClassVar[str] = "write_back_blocked"


__all__ = [
    "ApplicationBusinessError",
    "WorkflowGateError",
    "WriteBackGateError",
]
