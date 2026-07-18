"""正文提示词使用的术语表索引。"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.rmmz.schema import SYSTEM_FILE_NAME, GameData, TranslationItem

from .extraction import BASE_NAME_CATEGORIES, SYSTEM_TERM_CATEGORIES
from .schemas import TerminologyGlossary

PROMPT_MEANINGFUL_TERM_PATTERN: re.Pattern[str] = re.compile(
    r"[\w\u3040-\u30FF\u3400-\u9FFF]",
    re.UNICODE,
)


@dataclass(frozen=True, slots=True)
class TerminologyPromptEntry:
    """注入正文用户提示词的一条术语映射。"""

    category: Literal["glossary"]
    source_text: str
    translated_text: str


class TerminologyPromptIndex:
    """把正文术语表转成按批次查询的提示词索引。"""

    def __init__(
        self,
        *,
        entries: list[TerminologyPromptEntry],
        owner_entries: dict[str, list[TerminologyPromptEntry]],
        system_entries: list[TerminologyPromptEntry],
    ) -> None:
        """初始化索引。"""
        self.entries: list[TerminologyPromptEntry] = entries
        self._entries_by_match_text: dict[str, list[TerminologyPromptEntry]] = {}
        self._owner_entries: dict[str, list[TerminologyPromptEntry]] = owner_entries
        self._system_entries: list[TerminologyPromptEntry] = system_entries
        self._build_indexes(entries=entries)

    @classmethod
    def from_glossary(
        cls,
        glossary: TerminologyGlossary,
        game_data: GameData | None = None,
    ) -> "TerminologyPromptIndex":
        """从正文术语表构建索引。"""
        entries = [
            TerminologyPromptEntry("glossary", source_text, translated_text)
            for source_text, translated_text in glossary.terms.items()
        ]
        index = cls(
            entries=entries,
            owner_entries=_build_owner_entries(glossary=glossary, game_data=game_data),
            system_entries=_build_system_entries(glossary=glossary, game_data=game_data),
        )
        index._build_indexes(entries=entries)
        return index

    def select_for_batch(
        self,
        *,
        display_name: str,
        items: list[TranslationItem],
    ) -> list[TerminologyPromptEntry]:
        """根据当前地图、正文批次和数据库条目挑选相关术语。"""
        selected: list[TerminologyPromptEntry] = []
        if display_name:
            selected.extend(self._entries_by_match_text.get(display_name, []))

        joined_original_text = "\n".join(line for item in items for line in item.original_lines)
        for item in items:
            if item.role is not None:
                selected.extend(self._entries_by_match_text.get(item.role, []))
            selected.extend(self._select_owner_entries(item.location_path))

        for match_text, entries in self._entries_by_match_text.items():
            if match_text in joined_original_text:
                selected.extend(entries)

        return deduplicate_prompt_entries(selected)

    def _select_owner_entries(self, location_path: str) -> list[TerminologyPromptEntry]:
        """按正文条目所在数据库对象选择同条目名称术语。"""
        if location_path.startswith(f"{SYSTEM_FILE_NAME}/"):
            return self._system_entries
        parts = location_path.split("/")
        if len(parts) < 2:
            return []
        owner_key = "/".join(parts[:2])
        return self._owner_entries.get(owner_key, [])

    def _build_indexes(self, *, entries: list[TerminologyPromptEntry]) -> None:
        """构造按规范术语查询的索引。"""
        self._entries_by_match_text.clear()
        for entry in entries:
            self._entries_by_match_text.setdefault(entry.source_text, []).append(entry)


def _build_owner_entries(
    *,
    glossary: TerminologyGlossary,
    game_data: GameData | None,
) -> dict[str, list[TerminologyPromptEntry]]:
    """为数据库条目正文建立同条目术语索引。"""
    if game_data is None:
        return {}
    owner_entries: dict[str, list[TerminologyPromptEntry]] = {}
    for file_name in BASE_NAME_CATEGORIES:
        for item in game_data.base_data.get(file_name, []):
            if item is None:
                continue
            owner_key = f"{file_name}/{item.id}"
            owner_entries.setdefault(owner_key, []).extend(_entries_matching_text(glossary=glossary, text=item.name))
            if file_name != "Actors.json":
                continue
            owner_entries.setdefault(owner_key, []).extend(
                _entries_matching_text(glossary=glossary, text=item.nickname)
            )
    return {owner_key: deduplicate_prompt_entries(entries) for owner_key, entries in owner_entries.items() if entries}


def _build_system_entries(
    *,
    glossary: TerminologyGlossary,
    game_data: GameData | None,
) -> list[TerminologyPromptEntry]:
    """收集 System 正文翻译时可参考的系统类型术语。"""
    if game_data is None:
        return []
    entries: list[TerminologyPromptEntry] = []
    for field_name in SYSTEM_TERM_CATEGORIES:
        for value in _read_system_field_values(game_data=game_data, field_name=field_name):
            entries.extend(_entries_matching_text(glossary=glossary, text=value))
    return deduplicate_prompt_entries(entries)


def _read_system_field_values(*, game_data: GameData, field_name: str) -> list[str]:
    """读取 System 类型数组，避免把动态字段访问传入业务流程。"""
    if field_name == "elements":
        return game_data.system.elements
    if field_name == "skillTypes":
        return game_data.system.skillTypes
    if field_name == "weaponTypes":
        return game_data.system.weaponTypes
    if field_name == "armorTypes":
        return game_data.system.armorTypes
    if field_name == "equipTypes":
        return game_data.system.equipTypes
    raise ValueError(f"未知 System 术语字段: {field_name}")


def _entries_matching_text(*, glossary: TerminologyGlossary, text: str) -> list[TerminologyPromptEntry]:
    """按规范术语为某段游戏字段值选择提示词条目。"""
    source_text = text.strip()
    if not source_text:
        return []
    translated_text = glossary.terms.get(source_text)
    if translated_text is None:
        return []
    return [TerminologyPromptEntry("glossary", source_text, translated_text)]


def filter_terminology_prompt_entries(
    entries: Sequence[TerminologyPromptEntry],
) -> list[TerminologyPromptEntry]:
    """返回真正会进入模型提示词的去重术语。"""
    return [entry for entry in deduplicate_prompt_entries(list(entries)) if not _is_prompt_noise_entry(entry)]


def format_terminology_prompt_section(entries: Sequence[TerminologyPromptEntry]) -> str:
    """把术语映射格式化为用户提示词片段。"""
    prompt_entries = filter_terminology_prompt_entries(entries)
    if not prompt_entries:
        return ""

    sections = ["[[术语表]]"]
    sections.extend(format_prompt_entry(entry) for entry in prompt_entries)
    return "\n".join(sections)


def _is_prompt_noise_entry(entry: TerminologyPromptEntry) -> bool:
    """过滤不会提升翻译质量的术语提示噪音。"""
    source = entry.source_text.strip()
    translated = entry.translated_text.strip()
    if not source or not translated:
        return True
    return PROMPT_MEANINGFUL_TERM_PATTERN.search(source) is None


def format_prompt_entry(entry: TerminologyPromptEntry) -> str:
    """格式化单条术语映射。"""
    return f"{entry.source_text} => {entry.translated_text}"


def deduplicate_prompt_entries(entries: list[TerminologyPromptEntry]) -> list[TerminologyPromptEntry]:
    """按术语映射去重并保持原有顺序。"""
    seen: set[tuple[str, str, str]] = set()
    unique_entries: list[TerminologyPromptEntry] = []
    for entry in entries:
        key = (entry.category, entry.source_text, entry.translated_text)
        if key in seen:
            continue
        seen.add(key)
        unique_entries.append(entry)
    return unique_entries


__all__: list[str] = [
    "TerminologyPromptEntry",
    "TerminologyPromptIndex",
    "deduplicate_prompt_entries",
    "filter_terminology_prompt_entries",
    "format_terminology_prompt_section",
]
