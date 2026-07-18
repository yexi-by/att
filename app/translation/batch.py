"""正文翻译批次模型、惰性计划与模型可见 ID 绑定。"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from app.llm.schemas import ChatMessage
from app.rmmz.schema import TranslationItem

PROMPT_REQUEST_ID_PREFIX = "T"


@dataclass(frozen=True, slots=True)
class TranslationPromptBinding:
    """把单批次模型可见短 ID 唯一绑定到本地正文条目。"""

    request_id: str
    item: TranslationItem


def bind_translation_items(
    items: Sequence[TranslationItem],
) -> tuple[TranslationPromptBinding, ...]:
    """按批次顺序生成不含本地路径的稳定短 ID。"""
    return tuple(
        TranslationPromptBinding(
            request_id=f"{PROMPT_REQUEST_ID_PREFIX}{index:06d}",
            item=item,
        )
        for index, item in enumerate(items, start=1)
    )


@dataclass(frozen=True, slots=True)
class TranslationBatch:
    """一次模型请求需要处理的正文条目与消息列表。"""

    bindings: tuple[TranslationPromptBinding, ...]
    messages: list[ChatMessage]
    estimated_tokens: int
    token_limit: int

    @property
    def items(self) -> list[TranslationItem]:
        """按模型短 ID 的顺序返回本地正文条目。"""
        return [binding.item for binding in self.bindings]


@dataclass(frozen=True, slots=True)
class TranslationBatchPlan:
    """可重复遍历但不驻留完整 prompt 的惰性批次计划。"""

    iterator_factory: Callable[[], Iterator[TranslationBatch]]
    batch_item_counts: tuple[int, ...]

    def __post_init__(self) -> None:
        """拒绝无法生成可靠计划计数的非法元数据。"""
        for item_count in self.batch_item_counts:
            if isinstance(item_count, bool) or item_count < 0:
                raise ValueError("batch_item_counts 必须只包含大于等于 0 的整数")

    def __iter__(self) -> Iterator[TranslationBatch]:
        """为每次运行创建独立批次迭代器。"""
        return self.iterator_factory()

    def __len__(self) -> int:
        """返回本计划声明的准确批次数。"""
        return len(self.batch_item_counts)

    @property
    def item_count(self) -> int:
        """返回本计划包含的正文条目总数。"""
        return sum(self.batch_item_counts)


__all__: list[str] = [
    "TranslationBatch",
    "TranslationBatchPlan",
    "TranslationPromptBinding",
    "bind_translation_items",
]
