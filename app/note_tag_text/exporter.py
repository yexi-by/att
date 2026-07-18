"""Note 标签候选导出模块。"""

import json
from dataclasses import dataclass
from pathlib import Path

import aiofiles

from app.note_tag_text.sources import NoteTagSource, candidate_file_pattern, collect_note_tag_sources
from app.rmmz.schema import GameData
from app.rmmz.text_protocol import normalize_visible_text_for_extraction
from app.rmmz.text_rules import JsonArray, JsonObject, TextRules, get_default_text_rules


@dataclass(frozen=True, slots=True)
class NoteTagCandidateExport:
    """Note 标签候选导出结果。"""

    candidate_tag_count: int
    candidate_value_count: int
    translatable_value_count: int
    details: JsonObject

    def to_json_payload(self) -> JsonObject:
        """转换成 AgentReport 使用的 JSON 对象。"""
        return {
            "status": "ok",
            "errors": [],
            "warnings": [],
            "summary": {
                "candidate_tag_count": self.candidate_tag_count,
                "candidate_value_count": self.candidate_value_count,
                "translatable_value_count": self.translatable_value_count,
            },
            "details": self.details,
        }


async def export_note_tag_candidates_file(
    *,
    game_data: GameData,
    output_path: Path,
    text_rules: TextRules | None = None,
) -> NoteTagCandidateExport:
    """把标准 data Note 标签候选导出为 AgentReport JSON 文件。"""
    rules = text_rules if text_rules is not None else get_default_text_rules()
    candidates = collect_note_tag_candidates(game_data=game_data, text_rules=rules)
    candidate_count = len(candidates)
    value_count = _candidate_int_sum(candidates, "hit_count")
    translatable_value_count = _candidate_int_sum(candidates, "translatable_hit_count")
    export = NoteTagCandidateExport(
        candidate_tag_count=candidate_count,
        candidate_value_count=value_count,
        translatable_value_count=translatable_value_count,
        details={"candidates": candidates},
    )
    resolved_output_path = output_path.resolve()
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(resolved_output_path, "w", encoding="utf-8") as file:
        _ = await file.write(f"{json.dumps(export.to_json_payload(), ensure_ascii=False, indent=2)}\n")
    return export


def collect_note_tag_candidates(*, game_data: GameData, text_rules: TextRules) -> JsonArray:
    """收集标准 `data/*.json` 的 Note 标签候选摘要。"""
    return collect_note_tag_candidates_from_sources(
        sources=collect_note_tag_sources(game_data=game_data),
        text_rules=text_rules,
    )


def collect_note_tag_candidates_from_sources(
    *,
    sources: list[NoteTagSource] | tuple[NoteTagSource, ...],
    text_rules: TextRules,
) -> JsonArray:
    """基于已建立的 Note 索引生成候选摘要。"""
    stats: dict[tuple[str, str], JsonObject] = {}
    samples_by_key: dict[tuple[str, str], list[str]] = {}
    locations_by_key: dict[tuple[str, str], list[str]] = {}
    files_by_key: dict[tuple[str, str], set[str]] = {}
    for source in sources:
        file_pattern = candidate_file_pattern(source.file_name)
        for match in source.matches:
            key = (file_pattern, match.tag_name)
            stat = stats.setdefault(
                key,
                {
                    "file_name": file_pattern,
                    "tag_name": match.tag_name,
                    "hit_count": 0,
                    "value_hit_count": 0,
                    "translatable_hit_count": 0,
                    "matched_file_count": 0,
                    "sample_locations": [],
                    "sample_values": [],
                },
            )
            files = files_by_key.setdefault(key, set())
            files.add(source.file_name)
            stat["matched_file_count"] = len(files)
            stat["hit_count"] = _json_int(stat["hit_count"]) + 1
            if not match.has_value:
                continue
            stat["value_hit_count"] = _json_int(stat["value_hit_count"]) + 1
            normalized_value = normalize_visible_text_for_extraction(
                match.value,
                plain_text_normalizer=text_rules.normalize_extraction_text,
            )
            if text_rules.should_translate_source_text(normalized_value):
                stat["translatable_hit_count"] = _json_int(stat["translatable_hit_count"]) + 1
            samples = samples_by_key.setdefault(key, [])
            if normalized_value and normalized_value not in samples and len(samples) < 5:
                samples.append(normalized_value)
                stat["sample_values"] = [sample for sample in samples]
            locations = locations_by_key.setdefault(key, [])
            location = f"{source.location_prefix}/note/{match.tag_name}"
            if location not in locations and len(locations) < 5:
                locations.append(location)
                stat["sample_locations"] = [sample_location for sample_location in locations]
    return [stats[key] for key in sorted(stats, key=lambda item: (item[0], item[1]))]


def _json_int(value: object) -> int:
    """把 JSON 数字字段收窄为整数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Note 标签候选统计字段不是整数")
    return value


def _candidate_int_sum(candidates: JsonArray, key: str) -> int:
    """统计候选对象中的整数计数字段。"""
    total = 0
    for candidate_value in candidates:
        if not isinstance(candidate_value, dict):
            continue
        raw_count = candidate_value.get(key)
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            continue
        total += raw_count
    return total


__all__: list[str] = [
    "NoteTagCandidateExport",
    "collect_note_tag_candidates",
    "collect_note_tag_candidates_from_sources",
    "export_note_tag_candidates_file",
]
