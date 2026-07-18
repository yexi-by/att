"""Agent 工具箱 PlaceholderRuleAgentMixin 子服务。"""
# pyright: reportPrivateUsage=false
# mixin 通过 AgentToolkitService 组合成同一个服务边界，允许调用同门面的受保护核心方法。

from app.agent_toolkit.placeholder_scan import analyze_structured_placeholder_candidates
from app.application.flow_gate import collect_external_text_rule_gate_errors, ensure_empty_rule_import_allowed
from app.application.mutation_guard import open_game_for_mutation
from app.game_analysis import GameAnalysisContext
from app.rule_review import (
    PLACEHOLDER_RULE_DOMAIN,
    STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
    placeholder_rule_scope_hash,
    structured_placeholder_rule_scope_hash,
)

from .common import (
    DEFAULT_SOURCE_LANGUAGE,
    AgentIssue,
    AgentReport,
    AgentServiceContext,
    CustomPlaceholderRule,
    JsonArray,
    JsonObject,
    Path,
    PlaceholderRuleRecord,
    Sequence,
    SourceLanguage,
    StructuredPlaceholderRule,
    StructuredPlaceholderRuleRecord,
    TextRules,
    TranslationData,
    TranslationItem,
    _append_placeholder_rule_safety_issues,
    _build_custom_placeholder_rule_draft,
    _build_joined_text_boundary_warnings,
    _build_unprotected_control_warnings,
    _collect_placeholder_preview_samples,
    _collect_unprotected_control_warning_samples,
    _joined_text_boundary_markers,
    _placeholder_preview_loses_visible_source_text,
    _preview_placeholder_sample,
    aiofiles,
    count_uncovered_candidates,
    issue,
    json,
    load_custom_placeholder_rules_text,
    load_setting,
    load_structured_placeholder_rules_text,
    placeholder_candidates_to_details,
    scan_placeholder_candidates,
)


class PlaceholderRuleAgentMixin:
    """承载 AgentToolkitService 的 PlaceholderRuleAgentMixin 命令族。"""

    async def scan_placeholder_candidates(
        self: AgentServiceContext,
        *,
        game_title: str,
        custom_placeholder_rules_text: str | None,
    ) -> AgentReport:
        """扫描目标游戏中疑似需要自定义保护的控制符。"""
        async with await self.game_registry.open_game(game_title) as session:
            setting = load_setting(
                self.setting_path,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            game_data = await self._load_translation_source_game_data(session)
            custom_rules = await self._resolve_custom_rules(
                session=session,
                custom_placeholder_rules_text=custom_placeholder_rules_text,
            )
            structured_rules = await self._resolve_structured_rules(session=session)
            text_rules = TextRules.from_setting(
                setting.text_rules,
                custom_placeholder_rules=custom_rules,
                structured_placeholder_rules=structured_rules,
            )
            baseline_text_rules = TextRules.from_setting(
                setting.text_rules,
            )
            analysis_context = await self._build_game_analysis_context(
                session=session,
                game_data=game_data,
                text_rules=baseline_text_rules,
                placeholder_rules=_placeholder_rule_records_from_runtime(custom_rules),
                structured_placeholder_rules=_structured_placeholder_rule_records_from_runtime(structured_rules),
            )
            translation_data_map = analysis_context.translation_data_map

        candidates = scan_placeholder_candidates(translation_data_map, text_rules)
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
                "custom_rule_count": len(custom_rules),
            },
            details={
                "candidates": placeholder_candidates_to_details(candidates),
            },
        )

    async def validate_placeholder_rules(
        self: AgentServiceContext,
        *,
        game_title: str | None,
        custom_placeholder_rules_text: str | None,
        sample_texts: Sequence[str],
        analysis_context: GameAnalysisContext | None = None,
    ) -> AgentReport:
        """校验自定义占位符规则，并预览样本文本的替换与还原结果。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        setting_source_language: SourceLanguage = DEFAULT_SOURCE_LANGUAGE
        additional_source_languages: tuple[SourceLanguage, ...] = ()
        source_label = "--placeholder-rules"
        if custom_placeholder_rules_text is None and game_title is not None:
            source_label = "当前游戏数据库"
        elif custom_placeholder_rules_text is None:
            source_label = "空规则"

        try:
            if game_title is not None:
                async with await self.game_registry.open_game(game_title) as session:
                    setting_source_language = session.source_language
                    additional_source_languages = session.additional_source_languages
                    custom_rules = await self._resolve_custom_rules(
                        session=session,
                        custom_placeholder_rules_text=custom_placeholder_rules_text,
                    )
                    structured_rules = await self._resolve_structured_rules(session=session)
                    if not sample_texts:
                        setting = load_setting(
                            self.setting_path,
                            source_language=session.source_language,
                            additional_source_languages=session.additional_source_languages,
                        )
                        extraction_rules = TextRules.from_setting(
                            setting.text_rules,
                            structured_placeholder_rules=structured_rules,
                        )
                        preview_rules = TextRules.from_setting(
                            setting.text_rules,
                            custom_placeholder_rules=custom_rules,
                            structured_placeholder_rules=structured_rules,
                        )
                        if analysis_context is None:
                            game_data = await self._load_translation_source_game_data(session)
                            command_context = await self._build_game_analysis_context(
                                session=session,
                                game_data=game_data,
                                text_rules=extraction_rules,
                                placeholder_rules=_placeholder_rule_records_from_runtime(custom_rules),
                                structured_placeholder_rules=_structured_placeholder_rule_records_from_runtime(
                                    structured_rules
                                ),
                            )
                            translation_data_map = command_context.translation_data_map
                        else:
                            translation_data_map = analysis_context.build_scope_for_text_rules(
                                text_rules=extraction_rules
                            ).translation_data_map
                        sample_texts = _collect_placeholder_rule_validation_samples(
                            translation_data_map=translation_data_map,
                            text_rules=preview_rules,
                        )
            elif custom_placeholder_rules_text is None:
                custom_rules = ()
                structured_rules = ()
            else:
                custom_rules = load_custom_placeholder_rules_text(custom_placeholder_rules_text)
                structured_rules = ()
        except Exception as error:
            return AgentReport.from_parts(
                errors=[
                    issue(
                        "placeholder_rules_invalid",
                        f"自定义占位符规则不可用: {type(error).__name__}: {error}",
                    )
                ],
                warnings=[],
                summary={
                    "source": source_label,
                    "rule_count": 0,
                    "sample_count": len(sample_texts),
                },
                details={},
            )

        try:
            setting = load_setting(
                self.setting_path,
                source_language=setting_source_language,
                additional_source_languages=additional_source_languages,
            )
            text_rules = TextRules.from_setting(
                setting.text_rules,
                custom_placeholder_rules=custom_rules,
                structured_placeholder_rules=structured_rules,
            )
        except Exception as error:
            errors.append(issue("setting", f"配置加载失败: {type(error).__name__}: {error}"))
            return AgentReport.from_parts(
                errors=errors,
                warnings=warnings,
                summary={
                    "source": source_label,
                    "rule_count": len(custom_rules),
                    "sample_count": len(sample_texts),
                },
                details={},
            )

        return _build_placeholder_rule_validation_report(
            source_label=source_label,
            custom_rules=custom_rules,
            text_rules=text_rules,
            sample_texts=sample_texts,
        )

    async def import_placeholder_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
        confirm_empty: bool = False,
    ) -> AgentReport:
        """校验并导入当前游戏专用自定义占位符规则。"""
        validation_report = await self.validate_placeholder_rules(
            game_title=game_title,
            custom_placeholder_rules_text=rules_text,
            sample_texts=[],
        )
        if validation_report.errors:
            return AgentReport.from_parts(
                errors=validation_report.errors,
                warnings=validation_report.warnings,
                summary={
                    "game": game_title,
                    "imported_rule_count": 0,
                    "validated_rule_count": validation_report.summary.get("rule_count", 0),
                    "sample_count": validation_report.summary.get("sample_count", 0),
                },
                details={
                    "validation": {
                        "summary": validation_report.summary,
                        "details": validation_report.details,
                    }
                },
            )

        custom_rules = load_custom_placeholder_rules_text(rules_text)
        rule_records = [
            PlaceholderRuleRecord(
                pattern_text=rule.pattern_text,
                placeholder_template=rule.placeholder_template,
            )
            for rule in custom_rules
        ]
        coverage_report = await self.scan_placeholder_candidates(
            game_title=game_title,
            custom_placeholder_rules_text=rules_text,
        )
        if coverage_report.errors:
            return _coverage_scan_failure_report(
                game_title=game_title,
                validation_report=validation_report,
                coverage_report=coverage_report,
            )
        uncovered_count = _summary_int(coverage_report.summary, "uncovered_count")
        if not rule_records:
            try:
                ensure_empty_rule_import_allowed(
                    rule_label="普通占位符规则",
                    confirm_empty=confirm_empty,
                    candidate_count=uncovered_count,
                )
            except RuntimeError as error:
                return AgentReport.from_parts(
                    errors=[issue("placeholder_rules_empty_unconfirmed", str(error))],
                    warnings=validation_report.warnings,
                    summary={
                        "game": game_title,
                        "imported_rule_count": 0,
                        "validated_rule_count": validation_report.summary.get("rule_count", 0),
                        "sample_count": validation_report.summary.get("sample_count", 0),
                    },
                    details={
                        "validation": {
                            "summary": validation_report.summary,
                            "details": validation_report.details,
                        },
                        "coverage": {
                            "summary": coverage_report.summary,
                            "details": coverage_report.details,
                        },
                    },
                )
        candidate_details = _json_array_detail(coverage_report.details, "candidates")
        scope_hash = placeholder_rule_scope_hash(candidate_details)
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            await session.replace_placeholder_rules(rule_records)
            if rule_records:
                await session.delete_rule_review_state(rule_domain=PLACEHOLDER_RULE_DOMAIN)
            else:
                await session.replace_rule_review_state(
                    rule_domain=PLACEHOLDER_RULE_DOMAIN,
                    scope_hash=scope_hash,
                    reviewed_empty=True,
                )
        return AgentReport.from_parts(
            errors=[],
            warnings=validation_report.warnings
            if rule_records
            else [
                *validation_report.warnings,
                issue("placeholder_rules_empty", "已导入空自定义占位符规则"),
            ],
            summary={
                "game": game_title,
                "imported_rule_count": len(rule_records),
                "validated_rule_count": validation_report.summary.get("rule_count", len(rule_records)),
                "sample_count": validation_report.summary.get("sample_count", 0),
            },
            details={
                "validation": {
                    "summary": validation_report.summary,
                    "details": validation_report.details,
                }
            },
        )

    async def validate_structured_placeholder_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
        sample_texts: Sequence[str],
    ) -> AgentReport:
        """校验结构化占位符规则，并预览协议外壳保护效果。"""
        try:
            structured_rules = load_structured_placeholder_rules_text(rules_text)
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
                text_rules = TextRules.from_setting(
                    setting.text_rules,
                    custom_placeholder_rules=custom_rules,
                    structured_placeholder_rules=structured_rules,
                )
                if not sample_texts:
                    game_data = await self._load_translation_source_game_data(session)
                    baseline_text_rules = TextRules.from_setting(
                        setting.text_rules,
                        custom_placeholder_rules=custom_rules,
                    )
                    analysis_context = await self._build_game_analysis_context(
                        session=session,
                        game_data=game_data,
                        text_rules=baseline_text_rules,
                        placeholder_rules=_placeholder_rule_records_from_runtime(custom_rules),
                        structured_placeholder_rules=_structured_placeholder_rule_records_from_runtime(
                            structured_rules
                        ),
                    )
                    translation_data_map = analysis_context.translation_data_map
                    sample_texts = _collect_structured_placeholder_preview_samples(
                        translation_data_map=translation_data_map,
                        structured_rules=structured_rules,
                    )
        except Exception as error:
            return AgentReport.from_parts(
                errors=[
                    issue(
                        "structured_placeholder_rules_invalid",
                        f"结构化占位符规则不可用: {type(error).__name__}: {error}",
                    )
                ],
                warnings=[],
                summary={
                    "game": game_title,
                    "rule_count": 0,
                    "sample_count": len(sample_texts),
                },
                details={},
            )

        return _build_structured_placeholder_rule_validation_report(
            game_title=game_title,
            structured_rules=structured_rules,
            text_rules=text_rules,
            sample_texts=sample_texts,
        )

    async def scan_structured_placeholder_candidates(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
    ) -> AgentReport:
        """扫描结构化规则对当前正文中协议外壳候选的覆盖情况。"""
        try:
            structured_rules = load_structured_placeholder_rules_text(rules_text)
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
                text_rules = TextRules.from_setting(
                    setting.text_rules,
                    custom_placeholder_rules=custom_rules,
                    structured_placeholder_rules=structured_rules,
                )
                baseline_text_rules = TextRules.from_setting(
                    setting.text_rules,
                    custom_placeholder_rules=custom_rules,
                )
                game_data = await self._load_translation_source_game_data(session)
                analysis_context = await self._build_game_analysis_context(
                    session=session,
                    game_data=game_data,
                    text_rules=baseline_text_rules,
                    placeholder_rules=_placeholder_rule_records_from_runtime(custom_rules),
                    structured_placeholder_rules=_structured_placeholder_rule_records_from_runtime(structured_rules),
                )
                translation_data_map = analysis_context.translation_data_map
        except Exception as error:
            return AgentReport.from_parts(
                errors=[
                    issue(
                        "structured_placeholder_scan_failed",
                        f"结构化占位符覆盖扫描失败: {type(error).__name__}: {error}",
                    )
                ],
                warnings=[],
                summary={
                    "game": game_title,
                    "rule_count": 0,
                    "candidate_count": 0,
                    "covered_count": 0,
                    "uncovered_count": 0,
                },
                details={},
            )

        candidate_details = _collect_structured_placeholder_candidate_details(
            translation_data_map=translation_data_map,
            text_rules=text_rules,
        )
        covered_count = sum(
            1 for detail in candidate_details if isinstance(detail, dict) and detail.get("covered") is True
        )
        uncovered_count = len(candidate_details) - covered_count
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
                "rule_count": len(structured_rules),
                "candidate_count": len(candidate_details),
                "covered_count": covered_count,
                "uncovered_count": uncovered_count,
            },
            details={
                "candidates": candidate_details,
            },
        )

    async def import_structured_placeholder_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        rules_text: str,
        confirm_empty: bool = False,
    ) -> AgentReport:
        """校验并导入当前游戏专用结构化占位符规则。"""
        validation_report = await self.validate_structured_placeholder_rules(
            game_title=game_title,
            rules_text=rules_text,
            sample_texts=[],
        )
        if validation_report.errors:
            return AgentReport.from_parts(
                errors=validation_report.errors,
                warnings=validation_report.warnings,
                summary={
                    "game": game_title,
                    "imported_rule_count": 0,
                    "validated_rule_count": validation_report.summary.get("rule_count", 0),
                    "sample_count": validation_report.summary.get("sample_count", 0),
                },
                details={
                    "validation": {
                        "summary": validation_report.summary,
                        "details": validation_report.details,
                    }
                },
            )

        structured_rules = load_structured_placeholder_rules_text(rules_text)
        rule_records = _structured_placeholder_rule_records_from_runtime(structured_rules)
        coverage_report = await self.scan_structured_placeholder_candidates(
            game_title=game_title,
            rules_text=rules_text,
        )
        if coverage_report.errors:
            return _coverage_scan_failure_report(
                game_title=game_title,
                validation_report=validation_report,
                coverage_report=coverage_report,
            )
        uncovered_count = _summary_int(coverage_report.summary, "uncovered_count")
        if not rule_records:
            try:
                ensure_empty_rule_import_allowed(
                    rule_label="结构化占位符规则",
                    confirm_empty=confirm_empty,
                    candidate_count=uncovered_count,
                )
            except RuntimeError as error:
                return AgentReport.from_parts(
                    errors=[issue("structured_placeholder_rules_empty_unconfirmed", str(error))],
                    warnings=validation_report.warnings,
                    summary={
                        "game": game_title,
                        "imported_rule_count": 0,
                        "validated_rule_count": validation_report.summary.get("rule_count", 0),
                        "sample_count": validation_report.summary.get("sample_count", 0),
                    },
                    details={
                        "validation": {
                            "summary": validation_report.summary,
                            "details": validation_report.details,
                        },
                        "coverage": {
                            "summary": coverage_report.summary,
                            "details": coverage_report.details,
                        },
                    },
                )
        candidate_details = _json_array_detail(coverage_report.details, "candidates")
        scope_hash = structured_placeholder_rule_scope_hash(candidate_details)
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            await session.replace_structured_placeholder_rules(rule_records)
            if rule_records:
                await session.delete_rule_review_state(rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN)
            else:
                await session.replace_rule_review_state(
                    rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
                    scope_hash=scope_hash,
                    reviewed_empty=True,
                )
        return AgentReport.from_parts(
            errors=[],
            warnings=validation_report.warnings
            if rule_records
            else [
                *validation_report.warnings,
                issue("structured_placeholder_rules_empty", "已导入空结构化占位符规则"),
            ],
            summary={
                "game": game_title,
                "imported_rule_count": len(rule_records),
                "validated_rule_count": validation_report.summary.get("rule_count", len(rule_records)),
                "sample_count": validation_report.summary.get("sample_count", 0),
            },
            details={
                "validation": {
                    "summary": validation_report.summary,
                    "details": validation_report.details,
                }
            },
        )

    async def build_placeholder_rules(
        self: AgentServiceContext,
        *,
        game_title: str,
        output_path: Path,
    ) -> AgentReport:
        """根据未覆盖候选生成可编辑的自定义占位符规则草稿。"""
        async with await self.game_registry.open_game(game_title) as session:
            setting = load_setting(
                self.setting_path,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            structured_rules = await self._resolve_structured_rules(session=session)
            empty_rules = TextRules.from_setting(
                setting.text_rules,
                custom_placeholder_rules=(),
                structured_placeholder_rules=structured_rules,
            )
            game_data = await self._load_translation_source_game_data(session)
            analysis_context = await self._build_game_analysis_context(
                session=session,
                game_data=game_data,
                text_rules=empty_rules,
                placeholder_rules=[],
                structured_placeholder_rules=_structured_placeholder_rule_records_from_runtime(structured_rules),
            )
            external_rule_errors = await collect_external_text_rule_gate_errors(
                session=session,
                context=analysis_context,
                setting=setting,
            )
            if external_rule_errors:
                return AgentReport.from_parts(
                    errors=[issue(error.code, error.message) for error in external_rule_errors],
                    warnings=[],
                    summary={
                        "game": game_title,
                        "candidate_count": 0,
                        "uncovered_count_before_draft": 0,
                        "uncovered_count_after_draft_preview": 0,
                        "draft_rule_count": 0,
                        "manual_boundary_candidate_count": 0,
                        "coverage_conflict_count": 0,
                        "output": str(output_path),
                    },
                    details={},
                )
            translation_data_map = analysis_context.translation_data_map
        candidates = scan_placeholder_candidates(translation_data_map, empty_rules)
        uncovered_count_before_draft = count_uncovered_candidates(candidates)
        coverage_conflict_markers = [candidate.marker for candidate in candidates if candidate.coverage_conflict]
        manual_boundary_markers = _joined_text_boundary_markers(candidates)
        draft_rules = _build_custom_placeholder_rule_draft(candidates)
        draft_custom_rules = load_custom_placeholder_rules_text(json.dumps(draft_rules, ensure_ascii=False))
        draft_text_rules = TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=draft_custom_rules,
            structured_placeholder_rules=structured_rules,
        )
        draft_preview_candidates = scan_placeholder_candidates(translation_data_map, draft_text_rules)
        uncovered_count_after_draft_preview = count_uncovered_candidates(draft_preview_candidates)
        warnings = _build_unprotected_control_warnings(
            _collect_unprotected_control_warning_samples(translation_data_map, empty_rules),
            empty_rules,
        )
        warnings.extend(_build_joined_text_boundary_warnings(manual_boundary_markers))
        if coverage_conflict_markers:
            warnings.append(
                issue(
                    "placeholder_coverage_conflict",
                    "同一控制符标记同时存在已覆盖和未覆盖位置，已跳过自动草稿，请按 occurrence 报告核对上下文",
                )
            )
        if not draft_rules:
            warnings.append(issue("placeholder_draft_empty", "没有发现需要生成草稿的自定义控制符候选"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(output_path, "w", encoding="utf-8") as file:
            _ = await file.write(f"{json.dumps(draft_rules, ensure_ascii=False, indent=2)}\n")
        return AgentReport.from_parts(
            errors=[],
            warnings=warnings,
            summary={
                "candidate_count": len(candidates),
                "uncovered_count_before_draft": uncovered_count_before_draft,
                "uncovered_count_after_draft_preview": uncovered_count_after_draft_preview,
                "draft_rule_count": len(draft_rules),
                "manual_boundary_candidate_count": len(manual_boundary_markers),
                "coverage_conflict_count": len(coverage_conflict_markers),
                "output": str(output_path),
            },
            details={
                "rules": {key: value for key, value in draft_rules.items()},
                "manual_boundary_candidates": [marker for marker in manual_boundary_markers],
                "coverage_conflict_markers": [marker for marker in coverage_conflict_markers],
            },
        )


def _collect_placeholder_rule_validation_samples(
    *,
    translation_data_map: dict[str, TranslationData],
    text_rules: TextRules,
) -> tuple[str, ...]:
    """从显式正文视图收集普通规则预览样本，不触发候选重扫。"""
    samples = _collect_placeholder_preview_samples(translation_data_map, text_rules)
    if not samples:
        samples = _collect_unprotected_control_warning_samples(translation_data_map, text_rules)
    return tuple(samples)


def _build_placeholder_rule_validation_report(
    *,
    source_label: str,
    custom_rules: Sequence[CustomPlaceholderRule],
    text_rules: TextRules,
    sample_texts: Sequence[str],
) -> AgentReport:
    """仅消费已解析规则和样本事实，构建普通占位符验证报告。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    rule_details: JsonArray = []
    for rule in custom_rules:
        placeholder_preview = text_rules.format_custom_placeholder(
            template=rule.placeholder_template,
            index=1,
        )
        _append_placeholder_rule_safety_issues(
            rule=rule,
            errors=errors,
            warnings=warnings,
        )
        rule_details.append(
            {
                "pattern": rule.pattern_text,
                "placeholder_template": rule.placeholder_template,
                "placeholder_preview": placeholder_preview,
            }
        )

    sample_details: JsonArray = []
    for sample_text in sample_texts:
        try:
            sample_preview = _preview_placeholder_sample(text_rules, sample_text)
            sample_details.append(sample_preview)
            if _placeholder_preview_loses_visible_source_text(
                text_rules=text_rules,
                sample_preview=sample_preview,
            ):
                errors.append(
                    issue(
                        "placeholder_rule_loses_translatable_text",
                        "占位符规则把含源语言正文的样本文本整体遮蔽，模型将看不到需要翻译的内容",
                    )
                )
        except Exception as error:
            errors.append(
                issue(
                    "placeholder_preview",
                    f"样本文本预览失败: {type(error).__name__}: {error}",
                )
            )
    warnings.extend(_build_unprotected_control_warnings(sample_texts, text_rules))

    if not custom_rules:
        warnings.append(issue("placeholder_rules_empty", "当前没有自定义占位符规则"))

    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "source": source_label,
            "rule_count": len(custom_rules),
            "sample_count": len(sample_texts),
        },
        details={
            "rules": rule_details,
            "samples": sample_details,
        },
    )


def _build_structured_placeholder_rule_validation_report(
    *,
    game_title: str,
    structured_rules: Sequence[StructuredPlaceholderRule],
    text_rules: TextRules,
    sample_texts: Sequence[str],
) -> AgentReport:
    """仅消费已解析规则和样本事实，构建结构化占位符验证报告。"""
    errors: list[AgentIssue] = []
    warnings: list[AgentIssue] = []
    rule_details: JsonArray = []
    for rule in structured_rules:
        protected_group_details: JsonArray = []
        for group_name, placeholder_template in sorted(rule.protected_groups.items()):
            protected_group_details.append(
                {
                    "group_name": group_name,
                    "placeholder_template": placeholder_template,
                    "placeholder_preview": text_rules.format_custom_placeholder(
                        template=placeholder_template,
                        index=1,
                    ),
                }
            )
        rule_details.append(
            {
                "name": rule.rule_name,
                "type": rule.rule_type,
                "pattern": rule.pattern_text,
                "translatable_group": rule.translatable_group,
                "protected_groups": protected_group_details,
            }
        )

    sample_details: JsonArray = []
    for sample_text in sample_texts:
        try:
            sample_preview = _preview_placeholder_sample(text_rules, sample_text)
            sample_details.append(sample_preview)
            if _placeholder_preview_loses_visible_source_text(
                text_rules=text_rules,
                sample_preview=sample_preview,
            ):
                errors.append(
                    issue(
                        "structured_placeholder_loses_translatable_text",
                        "结构化占位符规则把含源语言正文的样本文本整体遮蔽，模型将看不到需要翻译的内容",
                    )
                )
        except Exception as error:
            errors.append(
                issue(
                    "structured_placeholder_preview",
                    f"结构化占位符样本文本预览失败: {type(error).__name__}: {error}",
                )
            )

    if not structured_rules:
        warnings.append(issue("structured_placeholder_rules_empty", "当前没有结构化占位符规则"))
    if structured_rules and not sample_texts:
        warnings.append(issue("structured_placeholder_samples_empty", "当前正文没有命中结构化占位符规则的样本文本"))

    return AgentReport.from_parts(
        errors=errors,
        warnings=warnings,
        summary={
            "game": game_title,
            "rule_count": len(structured_rules),
            "sample_count": len(sample_texts),
        },
        details={
            "rules": rule_details,
            "samples": sample_details,
        },
    )


def _placeholder_rule_records_from_runtime(
    rules: Sequence[CustomPlaceholderRule],
) -> list[PlaceholderRuleRecord]:
    """把已加载的普通占位符规则转为单命令事实记录。"""
    return [
        PlaceholderRuleRecord(
            pattern_text=rule.pattern_text,
            placeholder_template=rule.placeholder_template,
        )
        for rule in rules
    ]


def _structured_placeholder_rule_records_from_runtime(
    rules: Sequence[StructuredPlaceholderRule],
) -> list[StructuredPlaceholderRuleRecord]:
    """把运行时结构化规则转换成数据库记录。"""
    return [
        StructuredPlaceholderRuleRecord(
            rule_name=rule.rule_name,
            rule_type=rule.rule_type,
            pattern_text=rule.pattern_text,
            translatable_group=rule.translatable_group,
            protected_groups=dict(rule.protected_groups),
        )
        for rule in rules
    ]


def _coverage_scan_failure_report(
    *,
    game_title: str,
    validation_report: AgentReport,
    coverage_report: AgentReport,
) -> AgentReport:
    """保留覆盖扫描的原始错误，并明确声明本次没有导入任何规则。"""
    return AgentReport.from_parts(
        errors=coverage_report.errors,
        warnings=[*validation_report.warnings, *coverage_report.warnings],
        summary={
            "game": game_title,
            "imported_rule_count": 0,
            "validated_rule_count": validation_report.summary.get("rule_count", 0),
            "sample_count": validation_report.summary.get("sample_count", 0),
        },
        details={
            "validation": {
                "summary": validation_report.summary,
                "details": validation_report.details,
            },
            "coverage": {
                "summary": coverage_report.summary,
                "details": coverage_report.details,
            },
        },
    )


def _summary_int(summary: JsonObject, key: str) -> int:
    """从报告 summary 中读取整数计数字段。"""
    raw_value = summary.get(key)
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise RuntimeError(f"报告缺少有效计数字段: {key}")
    return raw_value


def _json_array_detail(details: JsonObject, key: str) -> JsonArray:
    """从报告 details 中读取 JSON 数组字段。"""
    raw_value = details.get(key)
    if not isinstance(raw_value, list):
        raise RuntimeError(f"报告缺少有效数组字段: {key}")
    return [item for item in raw_value]


def _collect_structured_placeholder_preview_samples(
    *,
    translation_data_map: dict[str, TranslationData],
    structured_rules: Sequence[StructuredPlaceholderRule],
) -> list[str]:
    """为结构化占位符规则收集少量当前正文样本。"""
    samples: list[str] = []
    seen_samples: set[str] = set()
    for item in _iter_translation_items_from_map(translation_data_map):
        for text in item.original_lines:
            if not _line_matches_structured_rules(text=text, structured_rules=structured_rules):
                continue
            if text in seen_samples:
                continue
            samples.append(text)
            seen_samples.add(text)
            if len(samples) >= 10:
                return samples
    return samples


def _collect_structured_placeholder_candidate_details(
    *,
    translation_data_map: dict[str, TranslationData],
    text_rules: TextRules,
) -> JsonArray:
    """扫描一次并返回已验证的结构化外壳覆盖事实。"""
    return analyze_structured_placeholder_candidates(translation_data_map, text_rules).details


def _iter_translation_items_from_map(translation_data_map: dict[str, TranslationData]) -> list[TranslationItem]:
    """从正文提取结果中取出翻译条目。"""
    items: list[TranslationItem] = []
    for translation_data in translation_data_map.values():
        items.extend(translation_data.translation_items)
    return items


def _line_matches_structured_rules(
    *,
    text: str,
    structured_rules: Sequence[StructuredPlaceholderRule],
) -> bool:
    """判断一行文本是否命中任一结构化规则。"""
    return any(rule.pattern.search(text) is not None for rule in structured_rules)
