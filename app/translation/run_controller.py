"""正文翻译单一运行控制器。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Iterator
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Literal, Protocol, final, override

from app.llm import ChatMessage, LLMHandler, LLMRequestFailure
from app.rmmz.schema import TranslationErrorItem, TranslationItem
from app.rmmz.text_rules import TextRules
from app.source_residual import SourceResidualRuleSet

from .batch import TranslationBatch, TranslationBatchPlan
from .rate_limit import RpmRateLimiter
from .retry import (
    TranslationRequestStopped,
    request_with_recoverable_retry_result,
)
from .verify import verify_translation_batch_result

type TranslationRunOutcome = Literal[
    "completed",
    "completed_with_quality_errors",
    "stopped",
    "failed",
    "cancelled",
]
type TranslationRunStopCode = Literal[
    "none",
    "quality_error_rate_reached",
    "time_limit_reached",
    "run_limit_reached",
    "llm_request_failed",
    "candidate_validation_failed",
    "persistence_failed",
    "user_cancelled",
]
type TranslationRunLimitReason = Literal["", "max_batches"]


@dataclass(frozen=True, slots=True)
class BatchExecutionResult:
    """一个批次完成本地校验后、等待保存的结果。"""

    batch: TranslationBatch
    right_items: list[TranslationItem]
    error_items: list[TranslationErrorItem]
    physical_request_count: int = 0
    retry_request_count: int = 0

    def __post_init__(self) -> None:
        """保证持久化元数据始终是合法的非负计数。"""
        for field_name, value in (
            ("physical_request_count", self.physical_request_count),
            ("retry_request_count", self.retry_request_count),
        ):
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} 必须是大于等于 0 的整数")
        if self.retry_request_count > self.physical_request_count:
            raise ValueError("retry_request_count 不能大于 physical_request_count")


@dataclass(frozen=True, slots=True)
class PersistedBatchCounts:
    """保存回调返回的实际位置数量。"""

    success_count: int
    quality_error_count: int
    reused_current_run_count: int = 0
    reused_saved_count: int = 0
    rejected_reuse_count: int = 0
    retranslation_batches: TranslationBatchPlan | None = None

    def __post_init__(self) -> None:
        """拒绝会破坏错误率与运行摘要的非法计数。"""
        for field_name, value in (
            ("success_count", self.success_count),
            ("quality_error_count", self.quality_error_count),
            ("reused_current_run_count", self.reused_current_run_count),
            ("reused_saved_count", self.reused_saved_count),
            ("rejected_reuse_count", self.rejected_reuse_count),
        ):
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} 必须是大于等于 0 的整数")


@dataclass(frozen=True, slots=True)
class TranslationRunResult:
    """Controller 返回给应用层的结构化运行结果。"""

    outcome: TranslationRunOutcome
    stop_code: TranslationRunStopCode
    stop_message: str
    planned_batch_count: int
    dispatched_batch_count: int
    completed_batch_count: int
    undispatched_batch_count: int
    cancelled_batch_count: int
    failed_batch_count: int
    waiting_permission_cancelled_count: int
    inflight_cancelled_count: int
    completed_after_stop_count: int
    success_count: int
    quality_error_count: int
    reused_current_run_count: int
    reused_saved_count: int
    rejected_reuse_count: int
    physical_request_count: int
    retry_request_count: int
    elapsed_ms: int
    limit_reason: TranslationRunLimitReason = ""
    llm_failure: LLMRequestFailure | None = None
    failure: Exception | None = None


@final
class TranslationRunCancelled(asyncio.CancelledError):
    """携带已收拢运行指标的用户取消异常。"""

    def __init__(self, result: TranslationRunResult) -> None:
        super().__init__(result.stop_message)
        self.result: TranslationRunResult = result


class ExecuteBatch(Protocol):
    """把模型原始响应转换为可保存批次结果的注入边界。"""

    def __call__(
        self,
        *,
        ai_result: str,
        batch: TranslationBatch,
    ) -> Awaitable[BatchExecutionResult]:
        """执行批次协议与质量检查。"""
        ...


class PersistBatch(Protocol):
    """以一个业务事务保存单批次结果的注入边界。"""

    def __call__(
        self,
        result: BatchExecutionResult,
        /,
    ) -> Awaitable[PersistedBatchCounts | None]:
        """保存一个批次，并返回展开后的实际位置数量。"""
        ...


class SizedBatchIterable(Protocol):
    """可惰性遍历且预先声明准确批次数的 Controller 输入。"""

    def __iter__(self) -> Iterator[TranslationBatch]:
        """返回仅由 Controller 消费的批次迭代器。"""
        ...

    def __len__(self) -> int:
        """返回无需物化 prompt 即可取得的准确批次数。"""
        ...


@dataclass(slots=True)
class _AttemptCounter:
    """跨任务取消边界保存已经进入 HTTP 的次数。"""

    count: int = 0


@dataclass(slots=True)
class _BatchProgress:
    """记录有效模型响应是否已经返回到业务层。"""

    response_received: bool = False


@final
class _CountingLLMHandler(LLMHandler):
    """记录物理请求，并在有效响应返回时推进批次阶段。"""

    def __init__(
        self,
        delegate: LLMHandler,
        counter: _AttemptCounter,
        progress: _BatchProgress,
    ) -> None:
        super().__init__()
        self._delegate: LLMHandler = delegate
        self._counter: _AttemptCounter = counter
        self._progress: _BatchProgress = progress

    @override
    async def get_ai_response(
        self,
        *,
        messages: list[ChatMessage],
        model: str,
        temperature: float | None = None,
    ) -> str:
        self._counter.count += 1
        response = await self._delegate.get_ai_response(
            messages=messages,
            model=model,
            temperature=temperature,
        )
        self._progress.response_received = True
        return response


@dataclass(frozen=True, slots=True)
class _CompletedBatch:
    """已经通过保存回调提交的批次。"""

    counts: PersistedBatchCounts
    completed_at: float
    soft_stop_started_at: float | None = None


@dataclass(frozen=True, slots=True)
class _StoppedBatch:
    """软停止阻止了尚未进入 HTTP 的批次或重试。"""

    completed_at: float


@dataclass(frozen=True, slots=True)
class _FailedBatch:
    """需要硬停止整个运行的批次故障。"""

    error: Exception
    stage: Literal["llm", "validation", "persistence"]
    completed_at: float


type _BatchTaskResult = _CompletedBatch | _StoppedBatch | _FailedBatch


@dataclass(slots=True)
class _ActiveBatch:
    """Controller 对一个已派发批次保留的运行态。"""

    dispatch_index: int
    batch: TranslationBatch
    attempt_counter: _AttemptCounter
    progress: _BatchProgress
    task: asyncio.Task[_BatchTaskResult]


@dataclass(slots=True)
class _RunCounters:
    """运行期间由 Controller 单协程更新的计数。"""

    dispatched_batch_count: int = 0
    completed_batch_count: int = 0
    cancelled_batch_count: int = 0
    failed_batch_count: int = 0
    waiting_permission_cancelled_count: int = 0
    inflight_cancelled_count: int = 0
    completed_after_stop_count: int = 0
    success_count: int = 0
    quality_error_count: int = 0
    reused_current_run_count: int = 0
    reused_saved_count: int = 0
    rejected_reuse_count: int = 0
    physical_request_count: int = 0
    retry_request_count: int = 0
    accounted_dispatch_indexes: set[int] = field(default_factory=set)


@final
class _RunStopCoordinator:
    """把批次提交和下一次请求许可串成同一个停止判定边界。"""

    def __init__(
        self,
        *,
        stop_event: asyncio.Event,
        stop_on_error_rate: float | None,
    ) -> None:
        self._stop_event: asyncio.Event = stop_event
        self._stop_on_error_rate: float | None = stop_on_error_rate
        self._permission_lock: asyncio.Lock = asyncio.Lock()
        self._success_count: int = 0
        self._quality_error_count: int = 0

    async def acquire(self, *, attempt_count: int) -> None:
        """在批次提交边界之外批准请求，避免阈值提交后继续重试。"""
        async with self._permission_lock:
            if self._stop_event.is_set():
                raise TranslationRequestStopped(attempt_count=attempt_count)

    async def persist_and_record(
        self,
        *,
        result: BatchExecutionResult,
        persist_batch: PersistBatch,
    ) -> tuple[PersistedBatchCounts, float | None]:
        """持有请求许可锁提交批次，并在释放锁前更新错误率停止状态。"""
        async with self._permission_lock:
            try:
                persisted_counts = await persist_batch(result)
            except BaseException:
                self._stop_event.set()
                raise
            if persisted_counts is None:
                persisted_counts = PersistedBatchCounts(
                    success_count=len(result.right_items),
                    quality_error_count=len(result.error_items),
                )
            self._success_count += persisted_counts.success_count
            self._quality_error_count += persisted_counts.quality_error_count
            stop_started_at: float | None = None
            if (
                self._stop_on_error_rate is not None
                and not self._stop_event.is_set()
                and self._is_error_rate_reached(self._stop_on_error_rate)
            ):
                self._stop_event.set()
                stop_started_at = perf_counter()
            return persisted_counts, stop_started_at

    def _is_error_rate_reached(self, stop_on_error_rate: float) -> bool:
        """按已经成功提交的实际目标位置计算质量错误率。"""
        processed_count = self._success_count + self._quality_error_count
        return processed_count > 0 and self._quality_error_count / processed_count >= stop_on_error_rate


@dataclass(slots=True)
class _PendingBatchSegment:
    """一个声明准确数量、由 Controller 独占消费的惰性批次段。"""

    iterator: Iterator[TranslationBatch]
    declared_count: int
    atomic_for_batch_limit: bool = False
    discovered_count: int = 0

    def pop_next(self) -> TranslationBatch | None:
        """返回下一批；耗尽时验证声明数量。"""
        try:
            batch = next(self.iterator)
        except StopIteration:
            if self.discovered_count != self.declared_count:
                raise RuntimeError("批次计划生成数量与声明值不一致") from None
            return None
        self.discovered_count += 1
        if self.discovered_count > self.declared_count:
            raise RuntimeError("批次计划生成数量超过声明值")
        return batch


@dataclass(slots=True)
class _PendingBatchSource:
    """由 Controller 独占消费初始计划并接收运行期追加计划。"""

    segments: deque[_PendingBatchSegment] = field(default_factory=deque)
    planned_batch_count: int = 0
    skipped_by_batch_limit_count: int = 0

    @classmethod
    def from_batches(
        cls,
        batches: SizedBatchIterable,
    ) -> _PendingBatchSource:
        """只接管具有准确计数的 iterable，拒绝为计数遍历生成器。"""
        try:
            declared_count = len(batches)
        except TypeError:
            raise TypeError("batches 必须声明准确批次数，不能传入 unsized iterable")
        if isinstance(declared_count, bool) or declared_count < 0:
            raise ValueError("batches 声明的批次数必须是大于等于 0 的整数")
        return cls(
            segments=deque(
                [
                    _PendingBatchSegment(
                        iterator=iter(batches),
                        declared_count=declared_count,
                        atomic_for_batch_limit=False,
                    )
                ]
            ),
            planned_batch_count=declared_count,
        )

    def pop_next(
        self,
        *,
        remaining_dispatch_count: int | None,
    ) -> TranslationBatch | None:
        """按计划段顺序取批，批次上限容纳不下时整段跳过动态重译。"""
        while self.segments:
            segment = self.segments[0]
            if (
                remaining_dispatch_count is not None
                and segment.atomic_for_batch_limit
                and segment.discovered_count == 0
                and segment.declared_count > remaining_dispatch_count
            ):
                self.skipped_by_batch_limit_count += segment.declared_count
                _ = self.segments.popleft()
                continue
            batch = segment.pop_next()
            if batch is not None:
                return batch
            _ = self.segments.popleft()
        return None

    def append(self, plan: TranslationBatchPlan | None) -> None:
        """把目标位置复验失败产生的惰性计划追加到待处理队尾。"""
        if plan is None or len(plan) == 0:
            return
        self.segments.append(
            _PendingBatchSegment(
                iterator=iter(plan),
                declared_count=len(plan),
                atomic_for_batch_limit=True,
            )
        )
        self.planned_batch_count += len(plan)

    def finalize_planned_count(self) -> int:
        """直接返回计划声明值，停止后绝不遍历未派发 prompt。"""
        return self.planned_batch_count


@final
class TranslationRunController:
    """懒派发、统一停止并等待所有批次任务的运行控制器。"""

    def __init__(
        self,
        *,
        llm_handler: LLMHandler,
        model: str,
        worker_count: int,
        retry_count: int,
        retry_delay: int,
        rpm: int | None,
        text_rules: TextRules | None,
        source_residual_rule_set: SourceResidualRuleSet | None = None,
        execute_batch: ExecuteBatch | None = None,
    ) -> None:
        """固定一次运行所需的模型、校验和并发配置。"""
        if isinstance(worker_count, bool) or worker_count <= 0:
            raise ValueError("worker_count 必须是大于 0 的整数")
        if isinstance(retry_count, bool) or retry_count < 0:
            raise ValueError("retry_count 必须是大于等于 0 的整数")
        if isinstance(retry_delay, bool) or retry_delay < 0:
            raise ValueError("retry_delay 必须是大于等于 0 的整数")
        if rpm is not None and (isinstance(rpm, bool) or rpm <= 0):
            raise ValueError("rpm 必须是大于 0 的整数或 None")
        if execute_batch is None and text_rules is None:
            raise ValueError("使用默认批次校验时必须提供 text_rules")
        self._llm_handler: LLMHandler = llm_handler
        self._model: str = model
        self._worker_count: int = worker_count
        self._retry_count: int = retry_count
        self._retry_delay: int = retry_delay
        self._rpm: int | None = rpm
        self._text_rules: TextRules | None = text_rules
        self._source_residual_rule_set: SourceResidualRuleSet | None = source_residual_rule_set
        self._execute_batch: ExecuteBatch = execute_batch or self._execute_batch_default
        self._soft_stop_event: asyncio.Event | None = None
        self._running: bool = False

    @property
    def soft_stop_requested(self) -> bool:
        """返回当前运行是否已经进入错误率软停止阶段。"""
        return self._soft_stop_event is not None and self._soft_stop_event.is_set()

    async def run(
        self,
        batches: SizedBatchIterable,
        *,
        persist_batch: PersistBatch,
        max_batches: int | None = None,
        time_limit_seconds: float | None = None,
        stop_on_error_rate: float | None = None,
    ) -> TranslationRunResult:
        """懒派发批次，并在所有终态下收回活动任务。"""
        if self._running:
            raise RuntimeError("TranslationRunController 正在执行，不能重复启动")
        if max_batches is not None and (isinstance(max_batches, bool) or max_batches <= 0):
            raise ValueError("max_batches 必须是大于 0 的整数或 None")
        if time_limit_seconds is not None and (isinstance(time_limit_seconds, bool) or time_limit_seconds <= 0):
            raise ValueError("time_limit_seconds 必须是大于 0 的数或 None")
        if stop_on_error_rate is not None and (isinstance(stop_on_error_rate, bool) or not 0 < stop_on_error_rate <= 1):
            raise ValueError("stop_on_error_rate 必须大于 0 且小于等于 1")

        self._running = True
        started_at = perf_counter()
        self._soft_stop_event = asyncio.Event()
        try:
            return await self._run(
                batches=batches,
                persist_batch=persist_batch,
                max_batches=max_batches,
                time_limit_seconds=time_limit_seconds,
                stop_on_error_rate=stop_on_error_rate,
                started_at=started_at,
            )
        finally:
            self._running = False

    async def _run(
        self,
        *,
        batches: SizedBatchIterable,
        persist_batch: PersistBatch,
        max_batches: int | None,
        time_limit_seconds: float | None,
        stop_on_error_rate: float | None,
        started_at: float,
    ) -> TranslationRunResult:
        """由单一协程维护派发、终止原因和汇总计数。"""
        soft_stop_event = self._require_soft_stop_event()
        rate_limiter = RpmRateLimiter(self._rpm) if self._rpm is not None else None
        persistence_lock = asyncio.Lock()
        stop_coordinator = _RunStopCoordinator(
            stop_event=soft_stop_event,
            stop_on_error_rate=stop_on_error_rate,
        )
        counters = _RunCounters()
        pending_batches = _PendingBatchSource.from_batches(batches)
        active: dict[asyncio.Task[_BatchTaskResult], _ActiveBatch] = {}
        next_dispatch_index = 0
        deadline = asyncio.get_running_loop().time() + time_limit_seconds if time_limit_seconds is not None else None
        outcome: TranslationRunOutcome = "completed"
        stop_code: TranslationRunStopCode = "none"
        stop_message = ""
        llm_failure: LLMRequestFailure | None = None
        failure: Exception | None = None
        limit_reason: TranslationRunLimitReason = ""
        soft_stop_started_at: float | None = None

        def dispatch_available() -> None:
            nonlocal next_dispatch_index
            while (
                not soft_stop_event.is_set()
                and len(active) < self._worker_count
                and (max_batches is None or counters.dispatched_batch_count < max_batches)
            ):
                remaining_dispatch_count = (
                    None if max_batches is None else max_batches - counters.dispatched_batch_count
                )
                batch = pending_batches.pop_next(
                    remaining_dispatch_count=remaining_dispatch_count,
                )
                if batch is None:
                    return
                counter = _AttemptCounter()
                progress = _BatchProgress()
                task = asyncio.create_task(
                    self._execute_and_persist_batch(
                        batch=batch,
                        persist_batch=persist_batch,
                        rate_limiter=rate_limiter,
                        soft_stop_event=soft_stop_event,
                        attempt_counter=counter,
                        progress=progress,
                        persistence_lock=persistence_lock,
                        stop_coordinator=stop_coordinator,
                    )
                )
                active[task] = _ActiveBatch(
                    dispatch_index=next_dispatch_index,
                    batch=batch,
                    attempt_counter=counter,
                    progress=progress,
                    task=task,
                )
                counters.dispatched_batch_count += 1
                next_dispatch_index += 1

        try:
            dispatch_available()
            while active:
                remaining_seconds = None if deadline is None else max(deadline - asyncio.get_running_loop().time(), 0.0)
                if remaining_seconds == 0:
                    outcome = "stopped"
                    stop_code = "time_limit_reached"
                    stop_message = f"达到本轮翻译时间上限: {time_limit_seconds:g} 秒"
                    break

                done, _pending = await asyncio.wait(
                    active,
                    timeout=remaining_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    outcome = "stopped"
                    stop_code = "time_limit_reached"
                    stop_message = f"达到本轮翻译时间上限: {time_limit_seconds:g} 秒"
                    break

                completed_results: list[tuple[_ActiveBatch, _BatchTaskResult]] = []
                for task in done:
                    active_batch = active.pop(task)
                    task_result = task.result()
                    self._account_attempts(counters=counters, active_batch=active_batch)
                    completed_results.append((active_batch, task_result))
                completed_results.sort(key=lambda pair: (pair[1].completed_at, pair[0].dispatch_index))

                hard_failure: _FailedBatch | None = None
                for _active_batch, task_result in completed_results:
                    if isinstance(task_result, _CompletedBatch):
                        counters.completed_batch_count += 1
                        counters.success_count += task_result.counts.success_count
                        counters.quality_error_count += task_result.counts.quality_error_count
                        counters.reused_current_run_count += task_result.counts.reused_current_run_count
                        counters.reused_saved_count += task_result.counts.reused_saved_count
                        counters.rejected_reuse_count += task_result.counts.rejected_reuse_count
                        pending_batches.append(task_result.counts.retranslation_batches)
                        if (
                            soft_stop_started_at is not None
                            and task_result.soft_stop_started_at is None
                            and task_result.completed_at >= soft_stop_started_at
                        ):
                            counters.completed_after_stop_count += 1
                        if task_result.soft_stop_started_at is not None and soft_stop_started_at is None:
                            if stop_on_error_rate is None:
                                raise RuntimeError("错误率停止缺少阈值配置")
                            soft_stop_started_at = task_result.soft_stop_started_at
                            outcome = "stopped"
                            stop_code = "quality_error_rate_reached"
                            stop_message = f"检查没通过的译文比例达到停止阈值: {stop_on_error_rate:g}"
                    elif isinstance(task_result, _StoppedBatch):
                        counters.cancelled_batch_count += 1
                        self._account_cancelled_phase(
                            counters=counters,
                            attempt_count=_active_batch.attempt_counter.count,
                        )
                    else:
                        counters.failed_batch_count += 1
                        hard_failure = hard_failure or task_result

                if hard_failure is not None:
                    soft_stop_event.set()
                    failure = hard_failure.error
                    if hard_failure.stage == "llm":
                        outcome = "failed"
                        stop_code = "llm_request_failed"
                        stop_message = f"模型请求失败: {hard_failure.error}"
                        if isinstance(hard_failure.error, LLMRequestFailure):
                            llm_failure = hard_failure.error
                    elif hard_failure.stage == "validation":
                        outcome = "failed"
                        stop_code = "candidate_validation_failed"
                        stop_message = f"候选译文校验失败: {hard_failure.error}"
                    else:
                        outcome = "failed"
                        stop_code = "persistence_failed"
                        stop_message = f"保存翻译批次失败: {hard_failure.error}"
                    break

                dispatch_available()
        except asyncio.CancelledError:
            soft_stop_event.set()
            cancel_started_at = perf_counter()
            drained_failure = await self._settle_active(
                active=active,
                counters=counters,
                pending_batches=pending_batches,
                stop_started_at=cancel_started_at,
                cancel_unfinished_requests=True,
            )
            if drained_failure is not None:
                failure = drained_failure.error
                if isinstance(drained_failure.error, LLMRequestFailure):
                    llm_failure = drained_failure.error
            result = self._build_run_result(
                outcome="cancelled",
                stop_code="user_cancelled",
                stop_message="用户取消了本轮正文翻译",
                pending_batches=pending_batches,
                counters=counters,
                started_at=started_at,
                llm_failure=llm_failure,
                failure=failure,
                limit_reason="",
            )
            raise TranslationRunCancelled(result) from None
        except Exception:
            soft_stop_event.set()
            _ = await self._settle_active(
                active=active,
                counters=counters,
                pending_batches=pending_batches,
                stop_started_at=perf_counter(),
                cancel_unfinished_requests=True,
            )
            raise

        if active:
            soft_stop_event.set()
            drained_failure = await self._settle_active(
                active=active,
                counters=counters,
                pending_batches=pending_batches,
                stop_started_at=soft_stop_started_at or perf_counter(),
                cancel_unfinished_requests=(stop_code != "quality_error_rate_reached"),
            )
            if failure is None and drained_failure is not None:
                failure = drained_failure.error
                outcome = "failed"
                if drained_failure.stage == "llm":
                    stop_code = "llm_request_failed"
                    stop_message = f"模型请求失败: {drained_failure.error}"
                    if isinstance(drained_failure.error, LLMRequestFailure):
                        llm_failure = drained_failure.error
                elif drained_failure.stage == "validation":
                    stop_code = "candidate_validation_failed"
                    stop_message = f"候选译文校验失败: {drained_failure.error}"
                else:
                    stop_code = "persistence_failed"
                    stop_message = f"保存翻译批次失败: {drained_failure.error}"

        if (
            outcome == "completed"
            and max_batches is not None
            and (
                pending_batches.skipped_by_batch_limit_count > 0
                or (
                    counters.dispatched_batch_count >= max_batches
                    and pending_batches.finalize_planned_count() > counters.dispatched_batch_count
                )
            )
        ):
            outcome = "stopped"
            stop_code = "run_limit_reached"
            stop_message = "达到本轮 max-batches 限制，仍有正文等待翻译"
            limit_reason = "max_batches"
        elif outcome == "completed" and counters.quality_error_count > 0:
            outcome = "completed_with_quality_errors"

        return self._build_run_result(
            outcome=outcome,
            stop_code=stop_code,
            stop_message=stop_message,
            pending_batches=pending_batches,
            counters=counters,
            started_at=started_at,
            llm_failure=llm_failure,
            failure=failure,
            limit_reason=limit_reason,
        )

    async def _execute_and_persist_batch(
        self,
        *,
        batch: TranslationBatch,
        persist_batch: PersistBatch,
        rate_limiter: RpmRateLimiter | None,
        soft_stop_event: asyncio.Event,
        attempt_counter: _AttemptCounter,
        progress: _BatchProgress,
        persistence_lock: asyncio.Lock,
        stop_coordinator: _RunStopCoordinator,
    ) -> _BatchTaskResult:
        """完成一个批次的请求、校验和原子保存。"""
        counting_handler = _CountingLLMHandler(
            self._llm_handler,
            attempt_counter,
            progress,
        )
        try:
            request_result = await request_with_recoverable_retry_result(
                llm_handler=counting_handler,
                model=self._model,
                messages=batch.messages,
                retry_count=self._retry_count,
                retry_delay=self._retry_delay,
                task_label="正文翻译",
                rate_limiter=rate_limiter,
                attempt_gate=stop_coordinator,
                stop_event=soft_stop_event,
            )
        except TranslationRequestStopped:
            return _StoppedBatch(completed_at=perf_counter())
        except LLMRequestFailure as error:
            soft_stop_event.set()
            return _FailedBatch(
                error=error,
                stage="llm",
                completed_at=perf_counter(),
            )

        try:
            execution_result = await self._execute_batch(ai_result=request_result.text, batch=batch)
            if execution_result.batch is not batch:
                raise ValueError("批次执行结果绑定了其他 TranslationBatch")
            execution_result = replace(
                execution_result,
                physical_request_count=request_result.attempt_count,
                retry_request_count=max(request_result.attempt_count - 1, 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            soft_stop_event.set()
            return _FailedBatch(
                error=error,
                stage="validation",
                completed_at=perf_counter(),
            )

        try:
            async with persistence_lock:
                persisted_counts, stop_started_at = await stop_coordinator.persist_and_record(
                    result=execution_result,
                    persist_batch=persist_batch,
                )
            return _CompletedBatch(
                counts=persisted_counts,
                completed_at=perf_counter(),
                soft_stop_started_at=stop_started_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return _FailedBatch(
                error=error,
                stage="persistence",
                completed_at=perf_counter(),
            )

    async def _execute_batch_default(
        self,
        *,
        ai_result: str,
        batch: TranslationBatch,
    ) -> BatchExecutionResult:
        """使用翻译层纯校验接口生成待保存结果。"""
        if self._text_rules is None:
            raise RuntimeError("默认批次校验缺少 text_rules")
        verification = verify_translation_batch_result(
            ai_result=ai_result,
            batch=batch,
            text_rules=self._text_rules,
            source_residual_rule_set=self._source_residual_rule_set,
        )
        return BatchExecutionResult(
            batch=batch,
            right_items=verification.right_items,
            error_items=verification.error_items,
        )

    async def _settle_active(
        self,
        *,
        active: dict[asyncio.Task[_BatchTaskResult], _ActiveBatch],
        counters: _RunCounters,
        pending_batches: _PendingBatchSource,
        stop_started_at: float | None = None,
        cancel_unfinished_requests: bool,
    ) -> _FailedBatch | None:
        """收拢活动批次；质量软停止允许已进入 HTTP 的请求按自身超时完成。"""
        active_batches = list(active.values())
        for active_batch in active_batches:
            if (
                cancel_unfinished_requests
                and not active_batch.task.done()
                and not active_batch.progress.response_received
            ):
                _ = active_batch.task.cancel()
        if active_batches:
            tasks = {active_batch.task for active_batch in active_batches}
            _ = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )
        failed_results: list[tuple[int, _FailedBatch]] = []
        for active_batch in active_batches:
            self._account_attempts(counters=counters, active_batch=active_batch)
            task = active_batch.task
            if task.cancelled():
                counters.cancelled_batch_count += 1
                self._account_cancelled_phase(
                    counters=counters,
                    attempt_count=active_batch.attempt_counter.count,
                )
                continue
            task_result = task.result()
            if isinstance(task_result, _CompletedBatch):
                counters.completed_batch_count += 1
                counters.success_count += task_result.counts.success_count
                counters.quality_error_count += task_result.counts.quality_error_count
                counters.reused_current_run_count += task_result.counts.reused_current_run_count
                counters.reused_saved_count += task_result.counts.reused_saved_count
                counters.rejected_reuse_count += task_result.counts.rejected_reuse_count
                pending_batches.append(task_result.counts.retranslation_batches)
                if (
                    stop_started_at is not None
                    and task_result.soft_stop_started_at is None
                    and task_result.completed_at >= stop_started_at
                ):
                    counters.completed_after_stop_count += 1
            elif isinstance(task_result, _StoppedBatch):
                counters.cancelled_batch_count += 1
                self._account_cancelled_phase(
                    counters=counters,
                    attempt_count=active_batch.attempt_counter.count,
                )
            else:
                counters.failed_batch_count += 1
                failed_results.append((active_batch.dispatch_index, task_result))
        active.clear()
        if not failed_results:
            return None
        failed_results.sort(
            key=lambda pair: (pair[1].completed_at, pair[0]),
        )
        return failed_results[0][1]

    @staticmethod
    def _build_run_result(
        *,
        outcome: TranslationRunOutcome,
        stop_code: TranslationRunStopCode,
        stop_message: str,
        pending_batches: _PendingBatchSource,
        counters: _RunCounters,
        started_at: float,
        llm_failure: LLMRequestFailure | None,
        failure: Exception | None,
        limit_reason: TranslationRunLimitReason,
    ) -> TranslationRunResult:
        """使用已经收拢的任务状态生成一致的终态摘要。"""
        planned_batch_count = pending_batches.finalize_planned_count()
        undispatched_batch_count = planned_batch_count - counters.dispatched_batch_count
        if undispatched_batch_count < 0:
            raise RuntimeError("最终计划批次数小于已经派发的批次数")
        return TranslationRunResult(
            outcome=outcome,
            stop_code=stop_code,
            stop_message=stop_message,
            planned_batch_count=planned_batch_count,
            dispatched_batch_count=counters.dispatched_batch_count,
            completed_batch_count=counters.completed_batch_count,
            undispatched_batch_count=undispatched_batch_count,
            cancelled_batch_count=counters.cancelled_batch_count,
            failed_batch_count=counters.failed_batch_count,
            waiting_permission_cancelled_count=(counters.waiting_permission_cancelled_count),
            inflight_cancelled_count=counters.inflight_cancelled_count,
            completed_after_stop_count=counters.completed_after_stop_count,
            success_count=counters.success_count,
            quality_error_count=counters.quality_error_count,
            reused_current_run_count=counters.reused_current_run_count,
            reused_saved_count=counters.reused_saved_count,
            rejected_reuse_count=counters.rejected_reuse_count,
            physical_request_count=counters.physical_request_count,
            retry_request_count=counters.retry_request_count,
            elapsed_ms=max(round((perf_counter() - started_at) * 1000), 0),
            limit_reason=limit_reason,
            llm_failure=llm_failure,
            failure=failure,
        )

    @staticmethod
    def _account_attempts(
        *,
        counters: _RunCounters,
        active_batch: _ActiveBatch,
    ) -> None:
        """每个派发批次只累计一次物理请求与重试次数。"""
        if active_batch.dispatch_index in counters.accounted_dispatch_indexes:
            return
        attempt_count = active_batch.attempt_counter.count
        counters.physical_request_count += attempt_count
        counters.retry_request_count += max(attempt_count - 1, 0)
        counters.accounted_dispatch_indexes.add(active_batch.dispatch_index)

    @staticmethod
    def _account_cancelled_phase(
        *,
        counters: _RunCounters,
        attempt_count: int,
    ) -> None:
        """区分请求许可前取消和已经产生物理请求后的取消。"""
        if attempt_count == 0:
            counters.waiting_permission_cancelled_count += 1
        else:
            counters.inflight_cancelled_count += 1

    def _require_soft_stop_event(self) -> asyncio.Event:
        """返回本轮共享软停止事件。"""
        if self._soft_stop_event is None:
            raise RuntimeError("TranslationRunController 尚未启动")
        return self._soft_stop_event


__all__: list[str] = [
    "BatchExecutionResult",
    "ExecuteBatch",
    "PersistBatch",
    "PersistedBatchCounts",
    "SizedBatchIterable",
    "TranslationRunController",
    "TranslationRunCancelled",
    "TranslationRunLimitReason",
    "TranslationRunOutcome",
    "TranslationRunResult",
    "TranslationRunStopCode",
]
