"""低开销的单命令阶段诊断收集器。

这里只记录命令级阶段耗时和重型扫描次数。普通模式不创建收集器，
也不允许调用方在逐行、逐文本或逐候选循环中打点。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import wraps
from time import perf_counter_ns
from typing import ParamSpec, TypeVar

from app.rmmz.json_types import JsonObject

_P = ParamSpec("_P")
_R = TypeVar("_R")


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    """可安全写进 CLI JSON 的阶段诊断快照。"""

    timings: dict[str, int]
    scan_counts: dict[str, int]

    def to_json_object(self) -> JsonObject:
        """返回不暴露内部计时实现的稳定 JSON 对象。"""
        return {
            "timings": dict(self.timings),
            "scan_counts": dict(self.scan_counts),
        }


@dataclass(frozen=True, slots=True)
class _ActiveStage:
    """一次仍在执行的阶段计时。"""

    name: str
    started_at_ns: int


class CommandDiagnostics:
    """一条 CLI 命令的唯一诊断 owner。"""

    def __init__(self) -> None:
        self._completed_timings_ns: dict[str, int] = {}
        self._active_stages: dict[int, _ActiveStage] = {}
        self._scan_counts: dict[str, int] = {}
        self._next_stage_id: int = 0

    def start_stage(self, name: str) -> int:
        """开始一个粗粒度阶段并返回内部 token。"""
        if not name:
            raise ValueError("诊断阶段名称不能为空")
        self._next_stage_id += 1
        stage_id = self._next_stage_id
        self._active_stages[stage_id] = _ActiveStage(
            name=name,
            started_at_ns=perf_counter_ns(),
        )
        return stage_id

    def finish_stage(self, stage_id: int) -> None:
        """结束阶段；同名阶段的耗时按命令内累计。"""
        stage = self._active_stages.pop(stage_id, None)
        if stage is None:
            raise RuntimeError("诊断阶段 token 无效或已结束")
        elapsed_ns = max(0, perf_counter_ns() - stage.started_at_ns)
        self._completed_timings_ns[stage.name] = self._completed_timings_ns.get(stage.name, 0) + elapsed_ns

    def add_scan_counts(self, counts: Mapping[str, int]) -> None:
        """累计一次共享分析上下文公开的重型扫描次数。"""
        for name, count in counts.items():
            if not name:
                raise ValueError("扫描计数名称不能为空")
            if isinstance(count, bool) or count < 0:
                raise ValueError(f"扫描计数 {name} 必须是非负整数")
            self._scan_counts[name] = self._scan_counts.get(name, 0) + count

    def snapshot(self) -> DiagnosticSnapshot:
        """取得当前快照；仍在运行的命令阶段也计入已耗时间。"""
        now_ns = perf_counter_ns()
        timings_ns = dict(self._completed_timings_ns)
        for stage in self._active_stages.values():
            elapsed_ns = max(0, now_ns - stage.started_at_ns)
            timings_ns[stage.name] = timings_ns.get(stage.name, 0) + elapsed_ns
        timings = {name: _nanoseconds_to_milliseconds(elapsed_ns) for name, elapsed_ns in sorted(timings_ns.items())}
        return DiagnosticSnapshot(
            timings=timings,
            scan_counts=dict(sorted(self._scan_counts.items())),
        )


_CURRENT_DIAGNOSTICS: ContextVar[CommandDiagnostics | None] = ContextVar(
    "att_mz_command_diagnostics",
    default=None,
)


@contextmanager
def command_diagnostics(*, enabled: bool) -> Generator[CommandDiagnostics | None]:
    """按 `--debug` 为当前命令建立诊断作用域。"""
    if not enabled:
        yield None
        return
    diagnostics = CommandDiagnostics()
    token: Token[CommandDiagnostics | None] = _CURRENT_DIAGNOSTICS.set(diagnostics)
    try:
        yield diagnostics
    finally:
        _CURRENT_DIAGNOSTICS.reset(token)


@contextmanager
def diagnostic_stage(name: str) -> Generator[None]:
    """记录一个阶段耗时；普通模式是零计时分配的空操作。"""
    diagnostics = _CURRENT_DIAGNOSTICS.get()
    if diagnostics is None:
        yield
        return
    stage_id = diagnostics.start_stage(name)
    try:
        yield
    finally:
        diagnostics.finish_stage(stage_id)


def timed_async_diagnostic_stage(
    name: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """给异步阶段入口添加一次粗粒度计时。"""

    def decorate(function: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        @wraps(function)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with diagnostic_stage(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def record_scan_counts(counts: Mapping[str, int]) -> None:
    """把一次共享上下文的扫描次数交给当前命令诊断 owner。"""
    diagnostics = _CURRENT_DIAGNOSTICS.get()
    if diagnostics is not None:
        diagnostics.add_scan_counts(counts)


def current_diagnostic_snapshot() -> DiagnosticSnapshot | None:
    """返回当前 debug 命令的诊断快照；普通模式返回 `None`。"""
    diagnostics = _CURRENT_DIAGNOSTICS.get()
    return diagnostics.snapshot() if diagnostics is not None else None


def _nanoseconds_to_milliseconds(value: int) -> int:
    """把内部纳秒计时稳定转换为非负整数毫秒。"""
    return max(0, round(value / 1_000_000))


__all__ = [
    "CommandDiagnostics",
    "DiagnosticSnapshot",
    "command_diagnostics",
    "current_diagnostic_snapshot",
    "diagnostic_stage",
    "record_scan_counts",
    "timed_async_diagnostic_stage",
]
