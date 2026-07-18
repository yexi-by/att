"""术语表工程临时文件读写模块。"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import aiofiles

from app.rmmz.json_types import coerce_json_value, ensure_json_object
from app.rmmz.schema import GameData, MvVirtualNameboxRuleRecord
from app.rmmz.text_rules import TextRules

from .extraction import SPEAKER_SAMPLE_DIRECTORY_NAME, TerminologyExtraction, build_speaker_sample_file_name
from .schemas import (
    TERMINOLOGY_CATEGORIES,
    DatabaseTermContext,
    SpeakerDialogueContext,
    TerminologyGlossary,
    TerminologyRegistry,
)

FIELD_TERMS_FILE_NAME = "field-terms.json"
GLOSSARY_FILE_NAME = "glossary.json"
CONTEXT_DIRECTORY_NAME = "contexts"
DATABASE_CONTEXT_FILE_NAME = "database_terms.json"
GLOSSARY_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"terms"})


@dataclass(frozen=True, slots=True)
class TerminologyExportSummary:
    """术语表工程导出结果摘要。"""

    field_terms_path: Path
    glossary_path: Path
    contexts_dir: Path
    speaker_context_dir: Path
    database_context_path: Path
    entry_count: int
    speaker_entry_count: int
    map_entry_count: int
    database_entry_count: int
    sample_file_count: int


async def export_terminology_artifacts(
    *,
    game_data: GameData,
    output_dir: Path,
    mv_virtual_namebox_rule_records: list[MvVirtualNameboxRuleRecord] | None = None,
    text_rules: TextRules | None = None,
) -> TerminologyExportSummary:
    """导出字段译名表、正文术语表和外部 Agent 可读取的只读上下文。"""
    target_dir = output_dir.resolve()
    contexts_dir = target_dir / CONTEXT_DIRECTORY_NAME
    sample_dir = contexts_dir / SPEAKER_SAMPLE_DIRECTORY_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    contexts_dir.mkdir(parents=True, exist_ok=True)
    sample_dir.mkdir(parents=True, exist_ok=True)

    registry, speaker_contexts, database_contexts = TerminologyExtraction(
        game_data=game_data,
        mv_virtual_namebox_rule_records=mv_virtual_namebox_rule_records,
        text_rules=text_rules,
    ).extract_registry_and_contexts()
    field_terms_path = target_dir / FIELD_TERMS_FILE_NAME
    glossary_path = target_dir / GLOSSARY_FILE_NAME
    await write_field_terms_json(field_terms_path, registry)
    await write_glossary_json(glossary_path, TerminologyGlossary())
    sample_file_names: dict[str, str] = {}
    for context in speaker_contexts:
        sample_file_name = reserve_speaker_sample_file_name(
            preferred_file_name=build_speaker_sample_file_name(context.name),
            speaker_name=context.name,
            used_file_names=sample_file_names,
        )
        await write_speaker_context_json(sample_dir / sample_file_name, context)

    database_context_path = contexts_dir / DATABASE_CONTEXT_FILE_NAME
    await write_database_contexts_json(database_context_path, database_contexts)

    return TerminologyExportSummary(
        field_terms_path=field_terms_path,
        glossary_path=glossary_path,
        contexts_dir=contexts_dir,
        speaker_context_dir=sample_dir,
        database_context_path=database_context_path,
        entry_count=registry.total_entry_count(),
        speaker_entry_count=len(registry.speaker_names),
        map_entry_count=len(registry.map_display_names),
        database_entry_count=registry.total_entry_count()
        - len(registry.speaker_names)
        - len(registry.map_display_names),
        sample_file_count=len(speaker_contexts),
    )


async def load_terminology_registry(*, field_terms_path: Path) -> TerminologyRegistry:
    """读取外部 Agent 填写后的字段译名表。"""
    resolved_path = field_terms_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"术语表导入文件不存在: {resolved_path}")

    async with aiofiles.open(resolved_path, "r", encoding="utf-8") as file:
        raw_text = await file.read()
    decoded_value = cast(object, json.loads(raw_text))
    raw_value = coerce_json_value(decoded_value)
    raw_object = ensure_json_object(raw_value, str(resolved_path))
    validate_terms_json_category_keys(raw_object, resolved_path)
    return TerminologyRegistry.model_validate(raw_object)


async def load_terminology_glossary(*, glossary_path: Path) -> TerminologyGlossary:
    """读取外部 Agent 填写后的正文术语表。"""
    resolved_path = glossary_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"正文术语表导入文件不存在: {resolved_path}")

    async with aiofiles.open(resolved_path, "r", encoding="utf-8") as file:
        raw_text = await file.read()
    decoded_value = cast(object, json.loads(raw_text))
    raw_value = coerce_json_value(decoded_value)
    raw_object = ensure_json_object(raw_value, str(resolved_path))
    validate_glossary_json_keys(raw_object, resolved_path)
    return TerminologyGlossary.model_validate(raw_object)


async def write_field_terms_json(path: Path, registry: TerminologyRegistry) -> None:
    """写入字段译名表 JSON。"""
    payload = json.dumps(registry.model_dump(mode="json"), ensure_ascii=False, indent=2)
    async with aiofiles.open(path, "w", encoding="utf-8") as file:
        _ = await file.write(f"{payload}\n")


async def write_glossary_json(path: Path, glossary: TerminologyGlossary) -> None:
    """写入正文术语表 JSON。"""
    payload = json.dumps(glossary.model_dump(mode="json"), ensure_ascii=False, indent=2)
    async with aiofiles.open(path, "w", encoding="utf-8") as file:
        _ = await file.write(f"{payload}\n")


async def write_speaker_context_json(path: Path, context: SpeakerDialogueContext) -> None:
    """写入单个名字的对白样本 JSON。"""
    payload = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, indent=2)
    async with aiofiles.open(path, "w", encoding="utf-8") as file:
        _ = await file.write(f"{payload}\n")


async def write_database_contexts_json(path: Path, contexts: list[DatabaseTermContext]) -> None:
    """写入数据库术语上下文 JSON。"""
    payload = json.dumps([context.model_dump(mode="json") for context in contexts], ensure_ascii=False, indent=2)
    async with aiofiles.open(path, "w", encoding="utf-8") as file:
        _ = await file.write(f"{payload}\n")


def sanitize_game_title_for_path(game_title: str) -> str:
    """把游戏标题转换为稳定且适合目录名的片段。"""
    normalized = re.sub(r"[<>:\"/\\|?*\s]+", "_", game_title.strip()).strip("._")
    if normalized:
        return normalized
    return "untitled_game"


def reserve_speaker_sample_file_name(
    *,
    preferred_file_name: str,
    speaker_name: str,
    used_file_names: dict[str, str],
) -> str:
    """为对白样本保留跨平台唯一文件名，避免大小写不敏感文件系统覆盖。"""
    stem = Path(preferred_file_name).stem
    suffix = Path(preferred_file_name).suffix
    candidate = preferred_file_name
    index = 2
    while True:
        key = candidate.casefold()
        previous_speaker_name = used_file_names.get(key)
        if previous_speaker_name is None:
            used_file_names[key] = speaker_name
            return candidate
        if previous_speaker_name == speaker_name:
            return candidate
        candidate = f"{stem}__{index}{suffix}"
        index += 1


def validate_terms_json_category_keys(payload: Mapping[str, object], path: Path) -> None:
    """校验外部字段译名表文件的顶层类别必须完整且不能新增。"""
    expected_categories = set(TERMINOLOGY_CATEGORIES)
    actual_categories = set(payload)
    missing_categories = sorted(expected_categories - actual_categories)
    extra_categories = sorted(actual_categories - expected_categories)
    errors: list[str] = []
    if missing_categories:
        errors.append(f"缺少类别: {', '.join(missing_categories)}")
    if extra_categories:
        errors.append(f"未知类别: {', '.join(extra_categories)}")
    if errors:
        raise ValueError(f"术语表类别不完整: {path}: {'; '.join(errors)}")


def validate_glossary_json_keys(payload: Mapping[str, object], path: Path) -> None:
    """校验正文术语表文件必须只包含固定顶层字段。"""
    actual_keys = set(payload)
    missing_keys = sorted(GLOSSARY_TOP_LEVEL_KEYS - actual_keys)
    extra_keys = sorted(actual_keys - GLOSSARY_TOP_LEVEL_KEYS)
    errors: list[str] = []
    if missing_keys:
        errors.append(f"缺少字段: {', '.join(missing_keys)}")
    if extra_keys:
        errors.append(f"未知字段: {', '.join(extra_keys)}")
    if errors:
        raise ValueError(f"正文术语表结构不完整: {path}: {'; '.join(errors)}")


__all__: list[str] = [
    "CONTEXT_DIRECTORY_NAME",
    "DATABASE_CONTEXT_FILE_NAME",
    "FIELD_TERMS_FILE_NAME",
    "GLOSSARY_FILE_NAME",
    "GLOSSARY_TOP_LEVEL_KEYS",
    "TerminologyExportSummary",
    "export_terminology_artifacts",
    "load_terminology_glossary",
    "load_terminology_registry",
    "sanitize_game_title_for_path",
    "validate_glossary_json_keys",
    "validate_terms_json_category_keys",
    "write_database_contexts_json",
    "write_field_terms_json",
    "write_glossary_json",
    "write_speaker_context_json",
]
