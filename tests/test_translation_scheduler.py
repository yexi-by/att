"""正文翻译单一运行控制器测试。"""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from typing import cast, final, override

import pytest

from app.application.use_cases.translation_run import build_translation_batches
from app.config.schemas import Setting
from app.llm import ChatMessage, EmptyLLMResponseError, LLMHandler
from app.rmmz.schema import TranslationData, TranslationItem
from app.rmmz.text_rules import get_default_text_rules
from app.translation.batch import TranslationBatch, TranslationBatchPlan
from app.translation.context import TranslationBatchBlueprint
from app.translation.run_controller import (
    BatchExecutionResult,
    PersistedBatchCounts,
    SizedBatchIterable,
    TranslationRunCancelled,
    TranslationRunController,
)


@final
class ScriptedLLMHandler(LLMHandler):
    """把批次标签转交给测试协程的无网络 LLM 门面。"""

    def __init__(self, responder: Callable[[str], Awaitable[str]]) -> None:
        super().__init__()
        self._responder: Callable[[str], Awaitable[str]] = responder
        self.active_count: int = 0
        self.max_active_count: int = 0
        self.call_count: int = 0

    @override
    async def get_ai_response(
        self,
        *,
        messages: list[ChatMessage],
        model: str,
        temperature: float | None = None,
    ) -> str:
        del model, temperature
        self.call_count += 1
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)
        try:
            return await self._responder(messages[-1].text)
        finally:
            self.active_count -= 1


def _make_batch(label: str) -> TranslationBatch:
    """构造只携带调度标签、不依赖正文模型的批次。"""
    return TranslationBatch(
        bindings=(),
        messages=[ChatMessage(role="user", text=label)],
        estimated_tokens=1,
        token_limit=100,
    )


def _make_batch_plan(*labels: str) -> TranslationBatchPlan:
    """构造声明准确数量、仅在取批时创建测试批次的计划。"""
    frozen_labels = tuple(labels)

    def iter_batches() -> Iterator[TranslationBatch]:
        return (_make_batch(label) for label in frozen_labels)

    return TranslationBatchPlan(
        iterator_factory=iter_batches,
        batch_item_counts=tuple(1 for _label in frozen_labels),
    )


async def _empty_execution(
    *,
    ai_result: str,
    batch: TranslationBatch,
) -> BatchExecutionResult:
    """跳过正文校验，返回空的可保存结果。"""
    assert ai_result == "ok"
    return BatchExecutionResult(batch=batch, right_items=[], error_items=[])


def _make_controller(
    *,
    llm_handler: LLMHandler,
    worker_count: int,
    retry_count: int = 0,
    rpm: int | None = None,
) -> TranslationRunController:
    """创建使用注入校验器的最小 Controller。"""
    return TranslationRunController(
        llm_handler=llm_handler,
        model="test-model",
        worker_count=worker_count,
        retry_count=retry_count,
        retry_delay=0,
        rpm=rpm,
        text_rules=None,
        execute_batch=_empty_execution,
    )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    """把并发状态等待限制在当前事件循环调度内。"""
    while not predicate():
        await asyncio.sleep(0)


async def test_controller_does_not_preload_plan_beyond_worker_slots() -> None:
    release = asyncio.Event()
    two_requests_started = asyncio.Event()
    yielded_count = 0

    async def responder(_label: str) -> str:
        if handler.active_count == 2:
            _ = two_requests_started.set()
        _ = await release.wait()
        return "ok"

    def iter_batches() -> Iterator[TranslationBatch]:
        nonlocal yielded_count
        for index in range(5):
            yielded_count += 1
            yield _make_batch(str(index))

    plan = TranslationBatchPlan(
        iterator_factory=iter_batches,
        batch_item_counts=(1, 1, 1, 1, 1),
    )

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    run_task = asyncio.create_task(
        controller.run(
            plan,
            persist_batch=persist_batch,
        )
    )
    _ = await asyncio.wait_for(two_requests_started.wait(), timeout=1)

    assert yielded_count == 2
    assert handler.call_count == 2

    _ = release.set()
    result = await asyncio.wait_for(run_task, timeout=1)

    assert yielded_count == 5
    assert result.planned_batch_count == 5
    assert result.dispatched_batch_count == 5
    assert result.undispatched_batch_count == 0


async def test_large_real_prompt_plan_materializes_only_worker_window_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实大计划停止后不得继续构造未派发批次的正文 prompt。"""
    item_count = 300
    data = TranslationData(
        display_name="大场景",
        translation_items=[
            TranslationItem(
                location_path=f"Map001.json/events/1/pages/0/list/{index}",
                item_type="short_text",
                role="村人",
                original_lines=[f"台詞{index}"],
            )
            for index in range(item_count)
        ],
    )
    setting = Setting.model_validate(
        {
            "llm": {
                "base_url": "https://example.invalid/v1",
                "api_key": "test",
                "model": "test-model",
                "timeout": 30,
            },
            "translation_context": {
                "token_size": 2000,
                "factor": 1.0,
                "max_command_items": 1,
            },
            "text_translation": {
                "worker_count": 2,
                "rpm": None,
                "retry_count": 0,
                "retry_delay": 0,
                "system_prompt_file": "<test>",
                "system_prompt": "系统提示",
            },
            "event_command_text": {
                "default_command_codes": [101],
                "default_command_codes_by_engine": {},
            },
        }
    )
    plan = build_translation_batches(
        translation_data_map={"Map001.json": data},
        setting=setting,
        text_rules=get_default_text_rules(),
        terminology_prompt_index=None,
    )
    assert len(plan) == item_count

    materialized_count = 0
    original_materialize = TranslationBatchBlueprint.materialize

    def counted_materialize(blueprint: TranslationBatchBlueprint) -> TranslationBatch:
        nonlocal materialized_count
        materialized_count += 1
        return original_materialize(blueprint)

    monkeypatch.setattr(TranslationBatchBlueprint, "materialize", counted_materialize)
    release_second_request = asyncio.Event()

    async def responder(_prompt: str) -> str:
        if handler.call_count == 1:
            return "ok"
        _ = await release_second_request.wait()
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)
    persisted_count = 0

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        nonlocal persisted_count
        persisted_count += 1
        if persisted_count == 1:
            return PersistedBatchCounts(success_count=0, quality_error_count=1)
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    run_task = asyncio.create_task(
        controller.run(
            plan,
            persist_batch=persist_batch,
            stop_on_error_rate=1,
        )
    )
    await asyncio.wait_for(_wait_until(lambda: controller.soft_stop_requested), timeout=1)

    assert materialized_count == 2
    release_second_request.set()
    result = await asyncio.wait_for(run_task, timeout=1)

    assert result.outcome == "stopped"
    assert result.dispatched_batch_count == 2
    assert result.undispatched_batch_count == item_count - 2
    assert materialized_count == 2


async def test_controller_rejects_unsized_iterable_without_consuming_it() -> None:
    yielded_count = 0

    def iter_batches() -> Iterator[TranslationBatch]:
        nonlocal yielded_count
        yielded_count += 1
        yield _make_batch("must-not-materialize")

    async def responder(_label: str) -> str:
        raise AssertionError("unsized iterable 不应派发模型请求")

    controller = _make_controller(
        llm_handler=ScriptedLLMHandler(responder),
        worker_count=1,
    )

    async def persist_batch(_result: BatchExecutionResult) -> None:
        raise AssertionError("unsized iterable 不应进入保存回调")

    with pytest.raises(TypeError, match="unsized iterable"):
        _ = await controller.run(
            cast(SizedBatchIterable, cast(object, iter_batches())),
            persist_batch=persist_batch,
        )

    assert yielded_count == 0


async def test_controller_lazily_dispatches_no_more_than_worker_count() -> None:
    release = asyncio.Event()
    two_requests_started = asyncio.Event()

    async def responder(_label: str) -> str:
        if handler.active_count == 2:
            _ = two_requests_started.set()
        _ = await release.wait()
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)
    active_persistence_count = 0
    max_active_persistence_count = 0

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        nonlocal active_persistence_count, max_active_persistence_count
        active_persistence_count += 1
        max_active_persistence_count = max(
            max_active_persistence_count,
            active_persistence_count,
        )
        try:
            await asyncio.sleep(0)
            return PersistedBatchCounts(success_count=1, quality_error_count=0)
        finally:
            active_persistence_count -= 1

    run_task = asyncio.create_task(
        controller.run(
            [_make_batch(str(index)) for index in range(5)],
            persist_batch=persist_batch,
        )
    )
    _ = await asyncio.wait_for(two_requests_started.wait(), timeout=1)
    assert handler.call_count == 2
    _ = release.set()

    result = await asyncio.wait_for(run_task, timeout=1)

    assert result.outcome == "completed"
    assert result.stop_code == "none"
    assert result.planned_batch_count == 5
    assert result.dispatched_batch_count == 5
    assert result.completed_batch_count == 5
    assert result.undispatched_batch_count == 0
    assert result.success_count == 5
    assert result.physical_request_count == 5
    assert result.retry_request_count == 0
    assert handler.max_active_count == 2
    assert max_active_persistence_count == 1


async def test_persisted_retranslation_batches_extend_controller_plan() -> None:
    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)
    persisted_labels: list[str] = []

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        label = result.batch.messages[-1].text
        persisted_labels.append(label)
        if label == "root":
            return PersistedBatchCounts(
                success_count=1,
                quality_error_count=0,
                retranslation_batches=_make_batch_plan("retry-1", "retry-2"),
            )
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    result = await controller.run(
        [_make_batch("root")],
        persist_batch=persist_batch,
    )

    assert persisted_labels == ["root", "retry-1", "retry-2"]
    assert result.outcome == "completed"
    assert result.planned_batch_count == 3
    assert result.dispatched_batch_count == 3
    assert result.completed_batch_count == 3
    assert result.undispatched_batch_count == 0
    assert result.success_count == 3
    assert result.physical_request_count == 3


async def test_max_batches_caps_initial_plan_inside_controller() -> None:
    """Controller 必须在创建任务前限制初始批次，不能依赖调用方预切片。"""

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=3)
    persisted_labels: list[str] = []

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        persisted_labels.append(result.batch.messages[-1].text)
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    result = await controller.run(
        _make_batch_plan("first", "second", "third"),
        persist_batch=persist_batch,
        max_batches=1,
    )

    assert persisted_labels == ["first"]
    assert handler.call_count == 1
    assert result.outcome == "stopped"
    assert result.stop_code == "run_limit_reached"
    assert result.limit_reason == "max_batches"
    assert result.planned_batch_count == 3
    assert result.dispatched_batch_count == 1
    assert result.completed_batch_count == 1
    assert result.undispatched_batch_count == 2
    assert result.physical_request_count == 1


async def test_max_batches_also_caps_dynamic_retranslation_plan() -> None:
    """复用目标复验失败追加的重译批次必须与初始计划共用同一上限。"""

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)
    persisted_labels: list[str] = []

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        label = result.batch.messages[-1].text
        persisted_labels.append(label)
        if label == "root":
            return PersistedBatchCounts(
                success_count=1,
                quality_error_count=0,
                retranslation_batches=_make_batch_plan("retry-1", "retry-2"),
            )
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    result = await controller.run(
        _make_batch_plan("root"),
        persist_batch=persist_batch,
        max_batches=2,
    )

    assert persisted_labels == ["root"]
    assert handler.call_count == 1
    assert result.outcome == "stopped"
    assert result.stop_code == "run_limit_reached"
    assert result.limit_reason == "max_batches"
    assert result.planned_batch_count == 3
    assert result.dispatched_batch_count == 1
    assert result.completed_batch_count == 1
    assert result.undispatched_batch_count == 2
    assert result.physical_request_count == 1


async def test_max_batches_dispatches_whole_dynamic_retranslation_plan_when_it_fits() -> None:
    """剩余批次数能容纳整段重译时，必须完整派发该段。"""

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)
    persisted_labels: list[str] = []

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        label = result.batch.messages[-1].text
        persisted_labels.append(label)
        if label == "root":
            return PersistedBatchCounts(
                success_count=1,
                quality_error_count=0,
                retranslation_batches=_make_batch_plan("retry-1", "retry-2"),
            )
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    result = await controller.run(
        _make_batch_plan("root"),
        persist_batch=persist_batch,
        max_batches=3,
    )

    assert persisted_labels == ["root", "retry-1", "retry-2"]
    assert handler.call_count == 3
    assert result.outcome == "completed"
    assert result.stop_code == "none"
    assert result.limit_reason == ""
    assert result.planned_batch_count == 3
    assert result.dispatched_batch_count == 3
    assert result.completed_batch_count == 3
    assert result.undispatched_batch_count == 0
    assert result.physical_request_count == 3


async def test_max_batches_keeps_multiple_dynamic_retranslation_segments_atomic() -> None:
    """多个动态重译段共用上限时，容纳不下的后续段不得部分派发。"""

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)
    persisted_labels: list[str] = []

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        label = result.batch.messages[-1].text
        persisted_labels.append(label)
        if label == "root-a":
            return PersistedBatchCounts(
                success_count=1,
                quality_error_count=0,
                retranslation_batches=_make_batch_plan("a-1", "a-2"),
            )
        if label == "root-b":
            return PersistedBatchCounts(
                success_count=1,
                quality_error_count=0,
                retranslation_batches=_make_batch_plan("b-1", "b-2"),
            )
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    result = await controller.run(
        _make_batch_plan("root-a", "root-b"),
        persist_batch=persist_batch,
        max_batches=5,
    )

    assert persisted_labels == ["root-a", "root-b", "a-1", "a-2"]
    assert handler.call_count == 4
    assert result.outcome == "stopped"
    assert result.stop_code == "run_limit_reached"
    assert result.limit_reason == "max_batches"
    assert result.planned_batch_count == 6
    assert result.dispatched_batch_count == 4
    assert result.completed_batch_count == 4
    assert result.undispatched_batch_count == 2
    assert result.physical_request_count == 4


async def test_soft_stop_counts_appended_retranslation_as_undispatched() -> None:
    retranslation_materialized_count = 0

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)

    def iter_retranslation_batches() -> Iterator[TranslationBatch]:
        nonlocal retranslation_materialized_count
        retranslation_materialized_count += 1
        yield _make_batch("retry")

    retranslation_plan = TranslationBatchPlan(
        iterator_factory=iter_retranslation_batches,
        batch_item_counts=(1,),
    )

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        return PersistedBatchCounts(
            success_count=0,
            quality_error_count=1,
            retranslation_batches=retranslation_plan,
        )

    result = await controller.run(
        [_make_batch("root")],
        persist_batch=persist_batch,
        stop_on_error_rate=1,
    )

    assert result.outcome == "stopped"
    assert result.stop_code == "quality_error_rate_reached"
    assert result.planned_batch_count == 2
    assert result.dispatched_batch_count == 1
    assert result.completed_batch_count == 1
    assert result.undispatched_batch_count == 1
    assert result.quality_error_count == 1
    assert handler.call_count == 1
    assert retranslation_materialized_count == 0


async def test_soft_stop_does_not_materialize_undispatched_plan_for_count() -> None:
    materialized_count = 0

    async def responder(_label: str) -> str:
        return "ok"

    def iter_batches() -> Iterator[TranslationBatch]:
        nonlocal materialized_count
        for index in range(4):
            materialized_count += 1
            yield _make_batch(str(index))

    plan = TranslationBatchPlan(
        iterator_factory=iter_batches,
        batch_item_counts=(1, 1, 1, 1),
    )
    controller = _make_controller(
        llm_handler=ScriptedLLMHandler(responder),
        worker_count=1,
    )

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        return PersistedBatchCounts(success_count=0, quality_error_count=1)

    result = await controller.run(
        plan,
        persist_batch=persist_batch,
        stop_on_error_rate=1,
    )

    assert result.planned_batch_count == 4
    assert result.dispatched_batch_count == 1
    assert result.undispatched_batch_count == 3
    assert materialized_count == 1


async def test_quality_soft_stop_keeps_inflight_request_and_persists_its_result() -> None:
    two_requests_started = asyncio.Event()
    release_second_request = asyncio.Event()
    second_request_cancelled = asyncio.Event()

    async def responder(label: str) -> str:
        if handler.active_count == 2:
            _ = two_requests_started.set()
        if label == "0":
            _ = await two_requests_started.wait()
            return "ok"
        try:
            _ = await release_second_request.wait()
        except asyncio.CancelledError:
            _ = second_request_cancelled.set()
            raise
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        label = result.batch.messages[-1].text
        if label == "0":
            return PersistedBatchCounts(success_count=0, quality_error_count=1)
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    run_task = asyncio.create_task(
        controller.run(
            [_make_batch(str(index)) for index in range(4)],
            persist_batch=persist_batch,
            stop_on_error_rate=0.5,
        )
    )
    _ = await asyncio.wait_for(two_requests_started.wait(), timeout=1)
    await asyncio.wait_for(
        _wait_until(lambda: controller.soft_stop_requested),
        timeout=1,
    )
    await asyncio.sleep(0.05)
    assert handler.active_count == 1
    assert not run_task.done()
    assert not second_request_cancelled.is_set()
    _ = release_second_request.set()

    result = await asyncio.wait_for(run_task, timeout=1)

    assert result.outcome == "stopped"
    assert result.stop_code == "quality_error_rate_reached"
    assert result.dispatched_batch_count == 2
    assert result.completed_batch_count == 2
    assert result.undispatched_batch_count == 2
    assert result.cancelled_batch_count == 0
    assert result.completed_after_stop_count == 1
    assert result.success_count == 1
    assert result.quality_error_count == 1
    assert result.physical_request_count == 2


async def test_quality_stop_commit_blocks_same_tick_zero_delay_retry() -> None:
    """质量错误提交后必须先发布停止状态，再允许其他任务取得重试许可。"""
    both_requests_started = asyncio.Event()
    persisted_quality_visible = asyncio.Event()
    started_labels: set[str] = set()
    b_attempt_count = 0

    async def responder(label: str) -> str:
        nonlocal b_attempt_count
        started_labels.add(label)
        if len(started_labels) == 2:
            both_requests_started.set()
        if label == "A":
            _ = await both_requests_started.wait()
            return "ok"
        b_attempt_count += 1
        if b_attempt_count > 1:
            raise AssertionError("质量错误已经提交后不得发出第二次 B 请求")
        _ = await persisted_quality_visible.wait()
        raise EmptyLLMResponseError("simulated retryable response failure")

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(
        llm_handler=handler,
        worker_count=2,
        retry_count=1,
    )
    persisted_labels: list[str] = []

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        label = result.batch.messages[-1].text
        persisted_labels.append(label)
        assert label == "A"
        persisted_quality_visible.set()
        # 模拟 SQLite commit 已经完成、保存协程尚未返回给 Controller 的调度窗口。
        await asyncio.sleep(0)
        return PersistedBatchCounts(success_count=0, quality_error_count=1)

    result = await asyncio.wait_for(
        controller.run(
            [_make_batch("A"), _make_batch("B")],
            persist_batch=persist_batch,
            stop_on_error_rate=1,
        ),
        timeout=1,
    )

    assert b_attempt_count == 1
    assert handler.call_count == 2
    assert persisted_labels == ["A"]
    assert result.outcome == "stopped"
    assert result.stop_code == "quality_error_rate_reached"
    assert result.completed_batch_count == 1
    assert result.cancelled_batch_count == 1
    assert result.quality_error_count == 1
    assert result.physical_request_count == 2
    assert result.retry_request_count == 0


async def test_quality_soft_stop_interrupts_batch_waiting_for_rpm_permission() -> None:
    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(
        llm_handler=handler,
        worker_count=2,
        rpm=60,
    )

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        return PersistedBatchCounts(success_count=0, quality_error_count=1)

    result = await asyncio.wait_for(
        controller.run(
            [_make_batch(str(index)) for index in range(3)],
            persist_batch=persist_batch,
            stop_on_error_rate=1,
        ),
        timeout=1,
    )

    assert result.outcome == "stopped"
    assert result.dispatched_batch_count == 2
    assert result.completed_batch_count == 1
    assert result.cancelled_batch_count == 1
    assert result.undispatched_batch_count == 1
    assert result.waiting_permission_cancelled_count == 1
    assert result.inflight_cancelled_count == 0
    assert result.physical_request_count == 1


async def test_time_limit_hard_cancels_and_waits_for_all_active_requests() -> None:
    request_cancelled = asyncio.Event()

    async def responder(_label: str) -> str:
        try:
            _ = await asyncio.Event().wait()
        finally:
            _ = request_cancelled.set()
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)

    async def persist_batch(_result: BatchExecutionResult) -> None:
        raise AssertionError("超时批次不应进入保存回调")

    result = await asyncio.wait_for(
        controller.run(
            [_make_batch(str(index)) for index in range(3)],
            persist_batch=persist_batch,
            time_limit_seconds=0.02,
        ),
        timeout=1,
    )

    assert result.outcome == "stopped"
    assert result.stop_code == "time_limit_reached"
    assert result.dispatched_batch_count == 2
    assert result.completed_batch_count == 0
    assert result.cancelled_batch_count == 2
    assert result.undispatched_batch_count == 1
    assert result.inflight_cancelled_count == 2
    assert result.physical_request_count == 2
    assert handler.active_count == 0
    assert request_cancelled.is_set()


async def test_time_limit_preserves_response_already_waiting_for_persistence() -> None:
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    persistence_completed = asyncio.Event()

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        _ = persistence_started.set()
        _ = await release_persistence.wait()
        _ = persistence_completed.set()
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    async def release_after_deadline() -> None:
        _ = await persistence_started.wait()
        await asyncio.sleep(0.03)
        _ = release_persistence.set()

    release_task = asyncio.create_task(release_after_deadline())
    result = await asyncio.wait_for(
        controller.run(
            [_make_batch("returned")],
            persist_batch=persist_batch,
            time_limit_seconds=0.01,
        ),
        timeout=1,
    )
    await release_task

    assert result.outcome == "stopped"
    assert result.stop_code == "time_limit_reached"
    assert result.completed_batch_count == 1
    assert result.cancelled_batch_count == 0
    assert result.completed_after_stop_count == 1
    assert result.success_count == 1
    assert persistence_completed.is_set()


async def test_llm_failure_hard_stops_and_waits_for_other_active_request() -> None:
    two_requests_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def responder(label: str) -> str:
        if handler.active_count == 2:
            _ = two_requests_started.set()
        if label == "0":
            _ = await two_requests_started.wait()
            raise ValueError("invalid model configuration")
        try:
            _ = await asyncio.Event().wait()
        finally:
            _ = sibling_cancelled.set()
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)

    async def persist_batch(_result: BatchExecutionResult) -> None:
        raise AssertionError("LLM 失败批次不应进入保存回调")

    result = await asyncio.wait_for(
        controller.run(
            [_make_batch(str(index)) for index in range(3)],
            persist_batch=persist_batch,
        ),
        timeout=1,
    )

    assert result.outcome == "failed"
    assert result.stop_code == "llm_request_failed"
    assert result.llm_failure is not None
    assert result.llm_failure.attempt_count == 1
    assert result.failed_batch_count == 1
    assert result.cancelled_batch_count == 1
    assert result.undispatched_batch_count == 1
    assert result.physical_request_count == 2
    assert handler.active_count == 0
    assert sibling_cancelled.is_set()


async def test_llm_failure_preserves_sibling_response_waiting_for_persistence() -> None:
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()

    async def responder(label: str) -> str:
        if label == "0":
            _ = await persistence_started.wait()
            raise ValueError("invalid model configuration")
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)

    async def persist_batch(result: BatchExecutionResult) -> PersistedBatchCounts:
        assert result.batch.messages[-1].text == "1"
        _ = persistence_started.set()
        _ = await release_persistence.wait()
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    async def release_after_failure() -> None:
        _ = await persistence_started.wait()
        await asyncio.sleep(0.02)
        _ = release_persistence.set()

    release_task = asyncio.create_task(release_after_failure())
    result = await asyncio.wait_for(
        controller.run(
            [_make_batch("0"), _make_batch("1")],
            persist_batch=persist_batch,
        ),
        timeout=1,
    )
    await release_task

    assert result.outcome == "failed"
    assert result.stop_code == "llm_request_failed"
    assert result.failed_batch_count == 1
    assert result.completed_batch_count == 1
    assert result.cancelled_batch_count == 0
    assert result.success_count == 1


async def test_persistence_failure_hard_stops_and_waits_for_other_request() -> None:
    two_requests_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def responder(label: str) -> str:
        if handler.active_count == 2:
            _ = two_requests_started.set()
        if label == "0":
            _ = await two_requests_started.wait()
            return "ok"
        try:
            _ = await asyncio.Event().wait()
        finally:
            _ = sibling_cancelled.set()
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=2)

    async def persist_batch(_result: BatchExecutionResult) -> None:
        raise RuntimeError("database is read-only")

    result = await asyncio.wait_for(
        controller.run(
            [_make_batch(str(index)) for index in range(3)],
            persist_batch=persist_batch,
        ),
        timeout=1,
    )

    assert result.outcome == "failed"
    assert result.stop_code == "persistence_failed"
    assert isinstance(result.failure, RuntimeError)
    assert result.failed_batch_count == 1
    assert result.cancelled_batch_count == 1
    assert result.undispatched_batch_count == 1
    assert result.physical_request_count == 2
    assert handler.active_count == 0
    assert sibling_cancelled.is_set()


async def test_candidate_validation_failure_is_not_reported_as_persistence() -> None:
    async def responder(_label: str) -> str:
        return "ok"

    async def fail_validation(
        *,
        ai_result: str,
        batch: TranslationBatch,
    ) -> BatchExecutionResult:
        del ai_result, batch
        raise RuntimeError("native quality contract mismatch")

    handler = ScriptedLLMHandler(responder)
    controller = TranslationRunController(
        llm_handler=handler,
        model="test-model",
        worker_count=1,
        retry_count=0,
        retry_delay=0,
        rpm=None,
        text_rules=None,
        execute_batch=fail_validation,
    )

    async def persist_batch(_result: BatchExecutionResult) -> None:
        raise AssertionError("校验失败后不应进入保存回调")

    result = await controller.run(
        [_make_batch("invalid")],
        persist_batch=persist_batch,
    )

    assert result.outcome == "failed"
    assert result.stop_code == "candidate_validation_failed"
    assert isinstance(result.failure, RuntimeError)
    assert result.failed_batch_count == 1
    assert result.completed_batch_count == 0
    assert result.physical_request_count == 1


async def test_controller_reports_physical_retries_and_persisted_expanded_counts() -> None:
    attempt_count = 0

    async def responder(_label: str) -> str:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise EmptyLLMResponseError("empty")
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(
        llm_handler=handler,
        worker_count=1,
        retry_count=1,
    )

    async def persist_batch(batch_result: BatchExecutionResult) -> PersistedBatchCounts:
        assert batch_result.physical_request_count == 2
        assert batch_result.retry_request_count == 1
        return PersistedBatchCounts(
            success_count=4,
            quality_error_count=1,
            reused_current_run_count=2,
            reused_saved_count=1,
            rejected_reuse_count=3,
        )

    result = await controller.run(
        [_make_batch("retry")],
        persist_batch=persist_batch,
    )

    assert result.outcome == "completed_with_quality_errors"
    assert result.success_count == 4
    assert result.quality_error_count == 1
    assert result.reused_current_run_count == 2
    assert result.reused_saved_count == 1
    assert result.rejected_reuse_count == 3
    assert result.physical_request_count == 2
    assert result.retry_request_count == 1


async def test_external_cancellation_waits_for_active_request_before_propagating() -> None:
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()

    async def responder(_label: str) -> str:
        _ = request_started.set()
        try:
            _ = await asyncio.Event().wait()
        finally:
            _ = request_cancelled.set()
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)

    async def persist_batch(_result: BatchExecutionResult) -> None:
        raise AssertionError("取消批次不应进入保存回调")

    run_task = asyncio.create_task(
        controller.run(
            [_make_batch("cancel")],
            persist_batch=persist_batch,
        )
    )
    _ = await asyncio.wait_for(request_started.wait(), timeout=1)
    _ = run_task.cancel()

    with pytest.raises(TranslationRunCancelled) as raised:
        await run_task
    result = raised.value.result
    assert result.outcome == "cancelled"
    assert result.stop_code == "user_cancelled"
    assert result.planned_batch_count == 1
    assert result.dispatched_batch_count == 1
    assert result.completed_batch_count == 0
    assert result.cancelled_batch_count == 1
    assert result.inflight_cancelled_count == 1
    assert result.physical_request_count == 1
    assert handler.active_count == 0
    assert request_cancelled.is_set()


async def test_external_cancellation_preserves_already_returned_response() -> None:
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    persistence_completed = asyncio.Event()

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)

    async def persist_batch(_result: BatchExecutionResult) -> None:
        _ = persistence_started.set()
        _ = await release_persistence.wait()
        _ = persistence_completed.set()

    run_task = asyncio.create_task(
        controller.run(
            [_make_batch("cancel-after-response")],
            persist_batch=persist_batch,
        )
    )
    _ = await asyncio.wait_for(persistence_started.wait(), timeout=1)
    _ = run_task.cancel()
    _ = release_persistence.set()

    with pytest.raises(TranslationRunCancelled) as raised:
        await run_task
    result = raised.value.result
    assert result.outcome == "cancelled"
    assert result.completed_batch_count == 1
    assert result.cancelled_batch_count == 0
    assert result.completed_after_stop_count == 1
    assert result.physical_request_count == 1
    assert persistence_completed.is_set()


async def test_external_cancellation_waits_until_returned_response_is_persisted() -> None:
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    persistence_completed = asyncio.Event()

    async def responder(_label: str) -> str:
        return "ok"

    handler = ScriptedLLMHandler(responder)
    controller = _make_controller(llm_handler=handler, worker_count=1)

    async def persist_batch(_result: BatchExecutionResult) -> PersistedBatchCounts:
        _ = persistence_started.set()
        _ = await release_persistence.wait()
        _ = persistence_completed.set()
        return PersistedBatchCounts(success_count=1, quality_error_count=0)

    run_task = asyncio.create_task(
        controller.run(
            [_make_batch("cancel-stuck-persistence")],
            persist_batch=persist_batch,
        )
    )
    _ = await asyncio.wait_for(persistence_started.wait(), timeout=1)
    _ = run_task.cancel()
    await asyncio.sleep(0)

    assert not run_task.done()
    assert not persistence_completed.is_set()
    release_persistence.set()

    with pytest.raises(TranslationRunCancelled) as raised:
        _ = await asyncio.wait_for(run_task, timeout=1)

    result = raised.value.result
    assert result.outcome == "cancelled"
    assert result.dispatched_batch_count == 1
    assert result.completed_batch_count == 1
    assert result.cancelled_batch_count == 0
    assert result.inflight_cancelled_count == 0
    assert result.physical_request_count == 1
    assert result.success_count == 1
    assert persistence_completed.is_set()


@pytest.mark.parametrize(
    ("max_batches", "time_limit_seconds", "stop_on_error_rate"),
    [
        (0, None, None),
        (None, 0, None),
        (None, None, 0),
        (None, None, 1.01),
    ],
)
async def test_controller_rejects_invalid_run_limits(
    max_batches: int | None,
    time_limit_seconds: float | None,
    stop_on_error_rate: float | None,
) -> None:
    async def responder(_label: str) -> str:
        return "ok"

    controller = _make_controller(
        llm_handler=ScriptedLLMHandler(responder),
        worker_count=1,
    )

    async def persist_batch(_result: BatchExecutionResult) -> None:
        return None

    with pytest.raises(ValueError):
        _ = await controller.run(
            [],
            persist_batch=persist_batch,
            max_batches=max_batches,
            time_limit_seconds=time_limit_seconds,
            stop_on_error_rate=stop_on_error_rate,
        )
