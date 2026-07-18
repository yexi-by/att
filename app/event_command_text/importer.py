"""事件指令文本规则导入模块。"""

import json
from pathlib import Path
from typing import ClassVar, cast

import aiofiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.plugin_text.paths import (
    build_json_string_leaf_path_hint,
    expand_rule_to_leaf_paths,
)
from app.rmmz.schema import (
    EventCommandParameterFilter,
    EventCommandTextRuleRecord,
    GameData,
)
from app.rmmz.text_rules import JsonValue, coerce_json_value

from .index import EventCommandAnalysisEntry, build_event_command_analysis_index


class StrictEventCommandRuleModel(BaseModel):
    """事件指令规则严格模型基类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class EventCommandRuleSpec(StrictEventCommandRuleModel):
    """同一类事件指令参数文本规则。"""

    match: dict[str, str] = Field(default_factory=dict)
    paths: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def _validate_paths(cls, value: list[str]) -> list[str]:
        """规则必须包含至少一条路径。"""
        normalized_paths = normalize_path_templates(value)
        if not normalized_paths:
            raise ValueError("paths 不能为空")
        return normalized_paths


type EventCommandRuleImportFile = dict[str, list[EventCommandRuleSpec]]
_EVENT_COMMAND_RULE_IMPORT_ADAPTER: TypeAdapter[EventCommandRuleImportFile] = TypeAdapter(EventCommandRuleImportFile)


async def load_event_command_rule_import_file(input_path: Path) -> EventCommandRuleImportFile:
    """读取外部事件指令规则 JSON 文件。"""
    resolved_path = input_path.resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"事件指令规则导入文件不存在: {resolved_path}")
    async with aiofiles.open(resolved_path, "r", encoding="utf-8") as file:
        raw_text = await file.read()
    return parse_event_command_rule_import_text(raw_text)


def parse_event_command_rule_import_text(raw_text: str) -> EventCommandRuleImportFile:
    """解析外部事件指令规则 JSON 文本。"""
    decoded_raw = cast(object, json.loads(raw_text))
    decoded = coerce_json_value(decoded_raw)
    return _EVENT_COMMAND_RULE_IMPORT_ADAPTER.validate_python(decoded)


def build_event_command_rule_records_from_import(
    *,
    game_data: GameData,
    import_file: EventCommandRuleImportFile,
    command_index: tuple[EventCommandAnalysisEntry, ...] | None = None,
) -> list[EventCommandTextRuleRecord]:
    """把外部事件指令路径映射转换成数据库记录。"""
    resolved_command_index = build_event_command_analysis_index(game_data) if command_index is None else command_index
    records_by_key: dict[tuple[int, tuple[tuple[int, str], ...]], EventCommandTextRuleRecord] = {}
    for command_code_text, specs in import_file.items():
        command_code = parse_command_code(command_code_text)
        for spec in specs:
            filters = parse_parameter_filters(spec.match)
            record = build_event_command_rule_record(
                command_index=resolved_command_index,
                command_code=command_code,
                parameter_filters=filters,
                path_templates=spec.paths,
            )
            key = event_command_rule_key(record)
            existing_record = records_by_key.get(key)
            if existing_record is None:
                records_by_key[key] = record
                continue
            existing_record.path_templates = normalize_path_templates(
                [*existing_record.path_templates, *record.path_templates]
            )
    return list(records_by_key.values())


def build_event_command_rule_record(
    *,
    command_index: tuple[EventCommandAnalysisEntry, ...],
    command_code: int,
    parameter_filters: list[EventCommandParameterFilter],
    path_templates: list[str],
) -> EventCommandTextRuleRecord:
    """校验单组事件指令规则并构造数据库记录。"""
    matched_entries = [
        entry
        for entry in command_index
        if entry.command.code == command_code
        and command_matches_filters(
            parameters=entry.command.parameters,
            filters=parameter_filters,
        )
    ]
    if not matched_entries:
        raise ValueError(f"事件指令规则没有命中当前游戏指令: {command_code}")

    accepted_paths: list[str] = []
    for path_template in normalize_path_templates(path_templates):
        matched_path_found = False
        hint: str | None = None
        for entry in matched_entries:
            resolved_leaves = entry.resolved_leaves
            if expand_rule_to_leaf_paths(
                path_template=path_template,
                resolved_leaves=resolved_leaves,
            ):
                matched_path_found = True
                break
            if hint is None:
                hint = build_json_string_leaf_path_hint(
                    path_template=path_template,
                    resolved_leaves=resolved_leaves,
                )
        if not matched_path_found:
            hint_suffix = "" if hint is None else f"。{hint}"
            raise ValueError(f"事件指令 {command_code} 的路径没有命中字符串叶子: {path_template}{hint_suffix}")
        accepted_paths.append(path_template)

    return EventCommandTextRuleRecord(
        command_code=command_code,
        parameter_filters=parameter_filters,
        path_templates=accepted_paths,
    )


def parse_command_code(value: str) -> int:
    """读取事件指令编码。"""
    normalized_value = value.strip()
    if not normalized_value.isdecimal():
        raise ValueError(f"事件指令编码必须是非负整数: {value}")
    return int(normalized_value)


def parse_parameter_filters(match: dict[str, str]) -> list[EventCommandParameterFilter]:
    """把外部 match 对象转换成参数过滤条件。"""
    filters: list[EventCommandParameterFilter] = []
    for index_text, expected_value in sorted(match.items(), key=lambda item: int(item[0])):
        if not index_text.isdecimal():
            raise ValueError(f"match 的键必须是参数索引: {index_text}")
        filters.append(EventCommandParameterFilter(index=int(index_text), value=expected_value))
    return filters


def normalize_path_templates(path_templates: list[str]) -> list[str]:
    """清理并去重路径模板。"""
    normalized_paths: list[str] = []
    seen_paths: set[str] = set()
    for path_template in path_templates:
        normalized_path = path_template.strip()
        if not normalized_path or normalized_path in seen_paths:
            continue
        normalized_paths.append(normalized_path)
        seen_paths.add(normalized_path)
    return normalized_paths


def event_command_rule_key(record: EventCommandTextRuleRecord) -> tuple[int, tuple[tuple[int, str], ...]]:
    """生成事件指令规则的稳定键。"""
    filters = tuple((item.index, item.value) for item in record.parameter_filters)
    return (record.command_code, filters)


def command_matches_filters(
    *,
    parameters: list[JsonValue],
    filters: list[EventCommandParameterFilter],
) -> bool:
    """判断事件指令参数是否满足过滤条件。"""
    for parameter_filter in filters:
        if parameter_filter.index >= len(parameters):
            return False
        value = parameters[parameter_filter.index]
        if not isinstance(value, str):
            return False
        if value != parameter_filter.value:
            return False
    return True


__all__: list[str] = [
    "EventCommandRuleImportFile",
    "EventCommandRuleSpec",
    "build_event_command_rule_records_from_import",
    "command_matches_filters",
    "event_command_rule_key",
    "load_event_command_rule_import_file",
    "parse_event_command_rule_import_text",
]
