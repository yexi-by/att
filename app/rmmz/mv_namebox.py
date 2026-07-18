"""MV 虚拟名字框外部规则。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from string import Formatter
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.rmmz.commands import iter_all_commands
from app.rmmz.game_data import EventCommand
from app.rmmz.json_types import JsonArray, JsonObject, coerce_json_value
from app.rmmz.schema import (
    Code,
    GameData,
    MvVirtualNameboxRuleRecord,
    MvVirtualNameboxSpeakerPolicy,
)

ACTOR_NAME_CONTROL_PATTERN: re.Pattern[str] = re.compile(r"^\\[Nn]\[(?P<actor_id>\d+)\]$")
TEMPLATE_FIELD_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z_]\w*$")
MV_VIRTUAL_NAMEBOX_RULES_FILE_NAME = "mv-virtual-namebox-rules.json"
MV_VIRTUAL_NAMEBOX_CANDIDATES_FILE_NAME = "mv-virtual-namebox-candidates.json"


@dataclass(frozen=True, slots=True)
class MvVirtualNameboxCandidate:
    """MV `101` 后首条非空 `401` 候选。"""

    location_path: str
    text: str
    following_lines: list[str]


@dataclass(frozen=True, slots=True)
class MvVirtualNameboxRule:
    """已编译的 MV 虚拟名字框规则。"""

    rule_order: int
    rule_name: str
    pattern_text: str
    pattern: re.Pattern[str]
    speaker_group: str
    body_group: str
    speaker_policy: MvVirtualNameboxSpeakerPolicy
    render_template: str

    @classmethod
    def from_record(cls, record: MvVirtualNameboxRuleRecord) -> "MvVirtualNameboxRule":
        """从数据库记录创建运行时规则。"""
        compiled_pattern = re.compile(record.pattern_text)
        validate_compiled_rule(
            rule_name=record.rule_name,
            pattern=compiled_pattern,
            speaker_group=record.speaker_group,
            body_group=record.body_group,
            render_template=record.render_template,
        )
        return cls(
            rule_order=record.rule_order,
            rule_name=record.rule_name,
            pattern_text=record.pattern_text,
            pattern=compiled_pattern,
            speaker_group=record.speaker_group,
            body_group=record.body_group,
            speaker_policy=record.speaker_policy,
            render_template=record.render_template,
        )


@dataclass(frozen=True, slots=True)
class MvVirtualSpeaker:
    """MV 文本首行按外部规则抽象出的虚拟名字框。"""

    speaker: str
    body_text: str
    matched_text: str
    rule_name: str
    speaker_policy: MvVirtualNameboxSpeakerPolicy
    source_speaker_text: str
    render_template: str
    group_values: dict[str, str]
    speaker_group: str
    body_group: str

    @property
    def requires_translation(self) -> bool:
        """判断说话人是否需要从字段译名表读取译名。"""
        return self.speaker_policy in {"translate", "actor_name"}

    def render(self, *, translated_speaker: str, translated_body: str | None = None) -> str:
        """按外部规则模板重建写回到 `401` 的文本。"""
        body_text = "" if translated_body is None else translated_body
        values = dict(self.group_values)
        values[self.speaker_group] = translated_speaker
        if self.body_group:
            values[self.body_group] = body_text
        values["speaker"] = translated_speaker
        values["body"] = body_text
        return self.render_template.format_map(values)

    def render_source(self) -> str:
        """按原始捕获组重建源文本，用于规则校验。"""
        body_text = self.group_values.get(self.body_group, "") if self.body_group else ""
        values = dict(self.group_values)
        values[self.speaker_group] = self.source_speaker_text
        if self.body_group:
            values[self.body_group] = body_text
        values["speaker"] = self.source_speaker_text
        values["body"] = body_text
        return self.render_template.format_map(values)


class StrictMvNameboxRuleModel(BaseModel):
    """MV 虚拟名字框规则严格模型基类。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)


class MvVirtualNameboxRuleSpec(StrictMvNameboxRuleModel):
    """单条 MV 虚拟名字框外部规则。"""

    name: str
    pattern: str
    speaker_group: str
    speaker_policy: MvVirtualNameboxSpeakerPolicy
    render_template: str
    body_group: str = ""

    @field_validator("name", "pattern", "speaker_group", "render_template")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        """必填字符串不能是空白。"""
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("必填字符串不能为空")
        return normalized_value

    @field_validator("body_group")
    @classmethod
    def _validate_body_group(cls, value: str) -> str:
        """正文分组允许省略，填写时必须清理空白。"""
        return value.strip()


class MvVirtualNameboxImportFile(StrictMvNameboxRuleModel):
    """MV 虚拟名字框规则导入文件。"""

    rules: list[MvVirtualNameboxRuleSpec] = Field(default_factory=list)


_MV_NAMEBOX_IMPORT_ADAPTER: TypeAdapter[MvVirtualNameboxImportFile] = TypeAdapter(MvVirtualNameboxImportFile)


def parse_mv_virtual_namebox_rule_import_text(raw_text: str) -> list[MvVirtualNameboxRuleRecord]:
    """解析 MV 虚拟名字框规则 JSON 文本。"""
    stripped_text = raw_text.strip()
    if not stripped_text:
        raise ValueError("MV 虚拟名字框规则 JSON 不能为空")
    decoded_raw = cast(object, json.loads(stripped_text))
    decoded = coerce_json_value(decoded_raw)
    import_file = _MV_NAMEBOX_IMPORT_ADAPTER.validate_python(decoded)
    return build_mv_virtual_namebox_rule_records(import_file.rules)


def build_mv_virtual_namebox_rule_records(
    specs: list[MvVirtualNameboxRuleSpec],
) -> list[MvVirtualNameboxRuleRecord]:
    """把外部规则模型转换为数据库记录。"""
    seen_names: set[str] = set()
    records: list[MvVirtualNameboxRuleRecord] = []
    for index, spec in enumerate(specs):
        if spec.name in seen_names:
            raise ValueError(f"MV 虚拟名字框规则名重复: {spec.name}")
        seen_names.add(spec.name)
        pattern = re.compile(spec.pattern)
        validate_compiled_rule(
            rule_name=spec.name,
            pattern=pattern,
            speaker_group=spec.speaker_group,
            body_group=spec.body_group,
            render_template=spec.render_template,
        )
        records.append(
            MvVirtualNameboxRuleRecord(
                rule_order=index,
                rule_name=spec.name,
                pattern_text=spec.pattern,
                speaker_group=spec.speaker_group,
                body_group=spec.body_group,
                speaker_policy=spec.speaker_policy,
                render_template=spec.render_template,
            )
        )
    return records


def validate_compiled_rule(
    *,
    rule_name: str,
    pattern: re.Pattern[str],
    speaker_group: str,
    body_group: str,
    render_template: str,
) -> None:
    """校验已编译规则的命名分组和模板字段。"""
    group_names = set(pattern.groupindex)
    if speaker_group not in group_names:
        raise ValueError(f"MV 虚拟名字框规则 {rule_name} 缺少说话人命名分组: {speaker_group}")
    if body_group and body_group not in group_names:
        raise ValueError(f"MV 虚拟名字框规则 {rule_name} 缺少正文命名分组: {body_group}")
    template_fields = read_template_fields(render_template)
    allowed_fields = group_names | {"speaker", "body"}
    unknown_fields = sorted(template_fields - allowed_fields)
    if unknown_fields:
        raise ValueError(f"MV 虚拟名字框规则 {rule_name} 的模板引用未知字段: {', '.join(unknown_fields)}")
    if speaker_group not in template_fields and "speaker" not in template_fields:
        raise ValueError(f"MV 虚拟名字框规则 {rule_name} 的模板没有引用说话人分组")
    if body_group and body_group not in template_fields and "body" not in template_fields:
        raise ValueError(f"MV 虚拟名字框规则 {rule_name} 的模板没有引用正文分组")


def read_template_fields(template: str) -> set[str]:
    """读取格式化模板中的字段名。"""
    fields: set[str] = set()
    for _literal_text, field_name, _format_spec, _conversion in Formatter().parse(template):
        if field_name is None:
            continue
        normalized_field = field_name.split(".", maxsplit=1)[0].split("[", maxsplit=1)[0]
        if not TEMPLATE_FIELD_PATTERN.fullmatch(normalized_field):
            raise ValueError(f"MV 虚拟名字框模板字段名非法: {field_name}")
        fields.add(normalized_field)
    return fields


def runtime_mv_virtual_namebox_rules(
    records: list[MvVirtualNameboxRuleRecord],
) -> tuple[MvVirtualNameboxRule, ...]:
    """把数据库记录转换为按顺序匹配的运行时规则。"""
    return tuple(
        MvVirtualNameboxRule.from_record(record) for record in sorted(records, key=lambda item: item.rule_order)
    )


def parse_mv_virtual_speaker_line(
    *,
    text: str,
    game_data: GameData,
    rules: tuple[MvVirtualNameboxRule, ...],
    location_path: str | None = None,
) -> MvVirtualSpeaker | None:
    """从 MV `401` 首条非空正文中按外部规则解析虚拟名字框。"""
    normalized_text = text.strip()
    if not normalized_text:
        return None
    matches: list[MvVirtualSpeaker] = []
    for rule in rules:
        match = rule.pattern.fullmatch(normalized_text)
        if match is None:
            continue
        try:
            matches.append(_build_virtual_speaker(game_data=game_data, rule=rule, match=match))
        except ValueError as error:
            if location_path is None:
                raise
            raise ValueError(f"{error}; 文本路径={location_path}") from error
    if len(matches) > 1:
        rule_names = ", ".join(match.rule_name for match in matches)
        path_message = "" if location_path is None else f"; 文本路径={location_path}"
        raise ValueError(f"MV 虚拟名字框规则命中冲突{path_message}: 规则={rule_names}; 文本={normalized_text}")
    if not matches:
        return None
    return matches[0]


def validate_mv_virtual_namebox_rules_against_game(
    *,
    game_data: GameData,
    records: list[MvVirtualNameboxRuleRecord],
) -> tuple[JsonArray, JsonArray]:
    """校验规则在当前游戏样本上的命中冲突和源文重建能力。"""
    return validate_mv_virtual_namebox_rules_against_candidates(
        game_data=game_data,
        records=records,
        candidates=collect_mv_virtual_namebox_candidates(game_data),
    )


def validate_mv_virtual_namebox_rules_against_candidates(
    *,
    game_data: GameData,
    records: list[MvVirtualNameboxRuleRecord],
    candidates: Sequence[MvVirtualNameboxCandidate],
) -> tuple[JsonArray, JsonArray]:
    """复用已建立的事件候选索引校验 MV 虚拟名字框规则。"""
    rules = runtime_mv_virtual_namebox_rules(records)
    errors: JsonArray = []
    details: JsonArray = []
    for candidate in candidates:
        matching_rules: list[str] = []
        virtual_speakers: list[MvVirtualSpeaker] = []
        for rule in rules:
            match = rule.pattern.fullmatch(candidate.text.strip())
            if match is None:
                continue
            matching_rules.append(rule.rule_name)
            try:
                virtual_speaker = _build_virtual_speaker(
                    game_data=game_data,
                    rule=rule,
                    match=match,
                )
                virtual_speakers.append(virtual_speaker)
            except Exception as error:
                errors.append(
                    {
                        "location_path": candidate.location_path,
                        "rule_name": rule.rule_name,
                        "text": candidate.text,
                        "message": f"{type(error).__name__}: {error}",
                    }
                )
                continue
            if virtual_speaker.speaker_policy == "translate" and is_actor_name_control_text(
                virtual_speaker.source_speaker_text
            ):
                errors.append(
                    {
                        "location_path": candidate.location_path,
                        "rule_name": rule.rule_name,
                        "text": candidate.text,
                        "source_speaker": virtual_speaker.source_speaker_text,
                        "message": "标准角色名控制符被 translate 规则命中，请改用 preserve 或 actor_name 规则，并收紧普通规则",
                    }
                )
            if virtual_speaker.render_source() != candidate.text.strip():
                errors.append(
                    {
                        "location_path": candidate.location_path,
                        "rule_name": virtual_speaker.rule_name,
                        "message": "规则模板无法重建源文本",
                        "source": candidate.text.strip(),
                        "rendered": virtual_speaker.render_source(),
                    }
                )
        if len(matching_rules) > 1:
            errors.append(
                {
                    "location_path": candidate.location_path,
                    "message": f"同一候选命中多条规则: {', '.join(matching_rules)}",
                }
            )
        if matching_rules:
            matching_rule_values: JsonArray = [rule_name for rule_name in matching_rules]
            following_lines: JsonArray = [line for line in candidate.following_lines[:3]]
            match_values: JsonArray = [
                {
                    "rule_name": virtual_speaker.rule_name,
                    "speaker": virtual_speaker.speaker,
                    "source_speaker": virtual_speaker.source_speaker_text,
                    "speaker_policy": virtual_speaker.speaker_policy,
                }
                for virtual_speaker in virtual_speakers
            ]
            detail: JsonObject = {
                "location_path": candidate.location_path,
                "text": candidate.text,
                "following_lines": following_lines,
                "matching_rules": matching_rule_values,
                "matches": match_values,
            }
            details.append(detail)
    return errors, details


def collect_mv_virtual_namebox_candidates(game_data: GameData) -> list[MvVirtualNameboxCandidate]:
    """收集 MV `101` 后首条非空 `401` 候选。"""
    command_snapshots = list(iter_all_commands(game_data))
    return collect_mv_virtual_namebox_candidates_from_command_snapshots(command_snapshots)


def collect_mv_virtual_namebox_candidates_from_command_snapshots(
    command_snapshots: Sequence[tuple[Sequence[str | int], str, EventCommand]],
) -> list[MvVirtualNameboxCandidate]:
    """复用已遍历的事件指令快照收集 MV 虚拟名字框候选。"""
    candidates: list[MvVirtualNameboxCandidate] = []
    commands_by_prefix: dict[tuple[str | int, ...], list[tuple[Sequence[str | int], EventCommand]]] = {}
    for path, _display_name, command in command_snapshots:
        commands_by_prefix.setdefault(tuple(path[:-1]), []).append((path, command))
    for command_group in commands_by_prefix.values():
        for index, (path, command) in enumerate(command_group):
            if command.code != Code.NAME:
                continue
            candidate = _candidate_after_name_command(command_group=command_group, start_index=index)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def mv_virtual_namebox_candidate_details(game_data: GameData) -> JsonArray:
    """生成供外部 Agent 阅读的 MV 虚拟名字框候选摘要。"""
    return mv_virtual_namebox_candidate_details_from_command_snapshots(list(iter_all_commands(game_data)))


def mv_virtual_namebox_candidate_details_from_command_snapshots(
    command_snapshots: Sequence[tuple[Sequence[str | int], str, EventCommand]],
) -> JsonArray:
    """复用已遍历的事件指令快照生成 MV 候选摘要。"""
    return mv_virtual_namebox_candidate_details_from_candidates(
        collect_mv_virtual_namebox_candidates_from_command_snapshots(command_snapshots)
    )


def mv_virtual_namebox_candidate_details_from_candidates(
    candidates: Sequence[MvVirtualNameboxCandidate],
) -> JsonArray:
    """把已建立的 MV 候选索引转换为外部摘要。"""
    details: JsonArray = []
    for candidate in candidates:
        following_lines: JsonArray = [line for line in candidate.following_lines[:3]]
        detail: JsonObject = {
            "location_path": candidate.location_path,
            "text": candidate.text,
            "following_lines": following_lines,
        }
        details.append(detail)
    return details


def mv_virtual_namebox_candidates_payload(game_data: GameData) -> JsonObject:
    """生成 MV 虚拟名字框候选导出 JSON。"""
    return mv_virtual_namebox_candidates_payload_from_candidates(collect_mv_virtual_namebox_candidates(game_data))


def mv_virtual_namebox_candidates_payload_from_candidates(
    candidates: Sequence[MvVirtualNameboxCandidate],
) -> JsonObject:
    """从已建立候选索引生成 MV 虚拟名字框导出 JSON。"""
    details = mv_virtual_namebox_candidate_details_from_candidates(candidates)
    return {
        "engine_kind": "mv",
        "candidate_count": len(details),
        "candidates": details,
    }


def mv_virtual_namebox_rule_records_to_import_json(
    records: list[MvVirtualNameboxRuleRecord] | tuple[MvVirtualNameboxRuleRecord, ...],
) -> JsonObject:
    """把数据库记录还原为外部 Agent 可编辑的规则 JSON。"""
    return {
        "rules": [
            {
                "name": record.rule_name,
                "pattern": record.pattern_text,
                "speaker_group": record.speaker_group,
                "speaker_policy": record.speaker_policy,
                "render_template": record.render_template,
                **({"body_group": record.body_group} if record.body_group else {}),
            }
            for record in sorted(records, key=lambda item: item.rule_order)
        ]
    }


def _candidate_after_name_command(
    *,
    command_group: list[tuple[Sequence[str | int], EventCommand]],
    start_index: int,
) -> MvVirtualNameboxCandidate | None:
    """读取单个 `101` 后首条非空 `401`。"""
    following_lines: list[str] = []
    next_index = start_index + 1
    first_path: Sequence[str | int] | None = None
    first_text: str | None = None
    while next_index < len(command_group):
        path, command = command_group[next_index]
        if command.code != Code.TEXT:
            break
        text = _read_first_parameter_text(command)
        if text is not None and text.strip():
            following_lines.append(text)
            if first_text is None:
                first_path = path
                first_text = text
        next_index += 1
    if first_path is None or first_text is None:
        return None
    return MvVirtualNameboxCandidate(
        location_path="/".join(map(str, first_path)),
        text=first_text.strip(),
        following_lines=following_lines[1:],
    )


def _read_first_parameter_text(command: EventCommand) -> str | None:
    """读取事件指令第一个字符串参数。"""
    if not command.parameters:
        return None
    value = command.parameters[0]
    if not isinstance(value, str):
        return None
    return value


def _build_virtual_speaker(
    *,
    game_data: GameData,
    rule: MvVirtualNameboxRule,
    match: re.Match[str],
) -> MvVirtualSpeaker:
    """把正则命中结果转换为虚拟名字框对象。"""
    group_values = {key: value for key, value in match.groupdict("").items()}
    source_speaker_text = group_values[rule.speaker_group].strip()
    if not source_speaker_text:
        raise ValueError(f"MV 虚拟名字框规则 {rule.rule_name} 命中了空说话人")
    speaker = source_speaker_text
    if rule.speaker_policy == "actor_name":
        speaker = _actor_name_from_control(game_data=game_data, text=source_speaker_text)
    body_text = group_values[rule.body_group].strip() if rule.body_group else ""
    return MvVirtualSpeaker(
        speaker=speaker,
        body_text=body_text,
        matched_text=match.string.strip(),
        rule_name=rule.rule_name,
        speaker_policy=rule.speaker_policy,
        source_speaker_text=source_speaker_text,
        render_template=rule.render_template,
        group_values=group_values,
        speaker_group=rule.speaker_group,
        body_group=rule.body_group,
    )


def _actor_name_from_control(*, game_data: GameData, text: str) -> str:
    """从 `\\N[n]` 控制符读取角色名。"""
    actor_match = ACTOR_NAME_CONTROL_PATTERN.fullmatch(text.strip())
    if actor_match is None:
        raise ValueError(f"actor_name 规则命中的说话人不是角色名控制符: {text}")
    actor_id = int(actor_match.group("actor_id"))
    for actor in game_data.base_data.get("Actors.json", []):
        if actor is None or actor.id != actor_id:
            continue
        actor_name = actor.name.strip()
        if actor_name:
            return actor_name
    raise ValueError(f"actor_name 规则无法解析角色 ID: {actor_id}")


def is_actor_name_control_text(text: str) -> bool:
    """判断文本是否是 RPG Maker 标准角色名控制符。"""
    return ACTOR_NAME_CONTROL_PATTERN.fullmatch(text.strip()) is not None


__all__: list[str] = [
    "is_actor_name_control_text",
    "MvVirtualNameboxCandidate",
    "MV_VIRTUAL_NAMEBOX_CANDIDATES_FILE_NAME",
    "MvVirtualNameboxRule",
    "MV_VIRTUAL_NAMEBOX_RULES_FILE_NAME",
    "MvVirtualNameboxRuleSpec",
    "MvVirtualSpeaker",
    "collect_mv_virtual_namebox_candidates",
    "collect_mv_virtual_namebox_candidates_from_command_snapshots",
    "mv_virtual_namebox_candidates_payload",
    "mv_virtual_namebox_candidates_payload_from_candidates",
    "mv_virtual_namebox_candidate_details",
    "mv_virtual_namebox_candidate_details_from_candidates",
    "mv_virtual_namebox_candidate_details_from_command_snapshots",
    "mv_virtual_namebox_rule_records_to_import_json",
    "parse_mv_virtual_namebox_rule_import_text",
    "parse_mv_virtual_speaker_line",
    "runtime_mv_virtual_namebox_rules",
    "validate_mv_virtual_namebox_rules_against_game",
    "validate_mv_virtual_namebox_rules_against_candidates",
]
