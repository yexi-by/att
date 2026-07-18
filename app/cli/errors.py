"""命令行异常类型。

本模块负责区分已知业务失败和参数解析失败，便于入口层生成稳定退出码和 JSON 错误。
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import NoReturn, override


class CliBusinessError(Exception):
    """表示命令行任务遇到了带稳定错误码的已知失败。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_command_input",
        details: Mapping[str, object] | None = None,
    ) -> None:
        """保存可供 JSON 调用方判断的稳定错误对象。"""
        super().__init__(message)
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError("CLI 错误码不能为空")
        self.code: str = normalized_code
        self.details: dict[str, object] = dict(details or {})


class CliArgumentError(Exception):
    """表示命令行参数解析失败。"""


class CliArgumentParser(argparse.ArgumentParser):
    """把 argparse 默认退出改成可被 JSON 输出层接管的异常。"""

    @override
    def error(self, message: str) -> NoReturn:
        """抛出参数错误，避免 argparse 直接写 stderr 并退出。"""
        raise CliArgumentError(message)


__all__ = ["CliArgumentError", "CliArgumentParser", "CliBusinessError"]
