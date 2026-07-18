"""正文翻译的上下文安全复用索引。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from app.rmmz.schema import ItemType, SourceResidualRuleRecord, TranslationData, TranslationItem
from app.rmmz.source_snapshot import SourceSnapshotFileRecord
from app.rmmz.text_rules import JsonObject, JsonValue
from app.terminology.prompt import (
    TerminologyPromptEntry,
    TerminologyPromptIndex,
    filter_terminology_prompt_entries,
)
from app.text_scope.models import TextScopeResult

PROMPT_PROTOCOL_VERSION = "translation-json-v3"


def canonical_json_hash(payload: JsonValue) -> str:
    """对 JSON 兼容载荷生成跨进程稳定的 SHA-256。"""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TranslationNeighbor:
    """会进入语义判断的相邻正文摘要。"""

    original_lines: tuple[str, ...]
    item_type: ItemType
    role: str | None

    def to_json_object(self) -> JsonObject:
        """转换为不含位置元数据的模型上下文载荷。"""
        return {
            "original_lines": list(self.original_lines),
            "item_type": self.item_type,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class TranslationPromptItemContext:
    """单个短 ID 独占的模型可见上下文事实。"""

    display_name: str
    previous_items: tuple[TranslationNeighbor, ...]
    next_items: tuple[TranslationNeighbor, ...]
    terminology_entries: tuple[TerminologyPromptEntry, ...]

    def terminology_tuples(self) -> tuple[tuple[str, str, str], ...]:
        """返回与提示词展示顺序完全一致的术语三元组。"""
        return tuple((entry.category, entry.source_text, entry.translated_text) for entry in self.terminology_entries)

    def to_json_object(self) -> JsonObject:
        """转换为可用于上下文键和协议测试的规范载荷。"""
        return cast(
            JsonObject,
            {
                "display_name": self.display_name,
                "previous_items": [neighbor.to_json_object() for neighbor in self.previous_items],
                "next_items": [neighbor.to_json_object() for neighbor in self.next_items],
                "terminology_entries": [list(entry) for entry in self.terminology_tuples()],
            },
        )


@dataclass(frozen=True, slots=True)
class TranslationContextKey:
    """只有全部模型可见语义上下文一致时才允许复用的键。"""

    original_lines: tuple[str, ...]
    item_type: ItemType
    role: str | None
    source_domain: str
    rule_source: str
    owner_key: str
    prompt_context: TranslationPromptItemContext
    source_language: str
    target_language: str
    prompt_protocol_version: str

    @property
    def display_name(self) -> str:
        """返回逐项 Prompt 使用的场景名称。"""
        return self.prompt_context.display_name

    @property
    def previous_items(self) -> tuple[TranslationNeighbor, ...]:
        """返回同容器内前两项上下文。"""
        return self.prompt_context.previous_items

    @property
    def next_items(self) -> tuple[TranslationNeighbor, ...]:
        """返回同容器内后两项上下文。"""
        return self.prompt_context.next_items

    @property
    def terminology_entries(self) -> tuple[tuple[str, str, str], ...]:
        """返回实际进入该项 Prompt 的术语。"""
        return self.prompt_context.terminology_tuples()

    def canonical_json(self) -> str:
        """返回可跨进程、跨轮持久化比较的规范 JSON。"""
        return json.dumps(
            self.to_json_object(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def canonical_hash(self) -> str:
        """返回规范上下文键的 SHA-256。"""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def terminology_fingerprint(self) -> str:
        """返回实际进入该条正文提示词的术语指纹。"""
        return canonical_json_hash([list(entry) for entry in self.terminology_entries])

    def language_fingerprint(self) -> str:
        """返回源语言组合与目标语言的稳定指纹。"""
        return canonical_json_hash(
            {
                "source_language": self.source_language,
                "target_language": self.target_language,
            }
        )

    def to_json_object(self) -> JsonObject:
        """转换为不含本地写入位置的规范上下文载荷。"""
        return cast(
            JsonObject,
            {
                "original_lines": list(self.original_lines),
                "item_type": self.item_type,
                "role": self.role,
                "source_domain": self.source_domain,
                "rule_source": self.rule_source,
                "owner_key": self.owner_key,
                "prompt_context": self.prompt_context.to_json_object(),
                "source_language": self.source_language,
                "target_language": self.target_language,
                "prompt_protocol_version": self.prompt_protocol_version,
            },
        )


type TranslationCacheKey = TranslationContextKey


@dataclass(frozen=True, slots=True)
class TranslationReuseContextData:
    """翻译层交给持久层精确匹配的历史复用契约。"""

    context_key_json: str
    context_key_hash: str
    source_fingerprint: str
    rule_fingerprint: str
    terminology_fingerprint: str
    language_fingerprint: str
    prompt_protocol_version: str


class TranslationCache:
    """单轮去重与跨轮已保存译文复用所需的内存索引。"""

    def __init__(self) -> None:
        """初始化尚未绑定当前文本范围的索引。"""
        self.seen_keys: set[TranslationContextKey] = set()
        self.duplicate_items: dict[TranslationContextKey, list[TranslationItem]] = {}
        self._keys_by_location: dict[str, TranslationContextKey] = {}
        self._keys_by_reuse_context: dict[
            TranslationReuseContextData,
            TranslationContextKey,
        ] = {}
        self._saved_translations: dict[TranslationContextKey, set[tuple[str, ...]]] = {}
        self._source_fingerprint: str | None = None
        self._rule_fingerprint: str | None = None

    def prepare(
        self,
        *,
        translation_data_map: dict[str, TranslationData],
        terminology_prompt_index: TerminologyPromptIndex | None,
        source_language: str,
        target_language: str = "zh-CN",
        source_fingerprint: str | None = None,
        rule_fingerprint: str | None = None,
        source_domains_by_path: dict[str, str] | None = None,
        rule_sources_by_path: dict[str, str] | None = None,
    ) -> None:
        """为当前可信文本范围预先构建完整上下文键。"""
        if (source_fingerprint is None) != (rule_fingerprint is None):
            raise ValueError("source_fingerprint 与 rule_fingerprint 必须同时提供")
        if source_fingerprint == "" or rule_fingerprint == "":
            raise ValueError("复用指纹不能为空字符串")
        self.seen_keys.clear()
        self.duplicate_items.clear()
        self._keys_by_location.clear()
        self._keys_by_reuse_context.clear()
        self._saved_translations.clear()
        self._source_fingerprint = source_fingerprint
        self._rule_fingerprint = rule_fingerprint

        seen_location_paths: set[str] = set()
        for rule_source, translation_data in translation_data_map.items():
            items = translation_data.translation_items
            for index, item in enumerate(items):
                if item.location_path in seen_location_paths:
                    raise ValueError(f"正文内部位置重复，无法构建复用上下文: {item.location_path}")
                seen_location_paths.add(item.location_path)
                display_name = translation_data.display_name or ""
                if source_domains_by_path is not None and item.location_path not in source_domains_by_path:
                    continue
                if rule_sources_by_path is not None and item.location_path not in rule_sources_by_path:
                    continue
                resolved_rule_source = (
                    rule_sources_by_path[item.location_path] if rule_sources_by_path is not None else rule_source
                )
                resolved_source_domain = (
                    source_domains_by_path[item.location_path]
                    if source_domains_by_path is not None
                    else _source_domain(item.location_path)
                )
                if not resolved_rule_source.strip() or not resolved_source_domain.strip():
                    continue
                context_key = _build_context_key(
                    item=item,
                    items=items,
                    item_index=index,
                    display_name=display_name,
                    rule_source=resolved_rule_source,
                    source_domain=resolved_source_domain,
                    terminology_prompt_index=terminology_prompt_index,
                    source_language=source_language,
                    target_language=target_language,
                )
                self._keys_by_location[item.location_path] = context_key
                reuse_context = self._build_reuse_context(context_key)
                if reuse_context is not None:
                    self._keys_by_reuse_context[reuse_context] = context_key

    def build_cache_key(self, item: TranslationItem) -> TranslationContextKey | None:
        """读取当前范围中为条目构造的上下文键；范围外条目不得复用。"""
        return self._keys_by_location.get(item.location_path)

    def build_prompt_context(
        self,
        item: TranslationItem,
    ) -> TranslationPromptItemContext | None:
        """读取与当前/跨轮复用键同源的逐项 Prompt 上下文。"""
        cache_key = self.build_cache_key(item)
        if cache_key is None:
            return None
        return cache_key.prompt_context

    def build_reuse_context(
        self,
        item: TranslationItem,
    ) -> TranslationReuseContextData | None:
        """为当前目标位置生成可持久化的完整复用上下文。"""
        cache_key = self.build_cache_key(item)
        if cache_key is None:
            return None
        return self._build_reuse_context(cache_key)

    def remember_saved_translation(
        self,
        item: TranslationItem,
        context: TranslationReuseContextData,
    ) -> bool:
        """登记当前范围内的已保存译文，返回是否能够参与跨轮复用。"""
        cache_key = self._keys_by_reuse_context.get(context)
        if (
            cache_key is None
            or not item.translation_lines
            or tuple(item.original_lines) != cache_key.original_lines
            or item.item_type != cache_key.item_type
            or item.role != cache_key.role
        ):
            return False
        self._saved_translations.setdefault(cache_key, set()).add(tuple(item.translation_lines))
        return True

    def _build_reuse_context(
        self,
        cache_key: TranslationContextKey,
    ) -> TranslationReuseContextData | None:
        """组合上下文键和运行级指纹；缺少证据时禁止跨轮复用。"""
        if self._source_fingerprint is None or self._rule_fingerprint is None:
            return None
        return TranslationReuseContextData(
            context_key_json=cache_key.canonical_json(),
            context_key_hash=cache_key.canonical_hash(),
            source_fingerprint=self._source_fingerprint,
            rule_fingerprint=self._rule_fingerprint,
            terminology_fingerprint=cache_key.terminology_fingerprint(),
            language_fingerprint=cache_key.language_fingerprint(),
            prompt_protocol_version=cache_key.prompt_protocol_version,
        )

    def reusable_saved_translation(
        self,
        item: TranslationItem,
    ) -> tuple[list[str] | None, bool]:
        """返回唯一可信的历史译文；多种译文时显式报告冲突。"""
        cache_key = self.build_cache_key(item)
        if cache_key is None:
            return None, False
        candidates = self._saved_translations.get(cache_key, set())
        if len(candidates) > 1:
            return None, True
        if not candidates:
            return None, False
        return list(next(iter(candidates))), False

    def remember_or_defer(self, item: TranslationItem) -> bool:
        """记录首条正文或暂存完整上下文相同的重复正文。"""
        cache_key = self.build_cache_key(item)
        if cache_key is None:
            return True
        if cache_key not in self.seen_keys:
            self.seen_keys.add(cache_key)
            return True

        self.duplicate_items.setdefault(cache_key, []).append(item)
        return False

    def pop_duplicate_items(self, item: TranslationItem) -> list[TranslationItem]:
        """取出与成功正文完整上下文相同的全部重复条目。"""
        cache_key = self.build_cache_key(item)
        if cache_key is None:
            return []
        return self.duplicate_items.pop(cache_key, [])

    def pop_duplicate_items_by_location(self, location_path: str) -> list[TranslationItem]:
        """按本地条目位置取出对应上下文的重复项。"""
        cache_key = self._keys_by_location.get(location_path)
        if cache_key is None:
            return []
        return self.duplicate_items.pop(cache_key, [])

    def pop_duplicate_items_by_fields(
        self,
        *,
        original_lines: list[str],
        item_type: ItemType,
        role: str | None,
    ) -> list[TranslationItem]:
        """旧调用无法证明上下文一致，保守地不展开重复项。"""
        _ = original_lines
        _ = item_type
        _ = role
        return []


def _build_context_key(
    *,
    item: TranslationItem,
    items: list[TranslationItem],
    item_index: int,
    display_name: str,
    rule_source: str,
    source_domain: str,
    terminology_prompt_index: TerminologyPromptIndex | None,
    source_language: str,
    target_language: str,
) -> TranslationContextKey:
    """从当前提取顺序和实际术语选择构造稳定键。"""
    owner_key = _owner_key(item.location_path, source_domain=source_domain)
    prompt_context = build_translation_prompt_item_context(
        item=item,
        items=items,
        item_index=item_index,
        display_name=display_name,
        terminology_prompt_index=terminology_prompt_index,
        source_domain=source_domain,
        owner_key=owner_key,
    )

    return TranslationContextKey(
        original_lines=tuple(item.original_lines),
        item_type=item.item_type,
        role=item.role,
        source_domain=source_domain,
        rule_source=rule_source,
        owner_key=owner_key,
        prompt_context=prompt_context,
        source_language=source_language,
        target_language=target_language,
        prompt_protocol_version=PROMPT_PROTOCOL_VERSION,
    )


def build_translation_prompt_item_context(
    *,
    item: TranslationItem,
    items: Sequence[TranslationItem],
    item_index: int,
    display_name: str,
    terminology_prompt_index: TerminologyPromptIndex | None,
    source_domain: str | None = None,
    owner_key: str | None = None,
) -> TranslationPromptItemContext:
    """从完整范围构造与批次边界无关的逐项 Prompt 上下文。"""
    resolved_source_domain = source_domain or _source_domain(item.location_path)
    resolved_owner_key = owner_key or _owner_key(
        item.location_path,
        source_domain=resolved_source_domain,
    )
    previous_items = [neighbor for neighbor in items[:item_index] if _item_owner_key(neighbor) == resolved_owner_key][
        -2:
    ]
    next_items = [neighbor for neighbor in items[item_index + 1 :] if _item_owner_key(neighbor) == resolved_owner_key][
        :2
    ]

    terminology_entries: tuple[TerminologyPromptEntry, ...] = ()
    if terminology_prompt_index is not None:
        selected_entries = terminology_prompt_index.select_for_batch(
            display_name=display_name,
            items=[item],
        )
        terminology_entries = tuple(
            sorted(
                filter_terminology_prompt_entries(selected_entries),
                key=lambda entry: (
                    entry.category,
                    entry.source_text,
                    entry.translated_text,
                ),
            )
        )

    return TranslationPromptItemContext(
        display_name=display_name,
        previous_items=tuple(_neighbor(neighbor) for neighbor in previous_items),
        next_items=tuple(_neighbor(neighbor) for neighbor in next_items),
        terminology_entries=terminology_entries,
    )


def _neighbor(item: TranslationItem) -> TranslationNeighbor:
    return TranslationNeighbor(
        original_lines=tuple(item.original_lines),
        item_type=item.item_type,
        role=item.role,
    )


def _item_owner_key(item: TranslationItem) -> str:
    """根据条目位置推导其语义容器。"""
    return _owner_key(
        item.location_path,
        source_domain=_source_domain(item.location_path),
    )


def _source_domain(location_path: str) -> str:
    normalized_path = location_path.replace("\\", "/")
    if normalized_path.lower().startswith("js/plugins/"):
        return "plugin_source"
    first_part = normalized_path.split("/", maxsplit=1)[0]
    lowered = first_part.lower()
    if lowered == "plugins.js":
        return "plugin_parameter"
    if "/note/" in f"/{normalized_path}/".lower():
        return "note_tag"
    if first_part.startswith(("Map", "CommonEvents", "Troops")):
        return "event_command"
    return "database_text"


def _owner_key(location_path: str, *, source_domain: str) -> str:
    """提取场景内稳定 owner，避免把不同事件或插件误并为同一上下文。"""
    parts = location_path.split("/")
    if not parts or not parts[0]:
        raise ValueError("正文内部位置不能为空")
    if "note" in parts:
        note_index = parts.index("note")
        return "/".join(parts[:note_index])
    if source_domain == "plugin_source" and len(parts) >= 3:
        return "/".join(parts[:3])
    if "list" in parts:
        list_index = parts.index("list")
        return "/".join(parts[:list_index])
    return "/".join(parts[:2])


def prepare_translation_cache_for_scope(
    *,
    translation_cache: TranslationCache,
    scope: TextScopeResult,
    terminology_prompt_index: TerminologyPromptIndex | None,
    source_language: str,
    additional_source_languages: Sequence[str],
    target_language: str,
    source_snapshot_records: Sequence[SourceSnapshotFileRecord],
    source_residual_records: Sequence[SourceResidualRuleRecord],
) -> None:
    """用当前文本范围的唯一事实构建翻译上下文与复用指纹。"""
    translation_cache.prepare(
        translation_data_map=scope.translation_data_map,
        terminology_prompt_index=terminology_prompt_index,
        source_language="|".join([source_language, *additional_source_languages]),
        target_language=target_language,
        source_fingerprint=canonical_json_hash(
            [
                {
                    "relative_path": record.relative_path,
                    "sha256": record.sha256,
                    "byte_size": record.byte_size,
                }
                for record in sorted(source_snapshot_records, key=lambda item: item.relative_path)
            ]
        ),
        rule_fingerprint=canonical_json_hash(
            {
                "text_scope": scope.translation_rule_fingerprint,
                "source_residual_rules": [record.model_dump(mode="json") for record in source_residual_records],
            }
        ),
        source_domains_by_path={
            entry.location_path: entry.source_type for entry in scope.entries if entry.enters_translation
        },
        rule_sources_by_path={
            entry.location_path: entry.rule_source for entry in scope.entries if entry.enters_translation
        },
    )


__all__: list[str] = [
    "PROMPT_PROTOCOL_VERSION",
    "TranslationReuseContextData",
    "TranslationCache",
    "TranslationCacheKey",
    "TranslationContextKey",
    "TranslationNeighbor",
    "TranslationPromptItemContext",
    "build_translation_prompt_item_context",
    "canonical_json_hash",
    "prepare_translation_cache_for_scope",
]
