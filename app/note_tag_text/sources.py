"""Note 标签来源扫描工具。"""

from dataclasses import dataclass
from fnmatch import fnmatchcase

from app.native_quality import collect_native_note_tag_sources
from app.rmmz.schema import MAP_PATTERN, PLUGINS_FILE_NAME, GameData
from app.rmmz.text_rules import ensure_json_array, ensure_json_object, ensure_json_string_list

MAP_NOTE_FILE_PATTERN = "Map*.json"


@dataclass(frozen=True, slots=True)
class NoteTagMatchFact:
    """原生 Note 扫描器返回的可复用标签匹配事实。"""

    tag_name: str
    value: str
    has_value: bool


@dataclass(frozen=True, slots=True)
class NoteTagSource:
    """单个 `note` 字段及其可回写定位信息。"""

    file_name: str
    owner_path: tuple[str, ...]
    note_text: str
    location_prefix: str
    matches: tuple[NoteTagMatchFact, ...]


def collect_note_tag_sources(game_data: GameData, file_pattern: str | None = None) -> list[NoteTagSource]:
    """收集标准 `data/*.json` 中所有对象的 `note` 字段。"""
    raw_sources = collect_native_note_tag_sources(game_data=game_data.data, file_pattern=None)
    sources: list[NoteTagSource] = []
    for index, raw_source in enumerate(raw_sources):
        source = ensure_json_object(raw_source, f"note_sources[{index}]")
        file_name = source.get("file_name")
        note_text = source.get("note_text")
        location_prefix = source.get("location_prefix")
        if not isinstance(file_name, str) or not isinstance(note_text, str) or not isinstance(location_prefix, str):
            raise TypeError(f"note_sources[{index}] 字段类型无效")
        matches: list[NoteTagMatchFact] = []
        for match_index, raw_match in enumerate(ensure_json_array(source["matches"], f"note_sources[{index}].matches")):
            match = ensure_json_object(raw_match, f"note_sources[{index}].matches[{match_index}]")
            tag_name = match.get("tag_name")
            value = match.get("value")
            has_value = match.get("has_value")
            if not isinstance(tag_name, str) or not isinstance(value, str) or not isinstance(has_value, bool):
                raise TypeError(f"note_sources[{index}].matches[{match_index}] 字段类型无效")
            matches.append(
                NoteTagMatchFact(
                    tag_name=tag_name,
                    value=value,
                    has_value=has_value,
                )
            )
        if file_pattern is not None and not note_file_pattern_matches(file_name=file_name, file_pattern=file_pattern):
            continue
        sources.append(
            NoteTagSource(
                file_name=file_name,
                owner_path=tuple(ensure_json_string_list(source["owner_path"], f"note_sources[{index}].owner_path")),
                note_text=note_text,
                location_prefix=location_prefix,
                matches=tuple(matches),
            )
        )
    return sources


def candidate_file_pattern(file_name: str) -> str:
    """返回候选导出使用的文件模式。"""
    if MAP_PATTERN.fullmatch(file_name):
        return MAP_NOTE_FILE_PATTERN
    return file_name


def note_file_pattern_matches(*, file_name: str, file_pattern: str) -> bool:
    """判断规则文件模式是否命中具体 data 文件名。"""
    return fnmatchcase(file_name, file_pattern)


def matched_note_file_names(*, game_data: GameData, file_pattern: str) -> list[str]:
    """返回规则文件模式命中的标准 data 文件名。"""
    return sorted(_iter_data_file_names(game_data=game_data, file_pattern=file_pattern))


def _iter_data_file_names(*, game_data: GameData, file_pattern: str | None) -> list[str]:
    """列出可参与 Note 标签扫描的标准 data JSON 文件。"""
    file_names: list[str] = []
    for file_name, value in game_data.data.items():
        if file_name == PLUGINS_FILE_NAME or not file_name.endswith(".json"):
            continue
        if isinstance(value, str):
            continue
        if file_pattern is not None and not note_file_pattern_matches(file_name=file_name, file_pattern=file_pattern):
            continue
        file_names.append(file_name)
    return file_names


__all__: list[str] = [
    "MAP_NOTE_FILE_PATTERN",
    "NoteTagMatchFact",
    "NoteTagSource",
    "candidate_file_pattern",
    "collect_note_tag_sources",
    "matched_note_file_names",
    "note_file_pattern_matches",
]
