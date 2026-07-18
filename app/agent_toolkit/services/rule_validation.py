"""Agent 工具箱 RuleValidationAgentMixin 子服务。"""
# pyright: reportPrivateUsage=false
# mixin 通过 AgentToolkitService 组合成同一个服务边界，允许调用同门面的受保护核心方法。

from app.application.flow_gate import (
    ensure_empty_rule_confirmed,
)
from app.application.mutation_guard import open_game_for_mutation
from app.application.rule_import_backup import write_rule_import_translation_backup
from app.event_command_text.index import build_event_command_analysis_index
from app.note_tag_text.exporter import collect_note_tag_candidates_from_sources
from app.note_tag_text.sources import collect_note_tag_sources
from app.persistence import RecoveryRequiredError
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
from app.plugin_text.index import build_plugin_parameter_analysis_index
from app.rmmz.mv_namebox import (
    collect_mv_virtual_namebox_candidates_from_command_snapshots,
    mv_virtual_namebox_candidate_details_from_candidates,
    mv_virtual_namebox_candidates_payload_from_candidates,
    mv_virtual_namebox_rule_records_to_import_json,
    parse_mv_virtual_namebox_rule_import_text,
    validate_mv_virtual_namebox_rules_against_candidates,
)
from app.rmmz.schema import GameData, MvVirtualNameboxRuleRecord, TranslationItem
from app.rule_review import (
    MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
    NOTE_TAG_TEXT_RULE_DOMAIN,
    PLUGIN_SOURCE_TEXT_RULE_DOMAIN,
    mv_virtual_namebox_rule_scope_hash,
    note_tag_rule_scope_hash_for_candidates,
    plugin_source_rule_scope_hash,
    plugin_source_text_rules_hash,
)
from app.text_scope.write_probe import collect_write_back_probe_reasons

from .common import (
    PLUGINS_FILE_NAME,
    AgentIssue,
    AgentReport,
    AgentServiceContext,
    EventCommandTextExtraction,
    JsonArray,
    JsonObject,
    JsonValue,
    NoteTagTextExtraction,
    Path,
    PluginTextExtraction,
    TextRules,
    _build_rule_metric_detail,
    _collect_write_protocol_unwritable_items,
    _json_items_by_location_path,
    _note_tag_item_matches_rule,
    _preview_event_command_write_back,
    _write_json_object,
    build_event_command_rule_records_from_import,
    build_note_tag_rule_records_from_import,
    build_plugin_rule_records_from_import,
    collect_translation_data_paths,
    export_note_tag_candidates_file,
    issue,
    load_setting,
    parse_event_command_rule_import_text,
    parse_note_tag_rule_import_text,
    parse_plugin_rule_import_text,
)


def _collect_plugin_source_unwritable_items(
    *,
    game_data: GameData,
    extracted_items: list[TranslationItem],
) -> JsonArray:
    """把插件源码写回预演原因转换为校验报告明细。"""
    if not extracted_items:
        return []
    reasons = collect_write_back_probe_reasons(
        game_data=game_data,
        active_items=extracted_items,
    )
    return [
        {
            "location_path": location_path,
            "reason": reason,
        }
        for location_path, reason in sorted(reasons.items())
        if location_path.startswith("js/plugins/")
    ]


class RuleValidationAgentMixin:
    """承载 AgentToolkitService 的 RuleValidationAgentMixin 命令族。"""

    async def export_mv_virtual_namebox_candidates(
        self: AgentServiceContext,
        *,
        game_title: str,
        output_path: Path,
    ) -> AgentReport:
        """导出 MV 虚拟名字框候选，供主代理填写外部规则。"""
        async with await self.game_registry.open_game(game_title) as session:
            game_data = await self._load_translation_source_game_data(session)
        if game_data.layout.engine_kind != "mv":
            return AgentReport.from_parts(
                errors=[issue("mv_virtual_namebox_rules_forbidden", "MV 虚拟名字框规则只允许 RPG Maker MV 游戏使用")],
                warnings=[],
                summary={"output": str(output_path), "candidate_count": 0},
                details={},
            )
        command_index = build_event_command_analysis_index(game_data)
        candidates = collect_mv_virtual_namebox_candidates_from_command_snapshots(
            tuple((entry.location_path, entry.display_name, entry.command) for entry in command_index)
        )
        payload = mv_virtual_namebox_candidates_payload_from_candidates(candidates)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await _write_json_object(output_path, payload)
        candidate_count = _summary_int_from_payload(payload, "candidate_count")
        warnings: list[AgentIssue] = []
        if candidate_count == 0:
            warnings.append(
                issue("mv_virtual_namebox_candidates_empty", "当前 MV 游戏没有发现 `101` 后首条非空 `401` 候选")
            )
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={"output": str(output_path), "candidate_count": candidate_count},
            details=payload,
        )

    async def validate_mv_virtual_namebox_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """校验 MV 虚拟名字框规则 JSON 文本并报告候选命中情况。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        details: JsonObject = {"rules": [], "matched_candidates": []}
        records: list[MvVirtualNameboxRuleRecord] = []
        existing_records: list[MvVirtualNameboxRuleRecord] = []
        candidate_count = 0
        matched_candidate_count = 0
        newly_matched_candidate_count = 0
        try:
            records = parse_mv_virtual_namebox_rule_import_text(rules_text)
            async with await self.game_registry.open_game(game_title) as session:
                game_data = await self._load_translation_source_game_data(session)
                if game_data.layout.engine_kind == "mv":
                    existing_records = await session.read_mv_virtual_namebox_rules()
            if game_data.layout.engine_kind != "mv":
                errors.append(
                    issue("mv_virtual_namebox_rules_forbidden", "MV 虚拟名字框规则只允许 RPG Maker MV 游戏使用")
                )
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
            command_index = build_event_command_analysis_index(game_data)
            candidates = collect_mv_virtual_namebox_candidates_from_command_snapshots(
                tuple((entry.location_path, entry.display_name, entry.command) for entry in command_index)
            )
            candidate_count = len(candidates)
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

    async def import_mv_virtual_namebox_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
        confirm_empty: bool = False,
    ) -> AgentReport:
        """校验并导入当前 MV 游戏的虚拟名字框规则。"""
        try:
            records = parse_mv_virtual_namebox_rule_import_text(rules_text)
            if not records and not confirm_empty:
                raise RuntimeError("MV 虚拟名字框规则为空，必须确认当前游戏不需要虚拟名字框后传 --confirm-empty")
            async with await open_game_for_mutation(self.game_registry, game_title) as session:
                game_data = await self._load_translation_source_game_data(session)
                if game_data.layout.engine_kind != "mv":
                    raise RuntimeError("MV 虚拟名字框规则只允许 RPG Maker MV 游戏使用")
                command_index = build_event_command_analysis_index(game_data)
                candidates = collect_mv_virtual_namebox_candidates_from_command_snapshots(
                    tuple((entry.location_path, entry.display_name, entry.command) for entry in command_index)
                )
                rule_errors, match_details = validate_mv_virtual_namebox_rules_against_candidates(
                    game_data=game_data,
                    records=records,
                    candidates=candidates,
                )
                if rule_errors:
                    messages = "；".join(_format_mv_namebox_rule_error(error_detail) for error_detail in rule_errors)
                    raise RuntimeError(messages)
                await session.replace_mv_virtual_namebox_rules(records)
                if records:
                    await session.delete_rule_review_state(rule_domain=MV_VIRTUAL_NAMEBOX_RULE_DOMAIN)
                else:
                    await session.replace_rule_review_state(
                        rule_domain=MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
                        scope_hash=mv_virtual_namebox_rule_scope_hash(
                            mv_virtual_namebox_candidate_details_from_candidates(candidates)
                        ),
                        reviewed_empty=True,
                    )
        except RecoveryRequiredError:
            raise
        except Exception as error:
            return AgentReport.from_parts(
                errors=[
                    issue(
                        "mv_virtual_namebox_rules_invalid",
                        f"MV 虚拟名字框规则导入失败: {type(error).__name__}: {error}",
                    )
                ],
                warnings=[],
                summary={"rule_count": 0, "matched_candidate_count": 0},
                details={},
            )
        warnings = [] if records else [issue("mv_virtual_namebox_rules_empty", "已导入空 MV 虚拟名字框规则")]
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={
                "rule_count": len(records),
                "matched_candidate_count": len(match_details),
            },
            details={
                "rules": mv_virtual_namebox_rule_records_to_import_json(records)["rules"],
                "matched_candidates": match_details,
            },
        )

    async def export_note_tag_candidates(
        self: AgentServiceContext,
        *,
        game_title: str,
        output_path: Path,
    ) -> AgentReport:
        """导出标准 data JSON Note 标签候选，供外部 Agent 判断可见文本标签。"""
        async with await self.game_registry.open_game(game_title) as session:
            setting = load_setting(
                self.setting_path,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
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
            game_data = await self._load_translation_source_game_data(session)
        report = await export_note_tag_candidates_file(
            game_data=game_data,
            output_path=output_path,
            text_rules=text_rules,
        )
        warnings: list[AgentIssue] = []
        if report.candidate_tag_count == 0:
            warnings.append(issue("note_tag_candidates_empty", "当前游戏没有发现 data Note 标签候选"))
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={
                "candidate_tag_count": report.candidate_tag_count,
                "candidate_value_count": report.candidate_value_count,
                "translatable_value_count": report.translatable_value_count,
                "output": str(output_path),
            },
            details=report.details,
        )

    async def validate_note_tag_rules(self: AgentServiceContext, *, game_title: str, rules_text: str) -> AgentReport:
        """校验 Note 标签规则 JSON 文本并报告命中情况。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        details: JsonObject = {"rules": []}
        try:
            import_file = parse_note_tag_rule_import_text(rules_text)
            async with await self.game_registry.open_game(game_title) as session:
                setting = load_setting(
                    self.setting_path,
                    source_language=session.source_language,
                    additional_source_languages=session.additional_source_languages,
                )
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
                game_data = await self._load_translation_source_game_data(session)
                translated_paths: set[str] = await session.read_translation_location_paths()
            note_sources = tuple(collect_note_tag_sources(game_data=game_data))
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
            translated_paths = set()
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

    async def import_note_tag_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
        confirm_empty: bool = False,
    ) -> AgentReport:
        """校验并导入当前游戏的 Note 标签文本规则。"""
        try:
            import_file = parse_note_tag_rule_import_text(rules_text)
            async with await open_game_for_mutation(self.game_registry, game_title) as session:
                setting = load_setting(
                    self.setting_path,
                    source_language=session.source_language,
                    additional_source_languages=session.additional_source_languages,
                )
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
                game_data = await self._load_translation_source_game_data(session)
                note_sources = tuple(collect_note_tag_sources(game_data=game_data))
                records = build_note_tag_rule_records_from_import(
                    game_data=game_data,
                    import_file=import_file,
                    text_rules=text_rules,
                    note_sources=note_sources,
                )
                if not records:
                    ensure_empty_rule_confirmed(
                        rule_label="Note 标签规则",
                        confirm_empty=confirm_empty,
                    )
                old_records = await session.read_note_tag_text_rules()
                old_note_paths = collect_translation_data_paths(
                    NoteTagTextExtraction(
                        game_data=game_data,
                        rule_records=old_records,
                        text_rules=text_rules,
                    ).extract_all_text_from_sources(note_sources)
                )
                new_note_paths = collect_translation_data_paths(
                    NoteTagTextExtraction(
                        game_data=game_data,
                        rule_records=records,
                        text_rules=text_rules,
                    ).extract_all_text_from_sources(note_sources)
                )
                stale_paths = sorted(old_note_paths - new_note_paths)
                deleted_translation_items = 0
                deleted_translation_backup_path: str | None = None
                if stale_paths:
                    stale_items = await session.read_translated_items_by_paths(stale_paths)
                    backup = await write_rule_import_translation_backup(
                        game_title=game_title,
                        domain="note-tag-rules",
                        items=stale_items,
                    )
                    if backup is not None:
                        deleted_translation_backup_path = backup.backup_path
                    deleted_translation_items = await session.delete_translation_items_by_paths(stale_paths)
                await session.replace_note_tag_text_rules(records)
                if records:
                    await session.delete_rule_review_state(rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN)
                else:
                    await session.replace_rule_review_state(
                        rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN,
                        scope_hash=note_tag_rule_scope_hash_for_candidates(
                            collect_note_tag_candidates_from_sources(
                                sources=note_sources,
                                text_rules=text_rules,
                            )
                        ),
                        reviewed_empty=True,
                    )
        except RecoveryRequiredError:
            raise
        except Exception as error:
            return AgentReport.from_parts(
                errors=[issue("note_tag_rules_invalid", f"Note 标签规则不可导入: {type(error).__name__}: {error}")],
                warnings=[],
                summary={
                    "file_count": 0,
                    "tag_count": 0,
                    "deleted_translation_items": 0,
                    "deleted_translation_backup_path": "",
                },
                details={},
            )
        warnings = [] if records else [issue("note_tag_rules_empty", "已导入空 Note 标签规则")]
        if deleted_translation_items > 0 and deleted_translation_backup_path is not None:
            warnings.append(
                issue(
                    "deleted_translations_backed_up",
                    f"本次导入 Note 标签规则已清理 {deleted_translation_items} 条不再属于当前规则范围的已保存译文；已先备份到 {deleted_translation_backup_path}。如果发现规则导错，先重新导入正确规则，再用 import-manual-translations 读取该备份文件恢复这些译文",
                )
            )
        elif deleted_translation_items > 0:
            warnings.append(
                issue(
                    "deleted_translations_without_backup",
                    f"本次导入 Note 标签规则已清理 {deleted_translation_items} 条不再属于当前规则范围的已保存译文；没有生成备份文件",
                )
            )
        details: JsonObject = {
            "rules": [
                {
                    "file_name": record.file_name,
                    "tag_names": list(record.tag_names),
                }
                for record in records
            ]
        }
        if deleted_translation_backup_path is not None:
            details["deleted_translation_backup"] = {
                "path": deleted_translation_backup_path,
                "restore_step": "先重新导入正确规则，再运行 import-manual-translations 并把 input 指向该备份文件。",
            }
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={
                "file_count": len(records),
                "tag_count": sum(len(record.tag_names) for record in records),
                "deleted_translation_items": deleted_translation_items,
                "deleted_translation_backup_path": deleted_translation_backup_path or "",
            },
            details=details,
        )

    async def validate_source_residual_rules(
        self: AgentServiceContext, *, game_title: str, rules_text: str
    ) -> AgentReport:
        """校验源文残留例外规则 JSON 文本并报告命中情况。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        details: JsonObject = {"rules": []}
        try:
            records = await self._build_source_residual_rule_records(
                game_title=game_title,
                rules_text=rules_text,
            )
            details["rules"] = [
                {
                    "rule_id": record.rule_id,
                    "rule_type": record.rule_type,
                    "location_path": record.location_path,
                    "pattern": record.pattern_text,
                    "allowed_terms": list(record.allowed_terms),
                    "check_group": record.check_group,
                    "reason": record.reason,
                }
                for record in records
            ]
            if not records:
                warnings.append(issue("source_residual_rules_empty", "源文残留例外规则为空"))
        except Exception as error:
            errors.append(
                issue("source_residual_rules_invalid", f"源文残留例外规则不可导入: {type(error).__name__}: {error}")
            )
            records = []
        return AgentReport.from_parts(
            errors=errors,
            warnings=warnings,
            summary={
                "rule_count": len(records),
                "position_rule_count": sum(1 for record in records if record.rule_type == "position"),
                "structural_rule_count": sum(1 for record in records if record.rule_type == "structural"),
                "term_count": sum(len(record.allowed_terms) for record in records),
            },
            details=details,
        )

    async def import_source_residual_rules(
        self: AgentServiceContext, *, game_title: str, rules_text: str
    ) -> AgentReport:
        """校验并导入当前游戏的源文残留例外规则。"""
        try:
            records = await self._build_source_residual_rule_records(
                game_title=game_title,
                rules_text=rules_text,
            )
            async with await open_game_for_mutation(self.game_registry, game_title) as session:
                await session.replace_source_residual_rules(records)
        except RecoveryRequiredError:
            raise
        except Exception as error:
            return AgentReport.from_parts(
                errors=[
                    issue("source_residual_rules_invalid", f"源文残留例外规则不可导入: {type(error).__name__}: {error}")
                ],
                warnings=[],
                summary={"rule_count": 0, "position_rule_count": 0, "structural_rule_count": 0, "term_count": 0},
                details={},
            )
        return AgentReport.from_parts(
            errors=[],
            warnings=[] if records else [issue("source_residual_rules_empty", "已导入空源文残留例外规则")],
            summary={
                "rule_count": len(records),
                "position_rule_count": sum(1 for record in records if record.rule_type == "position"),
                "structural_rule_count": sum(1 for record in records if record.rule_type == "structural"),
                "term_count": sum(len(record.allowed_terms) for record in records),
            },
            details={
                "rules": [
                    {
                        "rule_id": record.rule_id,
                        "rule_type": record.rule_type,
                        "location_path": record.location_path,
                        "pattern": record.pattern_text,
                        "allowed_terms": list(record.allowed_terms),
                        "check_group": record.check_group,
                        "reason": record.reason,
                    }
                    for record in records
                ]
            },
        )

    async def validate_plugin_rules(self: AgentServiceContext, *, game_title: str, rules_text: str) -> AgentReport:
        """校验插件规则 JSON 文本并报告命中情况。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        details: JsonObject = {"rules": []}
        try:
            import_file = parse_plugin_rule_import_text(rules_text)
            async with await self.game_registry.open_game(game_title) as session:
                setting = load_setting(
                    self.setting_path,
                    source_language=session.source_language,
                    additional_source_languages=session.additional_source_languages,
                )
                custom_rules = await self._resolve_custom_rules(
                    session=session,
                    custom_placeholder_rules_text=None,
                )
                structured_rules = await self._resolve_structured_rules(session=session)
                game_data = await self._load_translation_source_game_data(session)
                translated_paths: set[str] = await session.read_translation_location_paths()
            text_rules = TextRules.from_setting(
                setting.text_rules,
                custom_placeholder_rules=custom_rules,
                structured_placeholder_rules=structured_rules,
            )
            plugin_index = build_plugin_parameter_analysis_index(game_data)
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
            translated_paths = set()
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

    async def validate_plugin_source_rules(
        self: AgentServiceContext, *, game_title: str, rules_text: str
    ) -> AgentReport:
        """校验插件源码文本规则 JSON 文本并报告命中情况。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        details: JsonObject = {"rules": []}
        unreviewed_count = 0
        try:
            import_file = parse_plugin_source_rule_import_text(rules_text)
            async with await self.game_registry.open_game(game_title) as session:
                setting = load_setting(
                    self.setting_path,
                    source_language=session.source_language,
                    additional_source_languages=session.additional_source_languages,
                )
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
                game_data = await self._load_translation_source_game_data(session)
                translated_paths: set[str] = await session.read_translation_location_paths()
            plugin_source_raw_index = build_plugin_source_raw_index(game_data=game_data)
            scan = derive_plugin_source_scan(index=plugin_source_raw_index, text_rules=text_rules)
            if scan.risk.read_error_file_count:
                return _plugin_source_rules_read_error_report(scan)
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
                    [
                        item
                        for item in extracted_items
                        if item.location_path.startswith(f"js/plugins/{record.file_name}/")
                    ]
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
            errors.append(
                issue("plugin_source_rules_invalid", f"插件源码规则不可导入: {type(error).__name__}: {error}")
            )
            records = []
            extracted_items = []
            translated_paths = set()
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

    async def import_plugin_source_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
        confirm_empty: bool = False,
    ) -> AgentReport:
        """校验并导入当前游戏的插件源码文本规则。"""
        try:
            import_file = parse_plugin_source_rule_import_text(rules_text)
            async with await open_game_for_mutation(self.game_registry, game_title) as session:
                setting = load_setting(
                    self.setting_path,
                    source_language=session.source_language,
                    additional_source_languages=session.additional_source_languages,
                )
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
                game_data = await self._load_translation_source_game_data(session)
                plugin_source_raw_index = build_plugin_source_raw_index(game_data=game_data)
                scan = derive_plugin_source_scan(index=plugin_source_raw_index, text_rules=text_rules)
                if scan.risk.read_error_file_count:
                    return _plugin_source_rules_read_error_report(scan)
                await session.replace_plugin_source_assessment(
                    source_hash=plugin_source_rule_scope_hash(scan=scan),
                    text_rules_hash=plugin_source_text_rules_hash(text_rules),
                    high_risk=scan.risk.high_risk,
                    candidate_count=len(scan.candidates),
                    summary=scan.risk_report_json(),
                )
                records = build_plugin_source_rule_records_from_import(
                    import_file=import_file,
                    scan=scan,
                )
                old_records = await session.read_plugin_source_text_rules()
                review = collect_plugin_source_review_coverage(scan=scan, rule_records=records)
                unreviewed_count = len(review.unreviewed_candidates)
                reviewed_selector_count = sum(
                    len(record.selectors) + len(record.excluded_selectors) for record in records
                )
                if unreviewed_count and (scan.risk.high_risk or records or old_records):
                    return AgentReport.from_parts(
                        errors=[
                            issue(
                                "plugin_source_review_incomplete",
                                f"插件源码规则还有 {unreviewed_count} 个候选未归入翻译或排除",
                            )
                        ],
                        warnings=[],
                        summary={
                            "file_count": len(records),
                            "selector_count": sum(len(record.selectors) for record in records),
                            "excluded_selector_count": sum(len(record.excluded_selectors) for record in records),
                            "reviewed_selector_count": reviewed_selector_count,
                            "unreviewed_selector_count": unreviewed_count,
                            "deleted_translation_items": 0,
                            "deleted_translation_backup_path": "",
                        },
                        details={
                            "rules": plugin_source_rule_records_to_import_json(records),
                        },
                    )
                if not records:
                    ensure_empty_rule_confirmed(
                        rule_label="插件源码规则",
                        confirm_empty=confirm_empty,
                    )
                old_paths = collect_translation_data_paths(
                    PluginSourceTextExtraction(
                        game_data,
                        rule_records=old_records,
                        text_rules=text_rules,
                        scan=scan,
                    ).extract_all_text()
                )
                new_paths = collect_translation_data_paths(
                    PluginSourceTextExtraction(
                        game_data,
                        rule_records=records,
                        text_rules=text_rules,
                        scan=scan,
                    ).extract_all_text()
                )
                stale_paths = sorted(old_paths - new_paths)
                deleted_translation_items = 0
                deleted_translation_backup_path: str | None = None
                if stale_paths:
                    stale_items = await session.read_translated_items_by_paths(stale_paths)
                    backup = await write_rule_import_translation_backup(
                        game_title=game_title,
                        domain="plugin-source-rules",
                        items=stale_items,
                    )
                    if backup is not None:
                        deleted_translation_backup_path = backup.backup_path
                    deleted_translation_items = await session.delete_translation_items_by_paths(stale_paths)
                await session.replace_plugin_source_text_rules(records)
                await session.clear_plugin_source_runtime_write_maps()
                if records:
                    await session.delete_rule_review_state(rule_domain=PLUGIN_SOURCE_TEXT_RULE_DOMAIN)
                else:
                    await session.replace_rule_review_state(
                        rule_domain=PLUGIN_SOURCE_TEXT_RULE_DOMAIN,
                        scope_hash=plugin_source_rule_scope_hash(scan=scan),
                        reviewed_empty=True,
                    )
        except RecoveryRequiredError:
            raise
        except Exception as error:
            return AgentReport.from_parts(
                errors=[issue("plugin_source_rules_invalid", f"插件源码规则不可导入: {type(error).__name__}: {error}")],
                warnings=[],
                summary={
                    "file_count": 0,
                    "selector_count": 0,
                    "excluded_selector_count": 0,
                    "reviewed_selector_count": 0,
                    "unreviewed_selector_count": 0,
                    "deleted_translation_items": 0,
                    "deleted_translation_backup_path": "",
                },
                details={},
            )
        warnings = [] if records else [issue("plugin_source_rules_empty", "已导入空插件源码规则")]
        if unreviewed_count:
            warnings.append(
                issue("plugin_source_review_incomplete", f"插件源码规则还有 {unreviewed_count} 个候选未归入翻译或排除")
            )
        if deleted_translation_items > 0 and deleted_translation_backup_path is not None:
            warnings.append(
                issue(
                    "deleted_translations_backed_up",
                    f"本次导入插件源码规则已清理 {deleted_translation_items} 条不再属于当前规则范围的已保存译文；已先备份到 {deleted_translation_backup_path}",
                )
            )
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={
                "file_count": len(records),
                "selector_count": sum(len(record.selectors) for record in records),
                "excluded_selector_count": sum(len(record.excluded_selectors) for record in records),
                "reviewed_selector_count": sum(
                    len(record.selectors) + len(record.excluded_selectors) for record in records
                ),
                "unreviewed_selector_count": unreviewed_count,
                "deleted_translation_items": deleted_translation_items,
                "deleted_translation_backup_path": deleted_translation_backup_path or "",
            },
            details={
                "rules": plugin_source_rule_records_to_import_json(records),
            },
        )

    async def validate_event_command_rules(
        self: AgentServiceContext, *, game_title: str, rules_text: str
    ) -> AgentReport:
        """校验事件指令规则 JSON 文本并报告命中情况。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        details: JsonObject = {"rules": []}
        try:
            import_file = parse_event_command_rule_import_text(rules_text)
            async with await self.game_registry.open_game(game_title) as session:
                setting = load_setting(
                    self.setting_path,
                    source_language=session.source_language,
                    additional_source_languages=session.additional_source_languages,
                )
                custom_rules = await self._resolve_custom_rules(
                    session=session,
                    custom_placeholder_rules_text=None,
                )
                structured_rules = await self._resolve_structured_rules(session=session)
                game_data = await self._load_translation_source_game_data(session)
                translated_paths: set[str] = await session.read_translation_location_paths()
            command_index = build_event_command_analysis_index(game_data)
            records = build_event_command_rule_records_from_import(
                game_data=game_data,
                import_file=import_file,
                command_index=command_index,
            )
            text_rules = TextRules.from_setting(
                setting.text_rules,
                custom_placeholder_rules=custom_rules,
                structured_placeholder_rules=structured_rules,
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
            errors.append(
                issue("event_command_rules_invalid", f"事件指令规则不可导入: {type(error).__name__}: {error}")
            )
            records = []
            extracted_items = []
            translated_paths = set()
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


def _summary_int_from_payload(payload: JsonObject, key: str) -> int:
    """从导出载荷读取整数统计字段。"""
    raw_value = payload.get(key)
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise RuntimeError(f"MV 虚拟名字框候选导出缺少有效计数字段: {key}")
    return raw_value


def _plugin_source_rules_read_error_report(scan: PluginSourceScan) -> AgentReport:
    """启用插件源码读取不完整时拒绝校验或导入规则。"""
    read_error_count = scan.risk.read_error_file_count
    failure_summary = _plugin_source_unavailable_file_summary(scan)
    return AgentReport.from_parts(
        errors=[
            issue(
                "plugin_source_read_error",
                f"有 {read_error_count} 个已启用插件的翻译源源码不可用（{failure_summary}），"
                + "无法可信校验或导入插件源码规则",
            )
        ],
        warnings=[],
        summary={
            "file_count": 0,
            "selector_count": 0,
            "excluded_selector_count": 0,
            "reviewed_selector_count": 0,
            "unreviewed_selector_count": 0,
            "hit_count": 0,
            "extractable_count": 0,
            "translated_count": 0,
            "writable_count": 0,
            "unwritable_count": 0,
            "deleted_translation_items": 0,
            "deleted_translation_backup_path": "",
        },
        details=scan.risk_report_json(),
    )


def _plugin_source_unavailable_file_summary(scan: PluginSourceScan) -> str:
    """按三态扫描事实汇总缺失与读取失败数量。"""
    return "、".join(
        part
        for part in (
            f"缺失 {scan.missing_enabled_file_count} 个" if scan.missing_enabled_file_count else "",
            f"读取失败 {scan.unreadable_enabled_file_count} 个" if scan.unreadable_enabled_file_count else "",
        )
        if part
    )


def _mv_namebox_match_key(detail: JsonValue) -> tuple[str, str] | None:
    """生成虚拟名字框候选命中身份，用于对比已保存规则。"""
    if not isinstance(detail, dict):
        return None
    location_path = detail.get("location_path")
    text = detail.get("text")
    if isinstance(location_path, str) and isinstance(text, str):
        return location_path, text
    return None


def _mv_namebox_match_keys(details: JsonArray) -> set[tuple[str, str]]:
    """读取一组虚拟名字框候选命中的身份集合。"""
    keys: set[tuple[str, str]] = set()
    for detail in details:
        key = _mv_namebox_match_key(detail)
        if key is not None:
            keys.add(key)
    return keys


def _format_mv_namebox_rule_error(error_detail: JsonValue) -> str:
    """把 MV 虚拟名字框规则校验明细转换成一句用户可读错误。"""
    if not isinstance(error_detail, dict):
        return str(error_detail)
    message_value = error_detail.get("message")
    message = message_value if isinstance(message_value, str) and message_value else "规则校验失败"
    location_value = error_detail.get("location_path")
    rule_value = error_detail.get("rule_name")
    prefixes: list[str] = []
    if isinstance(location_value, str) and location_value:
        prefixes.append(location_value)
    if isinstance(rule_value, str) and rule_value:
        prefixes.append(rule_value)
    if not prefixes:
        return message
    return f"{' / '.join(prefixes)}: {message}"
