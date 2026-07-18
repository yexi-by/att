"""命令行进度展示适配器。

本模块负责把业务进度回调渲染为 Agent 可读的 stderr 单行进度。
"""

from __future__ import annotations

import sys
import threading
import time
from _thread import LockType
from collections.abc import Callable
from types import TracebackType
from typing import Self

from app.observability import logger


class AgentProgressReporter:
    """把长任务进度输出到 stderr，保持 stdout 只承载最终 JSON。"""

    def __init__(self, description: str) -> None:
        """初始化面向代理的单行进度适配器。"""
        self.description: str = description
        self._current: int = 0
        self._total: int = 1
        self._status: str = "准备开始"
        self._started_at: float = 0.0
        self._last_emit_at: float = 0.0
        self._last_emitted_percent: int = -1
        self._lock: LockType = threading.Lock()
        self._stop_event: threading.Event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        """启动 stderr 进度心跳。"""
        self._started_at = time.monotonic()
        self._last_emit_at = 0.0
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._emit_heartbeat,
            name=f"{self.description}-progress",
            daemon=True,
        )
        self._heartbeat_thread.start()
        self._emit(force=True, note="开始")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """停止 stderr 进度心跳。"""
        if exc_type is None:
            with self._lock:
                self._status = "已结束"
            self._emit(force=True, note="结束")
        self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
        self._heartbeat_thread = None

    def progress_callbacks(self) -> tuple[Callable[[int, int], None], Callable[[int], None]]:
        """返回基础进度回调。"""
        return (self.set_progress, self.advance_progress)

    def status_callbacks(
        self,
    ) -> tuple[Callable[[int, int], None], Callable[[int], None], Callable[[str], None]]:
        """返回带状态文本的进度回调。"""
        return (self.set_progress, self.advance_progress, self.set_status)

    def set_progress(self, current: int, total: int) -> None:
        """设置当前任务的绝对进度并立即输出。"""
        with self._lock:
            self._total = max(total, 1)
            self._current = min(max(current, 0), self._total)
        self._emit(force=True)

    def advance_progress(self, count: int) -> None:
        """推进当前任务进度，按百分比变化或时间间隔输出。"""
        with self._lock:
            self._current = min(self._current + max(count, 0), self._total)
        self._emit(force=False)

    def set_status(self, status: str) -> None:
        """更新当前任务状态文本并立即输出。"""
        with self._lock:
            self._status = status
        self._emit(force=True)
        logger.debug(f"[tag.phase]任务状态[/tag.phase] {status}")

    def _emit_heartbeat(self) -> None:
        """长时间没有进度推进时持续提示任务仍在运行。"""
        while not self._stop_event.wait(30):
            self._emit(force=True, note="仍在运行")

    def _emit(self, *, force: bool, note: str | None = None) -> None:
        """按节流规则向 stderr 输出一行进度。"""
        now = time.monotonic()
        with self._lock:
            current = self._current
            total = self._total
            status = self._status
            started_at = self._started_at or now
            percent = int(current * 100 / max(total, 1))
            should_emit = force or percent != self._last_emitted_percent or now - self._last_emit_at >= 10
            if not should_emit:
                return
            self._last_emitted_percent = percent
            self._last_emit_at = now

        elapsed_seconds = max(now - started_at, 0.0)
        eta_text = _format_eta(elapsed_seconds=elapsed_seconds, current=current, total=total)
        progress_bar = _format_progress_bar(current=current, total=total)
        note_text = f" | {note}" if note else ""
        line = (
            f"进度 {self.description} | {progress_bar} | {current}/{total} | {percent}% | "
            f"已用 {_format_duration(elapsed_seconds)} | 预计剩余 {eta_text} | {status}{note_text}"
        )
        _ = sys.stderr.write(f"{line}\n")
        _ = sys.stderr.flush()


def _format_duration(seconds: float) -> str:
    """把秒数格式化为适合终端扫读的时长。"""
    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, second = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{second:02d}"


def _format_progress_bar(*, current: int, total: int) -> str:
    """生成不含 ANSI 控制符的文本进度条。"""
    width = 20
    safe_total = max(total, 1)
    completed_width = min(width, max(0, int(current * width / safe_total)))
    return f"[{'#' * completed_width}{'-' * (width - completed_width)}]"


def _format_eta(*, elapsed_seconds: float, current: int, total: int) -> str:
    """按当前速度估算剩余时间。"""
    if current <= 0 or total <= current:
        if total <= current:
            return "00:00:00"
        return "计算中"
    seconds_per_item = elapsed_seconds / current
    remaining_seconds = seconds_per_item * (total - current)
    return _format_duration(remaining_seconds)


def build_progress_reporter(description: str) -> AgentProgressReporter:
    """创建固定的 Agent 文本进度回调适配器。"""
    return AgentProgressReporter(description)


__all__ = ["AgentProgressReporter", "build_progress_reporter"]
