"""Agent 工具箱 DoctorAgentMixin 子服务。"""

import sys

# pyright: reportPrivateUsage=false
# mixin 通过 AgentToolkitService 组合成同一个服务边界，允许调用同门面的受保护核心方法。
from app.agent_toolkit.placeholder_scan import (
    analyze_placeholder_candidates,
    analyze_structured_placeholder_candidates,
)
from app.application.flow_gate import (
    event_command_codes_from_review_state,
    normal_placeholder_scope_hash_from_analysis,
    structured_placeholder_scope_hash_from_analysis,
)
from app.persistence import RuleReviewStateRecord
from app.rule_review import (
    EVENT_COMMAND_TEXT_RULE_DOMAIN,
    MV_VIRTUAL_NAMEBOX_RULE_DOMAIN,
    NOTE_TAG_TEXT_RULE_DOMAIN,
    PLACEHOLDER_RULE_DOMAIN,
    PLUGIN_TEXT_RULE_DOMAIN,
    STRUCTURED_PLACEHOLDER_RULE_DOMAIN,
    event_command_rule_scope_hash_for_snapshots,
    mv_virtual_namebox_rule_scope_hash,
    note_tag_rule_scope_hash_for_candidates,
    plugin_rule_scope_hash,
)

from .common import (
    AgentIssue,
    AgentReport,
    AgentServiceContext,
    JsonObject,
    JsonValue,
    PlaceholderRuleRecord,
    StructuredPlaceholderRuleRecord,
    TextRules,
    _append_check,
    _current_python_major_minor,
    count_uncovered_candidates,
    ensure_db_directory,
    issue,
    load_environment_overrides,
    load_setting,
    platform,
    resolve_app_path,
    resolve_replacement_font_path,
    resolve_setting_path,
)


class DoctorAgentMixin:
    """承载 AgentToolkitService 的 DoctorAgentMixin 命令族。"""

    async def doctor(self: AgentServiceContext, *, game_title: str | None, check_llm: bool) -> AgentReport:
        """检查项目配置、模型连接和可选目标游戏状态。"""
        errors: list[AgentIssue] = []
        warnings: list[AgentIssue] = []
        summary: JsonObject = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "setting_path": str(resolve_setting_path(self.setting_path)),
        }
        details: JsonObject = {
            "environment_overrides": [],
            "checks": [],
        }

        python_major, python_minor = _current_python_major_minor()
        if (python_major, python_minor) < (3, 14):
            errors.append(issue("python_version", "当前 Python 版本低于项目要求的 3.14"))
        else:
            _append_check(details, "python_version", "ok")

        try:
            setting = load_setting(self.setting_path)
            _append_check(details, "setting", "ok")
            summary["llm_model"] = setting.llm.model
            environment_overrides = load_environment_overrides()
            enabled_names: list[JsonValue] = list(environment_overrides.enabled_names())
            details["environment_overrides"] = enabled_names
            if not setting.llm.base_url.strip():
                errors.append(issue("llm_base_url", "模型服务地址为空"))
            if not setting.llm.api_key.strip():
                errors.append(issue("llm_api_key", "模型 API Key 为空"))
            if check_llm:
                try:
                    self.llm_handler.configure(
                        base_url=setting.llm.base_url,
                        api_key=setting.llm.api_key,
                        timeout=setting.llm.timeout,
                        request_body_extra=setting.llm.request_body_extra,
                    )
                    await self.llm_check(self.llm_handler, setting.llm.model)
                    _append_check(details, "llm", "ok")
                except Exception as error:
                    errors.append(issue("llm", f"模型连通性检查失败: {type(error).__name__}: {error}"))
            else:
                _append_check(details, "llm_local_dialogue_structure", "ok")
                warnings.append(issue("llm_skipped", "仅完成本地对话结构检查，未访问模型"))
        except Exception as error:
            errors.append(issue("setting", f"配置加载失败: {type(error).__name__}: {error}"))
            setting = None

        self._check_static_paths(errors=errors, warnings=warnings, details=details)

        if game_title is not None:
            await self._check_game(
                game_title=game_title,
                setting_available=setting is not None,
                errors=errors,
                warnings=warnings,
                summary=summary,
                details=details,
            )

        return AgentReport.from_parts(
            errors=errors,
            warnings=warnings,
            summary=summary,
            details=details,
        )

    async def _check_game(
        self: AgentServiceContext,
        *,
        game_title: str,
        setting_available: bool,
        errors: list[AgentIssue],
        warnings: list[AgentIssue],
        summary: JsonObject,
        details: JsonObject,
    ) -> None:
        """检查目标游戏数据库、文件和导入状态。"""
        _ = details
        if not setting_available:
            warnings.append(issue("game_skipped", "配置不可用，已跳过目标游戏深度检查"))
            return
        try:
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
                _ = await self._load_active_runtime_game_data(
                    session,
                    include_plugin_source_files=False,
                )
                game_data = await self._load_translation_source_game_data(
                    session,
                    include_plugin_source_files=True,
                )
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
                plugin_rules = analysis_context.plugin_rules
                stale_plugin_rule_count = len(analysis_context.stale_plugin_rules)
                event_rules = analysis_context.event_rules
                note_tag_rules = analysis_context.note_tag_rules
                mv_virtual_namebox_rules = analysis_context.mv_virtual_namebox_rules
                plugin_review_state = await session.read_rule_review_state(rule_domain=PLUGIN_TEXT_RULE_DOMAIN)
                event_review_state = await session.read_rule_review_state(rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN)
                note_review_state = await session.read_rule_review_state(rule_domain=NOTE_TAG_TEXT_RULE_DOMAIN)
                placeholder_review_state = await session.read_rule_review_state(rule_domain=PLACEHOLDER_RULE_DOMAIN)
                structured_placeholder_review_state = await session.read_rule_review_state(
                    rule_domain=STRUCTURED_PLACEHOLDER_RULE_DOMAIN
                )
                mv_virtual_namebox_review_state = await session.read_rule_review_state(
                    rule_domain=MV_VIRTUAL_NAMEBOX_RULE_DOMAIN
                )
                mv_virtual_namebox_candidates = [
                    candidate for candidate in analysis_context.mv_virtual_namebox_candidates
                ]
                terminology_registry = await session.read_terminology_registry()
                terminology_glossary = await session.read_terminology_glossary()
                translation_data_map = analysis_context.translation_data_map
                placeholder_analysis = analyze_placeholder_candidates(translation_data_map, text_rules)
                uncovered_count = count_uncovered_candidates(placeholder_analysis.candidates)
                structured_placeholder_analysis = analyze_structured_placeholder_candidates(
                    translation_data_map,
                    text_rules,
                )
                structured_uncovered_count = structured_placeholder_analysis.uncovered_count
                plugin_rules_reviewed_empty, plugin_rules_review_state_stale = _rule_review_empty_state(
                    state=plugin_review_state,
                    current_scope_hash=plugin_rule_scope_hash(game_data),
                )
                reviewed_event_codes = (
                    None if event_review_state is None else event_command_codes_from_review_state(event_review_state)
                )
                event_rules_reviewed_empty, event_rules_review_state_stale = _rule_review_empty_state(
                    state=event_review_state,
                    current_scope_hash=(
                        "invalid_event_command_scope_payload"
                        if reviewed_event_codes is None
                        else event_command_rule_scope_hash_for_snapshots(
                            command_snapshots=analysis_context.event_command_snapshots,
                            command_codes=reviewed_event_codes,
                        )
                    ),
                )
                note_rules_reviewed_empty, note_rules_review_state_stale = _rule_review_empty_state(
                    state=note_review_state,
                    current_scope_hash=note_tag_rule_scope_hash_for_candidates(
                        [candidate for candidate in analysis_context.note_candidates]
                    ),
                )
                placeholder_rules_reviewed_empty, placeholder_rules_review_state_stale = _rule_review_empty_state(
                    state=placeholder_review_state,
                    current_scope_hash=normal_placeholder_scope_hash_from_analysis(placeholder_analysis),
                )
                (
                    structured_placeholder_rules_reviewed_empty,
                    structured_placeholder_rules_review_state_stale,
                ) = _rule_review_empty_state(
                    state=structured_placeholder_review_state,
                    current_scope_hash=structured_placeholder_scope_hash_from_analysis(structured_placeholder_analysis),
                )
                mv_virtual_namebox_rules_reviewed_empty, mv_virtual_namebox_rules_review_state_stale = (
                    _rule_review_empty_state(
                        state=mv_virtual_namebox_review_state,
                        current_scope_hash=mv_virtual_namebox_rule_scope_hash(mv_virtual_namebox_candidates),
                    )
                    if game_data.layout.engine_kind == "mv"
                    else (False, False)
                )
                summary["game_registered"] = True
                summary["game_id"] = session.game_id
                summary["source_language"] = session.source_language
                summary["additional_source_languages"] = list(session.additional_source_languages)
                summary["target_language"] = session.target_language
                summary["plugin_rule_count"] = sum(len(rule.path_templates) for rule in plugin_rules)
                summary["stale_plugin_rule_count"] = stale_plugin_rule_count
                summary["event_command_rule_count"] = sum(len(rule.path_templates) for rule in event_rules)
                summary["note_tag_rule_count"] = sum(len(rule.tag_names) for rule in note_tag_rules)
                summary["mv_virtual_namebox_candidate_count"] = len(mv_virtual_namebox_candidates)
                summary["mv_virtual_namebox_rule_count"] = len(mv_virtual_namebox_rules)
                summary["plugin_rules_reviewed_empty"] = plugin_rules_reviewed_empty
                summary["plugin_rules_review_state_stale"] = plugin_rules_review_state_stale
                summary["event_command_rules_reviewed_empty"] = event_rules_reviewed_empty
                summary["event_command_rules_review_state_stale"] = event_rules_review_state_stale
                summary["note_tag_rules_reviewed_empty"] = note_rules_reviewed_empty
                summary["note_tag_rules_review_state_stale"] = note_rules_review_state_stale
                summary["mv_virtual_namebox_rules_reviewed_empty"] = mv_virtual_namebox_rules_reviewed_empty
                summary["mv_virtual_namebox_rules_review_state_stale"] = mv_virtual_namebox_rules_review_state_stale
                summary["placeholder_rule_count"] = len(custom_rules)
                summary["structured_placeholder_rule_count"] = len(structured_rules)
                summary["placeholder_rules_reviewed_empty"] = placeholder_rules_reviewed_empty
                summary["placeholder_rules_review_state_stale"] = placeholder_rules_review_state_stale
                summary["structured_placeholder_rules_reviewed_empty"] = structured_placeholder_rules_reviewed_empty
                summary["structured_placeholder_rules_review_state_stale"] = (
                    structured_placeholder_rules_review_state_stale
                )
                summary["terminology_imported"] = terminology_registry is not None
                summary["glossary_imported"] = terminology_glossary is not None
                if not plugin_rules and stale_plugin_rule_count == 0:
                    if plugin_rules_review_state_stale:
                        warnings.append(
                            issue(
                                "plugin_rules_review_state_stale",
                                "插件文本规则曾确认为空，但当前插件配置已变化，请重新导出并检查插件规则",
                            )
                        )
                    elif not plugin_rules_reviewed_empty:
                        warnings.append(issue("plugin_rules", "当前游戏尚未导入插件文本规则"))
                if stale_plugin_rule_count:
                    warnings.append(
                        issue(
                            "stale_plugin_rules",
                            f"发现 {stale_plugin_rule_count} 个过期插件规则，请重新导出并导入插件规则",
                        )
                    )
                if not event_rules:
                    if event_rules_review_state_stale:
                        warnings.append(
                            issue(
                                "event_command_rules_review_state_stale",
                                "事件指令文本规则曾确认为空，但当前事件指令参数已变化，请重新导出并检查事件指令规则",
                            )
                        )
                    elif not event_rules_reviewed_empty:
                        warnings.append(issue("event_command_rules", "当前游戏尚未导入事件指令文本规则"))
                if not note_tag_rules:
                    if note_rules_review_state_stale:
                        warnings.append(
                            issue(
                                "note_tag_rules_review_state_stale",
                                "Note 标签规则曾确认为空，但当前 Note 文本已变化，请重新导出并检查 Note 标签规则",
                            )
                        )
                    elif not note_rules_reviewed_empty:
                        warnings.append(issue("note_tag_rules", "当前游戏尚未导入 Note 标签文本规则"))
                if game_data.layout.engine_kind == "mv" and not mv_virtual_namebox_rules:
                    if mv_virtual_namebox_rules_review_state_stale:
                        warnings.append(
                            issue(
                                "mv_virtual_namebox_rules_review_state_stale",
                                "MV 虚拟名字框规则曾确认为空，但当前候选已变化，请重新导出并检查 MV 虚拟名字框规则",
                            )
                        )
                    elif not mv_virtual_namebox_rules_reviewed_empty:
                        warnings.append(issue("mv_virtual_namebox_rules", "当前 MV 游戏尚未导入 MV 虚拟名字框规则"))
                if terminology_registry is None:
                    warnings.append(issue("terminology", "当前游戏尚未导入字段译名表"))
                if terminology_glossary is None:
                    warnings.append(issue("glossary", "当前游戏尚未导入正文术语表"))
                if not custom_rules:
                    if placeholder_rules_review_state_stale:
                        warnings.append(
                            issue(
                                "placeholder_rules_review_state_stale",
                                "普通占位符规则曾确认为空，但当前正文候选已变化，请重新扫描并检查普通占位符规则",
                            )
                        )
                    elif not placeholder_rules_reviewed_empty:
                        warnings.append(issue("placeholder_rules", "当前游戏尚未导入自定义占位符规则"))
                if not structured_rules:
                    if structured_placeholder_rules_review_state_stale:
                        warnings.append(
                            issue(
                                "structured_placeholder_rules_review_state_stale",
                                "结构化占位符规则曾确认为空，但当前正文候选已变化，请重新扫描并检查结构化占位符规则",
                            )
                        )
                    elif not structured_placeholder_rules_reviewed_empty:
                        warnings.append(issue("structured_placeholder_rules", "当前游戏尚未导入结构化占位符规则"))
                font_path = setting.write_back.replacement_font_path
                if font_path is not None:
                    try:
                        _ = resolve_replacement_font_path(font_path)
                    except (FileNotFoundError, ValueError) as error:
                        warnings.append(issue("replacement_font", f"配置的候选覆盖字体文件不可用: {error}"))
                summary["uncovered_placeholder_count"] = uncovered_count
                summary["uncovered_structured_placeholder_count"] = structured_uncovered_count
                if uncovered_count:
                    warnings.append(
                        issue("uncovered_placeholder", f"存在 {uncovered_count} 个未覆盖的疑似自定义控制符")
                    )
                if structured_uncovered_count:
                    warnings.append(
                        issue(
                            "uncovered_structured_placeholder",
                            f"存在 {structured_uncovered_count} 个未覆盖的结构化协议外壳候选",
                        )
                    )
        except Exception as error:
            errors.append(issue("game", f"目标游戏检查失败: {type(error).__name__}: {error}"))

    def _check_static_paths(
        self: AgentServiceContext,
        *,
        errors: list[AgentIssue],
        warnings: list[AgentIssue],
        details: JsonObject,
    ) -> None:
        """检查项目固定目录和终端编码。"""
        _ = warnings
        db_dir = self.game_registry.db_directory
        db_dir_already_exists = db_dir.exists()
        try:
            _ = ensure_db_directory(db_dir)
            _append_check(details, "db_dir", "ok" if db_dir_already_exists else "created")
        except Exception as error:
            errors.append(issue("db_dir", f"数据库目录创建失败: {type(error).__name__}: {error}"))
        logs_dir = resolve_app_path("logs")
        if not logs_dir.exists():
            _ = logs_dir.mkdir(exist_ok=True)
        try:
            encoding = sys.stdout.encoding or ""
            details["stdout_encoding"] = encoding
            _append_check(details, "stdout_encoding", "ok" if "utf" in encoding.lower() else "warning")
            if "utf" not in encoding.lower():
                warnings.append(issue("stdout_encoding", "当前 stdout 不是 UTF-8，可能影响 Agent 解析 JSON 输出"))
        except Exception as error:
            warnings.append(issue("stdout_encoding", f"终端编码检查失败: {type(error).__name__}: {error}"))


def _rule_review_empty_state(
    *,
    state: RuleReviewStateRecord | None,
    current_scope_hash: str,
) -> tuple[bool, bool]:
    """判断空规则审查状态是否仍适用于当前输入范围。"""
    if state is None or not state.reviewed_empty:
        return False, False
    state_stale = state.scope_hash != current_scope_hash
    return not state_stale, state_stale
