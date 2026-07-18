"""翻译业务层 LLM 请求重试策略。"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.llm import (
    ChatMessage,
    LLMHandler,
    LLMRequestFailure,
    classify_llm_error,
    format_llm_error,
    is_recoverable_llm_error,
)
from app.observability import logger


class RequestRateLimiter(Protocol):
    """每次物理 LLM 请求前必须获取的共享许可。"""

    async def acquire(self) -> None:
        """等待请求许可。"""
        ...


class RequestAttemptGate(Protocol):
    """在最终发送边界原子批准一次物理请求。"""

    async def acquire(self, *, attempt_count: int) -> None:
        """批准当前请求，或用 ``TranslationRequestStopped`` 拒绝。"""
        ...


@dataclass(frozen=True, slots=True)
class TranslationRequestResult:
    """一次业务请求的文本结果与物理请求次数。"""

    text: str
    attempt_count: int


class TranslationRequestStopped(RuntimeError):
    """软停止阻止了尚未进入 HTTP 的新请求或重试。"""

    def __init__(self, *, attempt_count: int) -> None:
        """记录停止前已经发出的物理请求数量。"""
        super().__init__("翻译运行已停止，不再发送新的模型请求")
        self.attempt_count: int = attempt_count


async def request_with_recoverable_retry_result(
    *,
    llm_handler: LLMHandler,
    model: str,
    messages: list[ChatMessage],
    retry_count: int,
    retry_delay: int,
    task_label: str,
    temperature: float | None = None,
    rate_limiter: RequestRateLimiter | None = None,
    attempt_gate: RequestAttemptGate | None = None,
    stop_event: asyncio.Event | None = None,
) -> TranslationRequestResult:
    """执行业务重试，并返回物理请求次数供运行汇总。"""
    if retry_count < 0:
        raise ValueError("retry_count 不能小于 0")
    if retry_delay < 0:
        raise ValueError("retry_delay 不能小于 0")

    max_attempts = retry_count + 1
    attempt_count = 0
    for attempt_index in range(1, max_attempts + 1):
        _raise_if_stopped(stop_event=stop_event, attempt_count=attempt_count)
        await _acquire_request_permit(
            rate_limiter=rate_limiter,
            stop_event=stop_event,
            attempt_count=attempt_count,
        )
        if attempt_gate is not None:
            await attempt_gate.acquire(attempt_count=attempt_count)
        _raise_if_stopped(stop_event=stop_event, attempt_count=attempt_count)
        attempt_count += 1
        try:
            text = await llm_handler.get_ai_response(
                messages=messages,
                model=model,
                temperature=temperature,
            )
            return TranslationRequestResult(text=text, attempt_count=attempt_count)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            info = classify_llm_error(error)
            if not is_recoverable_llm_error(error):
                logger.error(
                    f"[tag.failure]LLM 不可恢复错误，已停止流程[/tag.failure] 任务 [tag.count]{task_label}[/tag.count] 原因：{format_llm_error(error)}"
                )
                raise LLMRequestFailure(info=info, attempt_count=attempt_count) from error

            if attempt_index >= max_attempts:
                logger.error(
                    f"[tag.failure]LLM 可恢复错误重试耗尽[/tag.failure] 任务 [tag.count]{task_label}[/tag.count] 尝试 [tag.count]{attempt_index}[/tag.count] 次 原因：{format_llm_error(error)}"
                )
                raise LLMRequestFailure(info=info, attempt_count=attempt_count) from error

            delay_seconds = retry_delay * attempt_index
            _raise_if_stopped(stop_event=stop_event, attempt_count=attempt_count)
            logger.warning(
                f"[tag.warning]LLM 可恢复错误，准备重试[/tag.warning] 任务 [tag.count]{task_label}[/tag.count] 第 [tag.count]{attempt_index}[/tag.count] 次失败，等待 [tag.count]{delay_seconds}[/tag.count] 秒 原因：{format_llm_error(error)}"
            )
            await _wait_retry_delay(
                delay_seconds=delay_seconds,
                stop_event=stop_event,
                attempt_count=attempt_count,
            )

    raise RuntimeError(f"LLM 请求未返回结果: {task_label}")


def _raise_if_stopped(*, stop_event: asyncio.Event | None, attempt_count: int) -> None:
    """在发送新请求前把业务软停止转换成稳定的控制流异常。"""
    if stop_event is not None and stop_event.is_set():
        raise TranslationRequestStopped(attempt_count=attempt_count)


async def _acquire_request_permit(
    *,
    rate_limiter: RequestRateLimiter | None,
    stop_event: asyncio.Event | None,
    attempt_count: int,
) -> None:
    """取得共享请求许可，并允许软停止中断尚未取得的许可。"""
    if rate_limiter is None:
        return
    if stop_event is None:
        await rate_limiter.acquire()
        return

    acquire_task = asyncio.create_task(rate_limiter.acquire())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        _ = await asyncio.wait(
            {acquire_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        _raise_if_stopped(stop_event=stop_event, attempt_count=attempt_count)
        await acquire_task
    finally:
        for task in (acquire_task, stop_task):
            if not task.done():
                _ = task.cancel()
        _ = await asyncio.gather(acquire_task, stop_task, return_exceptions=True)


async def _wait_retry_delay(
    *,
    delay_seconds: int,
    stop_event: asyncio.Event | None,
    attempt_count: int,
) -> None:
    """等待重试退避，并让软停止立即结束等待。"""
    _raise_if_stopped(stop_event=stop_event, attempt_count=attempt_count)
    if delay_seconds > 0:
        if stop_event is None:
            await asyncio.sleep(delay_seconds)
        else:
            try:
                _ = await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
            except TimeoutError:
                pass
    _raise_if_stopped(stop_event=stop_event, attempt_count=attempt_count)


__all__: list[str] = [
    "RequestAttemptGate",
    "RequestRateLimiter",
    "TranslationRequestResult",
    "TranslationRequestStopped",
    "request_with_recoverable_retry_result",
]
