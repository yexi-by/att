"""
日志工具模块。

本模块统一封装 Loguru 与标准库 logging 的桥接逻辑，
为项目提供固定的 Agent stderr 日志和文件日志能力。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast, override

from loguru import logger

from app.runtime_paths import resolve_app_home_path

if TYPE_CHECKING:
    from loguru import Record

# --- 配置常量 ---
LOG_LEVEL = "INFO"
THIRD_PARTY_LOG_LEVEL = "WARNING"
DATE_FORMAT = "[%X]"
FILE_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
LOG_MARKUP_TAG_PATTERN = re.compile(r"\[/?[A-Za-z0-9_.# =:/-]+\]")

# --- 文件日志配置 ---
ENABLE_FILE_LOG = True
LOG_FILE_PATH = resolve_app_home_path(Path("logs") / "app.log")
LOG_FILE_LEVEL = "DEBUG"
LOG_ROTATION = "10 MB"
LOG_RETENTION = "1 week"
LOG_COMPRESSION = "zip"

NOISY_MODULES = [
    "httpcore",
    "httpx",
    "openai",
    "urllib3",
    "aiosqlite",
]


class InterceptHandler(logging.Handler):
    """
    拦截标准库 `logging` 日志，并转发给 Loguru。
    """

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """处理单条日志记录，并将其桥接到 Loguru。"""
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level,
            record.getMessage(),
        )


def should_show_in_console(record: Record) -> bool:
    """
    判断单条日志是否应该出现在终端。

    Args:
        record: Loguru 传入的日志记录。

    Returns:
        `True` 表示允许输出到终端。
    """
    extra = cast(dict[str, object], record["extra"])
    return not bool(extra.get("file_only", False))


def agent_console_sink(message: object) -> None:
    """用无 ANSI 单行文本输出 Agent 日志。"""
    text = str(message).rstrip()
    text = LOG_MARKUP_TAG_PATTERN.sub("", text)
    text = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    if not text:
        return
    _ = sys.stderr.write(f"{text}\n")
    _ = sys.stderr.flush()


def build_file_sink_format(record: Record) -> str:
    """
    为 Loguru sink 构造格式字符串。

    为什么使用可调用格式器：
    只有在当前记录确实携带异常时，才显式追加 `{exception}`，
    这样既能记录 traceback，又能保持普通日志紧凑。

    Args:
        record: Loguru 传入的单条日志记录字典。

    Returns:
        当前日志记录对应的格式字符串。
    """
    if record["exception"] is None:
        return FILE_LOG_FORMAT
    return f"{FILE_LOG_FORMAT}\n{{exception}}"


def resolve_log_file_path(file_path: str | Path | None = None) -> Path:
    """解析当前文件日志路径。"""
    if file_path is None:
        return resolve_app_home_path(Path("logs") / "app.log")
    return resolve_app_home_path(file_path)


def setup_logger(
    level: str = LOG_LEVEL,
    *,
    use_console: bool = True,
    file_path: str | Path | None = None,
    enqueue_file_log: bool = True,
) -> None:
    """
    配置并初始化全局日志系统。

    CLI 默认启用 stderr 单行日志，同时始终保留文件日志。

    Args:
        level: 控制台 sink 的最低日志级别。
        use_console: 是否启用 stderr 日志输出，测试可关闭。
        file_path: 文件日志路径，测试可传入临时路径避免污染真实日志。
        enqueue_file_log: 是否启用异步文件写入队列。
    """
    _ = logger.remove()

    if use_console:
        _ = logger.add(
            agent_console_sink,
            level=level,
            format="{level} {message}",
            filter=should_show_in_console,
            catch=True,
        )

    if ENABLE_FILE_LOG:
        resolved_file_path = resolve_log_file_path(file_path)
        resolved_file_path.parent.mkdir(parents=True, exist_ok=True)
        _ = logger.add(
            resolved_file_path,
            level=LOG_FILE_LEVEL,
            format=build_file_sink_format,
            rotation=LOG_ROTATION,
            retention=LOG_RETENTION,
            compression=LOG_COMPRESSION,
            enqueue=enqueue_file_log,
            encoding="utf-8",
            backtrace=True,
            diagnose=True,
        )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    for module_name in NOISY_MODULES:
        logging.getLogger(module_name).setLevel(logging.WARNING)


setup_logger()

__all__ = [
    "LOG_FILE_PATH",
    "build_file_sink_format",
    "logger",
    "resolve_log_file_path",
    "setup_logger",
]
