"""Agent 工具箱 WorkspaceAgentMixin 子服务。"""
# pyright: reportPrivateUsage=false
# mixin 通过 AgentToolkitService 组合成同一个服务边界，允许调用同门面的受保护核心方法。

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from typing import final

from app.agent_toolkit.placeholder_scan import (
    PlaceholderCandidateAnalysis,
    StructuredPlaceholderCandidateAnalysis,
    analyze_placeholder_candidates,
    analyze_structured_placeholder_candidates,
)
from app.agent_toolkit.services.placeholder_rules import (
    _build_placeholder_rule_validation_report,
    _build_structured_placeholder_rule_validation_report,
    _collect_placeholder_rule_validation_samples,
    _collect_structured_placeholder_preview_samples,
)
from app.agent_toolkit.services.rule_validation import (
    _collect_plugin_source_unwritable_items,
    _format_mv_namebox_rule_error,
    _mv_namebox_match_key,
    _mv_namebox_match_keys,
)
from app.application.flow_gate import (
    normal_placeholder_scope_hash_from_analysis,
    structured_placeholder_scope_hash_from_analysis,
)
from app.application.mutation_guard import open_game_for_mutation
from app.config.schemas import TextRulesSetting
from app.event_command_text.index import EventCommandAnalysisEntry
from app.game_analysis import GameAnalysisContext
from app.note_tag_text.sources import NoteTagSource
from app.plugin_source_text import (
    PluginSourceScan,
    PluginSourceTextExtraction,
    build_plugin_source_raw_index,
    build_plugin_source_rule_records_from_import,
    collect_plugin_source_review_coverage,
    derive_plugin_source_scan,
    parse_plugin_source_rule_import_text,
    plugin_source_rule_records_to_import_json,
)
from app.plugin_text.index import PluginParameterAnalysisEntry
from app.rmmz.game_file_view import GameFileView, parse_game_file_view
from app.rmmz.mv_namebox import (
    MV_VIRTUAL_NAMEBOX_CANDIDATES_FILE_NAME,
    MV_VIRTUAL_NAMEBOX_RULES_FILE_NAME,
    MvVirtualNameboxCandidate,
    mv_virtual_namebox_candidate_details_from_candidates,
    mv_virtual_namebox_rule_records_to_import_json,
    parse_mv_virtual_namebox_rule_import_text,
    validate_mv_virtual_namebox_rules_against_candidates,
)
from app.rmmz.schema import MvVirtualNameboxRuleRecord, PluginSourceTextRuleRecord
from app.rmmz.source_snapshot import SourceSnapshotFileRecord
from app.rule_review import (
    EVENT_COMMAND_TEXT_RULE_DOMAIN,
    MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
    NOTE_TAG_TEXT_RULE_DOMAIN,
    PLACEHOLDER_RULE_DOMAIN,
    PLUGIN_SOURCE_TEXT_RULE_DOMAIN,
    PLUGIN_TEXT_RULE_DOMAIN,
    STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
    RuleReviewDomain,
    event_command_rule_scope_hash_for_snapshots,
    mv_virtual_namebox_rule_scope_hash,
    note_tag_rule_scope_hash_for_candidates,
    plugin_rule_scope_hash,
    plugin_source_rule_scope_hash,
    plugin_source_text_rules_hash,
)
from app.terminology import collect_terminology_bundle_errors

from .common import (
    PLUGINS_FILE_NAME,
    STRUCTURED_PLACEHOLDER_RULES_FILE_NAME,
    TERMINOLOGY_SUBTASK_GROUPS,
    AgentIssue,
    AgentReport,
    AgentServiceContext,
    CustomPlaceholderRule,
    EventCommandTextExtraction,
    GameData,
    JsonArray,
    JsonObject,
    NoteTagTextExtraction,
    Path,
    PlaceholderRuleRecord,
    PluginTextExtraction,
    QualityProgressCallbacks,
    StructuredPlaceholderRule,
    StructuredPlaceholderRuleRecord,
    TargetGameSession,
    TerminologyExtraction,
    TerminologyGlossary,
    TerminologyRegistry,
    TextRules,
    TranslationData,
    TranslationItem,
    _agent_workflow_manifest,
    _build_custom_placeholder_rule_draft,
    _build_rule_metric_detail,
    _collect_terminology_duplicate_translation_samples,
    _collect_write_protocol_unwritable_items,
    _event_command_rule_records_to_import_json,
    _is_path_inside,
    _json_items_by_location_path,
    _merge_terminology_registry,
    _noop_quality_progress_callbacks,
    _note_tag_item_matches_rule,
    _note_tag_rule_records_to_import_json,
    _placeholder_rule_records_to_import_json,
    _plugin_rule_records_to_import_json,
    _preview_event_command_write_back,
    _structured_placeholder_rule_records_to_import_json,
    _validate_terminology_registry,
    _validate_terminology_registry_shape,
    _write_json_object,
    _write_json_value,
    _write_terminology_subtask_files,
    aiofiles,
    build_event_command_rule_records_from_import,
    build_note_tag_rule_records_from_import,
    build_plugin_rule_records_from_import,
    cast,
    coerce_json_value,
    count_uncovered_candidates,
    ensure_json_array,
    ensure_json_object,
    export_plugins_json_file,
    export_terminology_artifacts,
    issue,
    json,
    load_custom_placeholder_rules_text,
    load_setting,
    load_structured_placeholder_rules_text,
    load_terminology_glossary,
    load_terminology_registry,
    parse_event_command_rule_import_text,
    parse_note_tag_rule_import_text,
    parse_plugin_rule_import_text,
    placeholder_candidates_to_details,
    resolve_event_command_codes,
    scan_placeholder_candidates,
    write_field_terms_json,
    write_glossary_json,
)


@dataclass(frozen=True, slots=True)
class _WorkspacePlaceholderAnalysis:
    """工作区普通/结构化规则共享的单次扫描事实。"""

    custom_rules: tuple[CustomPlaceholderRule, ...]
    structured_rules: tuple[StructuredPlaceholderRule, ...]
    text_rules: TextRules
    translation_data_map: dict[str, TranslationData]
    placeholder_candidates: PlaceholderCandidateAnalysis
    structured_candidates: StructuredPlaceholderCandidateAnalysis
    placeholder_sample_texts: tuple[str, ...]
    structured_sample_texts: tuple[str, ...]


class WorkspaceAgentMixin:
    """承载 AgentToolkitService 的 WorkspaceAgentMixin 命令族。"""

    async def scan_plugin_source_text(
        self: AgentServiceContext,
        *,
        game_title: str,
        output_path: Path,
        source_view: GameFileView | str = GameFileView.TRANSLATION_SOURCE,
    ) -> AgentReport:
        """扫描插件源码文本风险，只输出轻量风险报告。"""
        resolved_view = parse_game_file_view(str(source_view))
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            setting = load_setting(
                self.setting_path,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            game_data = await self._load_game_data_for_view(
                session,
                source_view=resolved_view,
                include_writable_copies=False,
            )
            text_rules = TextRules.from_setting(setting.text_rules)
            plugin_source_raw_index = build_plugin_source_raw_index(game_data=game_data)
            scan = derive_plugin_source_scan(index=plugin_source_raw_index, text_rules=text_rules)
            scan_error = _plugin_source_scan_read_error_issue(scan)
            if resolved_view is GameFileView.TRANSLATION_SOURCE and scan_error is None:
                await session.replace_plugin_source_assessment(
                    source_hash=plugin_source_rule_scope_hash(scan=scan),
                    text_rules_hash=plugin_source_text_rules_hash(text_rules),
                    high_risk=scan.risk.high_risk,
                    candidate_count=len(scan.candidates),
                    summary=cast(dict[str, object], scan.risk_report_json()),
                )
        risk_report = scan.risk_report_json()
        risk_report["source_view"] = resolved_view.value
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await _write_json_object(output_path, risk_report)
        warnings: list[AgentIssue] = []
        if not scan.candidates and scan_error is None:
            warnings.append(issue("plugin_source_text_empty", "没有扫描到插件源码硬编码文本候选"))
        return AgentReport.from_parts(
            errors=[] if scan_error is None else [scan_error],
            warnings=warnings,
            summary={
                "source_view": resolved_view.value,
                "candidate_count": len(scan.candidates),
                "output": str(output_path),
                **scan.risk.to_json_object(),
            },
            details={
                **risk_report,
                "output": str(output_path),
            },
        )

    async def export_plugin_source_ast_map(
        self: AgentServiceContext,
        *,
        game_title: str,
        output_path: Path,
        source_view: GameFileView | str = GameFileView.TRANSLATION_SOURCE,
    ) -> AgentReport:
        """导出插件源码 AST 地图和候选文本。"""
        resolved_view = parse_game_file_view(str(source_view))
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            setting = load_setting(
                self.setting_path,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            game_data = await self._load_game_data_for_view(
                session,
                source_view=resolved_view,
                include_writable_copies=False,
            )
            text_rules = TextRules.from_setting(setting.text_rules)
            plugin_source_raw_index = build_plugin_source_raw_index(game_data=game_data)
            scan = derive_plugin_source_scan(index=plugin_source_raw_index, text_rules=text_rules)
            scan_error = _plugin_source_scan_read_error_issue(scan)
            if resolved_view is GameFileView.TRANSLATION_SOURCE and scan_error is None:
                await session.replace_plugin_source_assessment(
                    source_hash=plugin_source_rule_scope_hash(scan=scan),
                    text_rules_hash=plugin_source_text_rules_hash(text_rules),
                    high_risk=scan.risk.high_risk,
                    candidate_count=len(scan.candidates),
                    summary=cast(dict[str, object], scan.risk_report_json()),
                )
        payload = scan.to_json_object()
        payload["source_view"] = resolved_view.value
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await _write_json_object(output_path, payload)
        details = scan.risk_report_json()
        details["source_view"] = resolved_view.value
        details["output"] = str(output_path)
        return AgentReport.from_parts(
            errors=[] if scan_error is None else [scan_error],
            warnings=[],
            summary={
                "source_view": resolved_view.value,
                "output": str(output_path),
                "candidate_count": len(scan.candidates),
                **scan.risk.to_json_object(),
            },
            details=details,
        )

    async def prepare_agent_workspace(
        self: AgentServiceContext,
        *,
        game_title: str,
        output_dir: Path,
        command_codes: set[int] | None,
        default_command_codes_override: list[int] | None = None,
    ) -> AgentReport:
        """在同父目录暂存完整工作区，再一次性发布到目标目录。"""
        try:
            publish_target = _WorkspacePublishTarget.create(output_dir)
        except _WorkspacePublishTargetError as error:
            return AgentReport.from_parts(
                errors=[issue(error.code, error.message)],
                warnings=[],
                summary={"workspace": str(error.target_dir)},
                details={},
            )

        try:
            async with await open_game_for_mutation(self.game_registry, game_title) as session:
                report = await WorkspaceAgentMixin._prepare_agent_workspace_contents(
                    self,
                    session=session,
                    game_title=game_title,
                    target_dir=publish_target.staging_dir,
                    published_dir=publish_target.target_dir,
                    command_codes=command_codes,
                    default_command_codes_override=default_command_codes_override,
                )
                if report.status == "error":
                    return report
                try:
                    publish_target.publish()
                except _WorkspacePublishTargetError as error:
                    return AgentReport.from_parts(
                        errors=[issue(error.code, error.message)],
                        warnings=[],
                        summary={"workspace": str(error.target_dir)},
                        details={},
                    )
                return report
        finally:
            publish_target.cleanup()

    @staticmethod
    async def _prepare_agent_workspace_contents(
        context: AgentServiceContext,
        *,
        session: TargetGameSession,
        game_title: str,
        target_dir: Path,
        published_dir: Path,
        command_codes: set[int] | None,
        default_command_codes_override: list[int] | None = None,
    ) -> AgentReport:
        """只向服务创建的空暂存目录写入完整工作区。"""
        setting = load_setting(
            context.setting_path,
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
        )
        game_data = await context._load_translation_source_game_data(
            session,
            include_writable_copies=False,
        )
        terminology_registry = await session.read_terminology_registry()
        terminology_glossary = await session.read_terminology_glossary()
        placeholder_records = await session.read_placeholder_rules()
        structured_placeholder_records = await session.read_structured_placeholder_rules()
        custom_rules = tuple(
            CustomPlaceholderRule.create(
                pattern_text=record.pattern_text,
                placeholder_template=record.placeholder_template,
            )
            for record in placeholder_records
        )
        structured_rules = tuple(
            StructuredPlaceholderRule.create(
                rule_name=record.rule_name,
                rule_type=record.rule_type,
                pattern_text=record.pattern_text,
                translatable_group=record.translatable_group,
                protected_groups=dict(record.protected_groups),
            )
            for record in structured_placeholder_records
        )
        text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=custom_rules,
            structured_placeholder_rules=structured_rules,
        )
        analysis_context = await context._build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
            placeholder_rules=placeholder_records,
            structured_placeholder_rules=structured_placeholder_records,
        )
        plugin_rules = list(analysis_context.plugin_rules)
        stale_plugin_rule_count = len(analysis_context.stale_plugin_rules)
        note_tag_rules = list(analysis_context.note_tag_rules)
        event_rules = list(analysis_context.event_rules)
        plugin_source_rules = list(analysis_context.plugin_source_rules)
        mv_virtual_namebox_rules = list(analysis_context.mv_virtual_namebox_rules)
        translation_data_map = analysis_context.translation_data_map
        plugin_source_scan = analysis_context.plugin_source_scan
        scan_error = _plugin_source_scan_read_error_issue(plugin_source_scan)
        if scan_error is not None:
            return AgentReport.from_parts(
                errors=[scan_error],
                warnings=[],
                summary={
                    "game": game_title,
                    "workspace": str(published_dir),
                    **plugin_source_scan.risk.to_json_object(),
                },
                details=plugin_source_scan.risk_report_json(),
            )
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=plugin_source_scan.risk.high_risk,
            candidate_count=len(plugin_source_scan.candidates),
            summary=cast(dict[str, object], plugin_source_scan.risk_report_json()),
        )
        plugin_source_review = collect_plugin_source_review_coverage(
            scan=plugin_source_scan,
            rule_records=plugin_source_rules,
        )
        plugin_source_extension_active = plugin_source_scan.risk.high_risk or bool(plugin_source_rules)
        source_snapshot_digest = _build_source_snapshot_digest(await session.read_source_snapshot_records())
        language_profile_fingerprint = _build_workspace_language_fingerprint(
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
            target_language=session.target_language,
        )
        terminology_summary = await export_terminology_artifacts(
            game_data=game_data,
            output_dir=target_dir / "terminology",
            mv_virtual_namebox_rule_records=mv_virtual_namebox_rules,
            text_rules=text_rules,
        )
        if terminology_registry is not None:
            exported_registry = await load_terminology_registry(field_terms_path=terminology_summary.field_terms_path)
            merged_registry = _merge_terminology_registry(
                exported_registry=exported_registry,
                stored_registry=terminology_registry,
            )
            await write_field_terms_json(terminology_summary.field_terms_path, merged_registry)
        if terminology_glossary is not None:
            await write_glossary_json(terminology_summary.glossary_path, terminology_glossary)
        terminology_subtasks_dir = target_dir / "terminology" / "subtasks"
        terminology_subtask_summary = await _write_terminology_subtask_files(
            field_terms_path=terminology_summary.field_terms_path,
            subtasks_dir=terminology_subtasks_dir,
        )
        plugins_path = target_dir / "plugins.json"
        await export_plugins_json_file(game_data=game_data, output_path=plugins_path)
        plugin_json_string_leaf_candidates_path = target_dir / "plugin-json-string-leaf-candidates.json"
        plugin_json_string_leaf_candidates: JsonArray = [
            {key: value for key, value in candidate.items()}
            for candidate in analysis_context.plugin_parameter_candidates
        ]
        await _write_json_value(plugin_json_string_leaf_candidates_path, plugin_json_string_leaf_candidates)
        plugin_rules_path = target_dir / "plugin-rules.json"
        await _write_json_value(plugin_rules_path, _plugin_rule_records_to_import_json(plugin_rules))
        plugin_source_risk_path = target_dir / "plugin-source-risk-report.json"
        plugin_source_risk_report = plugin_source_scan.risk_report_json()
        plugin_source_risk_report["source_view"] = GameFileView.TRANSLATION_SOURCE.value
        await _write_json_object(plugin_source_risk_path, plugin_source_risk_report)
        plugin_source_rules_path: Path | None = None
        if plugin_source_extension_active:
            plugin_source_rules_path = target_dir / "plugin-source-rules.json"
            await _write_json_value(
                plugin_source_rules_path,
                plugin_source_rule_records_to_import_json(plugin_source_rules),
            )
        note_tag_candidates_path = target_dir / "note-tag-candidates.json"
        note_tag_candidates: JsonArray = [
            {key: value for key, value in candidate.items()} for candidate in analysis_context.note_candidates
        ]
        note_tag_candidate_count = len(note_tag_candidates)
        note_tag_value_count = _sum_candidate_int_field(note_tag_candidates, "hit_count")
        note_tag_translatable_count = _sum_candidate_int_field(
            note_tag_candidates,
            "translatable_hit_count",
        )
        await _write_json_object(
            note_tag_candidates_path,
            {
                "status": "ok",
                "errors": [],
                "warnings": [],
                "summary": {
                    "candidate_tag_count": note_tag_candidate_count,
                    "candidate_value_count": note_tag_value_count,
                    "translatable_value_count": note_tag_translatable_count,
                },
                "details": {"candidates": note_tag_candidates},
            },
        )
        note_tag_rules_path = target_dir / "note-tag-rules.json"
        await _write_json_object(note_tag_rules_path, _note_tag_rule_records_to_import_json(note_tag_rules))
        default_command_codes = (
            None
            if command_codes is not None
            else (
                default_command_codes_override
                if default_command_codes_override is not None
                else setting.event_command_text.default_codes_for_engine(game_data.layout.engine_kind)
            )
        )
        effective_codes = resolve_event_command_codes(
            command_codes=command_codes, default_command_codes=default_command_codes
        )
        event_commands_path = target_dir / "event-commands.json"
        event_samples: JsonObject = {str(code): [] for code in sorted(effective_codes)}
        seen_event_samples: set[tuple[int, str]] = set()
        for event_entry in analysis_context.event_commands:
            command = event_entry.command
            if command.code not in effective_codes:
                continue
            sample_key = json.dumps(
                command.parameters,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            dedupe_key = (command.code, sample_key)
            if dedupe_key in seen_event_samples:
                continue
            seen_event_samples.add(dedupe_key)
            samples = ensure_json_array(event_samples[str(command.code)], f"event_samples.{command.code}")
            samples.append([parameter for parameter in command.parameters])
        await _write_json_object(event_commands_path, event_samples)
        event_command_count = sum(
            len(ensure_json_array(value, f"event_samples.{code}")) for code, value in event_samples.items()
        )
        event_rules_path = target_dir / "event-command-rules.json"
        await _write_json_object(event_rules_path, _event_command_rule_records_to_import_json(event_rules))
        placeholder_candidates = scan_placeholder_candidates(translation_data_map, text_rules)
        placeholder_report = AgentReport.from_parts(
            errors=[],
            warnings=[],
            summary={},
            details={"candidates": placeholder_candidates_to_details(placeholder_candidates)},
        )
        placeholder_path = target_dir / "placeholder-candidates.json"
        async with aiofiles.open(placeholder_path, "w", encoding="utf-8") as file:
            _ = await file.write(f"{placeholder_report.to_json_text()}\n")
        placeholder_rule_drafts = _build_custom_placeholder_rule_draft(placeholder_candidates)
        placeholder_rules_path = target_dir / "placeholder-rules.json"
        placeholder_rule_payload: JsonObject = (
            _placeholder_rule_records_to_import_json(placeholder_records)
            if placeholder_records
            else {key: value for key, value in placeholder_rule_drafts.items()}
        )
        await _write_json_object(placeholder_rules_path, placeholder_rule_payload)
        structured_placeholder_rules_path = target_dir / STRUCTURED_PLACEHOLDER_RULES_FILE_NAME
        await _write_json_object(
            structured_placeholder_rules_path,
            _structured_placeholder_rule_records_to_import_json(structured_placeholder_records),
        )
        mv_virtual_namebox_candidates_path: Path | None = None
        mv_virtual_namebox_rules_path: Path | None = None
        mv_virtual_namebox_candidate_count = 0
        if game_data.layout.engine_kind == "mv":
            mv_virtual_namebox_candidates_path = target_dir / MV_VIRTUAL_NAMEBOX_CANDIDATES_FILE_NAME
            mv_candidates_payload: JsonObject = {
                "engine_kind": game_data.layout.engine_kind,
                "candidate_count": len(analysis_context.mv_virtual_namebox_candidates),
                "candidates": [candidate for candidate in analysis_context.mv_virtual_namebox_candidates],
            }
            mv_virtual_namebox_candidate_count = _summary_int(mv_candidates_payload, "candidate_count")
            await _write_json_object(mv_virtual_namebox_candidates_path, mv_candidates_payload)
            mv_virtual_namebox_rules_path = target_dir / MV_VIRTUAL_NAMEBOX_RULES_FILE_NAME
            await _write_json_object(
                mv_virtual_namebox_rules_path,
                mv_virtual_namebox_rule_records_to_import_json(mv_virtual_namebox_rules),
            )
        generated_summary: JsonObject = {
            "engine": game_data.layout.engine_label,
            "engine_kind": game_data.layout.engine_kind,
            "engine_version": game_data.layout.engine_version,
            "source_language": session.source_language,
            "additional_source_languages": list(session.additional_source_languages),
            "target_language": session.target_language,
            "content_root": _workspace_game_relative_path(
                path=game_data.layout.content_root,
                game_root=game_data.layout.game_root,
            ),
            "data_dir": _workspace_game_relative_path(
                path=game_data.layout.data_dir,
                game_root=game_data.layout.game_root,
            ),
            "event_command_codes": list(sorted(effective_codes)),
            "speaker_entry_count": terminology_summary.speaker_entry_count,
            "map_entry_count": terminology_summary.map_entry_count,
            "terminology_entry_count": terminology_summary.entry_count,
            "terminology_database_entry_count": terminology_summary.database_entry_count,
            "terminology_subtask_count": len(TERMINOLOGY_SUBTASK_GROUPS),
            "glossary_term_count": terminology_glossary.term_count() if terminology_glossary is not None else 0,
            "plugin_count": len(game_data.plugins_js),
            "plugin_json_string_leaf_candidate_count": len(plugin_json_string_leaf_candidates),
            "plugin_rule_count": sum(len(rule.path_templates) for rule in plugin_rules),
            "plugin_source_candidate_count": len(plugin_source_scan.candidates),
            "plugin_source_high_risk": plugin_source_scan.risk.high_risk,
            "stale_plugin_rule_count": stale_plugin_rule_count,
            "note_tag_candidate_count": note_tag_candidate_count,
            "note_tag_rule_count": sum(len(rule.tag_names) for rule in note_tag_rules),
            "event_command_count": event_command_count,
            "event_command_rule_count": sum(len(rule.path_templates) for rule in event_rules),
            "placeholder_rule_count": len(placeholder_records),
            "placeholder_rule_draft_count": len(placeholder_rule_drafts),
            "structured_placeholder_rule_count": len(structured_placeholder_records),
            "mv_virtual_namebox_candidate_count": mv_virtual_namebox_candidate_count,
            "mv_virtual_namebox_rule_count": len(mv_virtual_namebox_rules),
        }
        if plugin_source_extension_active:
            generated_summary.update(
                {
                    "plugin_source_rule_count": sum(len(rule.selectors) for rule in plugin_source_rules),
                    "plugin_source_excluded_selector_count": sum(
                        len(rule.excluded_selectors) for rule in plugin_source_rules
                    ),
                    "plugin_source_reviewed_selector_count": plugin_source_review.reviewed_selector_count,
                    "plugin_source_unreviewed_count": len(plugin_source_review.unreviewed_candidates),
                }
            )
        manifest_paths: list[Path] = [
            terminology_summary.field_terms_path,
            terminology_summary.glossary_path,
            plugins_path,
            plugin_json_string_leaf_candidates_path,
            plugin_rules_path,
            plugin_source_risk_path,
            note_tag_candidates_path,
            note_tag_rules_path,
            event_commands_path,
            event_rules_path,
            placeholder_path,
            placeholder_rules_path,
            structured_placeholder_rules_path,
        ]
        if plugin_source_rules_path is not None:
            manifest_paths.append(plugin_source_rules_path)
        if mv_virtual_namebox_candidates_path is not None:
            manifest_paths.append(mv_virtual_namebox_candidates_path)
        if mv_virtual_namebox_rules_path is not None:
            manifest_paths.append(mv_virtual_namebox_rules_path)
        for directory in (
            terminology_summary.contexts_dir,
            terminology_subtasks_dir,
        ):
            manifest_paths.extend(sorted(directory.rglob("*")))
            manifest_paths.append(directory)
        manifest_paths.append(target_dir / "terminology")
        manifest_files: JsonArray = [
            relative_path
            for relative_path in sorted({path.resolve().relative_to(target_dir).as_posix() for path in manifest_paths})
        ]
        manifest: JsonObject = {
            "contract_version": 2,
            "game_id": session.game_id,
            "engine_kind": game_data.layout.engine_kind,
            "source_snapshot_digest": source_snapshot_digest,
            "language_profile": {
                "primary": session.source_language,
                "additional": list(session.additional_source_languages),
                "target": session.target_language,
                "fingerprint": language_profile_fingerprint,
            },
            "files": manifest_files,
            "generated": generated_summary,
            "layout": {
                "engine": game_data.layout.engine_label,
                "engine_kind": game_data.layout.engine_kind,
                "engine_version": game_data.layout.engine_version,
                "game_root": ".",
                "content_root": _workspace_game_relative_path(
                    path=game_data.layout.content_root,
                    game_root=game_data.layout.game_root,
                ),
                "data_dir": _workspace_game_relative_path(
                    path=game_data.layout.data_dir,
                    game_root=game_data.layout.game_root,
                ),
                "js_dir": _workspace_game_relative_path(
                    path=game_data.layout.js_dir,
                    game_root=game_data.layout.game_root,
                ),
                "plugins_path": _workspace_game_relative_path(
                    path=game_data.layout.plugins_path,
                    game_root=game_data.layout.game_root,
                ),
            },
            "workflow": _agent_workflow_manifest(
                engine_kind=game_data.layout.engine_kind,
                terminology_subtask_summary=terminology_subtask_summary,
            ),
        }
        manifest_path = target_dir / "manifest.json"
        async with aiofiles.open(manifest_path, "w", encoding="utf-8") as file:
            _ = await file.write(f"{json.dumps(manifest, ensure_ascii=False, indent=2)}\n")
        return AgentReport.from_parts(
            errors=[],
            warnings=[],
            summary={
                **generated_summary,
                "workspace": str(published_dir),
                "manifest": str(published_dir / "manifest.json"),
            },
            details={"manifest": manifest},
        )

    async def validate_agent_workspace(
        self: AgentServiceContext,
        *,
        game_title: str,
        workspace: Path,
        callbacks: QualityProgressCallbacks | None = None,
    ) -> AgentReport:
        """检查 Agent 临时工作区里的可导入文件。"""
        set_progress, advance_progress, set_status = callbacks or _noop_quality_progress_callbacks()
        set_progress(0, 12)
        set_status("读取工作区清单")
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        details: JsonObject = {}
        field_terms_path = workspace / "terminology" / "field-terms.json"
        glossary_path = workspace / "terminology" / "glossary.json"
        plugin_rules_path = workspace / "plugin-rules.json"
        plugin_source_rules_path = workspace / "plugin-source-rules.json"
        note_tag_rules_path = workspace / "note-tag-rules.json"
        event_rules_path = workspace / "event-command-rules.json"
        mv_virtual_namebox_rules_path = workspace / MV_VIRTUAL_NAMEBOX_RULES_FILE_NAME
        placeholder_rules_path = workspace / "placeholder-rules.json"
        structured_placeholder_rules_path = workspace / STRUCTURED_PLACEHOLDER_RULES_FILE_NAME
        placeholder_rules_text: str | None = None
        workspace_custom_rules: tuple[CustomPlaceholderRule, ...] | None = None
        placeholder_rules_parse_error: Exception | None = None
        if placeholder_rules_path.exists():
            async with aiofiles.open(placeholder_rules_path, "r", encoding="utf-8") as file:
                placeholder_rules_text = await file.read()
            try:
                workspace_custom_rules = load_custom_placeholder_rules_text(placeholder_rules_text)
            except Exception as error:
                placeholder_rules_parse_error = error

        structured_placeholder_rules_text: str | None = None
        workspace_structured_rules: tuple[StructuredPlaceholderRule, ...] | None = None
        structured_placeholder_rules_parse_error: Exception | None = None
        if structured_placeholder_rules_path.exists():
            async with aiofiles.open(structured_placeholder_rules_path, "r", encoding="utf-8") as file:
                structured_placeholder_rules_text = await file.read()
            try:
                workspace_structured_rules = load_structured_placeholder_rules_text(structured_placeholder_rules_text)
            except Exception as error:
                structured_placeholder_rules_parse_error = error
        event_command_codes, event_command_codes_issue = await _read_workspace_event_command_codes(workspace)
        if event_command_codes_issue is not None:
            errors.append(event_command_codes_issue)
        advance_progress(1)
        async with await self.game_registry.open_game(game_title) as session:
            set_status("加载翻译源视图")
            setting = load_setting(
                self.setting_path,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            game_data = await self._load_translation_source_game_data(
                session,
                include_writable_copies=False,
            )
            manifest_binding_issue = await _validate_workspace_manifest_binding(
                workspace=workspace,
                session=session,
                game_data=game_data,
            )
            if manifest_binding_issue is not None:
                return AgentReport.from_parts(
                    errors=[*errors, manifest_binding_issue],
                    warnings=warnings,
                    summary={"workspace": str(workspace)},
                    details=details,
                )
            advance_progress(1)
            set_status("解析规则上下文")
            mv_virtual_namebox_rule_records = await session.read_mv_virtual_namebox_rules()
            custom_rules = await self._resolve_custom_rules(
                session=session,
                custom_placeholder_rules_text=None,
            )
            structured_rules = await self._resolve_structured_rules(session=session)
            text_rules = TextRules.from_setting(
                setting.text_rules,
                custom_placeholder_rules=custom_rules,
                structured_placeholder_rules=structured_rules,
            )
            advance_progress(1)
            set_status("抽取当前文本范围")
            analysis_context = await self._build_game_analysis_context(
                session=session,
                game_data=game_data,
                text_rules=text_rules,
                placeholder_rules=[
                    PlaceholderRuleRecord(
                        pattern_text=rule.pattern_text,
                        placeholder_template=rule.placeholder_template,
                    )
                    for rule in custom_rules
                ],
                structured_placeholder_rules=[
                    StructuredPlaceholderRuleRecord(
                        rule_name=rule.rule_name,
                        rule_type=rule.rule_type,
                        pattern_text=rule.pattern_text,
                        translatable_group=rule.translatable_group,
                        protected_groups=dict(rule.protected_groups),
                    )
                    for rule in structured_rules
                ],
            )
            workspace_placeholder_analysis: _WorkspacePlaceholderAnalysis | None = None
            workspace_placeholder_analysis_error: Exception | None = None
            if workspace_custom_rules is not None and workspace_structured_rules is not None:
                try:
                    workspace_placeholder_analysis = _build_workspace_placeholder_analysis(
                        analysis_context=analysis_context,
                        setting_text_rules=setting.text_rules,
                        custom_rules=workspace_custom_rules,
                        structured_rules=workspace_structured_rules,
                    )
                except Exception as error:
                    workspace_placeholder_analysis_error = error
            advance_progress(1)
            set_status("扫描插件源码")
            plugin_source_scan = analysis_context.plugin_source_scan
            plugin_source_required = plugin_source_scan.risk.high_risk
            advance_progress(1)
            set_status("读取已保存译文和空规则复核状态")
            stored_plugin_source_rules = list(analysis_context.plugin_source_rules)
            plugin_source_started = bool(stored_plugin_source_rules)
            translated_paths = set(analysis_context.translated_item_index)
            review_event_command_codes = event_command_codes
            if review_event_command_codes is None:
                review_event_command_codes = resolve_event_command_codes(
                    command_codes=None,
                    default_command_codes=setting.event_command_text.default_codes_for_engine(
                        game_data.layout.engine_kind
                    ),
                )
            empty_rule_issues = await _read_empty_rule_review_issues(
                session=session,
                game_data=game_data,
                event_command_codes=event_command_codes,
                event_command_scope_hash=event_command_rule_scope_hash_for_snapshots(
                    command_snapshots=analysis_context.event_command_snapshots,
                    command_codes=review_event_command_codes,
                ),
                note_tag_scope_hash=note_tag_rule_scope_hash_for_candidates(
                    [{key: value for key, value in candidate.items()} for candidate in analysis_context.note_candidates]
                ),
                plugin_source_scope_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
                mv_virtual_namebox_scope_hash_value=mv_virtual_namebox_rule_scope_hash(
                    [candidate for candidate in analysis_context.mv_virtual_namebox_candidates]
                ),
            )
            placeholder_empty_issue: AgentIssue | None = None
            if workspace_placeholder_analysis is not None and not workspace_placeholder_analysis.custom_rules:
                placeholder_empty_issue = await _empty_rule_review_issue(
                    session=session,
                    rule_domain=PLACEHOLDER_RULE_DOMAIN,
                    current_scope_hash=normal_placeholder_scope_hash_from_analysis(
                        workspace_placeholder_analysis.placeholder_candidates
                    ),
                    unconfirmed_code="placeholder_rules_empty_unconfirmed",
                    stale_code="placeholder_rules_empty_confirmation_stale",
                    label="普通占位符规则",
                )
            structured_placeholder_empty_issue: AgentIssue | None = None
            if workspace_placeholder_analysis is not None and not workspace_placeholder_analysis.structured_rules:
                structured_placeholder_empty_issue = await _empty_rule_review_issue(
                    session=session,
                    rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
                    current_scope_hash=structured_placeholder_scope_hash_from_analysis(
                        workspace_placeholder_analysis.structured_candidates
                    ),
                    unconfirmed_code="structured_placeholder_rules_empty_unconfirmed",
                    stale_code="structured_placeholder_rules_empty_confirmation_stale",
                    label="结构化占位符规则",
                )
            advance_progress(1)
        set_status("校验术语文件")
        if field_terms_path.exists():
            registry: TerminologyRegistry | None = None
            try:
                registry = await load_terminology_registry(field_terms_path=field_terms_path)
                expected_registry, _speaker_contexts, _database_contexts = TerminologyExtraction(
                    game_data=game_data,
                    mv_virtual_namebox_rule_records=mv_virtual_namebox_rule_records,
                    text_rules=text_rules,
                ).extract_registry_and_contexts()
                _validate_terminology_registry_shape(
                    imported_registry=registry,
                    expected_registry=expected_registry,
                    errors=errors,
                )
            except Exception as error:
                errors.append(
                    issue("terminology_validate_failed", f"字段译名表结构校验失败: {type(error).__name__}: {error}")
                )
            if registry is not None:
                terminology_issues = _validate_terminology_registry(registry)
                errors.extend(
                    issue_item
                    for issue_item in terminology_issues
                    if issue_item.code == "terminology_empty_translation"
                )
                warnings.extend(
                    issue_item
                    for issue_item in terminology_issues
                    if issue_item.code != "terminology_empty_translation"
                )
                details["terminology"] = {
                    "entry_count": registry.total_entry_count(),
                    "filled_count": registry.filled_entry_count(),
                    "speaker_count": len(registry.speaker_names),
                    "map_count": len(registry.map_display_names),
                    "duplicate_translation_samples": _collect_terminology_duplicate_translation_samples(registry),
                }
        else:
            errors.append(issue("terminology_missing", "工作区缺少 terminology/field-terms.json"))
            registry = None
        if glossary_path.exists():
            glossary: TerminologyGlossary | None = None
            try:
                glossary = await load_terminology_glossary(glossary_path=glossary_path)
            except Exception as error:
                errors.append(
                    issue("glossary_validate_failed", f"正文术语表结构校验失败: {type(error).__name__}: {error}")
                )
            if glossary is not None:
                details["glossary"] = {
                    "term_count": glossary.term_count(),
                }
        else:
            errors.append(issue("glossary_missing", "工作区缺少 terminology/glossary.json"))
            glossary = None
        if registry is not None or glossary is not None:
            errors.extend(
                issue("terminology_bundle_invalid", message)
                for message in collect_terminology_bundle_errors(registry=registry, glossary=glossary)
            )
        advance_progress(1)
        set_status("校验插件规则")
        if plugin_rules_path.exists():
            async with aiofiles.open(plugin_rules_path, "r", encoding="utf-8") as file:
                plugin_report = _validate_workspace_plugin_rules(
                    rules_text=await file.read(),
                    game_data=game_data,
                    text_rules=text_rules,
                    translated_paths=translated_paths,
                    plugin_index=analysis_context.analysis_index.plugin_parameters,
                )
            errors.extend(plugin_report.errors)
            warnings.extend(plugin_report.warnings)
            details["plugin_rules"] = plugin_report.details
            if _summary_int(plugin_report.summary, "rule_count") == 0:
                plugin_empty_issue = empty_rule_issues["plugin_rules"]
                if plugin_empty_issue is not None:
                    errors.append(plugin_empty_issue)
        else:
            errors.append(issue("plugin_rules_missing", "工作区缺少 plugin-rules.json"))
        if plugin_source_rules_path.exists():
            async with aiofiles.open(plugin_source_rules_path, "r", encoding="utf-8") as file:
                plugin_source_report = _validate_workspace_plugin_source_rules(
                    rules_text=await file.read(),
                    game_data=game_data,
                    text_rules=text_rules,
                    scan=plugin_source_scan,
                    translated_paths=translated_paths,
                )
            errors.extend(plugin_source_report.errors)
            plugin_source_warnings = plugin_source_report.warnings
            plugin_source_reviewed_count = _summary_int(plugin_source_report.summary, "reviewed_selector_count")
            promoted_plugin_source_warnings: list[AgentIssue] = []
            kept_plugin_source_warnings: list[AgentIssue] = []
            for warning in plugin_source_warnings:
                if warning.code == "plugin_source_review_incomplete" and (
                    plugin_source_required or plugin_source_reviewed_count > 0
                ):
                    promoted_plugin_source_warnings.append(warning)
                else:
                    kept_plugin_source_warnings.append(warning)
            plugin_source_warnings = kept_plugin_source_warnings
            errors.extend(promoted_plugin_source_warnings)
            if not plugin_source_required and plugin_source_reviewed_count == 0:
                plugin_source_warnings = [
                    warning for warning in plugin_source_warnings if warning.code != "plugin_source_rules_empty"
                ]
            warnings.extend(plugin_source_warnings)
            details["plugin_source_rules"] = plugin_source_report.details
            if plugin_source_reviewed_count == 0:
                if plugin_source_required:
                    errors.append(
                        issue(
                            "plugin_source_rules_empty_high_risk",
                            "插件源码风险较高，但工作区没有保存任何已审查的插件源码 selector；请先完成插件源码 AST 审查",
                        )
                    )
                elif plugin_source_started:
                    errors.append(
                        issue(
                            "plugin_source_rules_empty_started",
                            "插件源码支线已有审查结果，但工作区没有保存任何插件源码 selector；请补全翻译或排除 selector",
                        )
                    )
        else:
            if plugin_source_required or plugin_source_started:
                errors.append(issue("plugin_source_rules_missing", "工作区缺少 plugin-source-rules.json"))
        advance_progress(1)
        set_status("校验 Note 和事件规则")
        if note_tag_rules_path.exists():
            async with aiofiles.open(note_tag_rules_path, "r", encoding="utf-8") as file:
                note_tag_report = _validate_workspace_note_tag_rules(
                    rules_text=await file.read(),
                    game_data=game_data,
                    text_rules=text_rules,
                    translated_paths=translated_paths,
                    note_sources=analysis_context.analysis_index.note_sources,
                )
            errors.extend(note_tag_report.errors)
            warnings.extend(note_tag_report.warnings)
            details["note_tag_rules"] = note_tag_report.details
            if _summary_int(note_tag_report.summary, "tag_count") == 0:
                note_tag_empty_issue = empty_rule_issues["note_tag_rules"]
                if note_tag_empty_issue is not None:
                    errors.append(note_tag_empty_issue)
        else:
            errors.append(issue("note_tag_rules_missing", "工作区缺少 note-tag-rules.json"))
        if event_rules_path.exists():
            async with aiofiles.open(event_rules_path, "r", encoding="utf-8") as file:
                event_report = _validate_workspace_event_command_rules(
                    rules_text=await file.read(),
                    game_data=game_data,
                    text_rules=text_rules,
                    translated_paths=translated_paths,
                    command_index=analysis_context.analysis_index.event_commands,
                )
            errors.extend(event_report.errors)
            warnings.extend(event_report.warnings)
            details["event_command_rules"] = event_report.details
            if _summary_int(event_report.summary, "path_rule_count") == 0:
                event_empty_issue = empty_rule_issues["event_command_rules"]
                if event_empty_issue is not None:
                    errors.append(event_empty_issue)
        else:
            errors.append(issue("event_command_rules_missing", "工作区缺少 event-command-rules.json"))
        advance_progress(1)
        set_status("校验名字框和普通占位符规则")
        if game_data.layout.engine_kind == "mv":
            if mv_virtual_namebox_rules_path.exists():
                async with aiofiles.open(mv_virtual_namebox_rules_path, "r", encoding="utf-8") as file:
                    mv_namebox_report = _validate_workspace_mv_virtual_namebox_rules(
                        rules_text=await file.read(),
                        game_data=game_data,
                        existing_records=mv_virtual_namebox_rule_records,
                        candidates=analysis_context.mv_virtual_namebox_candidate_index,
                    )
                errors.extend(mv_namebox_report.errors)
                warnings.extend(mv_namebox_report.warnings)
                details["mv_virtual_namebox_rules"] = mv_namebox_report.details
                if _summary_int(mv_namebox_report.summary, "rule_count") == 0:
                    mv_namebox_empty_issue = empty_rule_issues["mv_virtual_namebox_rules"]
                    if mv_namebox_empty_issue is not None:
                        errors.append(mv_namebox_empty_issue)
            else:
                errors.append(
                    issue("mv_virtual_namebox_rules_missing", f"MV 工作区缺少 {MV_VIRTUAL_NAMEBOX_RULES_FILE_NAME}")
                )
        if placeholder_rules_path.exists():
            placeholder_failure = placeholder_rules_parse_error or workspace_placeholder_analysis_error
            if placeholder_failure is not None or workspace_placeholder_analysis is None:
                failure = placeholder_failure or RuntimeError("工作区占位符分析事实未生成")
                errors.extend(
                    [
                        issue(
                            "placeholder_rules_invalid",
                            f"自定义占位符规则不可用: {type(failure).__name__}: {failure}",
                        ),
                        issue(
                            "placeholder_coverage_scan_failed",
                            f"占位符覆盖扫描失败: {type(failure).__name__}: {failure}",
                        ),
                    ]
                )
            else:
                placeholder_report = _build_placeholder_rule_validation_report(
                    source_label="工作区 placeholder-rules.json",
                    custom_rules=workspace_placeholder_analysis.custom_rules,
                    text_rules=workspace_placeholder_analysis.text_rules,
                    sample_texts=workspace_placeholder_analysis.placeholder_sample_texts,
                )
                errors.extend(placeholder_report.errors)
                warnings.extend(placeholder_report.warnings)
                details["placeholder_rules"] = placeholder_report.details
                if not workspace_placeholder_analysis.custom_rules and placeholder_empty_issue is not None:
                    errors.append(placeholder_empty_issue)
                placeholder_coverage_report = _build_workspace_placeholder_coverage_report(
                    analysis=workspace_placeholder_analysis,
                )
                details["placeholder_coverage"] = {
                    "summary": placeholder_coverage_report.summary,
                    "details": placeholder_coverage_report.details,
                }
                uncovered_value = placeholder_coverage_report.summary.get("uncovered_count")
                if isinstance(uncovered_value, bool) or not isinstance(uncovered_value, int):
                    errors.append(issue("placeholder_coverage_invalid", "占位符候选扫描缺少有效的 uncovered_count"))
                elif uncovered_value > 0:
                    errors.append(
                        issue(
                            "placeholder_coverage_uncovered",
                            f"还有 {uncovered_value} 个当前正文会使用但未被规则覆盖的游戏控制符",
                        )
                    )
        else:
            errors.append(issue("placeholder_rules_missing", "工作区缺少 placeholder-rules.json"))
        advance_progress(1)
        set_status("校验结构化占位符规则")
        if structured_placeholder_rules_path.exists():
            structured_failure = structured_placeholder_rules_parse_error or workspace_placeholder_analysis_error
            if structured_failure is not None or workspace_placeholder_analysis is None:
                failure = structured_failure or RuntimeError("工作区结构化占位符分析事实未生成")
                errors.extend(
                    [
                        issue(
                            "structured_placeholder_rules_invalid",
                            f"结构化占位符规则不可用: {type(failure).__name__}: {failure}",
                        ),
                        issue(
                            "structured_placeholder_coverage_scan_failed",
                            f"结构化占位符覆盖扫描失败: {type(failure).__name__}: {failure}",
                        ),
                    ]
                )
            else:
                structured_placeholder_report = _validate_workspace_structured_placeholder_rules(
                    game_title=game_title,
                    analysis=workspace_placeholder_analysis,
                )
                errors.extend(structured_placeholder_report.errors)
                warnings.extend(
                    warning
                    for warning in structured_placeholder_report.warnings
                    if warning.code
                    not in {"structured_placeholder_rules_empty", "structured_placeholder_samples_empty"}
                )
                details["structured_placeholder_rules"] = structured_placeholder_report.details
                if (
                    not workspace_placeholder_analysis.structured_rules
                    and structured_placeholder_empty_issue is not None
                ):
                    errors.append(structured_placeholder_empty_issue)
                structured_placeholder_coverage_report = _build_workspace_structured_placeholder_coverage_report(
                    game_title=game_title,
                    analysis=workspace_placeholder_analysis,
                )
                errors.extend(structured_placeholder_coverage_report.errors)
                warnings.extend(structured_placeholder_coverage_report.warnings)
                details["structured_placeholder_coverage"] = {
                    "summary": structured_placeholder_coverage_report.summary,
                    "details": structured_placeholder_coverage_report.details,
                }
                uncovered_value = structured_placeholder_coverage_report.summary.get("uncovered_count")
                if isinstance(uncovered_value, bool) or not isinstance(uncovered_value, int):
                    errors.append(
                        issue(
                            "structured_placeholder_coverage_invalid", "结构化占位符候选扫描缺少有效的 uncovered_count"
                        )
                    )
                elif uncovered_value > 0:
                    errors.append(
                        issue(
                            "structured_placeholder_coverage_uncovered",
                            f"还有 {uncovered_value} 个当前正文会使用但未被结构化规则覆盖的协议外壳候选",
                        )
                    )
        else:
            errors.append(
                issue("structured_placeholder_rules_missing", f"工作区缺少 {STRUCTURED_PLACEHOLDER_RULES_FILE_NAME}")
            )
        advance_progress(1)
        set_status("汇总工作区校验报告")
        advance_progress(1)
        return AgentReport.from_parts(
            errors=errors, warnings=warnings, summary={"workspace": str(workspace)}, details=details
        )

    async def cleanup_agent_workspace(self: AgentServiceContext, *, workspace: Path) -> AgentReport:
        """按 manifest 删除 Agent 临时工作区文件。"""
        if _path_is_link(workspace):
            return AgentReport.from_parts(
                errors=[issue("manifest_path_unsafe", "工作区根目录是符号链接或目录联接，拒绝自动清理")],
                warnings=[],
                summary={"workspace": str(workspace)},
                details={},
            )
        workspace_root = workspace.resolve()
        manifest_path = workspace_root / "manifest.json"
        if not manifest_path.is_file() or _path_is_link(manifest_path):
            return AgentReport.from_parts(
                errors=[issue("manifest_missing", "工作区缺少 manifest.json，拒绝自动清理")],
                warnings=[],
                summary={"workspace": str(workspace_root)},
                details={},
            )
        try:
            async with aiofiles.open(manifest_path, "r", encoding="utf-8") as file:
                raw_manifest = cast(object, json.loads(await file.read()))
            manifest = ensure_json_object(coerce_json_value(raw_manifest), "manifest")
            if manifest.get("contract_version") != 2:
                raise TypeError("manifest.contract_version 必须是 2")
            files_value = ensure_json_array(manifest.get("files"), "manifest.files")
        except Exception as error:
            return AgentReport.from_parts(
                errors=[issue("manifest_invalid", f"工作区 manifest 不可读取: {type(error).__name__}: {error}")],
                warnings=[],
                summary={"workspace": str(workspace_root)},
                details={},
            )

        validated_paths: list[Path] = []
        seen_relative_paths: set[str] = set()
        for raw_path in files_value:
            if not isinstance(raw_path, str):
                return AgentReport.from_parts(
                    errors=[issue("manifest_invalid", "manifest.files 的每一项都必须是相对路径字符串")],
                    warnings=[],
                    summary={"workspace": str(workspace_root)},
                    details={},
                )
            relative_path = Path(raw_path)
            if (
                not raw_path.strip()
                or relative_path.is_absolute()
                or bool(relative_path.drive)
                or relative_path.as_posix() == "."
                or ".." in relative_path.parts
                or relative_path.name == "manifest.json"
                or any(":" in part for part in relative_path.parts)
            ):
                return AgentReport.from_parts(
                    errors=[issue("manifest_path_unsafe", f"manifest 包含不安全路径，拒绝清理: {raw_path}")],
                    warnings=[],
                    summary={"workspace": str(workspace_root)},
                    details={},
                )
            normalized_relative = relative_path.as_posix()
            if normalized_relative in seen_relative_paths:
                return AgentReport.from_parts(
                    errors=[issue("manifest_path_duplicate", f"manifest 包含重复路径，拒绝清理: {raw_path}")],
                    warnings=[],
                    summary={"workspace": str(workspace_root)},
                    details={},
                )
            seen_relative_paths.add(normalized_relative)
            lexical_path = workspace_root / relative_path
            try:
                resolved_path = lexical_path.resolve()
            except (OSError, ValueError) as error:
                return AgentReport.from_parts(
                    errors=[issue("manifest_path_unsafe", f"manifest 路径不可解析，拒绝清理: {raw_path}: {error}")],
                    warnings=[],
                    summary={"workspace": str(workspace_root)},
                    details={},
                )
            if not _is_path_inside(resolved_path, workspace_root) or _path_uses_symlink(
                path=lexical_path,
                workspace_root=workspace_root,
            ):
                return AgentReport.from_parts(
                    errors=[
                        issue("manifest_path_unsafe", f"manifest 路径越过工作区或经过符号链接，拒绝清理: {raw_path}")
                    ],
                    warnings=[],
                    summary={"workspace": str(workspace_root)},
                    details={},
                )
            validated_paths.append(lexical_path)

        deleted_count = 0
        warnings: list[AgentIssue] = []
        try:
            for path in sorted(validated_paths, key=lambda item: len(item.parts), reverse=True):
                if _path_uses_symlink(path=path, workspace_root=workspace_root):
                    raise OSError(f"待删除路径在校验后变成了链接: {path}")
                if not path.exists():
                    warnings.append(
                        issue(
                            "workspace_file_missing",
                            f"manifest 声明的文件已经不存在: {path.relative_to(workspace_root).as_posix()}",
                        )
                    )
                    continue
                if path.is_dir():
                    path.rmdir()
                elif path.is_file():
                    path.unlink()
                else:
                    raise OSError(f"manifest 声明路径不是普通文件或目录: {path}")
                deleted_count += 1
            if _path_is_link(manifest_path):
                raise OSError("manifest.json 在清理期间变成了链接")
            manifest_path.unlink()
            deleted_count += 1
        except OSError as error:
            return AgentReport.from_parts(
                errors=[issue("workspace_cleanup_failed", f"清理工作区失败，manifest 已保留，可修复后重试: {error}")],
                warnings=warnings,
                summary={"workspace": str(workspace_root), "deleted_count": deleted_count},
                details={},
            )
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={"workspace": str(workspace_root), "deleted_count": deleted_count},
            details={},
        )


def _validate_workspace_mv_virtual_namebox_rules(
    *,
    rules_text: str,
    game_data: GameData,
    existing_records: list[MvVirtualNameboxRuleRecord],
    candidates: tuple[MvVirtualNameboxCandidate, ...],
) -> AgentReport:
    """复用工作区上下文校验 MV 虚拟名字框规则。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    details: JsonObject = {"rules": [], "matched_candidates": []}
    records: list[MvVirtualNameboxRuleRecord] = []
    candidate_count = 0
    matched_candidate_count = 0
    newly_matched_candidate_count = 0
    try:
        records = parse_mv_virtual_namebox_rule_import_text(rules_text)
        if game_data.layout.engine_kind != "mv":
            errors.append(issue("mv_virtual_namebox_rules_forbidden", "MV 虚拟名字框规则只允许 RPG Maker MV 游戏使用"))
            return AgentReport.from_parts(
                errors=errors,
                warnings=[],
                summary={
                    "rule_count": 0,
                    "candidate_count": 0,
                    "matched_candidate_count": 0,
                    "newly_matched_candidate_count": 0,
                },
                details=details,
            )
        candidate_details = mv_virtual_namebox_candidate_details_from_candidates(candidates)
        candidate_count = len(candidate_details)
        rule_errors, match_details = validate_mv_virtual_namebox_rules_against_candidates(
            game_data=game_data,
            records=records,
            candidates=candidates,
        )
        errors.extend(
            issue("mv_virtual_namebox_rules_invalid", _format_mv_namebox_rule_error(error_detail))
            for error_detail in rule_errors
        )
        matched_candidate_count = len(match_details)
        _existing_errors, existing_match_details = validate_mv_virtual_namebox_rules_against_candidates(
            game_data=game_data,
            records=existing_records,
            candidates=candidates,
        )
        existing_match_keys = _mv_namebox_match_keys(existing_match_details)
        newly_matched_candidates: JsonArray = [
            detail for detail in match_details if _mv_namebox_match_key(detail) not in existing_match_keys
        ]
        newly_matched_candidate_count = len(newly_matched_candidates)
        details = {
            "rules": mv_virtual_namebox_rule_records_to_import_json(records)["rules"],
            "matched_candidates": match_details,
            "newly_matched_candidates": newly_matched_candidates,
            "candidate_count": candidate_count,
        }
        if not records:
            warnings.append(issue("mv_virtual_namebox_rules_empty", "MV 虚拟名字框规则为空"))
        elif matched_candidate_count == 0 and candidate_count > 0:
            warnings.append(issue("mv_virtual_namebox_rules_no_hits", "MV 虚拟名字框规则没有命中任何候选"))
    except Exception as error:
        errors.append(
            issue("mv_virtual_namebox_rules_invalid", f"MV 虚拟名字框规则不可导入: {type(error).__name__}: {error}")
        )
        records = []
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "rule_count": len(records),
            "candidate_count": candidate_count,
            "matched_candidate_count": matched_candidate_count,
            "newly_matched_candidate_count": newly_matched_candidate_count,
        },
        details=details,
    )


def _validate_workspace_plugin_rules(
    *,
    rules_text: str,
    game_data: GameData,
    text_rules: TextRules,
    translated_paths: set[str],
    plugin_index: tuple[PluginParameterAnalysisEntry, ...],
) -> AgentReport:
    """复用工作区上下文校验插件参数规则。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    details: JsonObject = {"rules": []}
    try:
        import_file = parse_plugin_rule_import_text(rules_text)
        records = build_plugin_rule_records_from_import(
            game_data=game_data,
            import_file=import_file,
            text_rules=text_rules,
            plugin_index=plugin_index,
        )
        extracted_map = PluginTextExtraction(
            game_data,
            plugin_rule_records=records,
            text_rules=text_rules,
        ).extract_all_text_from_index(plugin_index)
        extracted_items = [
            item for translation_data in extracted_map.values() for item in translation_data.translation_items
        ]
        unwritable_items = _collect_write_protocol_unwritable_items(
            game_data=game_data,
            extracted_items=extracted_items,
        )
        if unwritable_items:
            errors.append(issue("plugin_rules_unwritable", f"插件规则存在 {len(unwritable_items)} 个不可写命中项"))
        unwritable_items_by_path = _json_items_by_location_path(unwritable_items)
        details["rules"] = [
            {
                "plugin_index": record.plugin_index,
                "plugin_name": record.plugin_name,
                "plugin_hash": record.plugin_hash,
                "path_count": len(record.path_templates),
                "paths": list(record.path_templates),
                **_build_rule_metric_detail(
                    record_items=record_items,
                    translated_paths=translated_paths,
                    unwritable_items_by_path=unwritable_items_by_path,
                ),
            }
            for record in records
            for record_items in [
                [
                    item
                    for item in extracted_items
                    if item.location_path.startswith(f"{PLUGINS_FILE_NAME}/{record.plugin_index}/")
                ]
            ]
        ]
        if not records:
            warnings.append(issue("plugin_rules_empty", "插件规则为空"))
        if records and not extracted_items:
            errors.append(issue("plugin_rules_no_hits", "插件规则没有提取到任何可翻译文本"))
    except Exception as error:
        errors.append(issue("plugin_rules_invalid", f"插件规则不可导入: {type(error).__name__}: {error}"))
        records = []
        extracted_items = []
        unwritable_items = []
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "plugin_count": len(records),
            "rule_count": sum(len(record.path_templates) for record in records),
            "hit_count": len(extracted_items),
            "extractable_count": len(extracted_items),
            "translated_count": sum(1 for item in extracted_items if item.location_path in translated_paths),
            "writable_count": len(extracted_items) - len(unwritable_items),
            "unwritable_count": len(unwritable_items),
        },
        details=details,
    )


def _validate_workspace_plugin_source_rules(
    *,
    rules_text: str,
    game_data: GameData,
    text_rules: TextRules,
    scan: PluginSourceScan,
    translated_paths: set[str],
) -> AgentReport:
    """复用工作区上下文校验插件源码规则，避免重新加载游戏并重扫 AST。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    details: JsonObject = {"rules": []}
    records: list[PluginSourceTextRuleRecord] = []
    extracted_items: list[TranslationItem] = []
    unwritable_items: JsonArray = []
    unreviewed_count = 0
    try:
        import_file = parse_plugin_source_rule_import_text(rules_text)
        records = build_plugin_source_rule_records_from_import(
            import_file=import_file,
            scan=scan,
        )
        review = collect_plugin_source_review_coverage(scan=scan, rule_records=records)
        unreviewed_count = len(review.unreviewed_candidates)
        extracted_map = PluginSourceTextExtraction(
            game_data,
            rule_records=records,
            text_rules=text_rules,
            scan=scan,
        ).extract_all_text()
        extracted_items = [
            item for translation_data in extracted_map.values() for item in translation_data.translation_items
        ]
        unwritable_items = _collect_plugin_source_unwritable_items(
            game_data=game_data,
            extracted_items=extracted_items,
        )
        if unwritable_items:
            errors.append(
                issue(
                    "plugin_source_write_back_unwritable",
                    f"插件源码规则存在 {len(unwritable_items)} 个不可写命中项",
                )
            )
        unwritable_items_by_path = _json_items_by_location_path(unwritable_items)
        details["rules"] = [
            {
                "file": record.file_name,
                "file_hash": record.file_hash,
                "selector_count": len(record.selectors),
                "excluded_selector_count": len(record.excluded_selectors),
                "reviewed_selector_count": len(record.selectors) + len(record.excluded_selectors),
                "selectors": list(record.selectors),
                "excluded_selectors": list(record.excluded_selectors),
                **_build_rule_metric_detail(
                    record_items=record_items,
                    translated_paths=translated_paths,
                    unwritable_items_by_path=unwritable_items_by_path,
                ),
            }
            for record in records
            for record_items in [
                [item for item in extracted_items if item.location_path.startswith(f"js/plugins/{record.file_name}/")]
            ]
        ]
        if not records:
            warnings.append(issue("plugin_source_rules_empty", "插件源码规则为空"))
        excluded_selector_count = sum(len(record.excluded_selectors) for record in records)
        if records and not extracted_items and excluded_selector_count == 0:
            warnings.append(issue("plugin_source_rules_no_hits", "插件源码规则没有提取到任何可翻译文本"))
        if unreviewed_count:
            review_issue = issue(
                "plugin_source_review_incomplete",
                f"插件源码规则还有 {unreviewed_count} 个候选未归入翻译或排除",
            )
            if scan.risk.high_risk or records:
                errors.append(review_issue)
            else:
                warnings.append(review_issue)
    except Exception as error:
        errors.append(issue("plugin_source_rules_invalid", f"插件源码规则不可导入: {type(error).__name__}: {error}"))
        records = []
        extracted_items = []
        unwritable_items = []
        unreviewed_count = 0
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "file_count": len(records),
            "selector_count": sum(len(record.selectors) for record in records),
            "excluded_selector_count": sum(len(record.excluded_selectors) for record in records),
            "reviewed_selector_count": sum(
                len(record.selectors) + len(record.excluded_selectors) for record in records
            ),
            "unreviewed_selector_count": unreviewed_count,
            "hit_count": len(extracted_items),
            "extractable_count": len(extracted_items),
            "translated_count": sum(1 for item in extracted_items if item.location_path in translated_paths),
            "writable_count": len(extracted_items) - len(unwritable_items),
            "unwritable_count": len(unwritable_items),
        },
        details=details,
    )


def _validate_workspace_note_tag_rules(
    *,
    rules_text: str,
    game_data: GameData,
    text_rules: TextRules,
    translated_paths: set[str],
    note_sources: tuple[NoteTagSource, ...],
) -> AgentReport:
    """复用工作区上下文校验 Note 标签规则。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    details: JsonObject = {"rules": []}
    try:
        import_file = parse_note_tag_rule_import_text(rules_text)
        records = build_note_tag_rule_records_from_import(
            game_data=game_data,
            import_file=import_file,
            text_rules=text_rules,
            note_sources=note_sources,
        )
        extracted_map = NoteTagTextExtraction(
            game_data=game_data,
            rule_records=records,
            text_rules=text_rules,
        ).extract_all_text_from_sources(note_sources)
        extracted_items = [
            item for translation_data in extracted_map.values() for item in translation_data.translation_items
        ]
        unwritable_items = _collect_write_protocol_unwritable_items(
            game_data=game_data,
            extracted_items=extracted_items,
        )
        try:
            _preview_event_command_write_back(
                game_data=game_data,
                extracted_items=extracted_items,
                text_rules=text_rules,
            )
            details["write_back_preview"] = {
                "checked_item_count": len(extracted_items),
                "status": "ok",
            }
        except Exception as error:
            errors.append(
                issue(
                    "note_tag_write_back_invalid",
                    f"Note 标签规则命中项无法回写: {type(error).__name__}: {error}",
                )
            )
            details["write_back_preview"] = {
                "checked_item_count": len(extracted_items),
                "status": "error",
                "reason": f"{type(error).__name__}: {error}",
            }
        if unwritable_items:
            errors.append(
                issue("note_tag_write_back_unwritable", f"Note 标签规则存在 {len(unwritable_items)} 个不可写命中项")
            )
        unwritable_items_by_path = _json_items_by_location_path(unwritable_items)
        details["rules"] = [
            {
                "file_name": record.file_name,
                "tag_count": len(record.tag_names),
                "tag_names": list(record.tag_names),
                **_build_rule_metric_detail(
                    record_items=record_items,
                    translated_paths=translated_paths,
                    unwritable_items_by_path=unwritable_items_by_path,
                ),
            }
            for record in records
            for record_items in [
                [item for item in extracted_items if _note_tag_item_matches_rule(item=item, rule_record=record)]
            ]
        ]
        if not records:
            warnings.append(issue("note_tag_rules_empty", "Note 标签规则为空"))
    except Exception as error:
        errors.append(issue("note_tag_rules_invalid", f"Note 标签规则不可导入: {type(error).__name__}: {error}"))
        records = []
        extracted_items = []
        unwritable_items = []
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "file_count": len(records),
            "tag_count": sum(len(record.tag_names) for record in records),
            "hit_count": len(extracted_items),
            "extractable_count": len(extracted_items),
            "translated_count": sum(1 for item in extracted_items if item.location_path in translated_paths),
            "writable_count": len(extracted_items) - len(unwritable_items),
            "unwritable_count": len(unwritable_items),
        },
        details=details,
    )


def _validate_workspace_event_command_rules(
    *,
    rules_text: str,
    game_data: GameData,
    text_rules: TextRules,
    translated_paths: set[str],
    command_index: tuple[EventCommandAnalysisEntry, ...],
) -> AgentReport:
    """复用工作区上下文校验事件指令规则。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    details: JsonObject = {"rules": []}
    try:
        import_file = parse_event_command_rule_import_text(rules_text)
        records = build_event_command_rule_records_from_import(
            game_data=game_data,
            import_file=import_file,
            command_index=command_index,
        )
        extracted_map = EventCommandTextExtraction(
            game_data,
            rule_records=records,
            text_rules=text_rules,
        ).extract_all_text_from_index(command_index)
        extracted_items = [
            item for translation_data in extracted_map.values() for item in translation_data.translation_items
        ]
        unwritable_items = _collect_write_protocol_unwritable_items(
            game_data=game_data,
            extracted_items=extracted_items,
        )
        try:
            _preview_event_command_write_back(
                game_data=game_data,
                extracted_items=extracted_items,
                text_rules=text_rules,
            )
            details["write_back_preview"] = {
                "checked_item_count": len(extracted_items),
                "status": "ok",
            }
        except Exception as error:
            errors.append(
                issue(
                    "event_command_write_back_invalid",
                    f"事件指令规则命中项无法回写: {type(error).__name__}: {error}",
                )
            )
            details["write_back_preview"] = {
                "checked_item_count": len(extracted_items),
                "status": "error",
                "reason": f"{type(error).__name__}: {error}",
            }
        if unwritable_items:
            errors.append(
                issue("event_command_rules_unwritable", f"事件指令规则存在 {len(unwritable_items)} 个不可写命中项")
            )
        unwritable_items_by_path = _json_items_by_location_path(unwritable_items)
        rule_details: JsonArray = []
        for record in records:
            record_extracted_map = EventCommandTextExtraction(
                game_data,
                rule_records=[record],
                text_rules=text_rules,
            ).extract_all_text_from_index(command_index)
            record_items = [
                item
                for translation_data in record_extracted_map.values()
                for item in translation_data.translation_items
            ]
            rule_details.append(
                {
                    "command_code": record.command_code,
                    "match_count": len(record.parameter_filters),
                    "path_count": len(record.path_templates),
                    "paths": list(record.path_templates),
                    **_build_rule_metric_detail(
                        record_items=record_items,
                        translated_paths=translated_paths,
                        unwritable_items_by_path=unwritable_items_by_path,
                    ),
                }
            )
        details["rules"] = rule_details
        if not records:
            warnings.append(issue("event_command_rules_empty", "事件指令规则为空"))
        if records and not extracted_items:
            warnings.append(issue("event_command_rules_no_hits", "事件指令规则没有提取到任何可翻译文本"))
    except Exception as error:
        errors.append(issue("event_command_rules_invalid", f"事件指令规则不可导入: {type(error).__name__}: {error}"))
        records = []
        extracted_items = []
        unwritable_items = []
    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "rule_group_count": len(records),
            "path_rule_count": sum(len(record.path_templates) for record in records),
            "hit_count": len(extracted_items),
            "extractable_count": len(extracted_items),
            "translated_count": sum(1 for item in extracted_items if item.location_path in translated_paths),
            "writable_count": len(extracted_items) - len(unwritable_items),
            "unwritable_count": len(unwritable_items),
        },
        details=details,
    )


def _build_workspace_placeholder_analysis(
    *,
    analysis_context: GameAnalysisContext,
    setting_text_rules: TextRulesSetting,
    custom_rules: tuple[CustomPlaceholderRule, ...],
    structured_rules: tuple[StructuredPlaceholderRule, ...],
) -> _WorkspacePlaceholderAnalysis:
    """从单命令上下文构建一次工作区占位符分析事实。"""
    baseline_text_rules = TextRules.from_setting(setting_text_rules)
    baseline_scope = analysis_context.build_scope_for_text_rules(text_rules=baseline_text_rules)
    translation_data_map = baseline_scope.translation_data_map
    text_rules = TextRules.from_setting(
        setting_text_rules,
        custom_placeholder_rules=custom_rules,
        structured_placeholder_rules=structured_rules,
    )
    placeholder_candidates = analyze_placeholder_candidates(translation_data_map, text_rules)
    structured_candidates = analyze_structured_placeholder_candidates(translation_data_map, text_rules)
    return _WorkspacePlaceholderAnalysis(
        custom_rules=custom_rules,
        structured_rules=structured_rules,
        text_rules=text_rules,
        translation_data_map=translation_data_map,
        placeholder_candidates=placeholder_candidates,
        structured_candidates=structured_candidates,
        placeholder_sample_texts=_collect_placeholder_rule_validation_samples(
            translation_data_map=translation_data_map,
            text_rules=text_rules,
        ),
        structured_sample_texts=tuple(
            _collect_structured_placeholder_preview_samples(
                translation_data_map=translation_data_map,
                structured_rules=structured_rules,
            )
        ),
    )


def _build_workspace_placeholder_coverage_report(
    *,
    analysis: _WorkspacePlaceholderAnalysis,
) -> AgentReport:
    """复用已扫描的 occurrence 事实构建普通占位符覆盖报告。"""
    candidates = analysis.placeholder_candidates.candidates
    uncovered_count = count_uncovered_candidates(candidates)
    occurrence_count = sum(candidate.count for candidate in candidates)
    uncovered_occurrence_count = sum(candidate.uncovered_count for candidate in candidates)
    warnings: list[AgentIssue] = []
    if uncovered_count:
        warnings.append(issue("uncovered_placeholder", f"发现 {uncovered_count} 个未覆盖的疑似自定义控制符"))
    return AgentReport.from_parts(
        errors=[],
        warnings=warnings,
        summary={
            "candidate_count": len(candidates),
            "occurrence_count": occurrence_count,
            "uncovered_count": uncovered_count,
            "uncovered_occurrence_count": uncovered_occurrence_count,
            "coverage_conflict_count": sum(1 for candidate in candidates if candidate.coverage_conflict),
            "custom_rule_count": len(analysis.custom_rules),
        },
        details={
            "candidates": analysis.placeholder_candidates.details,
        },
    )


def _validate_workspace_structured_placeholder_rules(
    *,
    game_title: str,
    analysis: _WorkspacePlaceholderAnalysis,
) -> AgentReport:
    """复用工作区已解析规则和样本事实生成验证报告。"""
    return _build_structured_placeholder_rule_validation_report(
        game_title=game_title,
        structured_rules=analysis.structured_rules,
        text_rules=analysis.text_rules,
        sample_texts=analysis.structured_sample_texts,
    )


def _build_workspace_structured_placeholder_coverage_report(
    *,
    game_title: str,
    analysis: _WorkspacePlaceholderAnalysis,
) -> AgentReport:
    """复用已扫描的 occurrence 事实构建结构化覆盖报告。"""
    candidate_details = analysis.structured_candidates.details
    uncovered_count = analysis.structured_candidates.uncovered_count
    covered_count = len(candidate_details) - uncovered_count
    warnings: list[AgentIssue] = []
    if uncovered_count:
        warnings.append(
            issue("structured_placeholder_uncovered", f"发现 {uncovered_count} 个未被结构化规则覆盖的协议外壳候选")
        )
    return AgentReport.from_parts(
        errors=[],
        warnings=warnings,
        summary={
            "game": game_title,
            "rule_count": len(analysis.structured_rules),
            "candidate_count": len(candidate_details),
            "covered_count": covered_count,
            "uncovered_count": uncovered_count,
        },
        details={
            "candidates": candidate_details,
        },
    )


def _summary_int(summary: JsonObject, key: str) -> int:
    """从 Agent 报告摘要中读取整数计数字段。"""
    raw_value = summary.get(key)
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise RuntimeError(f"报告缺少有效计数字段: {key}")
    return raw_value


def _sum_candidate_int_field(candidates: JsonArray, key: str) -> int:
    """汇总已在 GameAnalysisContext 中建立的候选计数字段。"""
    total = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise TypeError("分析上下文候选不是 JSON 对象")
        value = candidate.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"分析上下文候选字段 {key} 不是整数")
        total += value
    return total


async def _read_empty_rule_review_issues(
    *,
    session: TargetGameSession,
    game_data: GameData,
    event_command_codes: frozenset[int] | None,
    event_command_scope_hash: str,
    note_tag_scope_hash: str,
    plugin_source_scope_hash: str,
    mv_virtual_namebox_scope_hash_value: str,
) -> dict[str, AgentIssue | None]:
    """读取工作区空规则文件对应的显式确认状态。"""
    return {
        "plugin_rules": await _empty_rule_review_issue(
            session=session,
            rule_domain=PLUGIN_TEXT_RULE_DOMAIN,
            current_scope_hash=plugin_rule_scope_hash(game_data),
            unconfirmed_code="plugin_rules_empty_unconfirmed",
            stale_code="plugin_rules_empty_confirmation_stale",
            label="插件规则",
        ),
        "plugin_source_rules": await _empty_rule_review_issue(
            session=session,
            rule_domain=PLUGIN_SOURCE_TEXT_RULE_DOMAIN,
            current_scope_hash=plugin_source_scope_hash,
            unconfirmed_code="plugin_source_rules_empty_unconfirmed",
            stale_code="plugin_source_rules_empty_confirmation_stale",
            label="插件源码规则",
        ),
        "event_command_rules": await _empty_rule_review_issue(
            session=session,
            rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN,
            current_scope_hash=event_command_scope_hash,
            unconfirmed_code="event_command_rules_empty_unconfirmed",
            stale_code="event_command_rules_empty_confirmation_stale",
            label="事件指令规则",
            expected_scope_payload=(
                None
                if event_command_codes is None
                else {
                    "kind": "event_command_codes",
                    "command_codes": sorted(event_command_codes),
                }
            ),
        ),
        "mv_virtual_namebox_rules": (
            await _empty_rule_review_issue(
                session=session,
                rule_domain=MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
                current_scope_hash=mv_virtual_namebox_scope_hash_value,
                unconfirmed_code="mv_virtual_namebox_rules_empty_unconfirmed",
                stale_code="mv_virtual_namebox_rules_empty_confirmation_stale",
                label="MV 虚拟名字框规则",
            )
            if game_data.layout.engine_kind == "mv"
            else None
        ),
        "note_tag_rules": await _empty_rule_review_issue(
            session=session,
            rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
            current_scope_hash=note_tag_scope_hash,
            unconfirmed_code="note_tag_rules_empty_unconfirmed",
            stale_code="note_tag_rules_empty_confirmation_stale",
            label="Note 标签规则",
        ),
    }


async def _empty_rule_review_issue(
    *,
    session: TargetGameSession,
    rule_domain: RuleReviewDomain,
    current_scope_hash: str,
    unconfirmed_code: str,
    stale_code: str,
    label: str,
    expected_scope_payload: dict[str, object] | None = None,
) -> AgentIssue | None:
    """判断空规则文件是否有仍然有效的显式确认。"""
    state = await session.read_rule_review_state(rule_domain=rule_domain)
    if state is None or not state.reviewed_empty:
        return issue(unconfirmed_code, f"{label}为空，必须先用对应导入命令传 --confirm-empty 保存当前范围的空结果确认")
    if state.scope_hash != current_scope_hash:
        return issue(stale_code, f"{label}曾确认为空，但当前游戏内容已经变化，请重新导出并检查规则")
    if expected_scope_payload is not None and (
        state.scope_contract_version != 1 or state.scope_payload != expected_scope_payload
    ):
        return issue(stale_code, f"{label}的空结果确认没有绑定当前实际检查范围，请重新导出并检查规则")
    return None


async def _read_workspace_event_command_codes(workspace: Path) -> tuple[frozenset[int] | None, AgentIssue | None]:
    """从工作区 manifest 读取本轮事件指令候选编码。"""
    manifest_path = workspace / "manifest.json"
    if not manifest_path.exists():
        return None, issue("manifest_missing", "工作区缺少 manifest.json，无法确认工作区来源和事件指令编码")
    try:
        async with aiofiles.open(manifest_path, "r", encoding="utf-8") as file:
            raw_manifest = cast(object, json.loads(await file.read()))
        manifest = ensure_json_object(coerce_json_value(raw_manifest), "manifest")
        if manifest.get("contract_version") != 2:
            return None, issue("manifest_version_unsupported", "工作区 manifest 不是 v2，请重新准备工作区")
        generated = ensure_json_object(manifest.get("generated"), "manifest.generated")
        raw_codes = ensure_json_array(generated.get("event_command_codes"), "manifest.generated.event_command_codes")
        codes: set[int] = set()
        for raw_code in raw_codes:
            if isinstance(raw_code, bool) or not isinstance(raw_code, int):
                return None, issue("manifest_invalid", "manifest.generated.event_command_codes 必须是整数数组")
            codes.add(raw_code)
        if not codes:
            return None, issue("manifest_invalid", "manifest.generated.event_command_codes 不能为空")
        return frozenset(codes), None
    except Exception as error:
        return None, issue("manifest_invalid", f"读取工作区 manifest 失败: {type(error).__name__}: {error}")


async def _validate_workspace_manifest_binding(
    *,
    workspace: Path,
    session: TargetGameSession,
    game_data: GameData,
) -> AgentIssue | None:
    """在读取规则文件前确认工作区属于当前游戏、快照和语言配置。"""
    manifest_path = workspace / "manifest.json"
    try:
        async with aiofiles.open(manifest_path, "r", encoding="utf-8") as file:
            raw_manifest = cast(object, json.loads(await file.read()))
        manifest = ensure_json_object(coerce_json_value(raw_manifest), "manifest")
        if manifest.get("contract_version") != 2:
            return issue("manifest_version_unsupported", "工作区 manifest 不是 v2，请重新准备工作区")
        if manifest.get("game_id") != session.game_id:
            return issue("manifest_game_mismatch", "工作区属于另一个已注册游戏，拒绝读取其中规则")
        if manifest.get("engine_kind") != game_data.layout.engine_kind:
            return issue("manifest_engine_mismatch", "工作区记录的 RPG Maker 引擎与当前游戏不一致")
        expected_snapshot_digest = _build_source_snapshot_digest(await session.read_source_snapshot_records())
        if manifest.get("source_snapshot_digest") != expected_snapshot_digest:
            return issue("manifest_source_snapshot_stale", "工作区对应的干净源快照已经变化，请重新准备工作区")
        language_profile = ensure_json_object(
            manifest.get("language_profile"),
            "manifest.language_profile",
        )
        expected_language_fingerprint = _build_workspace_language_fingerprint(
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
            target_language=session.target_language,
        )
        if language_profile.get("fingerprint") != expected_language_fingerprint:
            return issue("manifest_language_profile_mismatch", "工作区的源语言配置与当前游戏不一致")
    except Exception as error:
        return issue("manifest_invalid", f"读取工作区 manifest 失败: {type(error).__name__}: {error}")
    return None


def _build_source_snapshot_digest(records: list[SourceSnapshotFileRecord]) -> str:
    """计算不受工作区位置影响的可信源快照摘要。"""
    payload = [
        {
            "relative_path": record.relative_path,
            "sha256": record.sha256,
            "byte_size": record.byte_size,
        }
        for record in sorted(records, key=lambda item: item.relative_path)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _plugin_source_scan_read_error_issue(scan: PluginSourceScan) -> AgentIssue | None:
    """把启用插件源码读取失败转换为不可持久化评估的明确错误。"""
    read_error_count = scan.risk.read_error_file_count
    if read_error_count == 0:
        return None
    failure_summary = "、".join(
        part
        for part in (
            f"缺失 {scan.missing_enabled_file_count} 个" if scan.missing_enabled_file_count else "",
            f"读取失败 {scan.unreadable_enabled_file_count} 个" if scan.unreadable_enabled_file_count else "",
        )
        if part
    )
    return issue(
        "plugin_source_read_error",
        f"有 {read_error_count} 个已启用插件的翻译源源码不可用（{failure_summary}），风险扫描结果不可信；"
        + "请补齐缺失文件或将无法读取的源码转换为 UTF-8 后重新扫描",
    )


def _build_workspace_language_fingerprint(
    *,
    source_language: str,
    additional_source_languages: tuple[str, ...],
    target_language: str,
) -> str:
    """计算工作区必须绑定的语言配置指纹。"""
    encoded = json.dumps(
        {
            "primary": source_language,
            "additional": list(additional_source_languages),
            "target": target_language,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _workspace_game_relative_path(*, path: Path, game_root: Path) -> str:
    """把 manifest 中的游戏布局路径收敛为相对游戏根目录的稳定路径。"""
    try:
        relative_path = path.resolve().relative_to(game_root.resolve())
    except ValueError as error:
        raise RuntimeError(f"游戏布局路径越过游戏根目录: {path}") from error
    return relative_path.as_posix()


@final
class _WorkspacePublishTargetError(RuntimeError):
    """准备或发布工作区目标失败，并保留稳定错误码。"""

    def __init__(self, *, code: str, message: str, target_dir: Path) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.target_dir = target_dir


@final
class _WorkspacePublishTarget:
    """管理与目标同父目录的暂存区和一次性目录发布。"""

    def __init__(
        self,
        *,
        target_dir: Path,
        staging_dir: Path,
        existing_target_identity: tuple[int, int, int] | None,
    ) -> None:
        self.target_dir = target_dir
        self.staging_dir = staging_dir
        self._existing_target_identity = existing_target_identity
        self._published = False

    @classmethod
    def create(cls, output_dir: Path) -> _WorkspacePublishTarget:
        """校验目标并在同一父目录创建独占空暂存目录。"""
        target_dir = Path(os.path.abspath(os.fspath(output_dir)))
        parent_dir = target_dir.parent
        if parent_dir == target_dir:
            raise _WorkspacePublishTargetError(
                code="workspace_path_unsafe",
                message="目标工作区不能是文件系统根目录",
                target_dir=target_dir,
            )
        try:
            _assert_workspace_path_chain_safe(parent_dir)
            parent_dir.mkdir(parents=True, exist_ok=True)
            _assert_workspace_path_chain_safe(parent_dir)
        except (OSError, RuntimeError) as error:
            raise _WorkspacePublishTargetError(
                code="workspace_path_unsafe",
                message=f"目标工作区的父目录不可安全使用: {type(error).__name__}: {error}",
                target_dir=target_dir,
            ) from error

        existing_target_identity: tuple[int, int, int] | None = None
        if _path_exists_without_following(target_dir):
            if _path_is_link(target_dir):
                raise _WorkspacePublishTargetError(
                    code="workspace_path_unsafe",
                    message="目标工作区是符号链接、目录联接或重解析点，拒绝写入",
                    target_dir=target_dir,
                )
            if not target_dir.is_dir():
                raise _WorkspacePublishTargetError(
                    code="workspace_target_invalid",
                    message="目标工作区路径已存在但不是目录",
                    target_dir=target_dir,
                )
            try:
                if any(target_dir.iterdir()):
                    raise _WorkspacePublishTargetError(
                        code="workspace_not_empty",
                        message="目标工作区不是空目录，请先使用 cleanup-agent-workspace 清理或改用新目录",
                        target_dir=target_dir,
                    )
                target_stat = os.lstat(target_dir)
            except _WorkspacePublishTargetError:
                raise
            except OSError as error:
                raise _WorkspacePublishTargetError(
                    code="workspace_target_invalid",
                    message=f"无法检查目标工作区: {type(error).__name__}: {error}",
                    target_dir=target_dir,
                ) from error
            existing_target_identity = (
                target_stat.st_dev,
                target_stat.st_ino,
                target_stat.st_ctime_ns,
            )

        try:
            staging_dir = Path(
                tempfile.mkdtemp(
                    prefix=".att-mz-workspace-",
                    suffix=".tmp",
                    dir=parent_dir,
                )
            )
        except OSError as error:
            raise _WorkspacePublishTargetError(
                code="workspace_publish_failed",
                message=f"无法在目标同级目录创建工作区暂存区: {type(error).__name__}: {error}",
                target_dir=target_dir,
            ) from error
        return cls(
            target_dir=target_dir,
            staging_dir=staging_dir,
            existing_target_identity=existing_target_identity,
        )

    def publish(self) -> None:
        """确认目标未变化后，以同卷目录重命名发布完整工作区。"""
        if self._published:
            raise RuntimeError("工作区已经发布")
        if _path_is_link(self.staging_dir) or not self.staging_dir.is_dir():
            raise _WorkspacePublishTargetError(
                code="workspace_publish_failed",
                message="工作区暂存目录在发布前发生异常变化",
                target_dir=self.target_dir,
            )
        manifest_path = self.staging_dir / "manifest.json"
        if _path_is_link(manifest_path) or not manifest_path.is_file():
            raise _WorkspacePublishTargetError(
                code="workspace_publish_failed",
                message="工作区暂存内容不完整，缺少普通文件 manifest.json",
                target_dir=self.target_dir,
            )
        try:
            _assert_workspace_path_chain_safe(self.target_dir.parent)
            if self._existing_target_identity is None:
                if _path_exists_without_following(self.target_dir):
                    raise _WorkspacePublishTargetError(
                        code="workspace_target_changed",
                        message="目标工作区在生成期间被其他程序创建，未覆盖现有内容",
                        target_dir=self.target_dir,
                    )
                os.replace(self.staging_dir, self.target_dir)
            else:
                self._publish_over_empty_target()
        except _WorkspacePublishTargetError:
            raise
        except OSError as error:
            raise _WorkspacePublishTargetError(
                code="workspace_publish_failed",
                message=f"无法原子发布工作区: {type(error).__name__}: {error}",
                target_dir=self.target_dir,
            ) from error
        self._published = True

    def _publish_over_empty_target(self) -> None:
        """仅在原空目录身份及内容均未变化时替换它。"""
        try:
            if _path_is_link(self.target_dir) or not self.target_dir.is_dir():
                raise _WorkspacePublishTargetError(
                    code="workspace_target_changed",
                    message="目标空目录在生成期间被替换，未覆盖现有内容",
                    target_dir=self.target_dir,
                )
            target_stat = os.lstat(self.target_dir)
            if (
                target_stat.st_dev,
                target_stat.st_ino,
                target_stat.st_ctime_ns,
            ) != self._existing_target_identity:
                raise _WorkspacePublishTargetError(
                    code="workspace_target_changed",
                    message="目标空目录在生成期间发生变化，未覆盖现有内容",
                    target_dir=self.target_dir,
                )
            if any(self.target_dir.iterdir()):
                raise _WorkspacePublishTargetError(
                    code="workspace_target_changed",
                    message="目标工作区在生成期间出现新内容，未覆盖现有内容",
                    target_dir=self.target_dir,
                )
            self.target_dir.rmdir()
        except _WorkspacePublishTargetError:
            raise
        except OSError as error:
            raise _WorkspacePublishTargetError(
                code="workspace_target_changed",
                message=f"目标空目录在发布前无法安全替换: {type(error).__name__}: {error}",
                target_dir=self.target_dir,
            ) from error

        try:
            os.replace(self.staging_dir, self.target_dir)
        except OSError as error:
            restore_error: OSError | None = None
            if not _path_exists_without_following(self.target_dir):
                try:
                    self.target_dir.mkdir()
                except OSError as caught_error:
                    restore_error = caught_error
            restore_suffix = (
                ""
                if restore_error is None
                else f"；恢复原空目录也失败: {type(restore_error).__name__}: {restore_error}"
            )
            raise _WorkspacePublishTargetError(
                code="workspace_publish_failed",
                message=f"无法原子发布工作区: {type(error).__name__}: {error}{restore_suffix}",
                target_dir=self.target_dir,
            ) from error

    def cleanup(self) -> None:
        """删除尚未发布的暂存区；绝不跟随被替换成链接的路径。"""
        if self._published or not _path_exists_without_following(self.staging_dir):
            return
        if _path_is_link(self.staging_dir) or not self.staging_dir.is_dir():
            raise RuntimeError(f"工作区暂存路径发生异常变化，拒绝自动删除: {self.staging_dir}")
        shutil.rmtree(self.staging_dir)


def _path_exists_without_following(path: Path) -> bool:
    """判断路径项是否存在，包括断开的符号链接。"""
    return os.path.lexists(path)


def _assert_workspace_path_chain_safe(path: Path) -> None:
    """拒绝目标路径现存层级中的链接、junction 和其他重解析点。"""
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute_path.anchor)
    if _path_is_link(current):
        raise RuntimeError(f"路径根是链接或重解析点: {current}")
    for part in absolute_path.parts[1:]:
        current /= part
        if _path_exists_without_following(current) and _path_is_link(current):
            raise RuntimeError(f"路径经过链接或重解析点: {current}")


def _path_is_link(path: Path) -> bool:
    """同时识别符号链接、Windows junction 和其他重解析点。"""
    if path.is_symlink() or path.is_junction():
        return True
    try:
        file_attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _path_uses_symlink(*, path: Path, workspace_root: Path) -> bool:
    """检查工作区相对路径的任一现存层级是否为链接。"""
    try:
        relative_path = path.relative_to(workspace_root)
    except ValueError:
        return True
    current = workspace_root
    for part in relative_path.parts:
        current = current / part
        if _path_is_link(current):
            return True
    return False
