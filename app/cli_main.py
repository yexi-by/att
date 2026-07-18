"""
项目命令行启动入口。

本模块只负责解析全局参数、初始化日志、分发子命令和统一返回退出码。
具体业务流程由 `app.cli` 负责适配到 `TranslationHandler`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import warnings
from collections.abc import Sequence
from io import TextIOWrapper
from pathlib import Path


def _configure_stdio_encoding() -> None:
    """
    尽量把标准输出和标准错误切换为 UTF-8。

    Windows 终端与自动化工具的默认编码不一定一致，提前设置编码可以减少
    中文帮助信息和日志在命令行中出现乱码的概率。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if isinstance(stream, TextIOWrapper):
                stream.reconfigure(encoding="utf-8")
        except Exception:
            continue


def _suppress_known_third_party_warnings() -> None:
    """
    屏蔽已确认不影响当前项目运行的第三方已知警告。

    这里不能粗暴关闭全部 `UserWarning`，否则会把真正有价值的运行时提示一起吞掉。
    当前只精确屏蔽 `volcenginesdkarkruntime` 在 Python 3.14 下发出的那一条
    兼容性提示，避免每次启动 CLI 都污染终端输出。
    """
    warnings.filterwarnings(
        action="ignore",
        message=(
            r"Core Pydantic V1 functionality isn't compatible with Python 3\.14 "
            r"or greater\."
        ),
        category=UserWarning,
        module=r"volcenginesdkarkruntime\._compat",
    )


_configure_stdio_encoding()
_suppress_known_third_party_warnings()

from app.application.errors import ApplicationBusinessError  # noqa: E402
from app.cli import (  # noqa: E402
    CliArgumentError,
    CliBusinessError,
    build_parser,
    dispatch_command,
    format_argv,
    format_namespace,
)
from app.diagnostics import command_diagnostics, current_diagnostic_snapshot, diagnostic_stage  # noqa: E402
from app.observability import logger, resolve_log_file_path, setup_logger  # noqa: E402
from app.persistence.errors import PersistenceBusinessError  # noqa: E402


def format_exception_summary(error: BaseException) -> str:
    """
    将异常压缩为适合终端首行展示的稳定摘要。

    Args:
        error: 当前捕获到的异常对象。

    Returns:
        `异常类型: 异常信息` 形式的简短摘要；若异常消息为空则仅返回类型名。
    """
    message = str(error).strip()
    if message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__


def is_debug_enabled(args: argparse.Namespace) -> bool:
    """
    从 argparse 命名空间中读取全局调试开关。

    Args:
        args: argparse 返回的命名空间对象。

    Returns:
        用户是否启用了 `--debug`。
    """
    debug_value = getattr(args, "debug", False)
    return isinstance(debug_value, bool) and debug_value


def raw_flag_enabled(argv: Sequence[str], flag: str) -> bool:
    """
    在参数解析前检查原始开关是否存在。

    解析失败时仍需决定 stdout 是否保持 JSON，以及终端日志是否使用 Agent 模式。
    """
    return flag in argv


def print_json_error(
    *,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
    detail: str = "",
) -> None:
    """向 stdout 输出统一结构的 JSON 错误报告。

    CLI 必须保证 stdout 可被外部 Agent 直接解析。命令执行过程中
    即使出现业务错误或未知异常，也要返回和正常报告相同的外层结构。
    """
    payload_details = dict(details or {})
    if detail:
        payload_details["detail"] = detail
    payload: dict[str, object] = {
        "status": "error",
        "errors": [
            {
                "code": code,
                "message": message,
            }
        ],
        "warnings": [],
        "summary": {},
        "details": payload_details,
    }
    diagnostic_snapshot = current_diagnostic_snapshot()
    if diagnostic_snapshot is not None:
        payload["diagnostics"] = diagnostic_snapshot.to_json_object()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    """
    解析参数并执行对应 CLI 子命令。

    Args:
        argv: 可选的命令行参数序列。

    Returns:
        进程退出码。
    """
    raw_argv = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    with command_diagnostics(enabled=raw_flag_enabled(raw_argv, "--debug")):
        return _run_command(raw_argv)


def _run_command(raw_argv: tuple[str, ...]) -> int:
    """在已建立的单命令诊断作用域中解析并执行命令。"""
    try:
        with diagnostic_stage("argument_parsing"):
            args = build_parser().parse_args(raw_argv)
    except CliArgumentError as error:
        error_message = str(error)
        setup_logger(
            level="DEBUG" if raw_flag_enabled(raw_argv, "--debug") else "INFO",
        )
        print_json_error(code="argument_error", message=error_message)
        logger.error(f"[tag.failure]命令参数错误[/tag.failure]：{error_message}")
        return 2

    setup_logger(
        level="DEBUG" if is_debug_enabled(args) else "INFO",
    )
    log_file_path = resolve_log_file_path()

    started_at = time.perf_counter()
    exit_code = 0
    status = "成功"
    logger.info(
        "\n".join(
            (
                "[tag.phase]CLI 运行开始[/tag.phase]",
                f"命令参数: [tag.count]{format_argv(raw_argv)}[/tag.count]",
                f"解析参数: [tag.count]{format_namespace(args)}[/tag.count]",
                f"工作目录: [tag.path]{Path.cwd()}[/tag.path]",
                f"日志文件: [tag.path]{log_file_path}[/tag.path]",
            )
        )
    )

    try:
        with diagnostic_stage("command"):
            exit_code = asyncio.run(dispatch_command(args))
        if exit_code != 0:
            status = "失败"
    except CliBusinessError as error:
        exit_code = 1
        status = "失败"
        print_json_error(code=error.code, message=str(error), details=error.details)
        logger.error(f"[tag.failure]命令执行失败[/tag.failure]：{error}")
    except ApplicationBusinessError as error:
        exit_code = 1
        status = "失败"
        print_json_error(code=error.code, message=str(error), details=error.details)
        logger.error(f"[tag.failure]命令执行失败[/tag.failure]：{error}")
    except PersistenceBusinessError as error:
        exit_code = 1
        status = "失败"
        print_json_error(code=error.code, message=str(error), details=error.details)
        logger.error(f"[tag.failure]命令执行失败[/tag.failure]：{error}")
    except KeyboardInterrupt:
        exit_code = 130
        status = "中断"
        print_json_error(code="keyboard_interrupt", message="用户中断运行")
        logger.warning("[tag.warning]用户中断运行[/tag.warning]")
    except Exception as error:
        exit_code = 1
        status = "异常"
        summary = format_exception_summary(error)
        print_json_error(
            code="unexpected_error",
            message=summary,
            detail=f"完整 traceback 已写入 {log_file_path}",
        )
        logger.error(
            f"[tag.exception]未知异常[/tag.exception]：{summary}，完整 traceback 已写入 [tag.path]{log_file_path}[/tag.path]"
        )
        logger.bind(file_only=True).exception(f"[tag.exception]命令执行失败完整异常[/tag.exception]：{summary}")
    finally:
        duration = time.perf_counter() - started_at
        logger.info(
            f"[tag.phase]CLI 运行结束[/tag.phase] 状态 [tag.count]{status}[/tag.count] 退出码 [tag.count]{exit_code}[/tag.count] 耗时 [tag.count]{duration:.2f}[/tag.count] 秒"
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
