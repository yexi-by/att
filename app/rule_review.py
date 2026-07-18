"""外部文本规则审查状态与输入范围哈希工具。"""

import hashlib
import json
from collections.abc import Sequence
from typing import Literal

from app.plugin_source_text import PluginSourceScan
from app.plugin_text import build_plugins_file_hash
from app.rmmz.commands import iter_all_commands
from app.rmmz.schema import GameData
from app.rmmz.text_rules import JsonArray, JsonObject, JsonValue, TextRules

type RuleReviewDomain = Literal[
    "plugin_text",
    "plugin_source_text",
    "event_command_text",
    "note_tag_text",
    "placeholder_rules",
    "structured_placeholder_rules",
    "mv_virtual_namebox",
]

PLUGIN_TEXT_RULE_DOMAIN: RuleReviewDomain = "plugin_text"
PLUGIN_SOURCE_TEXT_RULE_DOMAIN: RuleReviewDomain = "plugin_source_text"
EVENT_COMMAND_TEXT_RULE_DOMAIN: RuleReviewDomain = "event_command_text"
NOTE_TAG_TEXT_RULE_DOMAIN: RuleReviewDomain = "note_tag_text"
PLACEHOLDER_RULE_DOMAIN: RuleReviewDomain = "placeholder_rules"
STRUCTURED_PLACEHOLDER_RULE_DOMAIN: RuleReviewDomain = "structured_placeholder_rules"
MV_VIRTUAL_NAMEBOX_RULE_DOMAIN: RuleReviewDomain = "mv_virtual_namebox"


def plugin_rule_scope_hash(game_data: GameData) -> str:
    """计算插件规则空结果审查依赖的当前插件配置哈希。"""
    return build_plugins_file_hash(game_data.plugins_js)


def plugin_source_rule_scope_hash(*, scan: PluginSourceScan) -> str:
    """按扫描事实计算完整启用插件源码集合与逐文件状态哈希。"""
    enabled_plugin_files: JsonArray = [file_name for file_name in sorted(scan.enabled_plugin_files)]
    file_states: JsonArray = []
    for state in sorted(scan.enabled_file_states, key=lambda item: item.file_name):
        payload: JsonObject = {
            "file": state.file_name,
            "status": state.status,
        }
        if state.status == "present":
            payload["hash"] = state.file_hash
        file_states.append(payload)
    return _stable_json_hash(
        {
            "enabled_plugin_files": enabled_plugin_files,
            "enabled_plugin_file_states": file_states,
        }
    )


def plugin_source_text_rules_hash(text_rules: TextRules) -> str:
    """计算会影响插件源码候选集合的完整文本规则指纹。"""
    return _stable_json_hash(
        {
            "setting": text_rules.setting.model_dump(mode="json"),
            "custom_placeholder_rules": [
                {
                    "pattern": rule.pattern_text,
                    "placeholder_template": rule.placeholder_template,
                }
                for rule in text_rules.custom_placeholder_rules
            ],
            "structured_placeholder_rules": [
                {
                    "name": rule.rule_name,
                    "type": rule.rule_type,
                    "pattern": rule.pattern_text,
                    "translatable_group": rule.translatable_group,
                    "protected_groups": dict(sorted(rule.protected_groups.items())),
                }
                for rule in text_rules.structured_placeholder_rules
            ],
        }
    )


def event_command_rule_scope_hash(game_data: GameData) -> str:
    """计算事件指令规则空结果审查依赖的当前事件指令参数哈希。"""
    command_codes = frozenset(command.code for _path, _display_name, command in iter_all_commands(game_data))
    return event_command_rule_scope_hash_for_codes(game_data=game_data, command_codes=command_codes)


def event_command_rule_scope_hash_for_codes(*, game_data: GameData, command_codes: frozenset[int]) -> str:
    """按指定事件指令编码计算空结果审查依赖的参数哈希。"""
    command_snapshots: list[JsonObject] = []
    for path, _display_name, command in iter_all_commands(game_data):
        if command.code not in command_codes:
            continue
        command_snapshots.append(
            {
                "path": [part for part in path],
                "code": command.code,
                "parameters": [parameter for parameter in command.parameters],
            }
        )
    return event_command_rule_scope_hash_for_snapshots(
        command_snapshots=command_snapshots,
        command_codes=command_codes,
    )


def event_command_rule_scope_hash_for_snapshots(
    *,
    command_snapshots: Sequence[JsonObject],
    command_codes: frozenset[int],
) -> str:
    """按单命令上下文的预建事件指令快照计算范围哈希。"""
    selected_snapshots: JsonArray = [
        {key: value for key, value in snapshot.items()}
        for snapshot in command_snapshots
        if snapshot.get("code") in command_codes
    ]
    return _stable_json_hash(
        {
            "command_codes": [code for code in sorted(command_codes)],
            "commands": selected_snapshots,
        }
    )


def note_tag_rule_scope_hash(game_data: GameData) -> str:
    """计算 Note 标签规则空结果审查依赖的当前 Note 文本哈希。"""
    notes: JsonArray = []
    for file_name, value in sorted(game_data.data.items()):
        _append_note_values(value=value, path=[file_name], notes=notes)
    return _stable_json_hash(notes)


def note_tag_rule_scope_hash_for_candidates(candidates: JsonArray) -> str:
    """按当前 Note 标签候选集合计算空结果审查依赖的哈希。"""
    return _stable_json_hash(candidates)


def placeholder_rule_scope_hash(payload: JsonValue) -> str:
    """计算普通占位符空结果审查依赖的当前候选哈希。"""
    return _stable_json_hash(payload)


def structured_placeholder_rule_scope_hash(payload: JsonValue) -> str:
    """计算结构化占位符空结果审查依赖的当前候选哈希。"""
    return _stable_json_hash(payload)


def mv_virtual_namebox_rule_scope_hash(payload: JsonValue) -> str:
    """计算 MV 虚拟名字框空规则审查依赖的当前候选哈希。"""
    return _stable_json_hash(payload)


def _append_note_values(*, value: JsonValue, path: list[str | int], notes: JsonArray) -> None:
    """递归收集 data JSON 中的 Note 字段值。"""
    if isinstance(value, dict):
        for key, child in sorted(value.items()):
            child_path = [*path, key]
            if key == "note" and isinstance(child, str):
                notes.append(
                    {
                        "path": [part for part in child_path],
                        "value": child,
                    }
                )
                continue
            _append_note_values(value=child, path=child_path, notes=notes)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _append_note_values(value=child, path=[*path, index], notes=notes)


def _stable_json_hash(payload: JsonValue) -> str:
    """对 JSON 兼容载荷计算稳定哈希。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse_rule_review_domain(value: str) -> RuleReviewDomain:
    """校验并收窄数据库中的外部规则审查领域。"""
    if value == PLUGIN_TEXT_RULE_DOMAIN:
        return PLUGIN_TEXT_RULE_DOMAIN
    if value == PLUGIN_SOURCE_TEXT_RULE_DOMAIN:
        return PLUGIN_SOURCE_TEXT_RULE_DOMAIN
    if value == EVENT_COMMAND_TEXT_RULE_DOMAIN:
        return EVENT_COMMAND_TEXT_RULE_DOMAIN
    if value == NOTE_TAG_TEXT_RULE_DOMAIN:
        return NOTE_TAG_TEXT_RULE_DOMAIN
    if value == PLACEHOLDER_RULE_DOMAIN:
        return PLACEHOLDER_RULE_DOMAIN
    if value == STRUCTURED_PLACEHOLDER_RULE_DOMAIN:
        return STRUCTURED_PLACEHOLDER_RULE_DOMAIN
    if value == MV_VIRTUAL_NAMEBOX_RULE_DOMAIN:
        return MV_VIRTUAL_NAMEBOX_RULE_DOMAIN
    raise ValueError(f"未知外部规则审查领域: {value}")


__all__: list[str] = [
    "EVENT_COMMAND_TEXT_RULE_DOMAIN",
    "NOTE_TAG_TEXT_RULE_DOMAIN",
    "PLACEHOLDER_RULE_DOMAIN",
    "PLUGIN_TEXT_RULE_DOMAIN",
    "PLUGIN_SOURCE_TEXT_RULE_DOMAIN",
    "MV_VIRTUAL_NAMEBOX_RULE_DOMAIN",
    "RuleReviewDomain",
    "STRUCTURED_PLACEHOLDER_RULE_DOMAIN",
    "event_command_rule_scope_hash",
    "event_command_rule_scope_hash_for_codes",
    "event_command_rule_scope_hash_for_snapshots",
    "note_tag_rule_scope_hash",
    "note_tag_rule_scope_hash_for_candidates",
    "parse_rule_review_domain",
    "placeholder_rule_scope_hash",
    "plugin_rule_scope_hash",
    "plugin_source_rule_scope_hash",
    "plugin_source_text_rules_hash",
    "mv_virtual_namebox_rule_scope_hash",
    "structured_placeholder_rule_scope_hash",
]
