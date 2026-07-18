"""源文残留例外规则解析与校验。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

import aiofiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.rmmz.schema import SourceResidualRuleRecord, TranslationItem
from app.rmmz.text_rules import TextRules, coerce_json_value


class StrictSourceResidualRuleModel(BaseModel):
    """源文残留例外规则严格模型基类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class PositionSourceResidualRuleSpec(StrictSourceResidualRuleModel):
    """单个文本位置允许保留的源文片段。"""

    allowed_terms: list[str] = Field(default_factory=list)
    reason: str

    @field_validator("allowed_terms")
    @classmethod
    def _validate_allowed_terms(cls, value: list[str]) -> list[str]:
        """清理并校验允许保留的源文片段。"""
        return _normalize_allowed_terms(value)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        """校验例外原因必须显式填写。"""
        return _normalize_reason(value)


class StructuralSourceResidualRuleSpec(StrictSourceResidualRuleModel):
    """结构性协议词保留规则。"""

    pattern: str
    allowed_terms: list[str] = Field(default_factory=list)
    check_group: str
    reason: str

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        """校验结构性正则必须非空且可编译。"""
        normalized_pattern = value.strip()
        if not normalized_pattern:
            raise ValueError("pattern 不能为空")
        try:
            _ = re.compile(normalized_pattern)
        except re.error as error:
            raise ValueError(f"pattern 不是合法正则: {normalized_pattern}") from error
        return normalized_pattern

    @field_validator("allowed_terms")
    @classmethod
    def _validate_allowed_terms(cls, value: list[str]) -> list[str]:
        """清理并校验允许保留的源文片段。"""
        return _normalize_allowed_terms(value)

    @field_validator("check_group")
    @classmethod
    def _validate_check_group(cls, value: str) -> str:
        """校验显示文本分组必须非空。"""
        normalized_group = value.strip()
        if not normalized_group:
            raise ValueError("check_group 不能为空")
        return normalized_group

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        """校验例外原因必须显式填写。"""
        return _normalize_reason(value)


class SourceResidualRuleImportFile(StrictSourceResidualRuleModel):
    """源文残留例外规则导入文件。"""

    position_rules: dict[str, PositionSourceResidualRuleSpec] = Field(default_factory=dict)
    structural_rules: list[StructuralSourceResidualRuleSpec] = Field(default_factory=list)


_SOURCE_RESIDUAL_RULE_IMPORT_ADAPTER: TypeAdapter[SourceResidualRuleImportFile] = TypeAdapter(
    SourceResidualRuleImportFile
)


@dataclass(frozen=True, slots=True)
class SourceResidualRuleSet:
    """按定位路径索引的源文残留例外规则集合。"""

    records_by_path: dict[str, SourceResidualRuleRecord]
    structural_records: tuple[SourceResidualRuleRecord, ...] = ()

    @classmethod
    def from_records(cls, records: Sequence[SourceResidualRuleRecord]) -> "SourceResidualRuleSet":
        """从数据库记录构建路径索引和结构性规则集合。"""
        position_records: list[SourceResidualRuleRecord] = []
        structural_records: list[SourceResidualRuleRecord] = []
        for record in records:
            if record.rule_type == "position":
                position_records.append(_validate_position_record(record))
                continue
            if record.rule_type == "structural":
                structural_records.append(_validate_structural_record(record))
                continue
            raise ValueError(f"源文残留例外规则类型无效: {record.rule_id}: {record.rule_type}")
        return cls(
            records_by_path={record.location_path: record for record in position_records},
            structural_records=tuple(structural_records),
        )

    def allowed_terms_for_path(self, location_path: str) -> list[str]:
        """读取指定路径允许保留的源文片段。"""
        record = self.records_by_path.get(location_path)
        if record is None:
            return []
        return list(record.allowed_terms)

    def reason_for_path(self, location_path: str) -> str:
        """读取指定路径的例外原因。"""
        record = self.records_by_path.get(location_path)
        if record is None:
            return ""
        return record.reason


async def load_source_residual_rule_import_file(input_path: Path) -> SourceResidualRuleImportFile:
    """读取外部源文残留例外规则 JSON 文件。"""
    resolved_path = input_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"源文残留例外规则文件不存在: {resolved_path}")
    async with aiofiles.open(resolved_path, "r", encoding="utf-8-sig") as file:
        raw_text = await file.read()
    return parse_source_residual_rule_import_text(raw_text)


def parse_source_residual_rule_import_text(raw_text: str) -> SourceResidualRuleImportFile:
    """解析外部源文残留例外规则 JSON 文本。"""
    # JSON 解析边界只能先得到动态对象，下一行立即交给 coerce_json_value 收窄。
    decoded_raw = cast(object, json.loads(raw_text))
    decoded = coerce_json_value(decoded_raw)
    return _SOURCE_RESIDUAL_RULE_IMPORT_ADAPTER.validate_python(decoded)


def build_source_residual_rule_records_from_import(
    *,
    import_file: SourceResidualRuleImportFile,
    active_items: Sequence[TranslationItem],
    translated_items: Sequence[TranslationItem],
    ignore_case: bool = False,
) -> list[SourceResidualRuleRecord]:
    """把外部例外规则转换成数据库记录，并校验定位和结构。"""
    records: list[SourceResidualRuleRecord] = []
    records.extend(
        _build_position_records(
            import_file=import_file,
            active_items=active_items,
            translated_items=translated_items,
            ignore_case=ignore_case,
        )
    )
    records.extend(_build_structural_records(import_file=import_file))
    return records


def check_source_residual_for_item(
    *,
    item: TranslationItem,
    text_rules: TextRules,
    rule_set: SourceResidualRuleSet | None,
) -> None:
    """按逐位置例外和结构性协议词例外检查单条译文源文残留。"""
    if rule_set is None:
        text_rules.check_source_residual(item.translation_lines)
        return
    masked_lines = text_rules.mask_source_residual_terms(
        item.translation_lines,
        rule_set.allowed_terms_for_path(item.location_path),
    )
    masked_lines = _mask_structural_source_residual_terms(
        lines=masked_lines,
        records=rule_set.structural_records,
        ignore_case=text_rules.setting.source_residual_terms_ignore_case,
    )
    text_rules.check_source_residual(masked_lines)


def _build_position_records(
    *,
    import_file: SourceResidualRuleImportFile,
    active_items: Sequence[TranslationItem],
    translated_items: Sequence[TranslationItem],
    ignore_case: bool,
) -> list[SourceResidualRuleRecord]:
    """构建逐位置源文保留规则记录。"""
    active_items_by_path = {item.location_path: item for item in active_items}
    translated_items_by_path = {item.location_path: item for item in translated_items}
    records: list[SourceResidualRuleRecord] = []
    for location_path, spec in import_file.position_rules.items():
        normalized_path = location_path.strip()
        if not normalized_path:
            raise ValueError("position_rules 不能包含空定位路径")
        active_item = active_items_by_path.get(normalized_path)
        if active_item is None:
            raise ValueError(f"源文残留例外规则定位不在当前可提取文本范围内: {location_path}")
        _validate_allowed_terms_appear_in_item(
            location_path=normalized_path,
            allowed_terms=spec.allowed_terms,
            active_item=active_item,
            translated_item=translated_items_by_path.get(normalized_path),
            ignore_case=ignore_case,
        )
        records.append(
            SourceResidualRuleRecord(
                rule_id=f"position:{normalized_path}",
                rule_type="position",
                location_path=normalized_path,
                allowed_terms=list(spec.allowed_terms),
                reason=spec.reason,
            )
        )
    return records


def _build_structural_records(
    *,
    import_file: SourceResidualRuleImportFile,
) -> list[SourceResidualRuleRecord]:
    """构建结构性协议词规则记录。"""
    records: list[SourceResidualRuleRecord] = []
    for index, spec in enumerate(import_file.structural_rules):
        pattern = re.compile(spec.pattern)
        if spec.check_group not in pattern.groupindex:
            raise ValueError(f"结构性源文保留规则缺少命名分组: {spec.check_group}")
        records.append(
            SourceResidualRuleRecord(
                rule_id=f"structural:{index}",
                rule_type="structural",
                pattern_text=spec.pattern,
                allowed_terms=list(spec.allowed_terms),
                check_group=spec.check_group,
                reason=spec.reason,
            )
        )
    return records


def _mask_structural_source_residual_terms(
    *,
    lines: Sequence[str],
    records: Sequence[SourceResidualRuleRecord],
    ignore_case: bool,
) -> list[str]:
    """只遮蔽结构性规则中显示文本分组外侧的协议词。"""
    if not records:
        return list(lines)
    compiled_records = _compile_structural_records(records)
    if not compiled_records:
        return list(lines)
    return [
        _mask_structural_source_residual_terms_in_line(
            line=line,
            compiled_records=compiled_records,
            ignore_case=ignore_case,
        )
        for line in lines
    ]


def _compile_structural_records(
    records: Sequence[SourceResidualRuleRecord],
) -> list[tuple[re.Pattern[str], SourceResidualRuleRecord]]:
    """编译结构性规则，运行时发现损坏规则必须立刻报错。"""
    compiled_records: list[tuple[re.Pattern[str], SourceResidualRuleRecord]] = []
    for record in records:
        try:
            pattern = re.compile(record.pattern_text)
        except re.error as error:
            raise ValueError(f"结构性源文保留规则正则损坏: {record.pattern_text}") from error
        if not record.check_group or record.check_group not in pattern.groupindex:
            raise ValueError(f"结构性源文保留规则缺少命名分组: {record.check_group}")
        compiled_records.append((pattern, record))
    return compiled_records


def _mask_structural_source_residual_terms_in_line(
    *,
    line: str,
    compiled_records: Sequence[tuple[re.Pattern[str], SourceResidualRuleRecord]],
    ignore_case: bool,
) -> str:
    """遮蔽单行中结构性协议词，显示文本分组原样保留给残留检查。"""
    masked_line = line
    for pattern, record in compiled_records:
        masked_line = _mask_one_structural_rule_in_line(
            line=masked_line,
            pattern=pattern,
            record=record,
            ignore_case=ignore_case,
        )
    return masked_line


def _mask_one_structural_rule_in_line(
    *,
    line: str,
    pattern: re.Pattern[str],
    record: SourceResidualRuleRecord,
    ignore_case: bool,
) -> str:
    """应用单条结构性规则，只处理命名显示分组之外的 allowed_terms。"""
    mask_ranges: list[tuple[int, int]] = []
    for match in pattern.finditer(line):
        try:
            group_start, group_end = match.span(record.check_group)
        except IndexError:
            continue
        if group_start < 0 or group_end < 0:
            continue
        if not line[group_start:group_end].strip():
            continue
        outside_ranges = [
            (match.start(), group_start),
            (group_end, match.end()),
        ]
        for term in record.allowed_terms:
            mask_ranges.extend(
                _find_term_ranges_outside_group(
                    line=line,
                    term=term,
                    outside_ranges=outside_ranges,
                    ignore_case=ignore_case,
                )
            )
    if not mask_ranges:
        return line
    return _replace_ranges_with_spaces(line=line, ranges=mask_ranges)


def _find_term_ranges_outside_group(
    *,
    line: str,
    term: str,
    outside_ranges: Sequence[tuple[int, int]],
    ignore_case: bool,
) -> list[tuple[int, int]]:
    """查找显示分组外侧的协议词位置。"""
    if not term:
        return []
    ranges: list[tuple[int, int]] = []
    flags = re.IGNORECASE if ignore_case else 0
    pattern = re.compile(re.escape(term), flags=flags)
    for start, end in outside_ranges:
        segment = line[start:end]
        for match in pattern.finditer(segment):
            ranges.append((start + match.start(), start + match.end()))
    return ranges


def _validate_structural_record(record: SourceResidualRuleRecord) -> SourceResidualRuleRecord:
    """校验数据库里的结构性源文保留规则仍可执行。"""
    if not record.pattern_text:
        raise ValueError("结构性源文保留规则缺少 pattern_text")
    if not record.check_group:
        raise ValueError("结构性源文保留规则缺少 check_group")
    try:
        pattern = re.compile(record.pattern_text)
    except re.error as error:
        raise ValueError(f"结构性源文保留规则正则损坏: {record.pattern_text}") from error
    if record.check_group not in pattern.groupindex:
        raise ValueError(f"结构性源文保留规则缺少命名分组: {record.check_group}")
    return record


def _validate_position_record(record: SourceResidualRuleRecord) -> SourceResidualRuleRecord:
    """校验数据库里的位置源文保留规则仍可执行。"""
    if not record.location_path:
        raise ValueError(f"位置源文保留规则缺少内部位置: {record.rule_id}")
    if not record.allowed_terms:
        raise ValueError(f"位置源文保留规则缺少允许保留的源文片段: {record.rule_id}")
    return record


def _replace_ranges_with_spaces(
    *,
    line: str,
    ranges: Sequence[tuple[int, int]],
) -> str:
    """用空格替换指定字符区间，保持其他文本不变。"""
    if not ranges:
        return line
    characters = list(line)
    for start, end in ranges:
        for index in range(max(start, 0), min(end, len(characters))):
            characters[index] = " "
    return "".join(characters)


def _validate_allowed_terms_appear_in_item(
    *,
    location_path: str,
    allowed_terms: list[str],
    active_item: TranslationItem,
    translated_item: TranslationItem | None,
    ignore_case: bool,
) -> None:
    """确认逐位置例外片段来自当前条目的原文或已保存译文。"""
    visible_text_parts = [*active_item.original_lines]
    if translated_item is not None:
        visible_text_parts.extend(translated_item.translation_lines)
    visible_text = "\n".join(visible_text_parts)
    if ignore_case:
        visible_text_for_check = visible_text.casefold()
        missing_terms = [term for term in allowed_terms if term.casefold() not in visible_text_for_check]
    else:
        missing_terms = [term for term in allowed_terms if term not in visible_text]
    if missing_terms:
        joined_terms = "、".join(missing_terms)
        raise ValueError(f"{location_path} 的 allowed_terms 未出现在当前条目原文或译文中: {joined_terms}")


def _normalize_allowed_terms(value: list[str]) -> list[str]:
    """清理并去重允许保留的源文片段。"""
    normalized_terms = _deduplicate_terms(value)
    if not normalized_terms:
        raise ValueError("allowed_terms 不能为空")
    return normalized_terms


def _deduplicate_terms(value: Sequence[str]) -> list[str]:
    """清理并去重源文片段，不要求结果非空。"""
    normalized_terms: list[str] = []
    seen_terms: set[str] = set()
    for term in value:
        normalized_term = term.strip()
        if not normalized_term or normalized_term in seen_terms:
            continue
        normalized_terms.append(normalized_term)
        seen_terms.add(normalized_term)
    return normalized_terms


def _normalize_reason(value: str) -> str:
    """清理并校验例外原因。"""
    normalized_reason = value.strip()
    if not normalized_reason:
        raise ValueError("reason 不能为空")
    return normalized_reason


__all__: list[str] = [
    "PositionSourceResidualRuleSpec",
    "SourceResidualRuleImportFile",
    "SourceResidualRuleSet",
    "StructuralSourceResidualRuleSpec",
    "build_source_residual_rule_records_from_import",
    "check_source_residual_for_item",
    "load_source_residual_rule_import_file",
    "parse_source_residual_rule_import_text",
]
