"""Note 标签文本规则导入与校验模块。"""

import json
from pathlib import Path
from typing import cast

import aiofiles
from pydantic import TypeAdapter

from app.note_tag_text.sources import (
    NoteTagSource,
    collect_note_tag_sources,
    matched_note_file_names,
    note_file_pattern_matches,
)
from app.rmmz.schema import GameData, NoteTagTextRuleRecord
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import TextRules, coerce_json_value, get_default_text_rules

type NoteTagRuleImportFile = dict[str, list[str]]
_NOTE_TAG_RULE_IMPORT_ADAPTER: TypeAdapter[NoteTagRuleImportFile] = TypeAdapter(NoteTagRuleImportFile)

MACHINE_NOTE_TAG_NAMES: frozenset[str] = frozenset(
    {
        "upgrade",
        "chainskill",
        "equipstate",
        "passivestate",
        "skillid",
        "itemid",
        "weaponid",
        "armorid",
        "stateid",
        "switch",
        "variable",
        "eval",
        "script",
        "formula",
    }
)


async def load_note_tag_rule_import_file(input_path: Path) -> NoteTagRuleImportFile:
    """读取外部 Note 标签规则 JSON 文件。"""
    resolved_path = input_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Note 标签规则导入文件不存在: {resolved_path}")
    async with aiofiles.open(resolved_path, "r", encoding="utf-8") as file:
        raw_text = await file.read()
    return parse_note_tag_rule_import_text(raw_text)


def parse_note_tag_rule_import_text(raw_text: str) -> NoteTagRuleImportFile:
    """解析外部 Note 标签规则 JSON 文本。"""
    decoded_raw = cast(object, json.loads(raw_text))
    decoded = coerce_json_value(decoded_raw)
    return _NOTE_TAG_RULE_IMPORT_ADAPTER.validate_python(decoded)


def build_note_tag_rule_records_from_import(
    *,
    game_data: GameData,
    import_file: NoteTagRuleImportFile,
    text_rules: TextRules | None = None,
    note_sources: tuple[NoteTagSource, ...] | None = None,
) -> list[NoteTagTextRuleRecord]:
    """把外部 Note 标签映射转换成数据库规则记录。"""
    rules = text_rules if text_rules is not None else get_default_text_rules()
    resolved_note_sources = (
        tuple(collect_note_tag_sources(game_data=game_data)) if note_sources is None else note_sources
    )
    records: list[NoteTagTextRuleRecord] = []
    for file_name, tag_names in import_file.items():
        normalized_file_name = file_name.strip()
        if not normalized_file_name:
            raise ValueError("Note 标签规则不能包含空文件名")
        if not normalized_file_name.endswith(".json"):
            raise ValueError(f"Note 标签规则文件模式必须指向 data JSON 文件: {normalized_file_name}")
        if not matched_note_file_names(game_data=game_data, file_pattern=normalized_file_name):
            raise ValueError(f"Note 标签规则文件模式没有匹配当前 data 文件: {normalized_file_name}")
        normalized_tag_names = normalize_tag_names(tag_names)
        if not normalized_tag_names:
            raise ValueError(f"Note 标签规则不能为空: {normalized_file_name}")
        for tag_name in normalized_tag_names:
            _validate_note_tag_rule_hit(
                note_sources=resolved_note_sources,
                file_name=normalized_file_name,
                tag_name=tag_name,
                text_rules=rules,
            )
        records.append(
            NoteTagTextRuleRecord(
                file_name=normalized_file_name,
                tag_names=normalized_tag_names,
            )
        )
    return records


def normalize_tag_names(tag_names: list[str]) -> list[str]:
    """清理并去重 Note 标签名。"""
    normalized_tag_names: list[str] = []
    seen_tags: set[str] = set()
    for tag_name in tag_names:
        normalized_tag_name = tag_name.strip()
        if not normalized_tag_name or normalized_tag_name in seen_tags:
            continue
        if "/" in normalized_tag_name:
            raise ValueError(f"Note 标签名不能包含定位路径分隔符 `/`: {normalized_tag_name}")
        if normalized_tag_name.casefold() in MACHINE_NOTE_TAG_NAMES:
            raise ValueError(f"Note 标签属于机器协议，不能作为玩家可见文本导入: {normalized_tag_name}")
        normalized_tag_names.append(normalized_tag_name)
        seen_tags.add(normalized_tag_name)
    return normalized_tag_names


def _validate_note_tag_rule_hit(
    *,
    note_sources: tuple[NoteTagSource, ...],
    file_name: str,
    tag_name: str,
    text_rules: TextRules,
) -> None:
    """校验单个 Note 标签规则至少命中一条可翻译值。"""
    hit_count = 0
    translatable_hit_count = 0
    for source in note_sources:
        if not note_file_pattern_matches(file_name=source.file_name, file_pattern=file_name):
            continue
        matches = [match for match in source.matches if match.tag_name == tag_name and match.has_value]
        if len(matches) > 1:
            raise ValueError(f"{source.location_prefix}/note/{tag_name} 标签重复，无法生成唯一定位路径")
        if not matches:
            continue
        hit_count += 1
        normalized_value = normalize_visible_text_for_extraction(
            matches[0].value,
            plain_text_normalizer=text_rules.normalize_extraction_text,
        )
        if not normalized_value:
            continue
        if text_rules.should_translate_source_text(normalized_value):
            translatable_hit_count += 1

    if hit_count == 0:
        raise ValueError(f"Note 标签规则没有命中当前游戏 Note 标签: {file_name}/{tag_name}")

    if translatable_hit_count == 0:
        raise ValueError(f"Note 标签规则没有命中玩家可见可翻译文本: {file_name}/{tag_name}")


__all__: list[str] = [
    "NoteTagRuleImportFile",
    "build_note_tag_rule_records_from_import",
    "load_note_tag_rule_import_file",
    "parse_note_tag_rule_import_text",
]
