"""翻译与写入前置硬闸。

本模块把 Skill 中“必须先完成”的步骤固化为程序不变量。翻译、质量报告和写入游戏文件
都应复用这里的检查，避免不同入口各写一套宽松判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from app.agent_toolkit.placeholder_scan import (
    PlaceholderCandidateAnalysis,
    StructuredPlaceholderCandidateAnalysis,
    analyze_placeholder_candidates,
    analyze_structured_placeholder_candidates,
    count_uncovered_candidates,
)
from app.application.errors import WorkflowGateError
from app.config.schemas import Setting
from app.event_command_text import resolve_event_command_codes
from app.game_analysis import GameAnalysisContext
from app.note_tag_text.exporter import collect_note_tag_candidates
from app.persistence import TargetGameSession
from app.persistence.plugin_source_assessment_records import PLUGIN_SOURCE_SCANNER_VERSION
from app.persistence.records import RuleReviewStateRecord
from app.plugin_source_text import (
    collect_plugin_source_review_coverage,
    filter_fresh_plugin_source_text_rules,
)
from app.plugin_text import collect_plugin_json_string_leaf_candidates, extract_plugin_name
from app.rmmz.commands import iter_all_commands
from app.rmmz.schema import GameData
from app.rmmz.text_rules import JsonArray, JsonValue, TextRules
from app.rule_review import (
    EVENT_COMMAND_TEXT_RULE_DOMAIN,
    MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
    NOTE_TAG_TEXT_RULE_DOMAIN,
    PLACEHOLDER_RULE_DOMAIN,
    PLUGIN_TEXT_RULE_DOMAIN,
    STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
    RuleReviewDomain,
    event_command_rule_scope_hash_for_codes,
    event_command_rule_scope_hash_for_snapshots,
    mv_virtual_namebox_rule_scope_hash,
    note_tag_rule_scope_hash_for_candidates,
    placeholder_rule_scope_hash,
    plugin_rule_scope_hash,
    plugin_source_rule_scope_hash,
    plugin_source_text_rules_hash,
    structured_placeholder_rule_scope_hash,
)
from app.terminology import collect_terminology_bundle_errors
from app.text_scope import TextScopeResult


@dataclass(frozen=True, slots=True)
class WorkflowGateIssue:
    """单个会阻断翻译或写入的流程前置错误。"""

    code: str
    message: str


async def collect_workflow_gate_errors(
    *,
    session: TargetGameSession,
    context: GameAnalysisContext,
    setting: Setting,
    custom_placeholder_rules_supplied: bool,
) -> list[WorkflowGateIssue]:
    """收集当前游戏不能继续翻译或写入的全部硬闸错误。"""
    text_rules = context.text_rules
    scope = context.scope
    errors: list[WorkflowGateIssue] = []
    errors.extend(
        await _plugin_source_rule_gate_errors(
            session=session,
            context=context,
        )
    )
    errors.extend(await _terminology_gate_errors(session))
    errors.extend(
        await _external_rule_gate_errors(
            session=session,
            context=context,
        )
    )
    errors.extend(
        await _placeholder_gate_errors(
            session=session,
            context=context,
            custom_placeholder_rules_supplied=custom_placeholder_rules_supplied,
        )
    )
    errors.extend(
        await _structured_placeholder_gate_errors(
            session=session,
            context=context,
        )
    )
    errors.extend(_text_scope_gate_errors(scope=scope, text_rules=text_rules))
    _ = setting
    return errors


async def collect_external_text_rule_gate_errors(
    *,
    session: TargetGameSession,
    context: GameAnalysisContext,
    setting: Setting,
) -> list[WorkflowGateIssue]:
    """收集三类外部文本规则未完成导致的前置错误。"""
    _ = setting
    return await _external_rule_gate_errors(
        session=session,
        context=context,
    )


def format_workflow_gate_error(errors: list[WorkflowGateIssue]) -> str:
    """把硬闸错误转换成用户可读的失败原因。"""
    messages = "；".join(error.message for error in errors)
    return f"检查没通过，不能继续：{messages}"


async def assert_workflow_gate_passed(
    *,
    session: TargetGameSession,
    context: GameAnalysisContext,
    setting: Setting,
    custom_placeholder_rules_supplied: bool,
) -> None:
    """不满足流程前置条件时立刻中断当前任务。"""
    errors = await collect_workflow_gate_errors(
        session=session,
        context=context,
        setting=setting,
        custom_placeholder_rules_supplied=custom_placeholder_rules_supplied,
    )
    if errors:
        primary_error = errors[0]
        raise WorkflowGateError(
            format_workflow_gate_error(errors),
            code=primary_error.code,
            details={
                "issues": [
                    {
                        "code": error.code,
                        "message": error.message,
                    }
                    for error in errors
                ]
            },
        )


def ensure_empty_rule_import_allowed(
    *,
    rule_label: str,
    confirm_empty: bool,
    candidate_count: int,
) -> None:
    """校验空规则导入是否经过显式确认，且机器候选已经全部处理。"""
    ensure_empty_rule_confirmed(rule_label=rule_label, confirm_empty=confirm_empty)
    if candidate_count > 0:
        raise RuntimeError(f"{rule_label}为空，但当前扫描仍有 {candidate_count} 个候选，不能保存为空规则")


def ensure_empty_rule_confirmed(
    *,
    rule_label: str,
    confirm_empty: bool,
) -> None:
    """校验人工审查为空的规则导入是否经过显式确认。"""
    if not confirm_empty:
        raise RuntimeError(f"{rule_label}为空，必须先确认当前游戏确实没有对应规则，再传 --confirm-empty")


def count_plugin_rule_candidates(game_data: GameData) -> int:
    """统计当前插件配置中的字符串叶子候选数量。"""
    count = 0
    for plugin_index, plugin in enumerate(game_data.plugins_js):
        plugin_name = extract_plugin_name(plugin, plugin_index)
        count += len(
            collect_plugin_json_string_leaf_candidates(
                plugin_index=plugin_index,
                plugin_name=plugin_name,
                plugin=plugin,
            )
        )
    return count


def count_event_command_rule_candidates(*, game_data: GameData, setting: Setting) -> int:
    """统计当前配置会导出的事件指令参数候选数量。"""
    command_codes = event_command_rule_codes_for_setting(game_data=game_data, setting=setting)
    return count_event_command_rule_candidates_for_codes(game_data=game_data, command_codes=command_codes)


def count_event_command_rule_candidates_for_codes(
    *,
    game_data: GameData,
    command_codes: frozenset[int],
) -> int:
    """按指定事件指令编码统计参数候选数量。"""
    if not command_codes:
        raise ValueError("事件指令编码不能为空")
    seen_samples: set[tuple[int, str]] = set()
    for _path, _display_name, command in iter_all_commands(game_data):
        if command.code not in command_codes:
            continue
        sample_key = json.dumps(command.parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        seen_samples.add((command.code, sample_key))
    return len(seen_samples)


def event_command_rule_scope_hash_for_setting(*, game_data: GameData, setting: Setting) -> str:
    """按当前事件指令导出配置计算空规则确认范围哈希。"""
    command_codes = event_command_rule_codes_for_setting(game_data=game_data, setting=setting)
    return event_command_rule_scope_hash_for_command_codes(game_data=game_data, command_codes=command_codes)


def event_command_rule_codes_for_setting(*, game_data: GameData, setting: Setting) -> frozenset[int]:
    """读取当前配置实际启用的事件指令编码集合。"""
    return resolve_event_command_codes(
        command_codes=None,
        default_command_codes=setting.event_command_text.default_codes_for_engine(game_data.layout.engine_kind),
    )


def event_command_rule_scope_hash_for_command_codes(
    *,
    game_data: GameData,
    command_codes: frozenset[int],
) -> str:
    """按指定事件指令编码计算空规则确认范围哈希。"""
    if not command_codes:
        raise ValueError("事件指令编码不能为空")
    return event_command_rule_scope_hash_for_codes(game_data=game_data, command_codes=command_codes)


def event_command_codes_from_review_state(
    state: RuleReviewStateRecord,
) -> frozenset[int] | None:
    """从空规则审查记录恢复当时实际检查的事件编码集合。"""
    if state.scope_contract_version != 1:
        return None
    if state.scope_payload.get("kind") != "event_command_codes":
        return None
    raw_codes_value = state.scope_payload.get("command_codes")
    if not isinstance(raw_codes_value, list) or not raw_codes_value:
        return None
    raw_codes = cast(list[object], raw_codes_value)
    codes: set[int] = set()
    for raw_code in raw_codes:
        if isinstance(raw_code, bool) or not isinstance(raw_code, int):
            return None
        codes.add(raw_code)
    return frozenset(codes) if codes else None


def count_note_tag_rule_candidates(*, game_data: GameData, text_rules: TextRules) -> int:
    """统计当前 Note 标签候选中实际含可翻译值的数量。"""
    candidates = collect_note_tag_candidates(game_data=game_data, text_rules=text_rules)
    return _candidate_int_sum(candidates, "translatable_hit_count")


def note_tag_rule_scope_hash_for_text_rules(*, game_data: GameData, text_rules: TextRules) -> str:
    """按当前文本规则计算 Note 标签空规则确认范围哈希。"""
    candidates = collect_note_tag_candidates(game_data=game_data, text_rules=text_rules)
    return note_tag_rule_scope_hash_for_candidates(candidates)


def normal_placeholder_scope_hash_from_analysis(analysis: PlaceholderCandidateAnalysis) -> str:
    """从已扫描的普通占位符事实计算范围哈希。"""
    return placeholder_rule_scope_hash(analysis.details)


def structured_placeholder_scope_hash_from_analysis(analysis: StructuredPlaceholderCandidateAnalysis) -> str:
    """从已扫描的结构化外壳事实计算范围哈希。"""
    return structured_placeholder_rule_scope_hash(analysis.details)


async def _terminology_gate_errors(session: TargetGameSession) -> list[WorkflowGateIssue]:
    """检查字段译名表和正文术语表是否完整一致。"""
    registry = await session.read_terminology_registry()
    glossary = await session.read_terminology_glossary()
    return [
        WorkflowGateIssue(code="terminology_bundle", message=message)
        for message in collect_terminology_bundle_errors(registry=registry, glossary=glossary)
    ]


async def _external_rule_gate_errors(
    *,
    session: TargetGameSession,
    context: GameAnalysisContext,
) -> list[WorkflowGateIssue]:
    """检查插件、事件指令和 Note 标签外部规则是否完成导入或空结果确认。"""
    game_data = context.game_data
    errors: list[WorkflowGateIssue] = []
    plugin_rules = context.plugin_rules
    stale_plugin_rules = context.stale_plugin_rules
    if stale_plugin_rules:
        errors.append(
            WorkflowGateIssue(
                code="stale_plugin_rules",
                message=f"存在 {len(stale_plugin_rules)} 个过期插件规则，请重新导入插件规则",
            )
        )
    if not plugin_rules and not stale_plugin_rules:
        errors.extend(
            await _empty_rule_review_errors(
                session=session,
                rule_domain=PLUGIN_TEXT_RULE_DOMAIN,
                current_scope_hash=plugin_rule_scope_hash(game_data),
                label="插件规则",
            )
        )

    if game_data.layout.engine_kind == "mv":
        if not context.mv_virtual_namebox_rules:
            errors.extend(
                await _empty_rule_review_errors(
                    session=session,
                    rule_domain=MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
                    current_scope_hash=mv_virtual_namebox_rule_scope_hash(
                        [candidate for candidate in context.mv_virtual_namebox_candidates]
                    ),
                    label="MV 虚拟名字框规则",
                )
            )

    if not context.event_rules:
        state = await session.read_rule_review_state(rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN)
        if state is None or not state.reviewed_empty:
            errors.append(
                WorkflowGateIssue(
                    code="event_command_text_missing",
                    message="事件指令规则为空且没有显式确认当前游戏没有对应规则，检查没通过，不能继续",
                )
            )
        else:
            reviewed_codes = event_command_codes_from_review_state(state)
            if reviewed_codes is None:
                errors.append(
                    WorkflowGateIssue(
                        code="event_command_text_invalid_empty_confirmation",
                        message="事件指令空规则确认缺少实际检查的编码范围，请重新导出并确认规则",
                    )
                )
            elif state.scope_hash != event_command_rule_scope_hash_for_snapshots(
                command_snapshots=context.event_command_snapshots,
                command_codes=reviewed_codes,
            ):
                errors.append(
                    WorkflowGateIssue(
                        code="event_command_text_stale_empty_confirmation",
                        message="事件指令规则曾确认为空，但已确认编码范围内的游戏内容发生变化，请重新扫描并导入规则",
                    )
                )

    if not context.note_tag_rules:
        errors.extend(
            await _empty_rule_review_errors(
                session=session,
                rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
                current_scope_hash=note_tag_rule_scope_hash_for_candidates(
                    [candidate for candidate in context.note_candidates]
                ),
                label="Note 标签规则",
            )
        )
    return errors


async def _plugin_source_rule_gate_errors(
    *,
    session: TargetGameSession,
    context: GameAnalysisContext,
) -> list[WorkflowGateIssue]:
    """高风险插件源码文本必须先确认并导入源码规则。"""
    text_rules = context.text_rules
    records = list(context.plugin_source_rules)
    scan = context.plugin_source_scan
    if scan.risk.read_error_file_count:
        missing_count = scan.missing_enabled_file_count
        unreadable_count = scan.unreadable_enabled_file_count
        failure_summary = "、".join(
            part
            for part in (
                f"缺失 {missing_count} 个" if missing_count else "",
                f"读取失败 {unreadable_count} 个" if unreadable_count else "",
            )
            if part
        )
        return [
            WorkflowGateIssue(
                code="plugin_source_read_error",
                message=(
                    f"有 {scan.risk.read_error_file_count} 个已启用插件的翻译源源码不可用（{failure_summary}），"
                    "当前风险扫描结果不可信，请补齐缺失文件或将无法读取的源码转换为 UTF-8 后重新扫描"
                ),
            )
        ]
    assessment = await session.read_plugin_source_assessment()
    if assessment is None:
        return [
            WorkflowGateIssue(
                code="plugin_source_assessment_missing",
                message="插件源码尚未完成当前规则下的风险扫描，请先运行 scan-plugin-source-text 或准备 Agent 工作区",
            )
        ]
    current_source_hash = plugin_source_rule_scope_hash(scan=scan)
    current_text_rules_hash = plugin_source_text_rules_hash(text_rules)
    if (
        assessment.source_hash != current_source_hash
        or assessment.text_rules_hash != current_text_rules_hash
        or assessment.scanner_version != PLUGIN_SOURCE_SCANNER_VERSION
        or assessment.high_risk != scan.risk.high_risk
        or assessment.candidate_count != len(scan.candidates)
    ):
        return [
            WorkflowGateIssue(
                code="plugin_source_assessment_stale",
                message="插件源码、文本规则或扫描器已经变化，请重新运行插件源码风险扫描",
            )
        ]
    fresh_records, stale_records = filter_fresh_plugin_source_text_rules(
        rule_records=records,
        scan=scan,
    )
    if stale_records:
        return [
            WorkflowGateIssue(
                code="stale_plugin_source_rules",
                message=f"存在 {len(stale_records)} 个过期插件源码规则，请重新导入插件源码规则",
            )
        ]
    if not scan.risk.high_risk and not fresh_records:
        return []
    if scan.risk.high_risk and not fresh_records:
        return [
            WorkflowGateIssue(
                code="plugin_source_text_high_risk",
                message=(
                    "发现大量插件源码文本候选，可能有玩家可见正文存放在 js/plugins 源码文件中；"
                    "正文翻译已暂停，请先确认并完成插件源码 AST 分析支线，导入插件源码规则后再继续"
                ),
            )
        ]
    coverage = collect_plugin_source_review_coverage(scan=scan, rule_records=fresh_records)
    if not coverage.unreviewed_candidates:
        return []
    return [
        WorkflowGateIssue(
            code="plugin_source_review_incomplete",
            message=(
                f"插件源码支线还有 {len(coverage.unreviewed_candidates)} 个候选未由外部 Agent 归入翻译或排除；"
                "请补全插件源码规则后再继续"
            ),
        )
    ]


async def _placeholder_gate_errors(
    *,
    session: TargetGameSession,
    context: GameAnalysisContext,
    custom_placeholder_rules_supplied: bool,
) -> list[WorkflowGateIssue]:
    """检查普通自定义占位符规则是否覆盖当前正文候选。"""
    scope = context.scope
    text_rules = context.text_rules
    analysis = analyze_placeholder_candidates(scope.translation_data_map, text_rules)
    uncovered_count = count_uncovered_candidates(analysis.candidates)
    errors: list[WorkflowGateIssue] = []
    if uncovered_count:
        errors.append(
            WorkflowGateIssue(
                code="placeholder_uncovered",
                message=f"发现 {uncovered_count} 个未覆盖的疑似自定义控制符，请先导入普通占位符规则",
            )
        )
    if custom_placeholder_rules_supplied:
        return errors
    if context.placeholder_rules:
        return errors
    current_scope_hash = normal_placeholder_scope_hash_from_analysis(analysis)
    errors.extend(
        await _empty_rule_review_errors(
            session=session,
            rule_domain=PLACEHOLDER_RULE_DOMAIN,
            current_scope_hash=current_scope_hash,
            label="普通占位符规则",
        )
    )
    return errors


async def _structured_placeholder_gate_errors(
    *,
    session: TargetGameSession,
    context: GameAnalysisContext,
) -> list[WorkflowGateIssue]:
    """检查结构化占位符规则是否覆盖当前正文候选。"""
    scope = context.scope
    text_rules = context.text_rules
    analysis = analyze_structured_placeholder_candidates(scope.translation_data_map, text_rules)
    uncovered_count = analysis.uncovered_count
    errors: list[WorkflowGateIssue] = []
    if uncovered_count:
        errors.append(
            WorkflowGateIssue(
                code="structured_placeholder_uncovered",
                message=f"发现 {uncovered_count} 个未被结构化规则覆盖的协议外壳候选，请先导入结构化占位符规则",
            )
        )
    if context.structured_placeholder_rules:
        return errors
    current_scope_hash = structured_placeholder_scope_hash_from_analysis(analysis)
    errors.extend(
        await _empty_rule_review_errors(
            session=session,
            rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
            current_scope_hash=current_scope_hash,
            label="结构化占位符规则",
        )
    )
    return errors


def _text_scope_gate_errors(*, scope: TextScopeResult, text_rules: TextRules) -> list[WorkflowGateIssue]:
    """检查当前文本范围是否存在过期规则或不可写条目。"""
    errors: list[WorkflowGateIssue] = []
    if scope.stale_plugin_rules:
        errors.append(
            WorkflowGateIssue(
                code="stale_plugin_rules",
                message=f"存在 {len(scope.stale_plugin_rules)} 个过期插件规则，请重新导入插件规则",
            )
        )
    if scope.write_back_probe_error:
        errors.append(WorkflowGateIssue(code="write_back_probe_error", message=scope.write_back_probe_error))
    if scope.unwritable_entries:
        errors.append(
            WorkflowGateIssue(
                code="coverage_unwritable",
                message=f"存在 {len(scope.unwritable_entries)} 条当前文本无法写进游戏文件，请先运行 audit-coverage 查看明细",
            )
        )
    unwritable_rule_hit_count = sum(
        1
        for entry in scope.entries
        if not entry.enters_translation
        and entry.source_type != "standard_data"
        and text_rules.should_translate_source_lines(entry.original_lines)
    )
    if unwritable_rule_hit_count:
        errors.append(
            WorkflowGateIssue(
                code="rule_hits_unwritable",
                message=f"存在 {unwritable_rule_hit_count} 条规则命中文本没有进入当前可写范围，请先运行 audit-coverage 查看明细",
            )
        )
    return errors


async def _empty_rule_review_errors(
    *,
    session: TargetGameSession,
    rule_domain: RuleReviewDomain,
    current_scope_hash: str,
    label: str,
) -> list[WorkflowGateIssue]:
    """检查空规则是否经过显式确认且确认范围仍然有效。"""
    state = await session.read_rule_review_state(rule_domain=rule_domain)
    if state is None or not state.reviewed_empty:
        return [
            WorkflowGateIssue(
                code=f"{rule_domain}_missing",
                message=f"{label}为空且没有显式确认当前游戏没有对应规则，检查没通过，不能继续",
            )
        ]
    if state.scope_hash != current_scope_hash:
        return [
            WorkflowGateIssue(
                code=f"{rule_domain}_stale_empty_confirmation",
                message=f"{label}曾确认为空，但当前游戏内容已经变化，请重新扫描并导入规则",
            )
        ]
    return []


def _candidate_int_sum(candidates: JsonArray, key: str) -> int:
    """统计候选对象中的整数计数字段。"""
    total = 0
    for candidate_value in candidates:
        if not isinstance(candidate_value, dict):
            continue
        raw_count: JsonValue | None = candidate_value.get(key)
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            continue
        total += raw_count
    return total


__all__: list[str] = [
    "WorkflowGateIssue",
    "collect_external_text_rule_gate_errors",
    "assert_workflow_gate_passed",
    "collect_workflow_gate_errors",
    "count_event_command_rule_candidates_for_codes",
    "event_command_rule_scope_hash_for_setting",
    "event_command_rule_codes_for_setting",
    "event_command_codes_from_review_state",
    "event_command_rule_scope_hash_for_command_codes",
    "count_event_command_rule_candidates",
    "count_note_tag_rule_candidates",
    "count_plugin_rule_candidates",
    "ensure_empty_rule_confirmed",
    "ensure_empty_rule_import_allowed",
    "format_workflow_gate_error",
    "note_tag_rule_scope_hash_for_text_rules",
    "normal_placeholder_scope_hash_from_analysis",
    "structured_placeholder_scope_hash_from_analysis",
]
