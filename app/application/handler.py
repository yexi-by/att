"""
核心 CLI 翻译编排模块。

本模块串起游戏注册、外部规则导入、正文翻译、已保存译文复用与游戏文件回写。
"""

import asyncio
import tempfile
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from app.application.errors import (
    ApplicationBusinessError,
    WorkflowGateError,
    WriteBackGateError,
)
from app.application.flow_gate import (
    assert_workflow_gate_passed,
    ensure_empty_rule_confirmed,
    event_command_rule_codes_for_setting,
)
from app.application.font_replacement import (
    collect_replacement_font_names,
    plan_font_references_from_origin_backups,
)
from app.application.font_replacement.constants import (
    FONTS_DIRECTORY_NAME,
    GAMEFONT_CSS_FILE_NAME,
    GAMEFONT_CSS_ORIGIN_FILE_NAME,
)
from app.application.font_replacement.css import replace_gamefont_css_text
from app.application.font_replacement.files import (
    collect_replaced_source_font_names,
    resolve_replacement_font_path,
)
from app.application.mutation_guard import open_game_for_mutation, open_game_for_recovery
from app.application.rule_import_backup import write_rule_import_translation_backup
from app.application.runtime import load_runtime_setting
from app.application.summaries import (
    EventCommandJsonExportSummary,
    EventCommandRuleImportSummary,
    FontRestoreSummary,
    NoteTagJsonExportSummary,
    NoteTagRuleImportSummary,
    PluginJsonExportSummary,
    PluginRuleImportSummary,
    TerminologyImportSummary,
    TerminologyWriteSummary,
    TextTranslationSummary,
    WriteBackSummary,
    WriteTransactionRecoverySummary,
)
from app.application.use_cases.translation_run import (
    TranslationProgressState,
    TranslationRunLimits,
    build_llm_failure_record,
    build_translation_batches,
    count_translation_items,
    deduplicate_translation_data,
    filter_pending_translation_data,
    limit_translation_data,
)
from app.application.write_back_gate import assert_write_back_quality_passed
from app.application.write_transaction import (
    DurableFileWriteTransaction,
    FileWriteManifestEntry,
    PlannedFileWrite,
    file_write_transaction_journal_path,
    new_file_write_transaction_id,
)
from app.config import (
    SettingOverrides,
    load_custom_placeholder_rules_text,
)
from app.config.schemas import Setting
from app.event_command_text import (
    EventCommandAnalysisEntry,
    build_event_command_analysis_index,
    build_event_command_rule_records_from_import,
    command_matches_filters,
    event_command_analysis_snapshots,
    event_command_rule_key,
    export_event_commands_json_file,
    load_event_command_rule_import_file,
    resolve_event_command_codes,
)
from app.game_analysis import build_game_analysis_context
from app.language import DEFAULT_SOURCE_LANGUAGE, SourceLanguage
from app.llm import LLMHandler
from app.native_quality import build_native_text_rules_payload
from app.native_write_plan import build_native_write_back_plan
from app.note_tag_text import (
    NoteTagTextExtraction,
    build_note_tag_rule_records_from_import,
    export_note_tag_candidates_file,
    load_note_tag_rule_import_file,
)
from app.note_tag_text.exporter import collect_note_tag_candidates_from_sources
from app.note_tag_text.sources import collect_note_tag_sources
from app.observability.logging import logger
from app.persistence import (
    GameRegistry,
    RecoveryRequiredError,
    TargetGameSession,
    TranslationRunRecoveryRequiredError,
)
from app.persistence.records import (
    WriteTransactionFileRecord,
    WriteTransactionPayload,
    WriteTransactionRecord,
)
from app.persistence.repository import current_timestamp_text
from app.persistence.translation_run_recovery import (
    build_bounded_persistence_failure_text,
    translation_run_stable_fingerprint,
)
from app.plugin_source_text import audit_active_runtime_plugin_source
from app.plugin_text import (
    build_plugin_parameter_analysis_index,
    build_plugin_rule_records_from_import,
    export_plugins_json_file,
    load_plugin_rule_import_file,
)
from app.rmmz.control_codes import CustomPlaceholderRule, StructuredPlaceholderRule
from app.rmmz.game_file_view import GameFileView
from app.rmmz.loader import (
    load_active_runtime_game_data,
    load_game_data_for_view,
    read_game_title,
    resolve_game_directory,
    resolve_game_layout,
)
from app.rmmz.schema import (
    PLUGINS_FILE_NAME,
    EventCommandTextRuleRecord,
    FontReplacementRecord,
    GameData,
    LlmFailureRecord,
    NoteTagTextRuleRecord,
    PlaceholderRuleRecord,
    PluginSourceRuntimeWriteMapRecord,
    PluginTextRuleRecord,
    StructuredPlaceholderRuleRecord,
    TranslationData,
    TranslationItem,
    TranslationRunRecord,
)
from app.rmmz.source_snapshot import validate_source_snapshot_manifest
from app.rmmz.text_rules import JsonObject, TextRules
from app.rule_review import (
    EVENT_COMMAND_TEXT_RULE_DOMAIN,
    NOTE_TAG_TEXT_RULE_DOMAIN,
    PLUGIN_TEXT_RULE_DOMAIN,
    event_command_rule_scope_hash_for_snapshots,
    note_tag_rule_scope_hash_for_candidates,
    plugin_rule_scope_hash,
)
from app.source_residual import SourceResidualRuleSet
from app.terminology import (
    TerminologyExportSummary,
    TerminologyExtraction,
    TerminologyPromptIndex,
    TerminologyRegistry,
    export_terminology_artifacts,
    load_terminology_glossary,
    load_terminology_registry,
    validate_terminology_bundle,
)
from app.text_scope import TextScopeResult, collect_translation_data_paths
from app.translation import (
    PromptItemTooLargeError,
    TextTranslation,
    TranslationBatch,
    TranslationBatchPlan,
    TranslationCache,
    TranslationCandidateValidator,
    TranslationRunCancelled,
    build_translation_reuse_contexts_by_path,
    evaluate_translation_freshness,
    translation_record_matches_current_target,
    verify_translation_batch_result,
)
from app.translation.reuse import (
    SavedTranslationReuseResult,
    collect_saved_translation_reuse,
    expand_current_run_reuse,
)
from app.translation.run_controller import (
    BatchExecutionResult,
    PersistedBatchCounts,
    TranslationRunResult,
)
from app.utils.config_loader_utils import load_setting

type WriteRuntimeMode = Literal["write_back", "rebuild_active_runtime", "write_terminology"]
type WriteProgressCallbacks = (
    tuple[Callable[[int, int], None], Callable[[int], None]]
    | tuple[Callable[[int, int], None], Callable[[int], None], Callable[[str], None]]
)


@dataclass(frozen=True, slots=True)
class PreparedWriteOperation:
    """写入游戏文件前已经完成门禁检查的上下文。"""

    game_data: GameData
    setting: Setting
    text_rules: TextRules
    translated_items: list[TranslationItem]
    writable_location_paths: list[str]
    scope: TextScopeResult
    pre_write_check_ms: int = 0


def _unpack_write_progress_callbacks(
    callbacks: WriteProgressCallbacks,
) -> tuple[Callable[[int, int], None], Callable[[int], None], Callable[[str], None]]:
    """拆分写文件进度回调和阶段状态回调。"""
    if len(callbacks) == 3:
        return callbacks[0], callbacks[1], callbacks[2]
    progress_callbacks = callbacks
    set_progress, advance_progress = progress_callbacks

    def set_status(status: str) -> None:
        logger.debug(f"[tag.phase]写文件阶段[/tag.phase] {status}")

    return set_progress, advance_progress, set_status


class TranslationHandler:
    """核心 CLI 翻译业务总编排器。"""

    def __init__(
        self,
        game_registry: GameRegistry,
        llm_handler: LLMHandler,
    ) -> None:
        """初始化编排器。"""
        self.game_registry: GameRegistry = game_registry
        self.llm_handler: LLMHandler = llm_handler

    @classmethod
    async def create(cls) -> Self:
        """创建编排器，不打开任何游戏数据库。"""
        game_registry = GameRegistry()
        llm_handler = LLMHandler()
        logger.info("[tag.phase]编排器初始化完成[/tag.phase] 数据库将在目标命令执行时按需打开")
        return cls(game_registry, llm_handler)

    async def close(self) -> None:
        """释放编排器持有的运行时资源。"""
        self.llm_handler.clean()

    def _load_runtime_setting(
        self,
        setting_overrides: SettingOverrides | None = None,
        source_language: SourceLanguage = DEFAULT_SOURCE_LANGUAGE,
        additional_source_languages: tuple[SourceLanguage, ...] = (),
    ) -> Setting:
        """加载配置并按本轮命令重置模型服务。"""
        return load_runtime_setting(
            self.llm_handler,
            overrides=setting_overrides,
            source_language=source_language,
            additional_source_languages=additional_source_languages,
        )

    def _load_setting(
        self,
        setting_overrides: SettingOverrides | None = None,
        source_language: SourceLanguage = DEFAULT_SOURCE_LANGUAGE,
        additional_source_languages: tuple[SourceLanguage, ...] = (),
    ) -> Setting:
        """加载当前配置，不改动模型服务连接状态。"""
        return load_setting(
            overrides=setting_overrides,
            source_language=source_language,
            additional_source_languages=additional_source_languages,
        )

    def _load_text_rules(
        self,
        setting: Setting,
        custom_placeholder_rules_text: str | None = None,
        placeholder_rule_records: list[PlaceholderRuleRecord] | None = None,
        structured_placeholder_rule_records: list[StructuredPlaceholderRuleRecord] | None = None,
    ) -> TextRules:
        """加载文本过滤规则、自定义占位符规则和结构化占位符规则。"""
        if custom_placeholder_rules_text is not None:
            custom_rules = load_custom_placeholder_rules_text(custom_placeholder_rules_text)
            source_label = "CLI 参数"
        elif placeholder_rule_records is not None:
            custom_rules = tuple(
                CustomPlaceholderRule.create(
                    pattern_text=record.pattern_text,
                    placeholder_template=record.placeholder_template,
                )
                for record in placeholder_rule_records
            )
            source_label = "当前游戏数据库"
        else:
            custom_rules = ()
            source_label = "空规则"

        structured_rules = self._build_structured_placeholder_rules(structured_placeholder_rule_records or [])
        if custom_rules:
            logger.info(
                f"[tag.phase]已加载自定义占位符规则[/tag.phase] 来源 {source_label} 数量 [tag.count]{len(custom_rules)}[/tag.count] 条"
            )
        elif custom_placeholder_rules_text is not None:
            logger.info("[tag.skip]CLI 指定的自定义占位符规则为空对象[/tag.skip]")
        if structured_rules:
            logger.info(
                f"[tag.phase]已加载结构化占位符规则[/tag.phase] 来源 当前游戏数据库 数量 [tag.count]{len(structured_rules)}[/tag.count] 条"
            )
        return TextRules.from_setting(
            setting.text_rules,
            custom_placeholder_rules=custom_rules,
            structured_placeholder_rules=structured_rules,
        )

    def _build_structured_placeholder_rules(
        self,
        records: list[StructuredPlaceholderRuleRecord],
    ) -> tuple[StructuredPlaceholderRule, ...]:
        """把数据库结构化占位符规则转换成运行时规则。"""
        return tuple(
            StructuredPlaceholderRule.create(
                rule_name=record.rule_name,
                rule_type=record.rule_type,
                pattern_text=record.pattern_text,
                translatable_group=record.translatable_group,
                protected_groups=dict(record.protected_groups),
            )
            for record in records
        )

    async def _load_session_profile_text_rules(self, session: TargetGameSession) -> TextRules:
        """按当前配置和已导入占位符规则构造文本判断规则。"""
        setting = self._load_setting(
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
        )
        placeholder_records = await session.read_placeholder_rules()
        structured_placeholder_records = await session.read_structured_placeholder_rules()
        return self._load_text_rules(
            setting,
            placeholder_rule_records=placeholder_records,
            structured_placeholder_rule_records=structured_placeholder_records,
        )

    async def _load_session_game_data(self, session: TargetGameSession) -> GameData:
        """加载目标游戏数据并绑定到当前命令会话。"""
        game_data = await load_game_data_for_view(
            session.game_path,
            source_view=GameFileView.TRANSLATION_SOURCE,
        )
        snapshot_records = await session.read_source_snapshot_records()
        if not snapshot_records:
            raise ApplicationBusinessError("当前游戏缺少可信源快照 manifest，请使用干净游戏目录重新执行 add-game")
        validate_source_snapshot_manifest(
            layout=game_data.layout,
            records=snapshot_records,
        )
        session.set_game_data(game_data)
        return session.require_game_data()

    async def resolve_game_title_by_path(self, game_path: str | Path) -> str:
        """根据已注册游戏目录解析可用于 CLI 的游戏标题。"""
        return await self.game_registry.resolve_registered_title_by_path(game_path)

    async def add_game(
        self,
        game_path: str | Path,
        source_language: SourceLanguage,
        additional_source_languages: tuple[SourceLanguage, ...] = (),
    ) -> str:
        """注册一个新的游戏。"""
        resolved_game_path = resolve_game_directory(game_path)
        layout = resolve_game_layout(resolved_game_path)
        game_title = read_game_title(resolved_game_path)
        record = await self.game_registry.register_game(
            resolved_game_path,
            source_language=source_language,
            additional_source_languages=additional_source_languages,
        )
        logger.success(
            f"[tag.success]游戏已加入核心 CLI[/tag.success] 标题 [tag.count]{game_title}[/tag.count] 引擎 [tag.count]{layout.engine_label}[/tag.count] 源语言 [tag.count]{source_language}[/tag.count] 数据目录 [tag.path]{layout.data_dir}[/tag.path] 路径 [tag.path]{record.game_path}[/tag.path]"
        )
        return game_title

    async def import_plugin_rules(
        self,
        game_title: str,
        input_path: Path,
        confirm_empty: bool = False,
    ) -> PluginRuleImportSummary:
        """把外部插件规则 JSON 导入当前游戏数据库。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            game_data = await self._load_session_game_data(session)
            text_rules = await self._load_session_profile_text_rules(session)
            import_file = await load_plugin_rule_import_file(input_path)
            plugin_index = build_plugin_parameter_analysis_index(game_data)
            rule_records = build_plugin_rule_records_from_import(
                game_data=game_data,
                import_file=import_file,
                text_rules=text_rules,
                plugin_index=plugin_index,
            )
            if not rule_records:
                ensure_empty_rule_confirmed(
                    rule_label="插件规则",
                    confirm_empty=confirm_empty,
                )
            old_rules = {rule.plugin_index: rule for rule in await session.read_plugin_text_rules()}
            deleted_translation_items = 0
            deleted_translation_backup_path: str | None = None
            stale_prefixes: set[str] = set()
            for rule_record in rule_records:
                old_rule = old_rules.get(rule_record.plugin_index)
                if self._should_refresh_plugin_translation_items(old_rule, rule_record):
                    stale_prefixes.add(f"{PLUGINS_FILE_NAME}/{rule_record.plugin_index}/")
            new_plugin_indexes = {rule.plugin_index for rule in rule_records}
            for plugin_index in sorted(set(old_rules) - new_plugin_indexes):
                stale_prefixes.add(f"{PLUGINS_FILE_NAME}/{plugin_index}/")
            if stale_prefixes:
                stale_items = await session.read_translated_items_by_prefixes(sorted(stale_prefixes))
                backup = await write_rule_import_translation_backup(
                    game_title=game_title,
                    domain="plugin-rules",
                    items=stale_items,
                )
                if backup is not None:
                    deleted_translation_backup_path = backup.backup_path
                deleted_translation_items = await session.delete_translation_items_by_prefixes(
                    sorted(stale_prefixes),
                )
            await session.replace_plugin_text_rules(rule_records)
            if rule_records:
                await session.delete_rule_review_state(rule_domain=PLUGIN_TEXT_RULE_DOMAIN)
            else:
                await session.replace_rule_review_state(
                    rule_domain=PLUGIN_TEXT_RULE_DOMAIN,
                    scope_hash=plugin_rule_scope_hash(game_data),
                    reviewed_empty=True,
                )
        imported_rule_count = sum(len(record.path_templates) for record in rule_records)
        logger.success(
            f"[tag.success]插件规则导入完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 插件 [tag.count]{len(rule_records)}[/tag.count] 个，规则 [tag.count]{imported_rule_count}[/tag.count] 条，清理失效译文 [tag.count]{deleted_translation_items}[/tag.count] 条"
        )
        if deleted_translation_backup_path is not None:
            logger.warning(
                f"[tag.warning]已备份被清理的插件译文[/tag.warning] 文件 [tag.path]{deleted_translation_backup_path}[/tag.path]"
            )
        return PluginRuleImportSummary(
            imported_plugin_count=len(rule_records),
            imported_rule_count=imported_rule_count,
            deleted_translation_items=deleted_translation_items,
            deleted_translation_backup_path=deleted_translation_backup_path,
        )

    async def export_plugins_json(
        self,
        game_title: str,
        output_path: Path,
    ) -> PluginJsonExportSummary:
        """把当前游戏的 plugins.js 导出为纯 JSON。"""
        async with await self.game_registry.open_game(game_title) as session:
            game_data = await self._load_session_game_data(session)
            resolved_output_path = output_path.resolve()
            await export_plugins_json_file(game_data=game_data, output_path=resolved_output_path)
            logger.success(
                f"[tag.success]插件配置 JSON 导出完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 插件 [tag.count]{len(game_data.plugins_js)}[/tag.count] 个 文件 [tag.path]{resolved_output_path}[/tag.path]"
            )
            return PluginJsonExportSummary(
                output_path=str(resolved_output_path),
                plugin_count=len(game_data.plugins_js),
            )

    async def export_event_commands_json(
        self,
        game_title: str,
        output_path: Path,
        command_codes: set[int] | None,
        default_command_codes_override: list[int] | None = None,
    ) -> EventCommandJsonExportSummary:
        """把指定事件指令的原始参数导出为 JSON。"""
        async with await self.game_registry.open_game(game_title) as session:
            game_data = await self._load_session_game_data(session)
            resolved_output_path = output_path.resolve()
            default_command_codes: list[int] | None = None
            if command_codes is None:
                if default_command_codes_override is not None:
                    default_command_codes = default_command_codes_override
                else:
                    setting = self._load_setting(
                        source_language=session.source_language,
                        additional_source_languages=session.additional_source_languages,
                    )
                    default_command_codes = setting.event_command_text.default_codes_for_engine(
                        game_data.layout.engine_kind
                    )
            effective_command_codes = resolve_event_command_codes(
                command_codes=command_codes,
                default_command_codes=default_command_codes,
            )
            command_count = await export_event_commands_json_file(
                game_data=game_data,
                output_path=resolved_output_path,
                command_codes=effective_command_codes,
            )
            code_label = ", ".join(map(str, sorted(effective_command_codes)))
            logger.success(
                f"[tag.success]事件指令参数 JSON 导出完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 编码 [tag.count]{code_label}[/tag.count] 指令 [tag.count]{command_count}[/tag.count] 条 文件 [tag.path]{resolved_output_path}[/tag.path]"
            )
            return EventCommandJsonExportSummary(
                output_path=str(resolved_output_path),
                command_count=command_count,
            )

    async def export_note_tag_candidates(
        self,
        game_title: str,
        output_path: Path,
    ) -> NoteTagJsonExportSummary:
        """把当前游戏 data Note 标签候选导出为 JSON。"""
        async with await self.game_registry.open_game(game_title) as session:
            setting = self._load_setting(
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            game_data = await self._load_session_game_data(session)
            text_rules = self._load_text_rules(
                setting=setting,
                placeholder_rule_records=await session.read_placeholder_rules(),
                structured_placeholder_rule_records=await session.read_structured_placeholder_rules(),
            )
            resolved_output_path = output_path.resolve()
            report = await export_note_tag_candidates_file(
                game_data=game_data,
                output_path=resolved_output_path,
                text_rules=text_rules,
            )
        logger.success(
            f"[tag.success]Note 标签候选 JSON 导出完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 候选标签 [tag.count]{report.candidate_tag_count}[/tag.count] 个 文件 [tag.path]{resolved_output_path}[/tag.path]"
        )
        return NoteTagJsonExportSummary(
            output_path=str(resolved_output_path),
            candidate_tag_count=report.candidate_tag_count,
            translatable_value_count=report.translatable_value_count,
        )

    async def import_event_command_rules(
        self,
        game_title: str,
        input_path: Path,
        confirm_empty: bool = False,
        command_codes: set[int] | None = None,
        default_command_codes_override: list[int] | None = None,
    ) -> EventCommandRuleImportSummary:
        """把外部事件指令规则 JSON 导入当前游戏数据库。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            game_data = await self._load_session_game_data(session)
            import_file = await load_event_command_rule_import_file(input_path)
            command_index = build_event_command_analysis_index(game_data)
            rule_records = build_event_command_rule_records_from_import(
                game_data=game_data,
                import_file=import_file,
                command_index=command_index,
            )
            empty_review_scope_hash: str | None = None
            effective_command_codes: frozenset[int] | None = None
            if not rule_records:
                ensure_empty_rule_confirmed(
                    rule_label="事件指令规则",
                    confirm_empty=confirm_empty,
                )
                if command_codes is None:
                    if default_command_codes_override is not None:
                        effective_command_codes = resolve_event_command_codes(
                            command_codes=None,
                            default_command_codes=default_command_codes_override,
                        )
                    else:
                        setting = self._load_setting(
                            source_language=session.source_language,
                            additional_source_languages=session.additional_source_languages,
                        )
                        effective_command_codes = event_command_rule_codes_for_setting(
                            game_data=game_data,
                            setting=setting,
                        )
                else:
                    effective_command_codes = resolve_event_command_codes(
                        command_codes=command_codes,
                        default_command_codes=None,
                    )
                empty_review_scope_hash = event_command_rule_scope_hash_for_snapshots(
                    command_snapshots=event_command_analysis_snapshots(command_index),
                    command_codes=effective_command_codes,
                )
            old_rules = {event_command_rule_key(rule): rule for rule in await session.read_event_command_text_rules()}
            deleted_translation_items = 0
            deleted_translation_backup_path: str | None = None
            stale_prefixes: set[str] = set()
            for rule_record in rule_records:
                rule_key = event_command_rule_key(rule_record)
                old_rule = old_rules.get(rule_key)
                if self._should_refresh_event_command_translation_items(old_rule, rule_record):
                    if old_rule is not None:
                        stale_prefixes.update(
                            self._event_command_rule_prefixes(command_index=command_index, rule_record=old_rule),
                        )
                    stale_prefixes.update(
                        self._event_command_rule_prefixes(command_index=command_index, rule_record=rule_record),
                    )
            new_rule_keys = {event_command_rule_key(rule) for rule in rule_records}
            for rule_key, old_rule in old_rules.items():
                if rule_key not in new_rule_keys:
                    stale_prefixes.update(
                        self._event_command_rule_prefixes(command_index=command_index, rule_record=old_rule),
                    )
            if stale_prefixes:
                stale_items = await session.read_translated_items_by_prefixes(sorted(stale_prefixes))
                backup = await write_rule_import_translation_backup(
                    game_title=game_title,
                    domain="event-command-rules",
                    items=stale_items,
                )
                if backup is not None:
                    deleted_translation_backup_path = backup.backup_path
                deleted_translation_items = await session.delete_translation_items_by_prefixes(
                    sorted(stale_prefixes),
                )
            await session.replace_event_command_text_rules(rule_records)
            if rule_records:
                await session.delete_rule_review_state(rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN)
            else:
                if empty_review_scope_hash is None:
                    raise RuntimeError("事件指令空规则确认范围未计算")
                if effective_command_codes is None:
                    raise RuntimeError("事件指令空规则确认编码范围未计算")
                await session.replace_rule_review_state(
                    rule_domain=EVENT_COMMAND_TEXT_RULE_DOMAIN,
                    scope_hash=empty_review_scope_hash,
                    scope_payload={
                        "kind": "event_command_codes",
                        "command_codes": sorted(effective_command_codes),
                    },
                    reviewed_empty=True,
                )
        imported_path_rule_count = sum(len(record.path_templates) for record in rule_records)
        logger.success(
            f"[tag.success]事件指令规则导入完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 规则组 [tag.count]{len(rule_records)}[/tag.count] 个，路径规则 [tag.count]{imported_path_rule_count}[/tag.count] 条，清理失效译文 [tag.count]{deleted_translation_items}[/tag.count] 条"
        )
        if deleted_translation_backup_path is not None:
            logger.warning(
                f"[tag.warning]已备份被清理的事件指令译文[/tag.warning] 文件 [tag.path]{deleted_translation_backup_path}[/tag.path]"
            )
        return EventCommandRuleImportSummary(
            imported_rule_group_count=len(rule_records),
            imported_path_rule_count=imported_path_rule_count,
            deleted_translation_items=deleted_translation_items,
            deleted_translation_backup_path=deleted_translation_backup_path,
        )

    async def import_note_tag_rules(
        self,
        game_title: str,
        input_path: Path,
        confirm_empty: bool = False,
    ) -> NoteTagRuleImportSummary:
        """把外部 Note 标签规则 JSON 导入当前游戏数据库。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            setting = self._load_setting(
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            game_data = await self._load_session_game_data(session)
            text_rules = self._load_text_rules(
                setting=setting,
                placeholder_rule_records=await session.read_placeholder_rules(),
                structured_placeholder_rule_records=await session.read_structured_placeholder_rules(),
            )
            import_file = await load_note_tag_rule_import_file(input_path)
            note_sources = tuple(collect_note_tag_sources(game_data=game_data))
            rule_records = build_note_tag_rule_records_from_import(
                game_data=game_data,
                import_file=import_file,
                text_rules=text_rules,
                note_sources=note_sources,
            )
            if not rule_records:
                ensure_empty_rule_confirmed(
                    rule_label="Note 标签规则",
                    confirm_empty=confirm_empty,
                )
            old_rules = {rule.file_name: rule for rule in await session.read_note_tag_text_rules()}
            old_note_paths = collect_translation_data_paths(
                NoteTagTextExtraction(
                    game_data=game_data,
                    rule_records=list(old_rules.values()),
                    text_rules=text_rules,
                ).extract_all_text_from_sources(note_sources)
            )
            new_note_paths = collect_translation_data_paths(
                NoteTagTextExtraction(
                    game_data=game_data,
                    rule_records=rule_records,
                    text_rules=text_rules,
                ).extract_all_text_from_sources(note_sources)
            )
            changed_rule_count = sum(
                1
                for rule_record in rule_records
                if self._should_refresh_note_tag_translation_items(old_rules.get(rule_record.file_name), rule_record)
            )
            removed_rule_count = len(set(old_rules) - {rule.file_name for rule in rule_records})
            stale_paths = sorted(old_note_paths - new_note_paths)
            deleted_translation_items = 0
            deleted_translation_backup_path: str | None = None
            if stale_paths and (changed_rule_count or removed_rule_count):
                stale_items = await session.read_translated_items_by_paths(stale_paths)
                backup = await write_rule_import_translation_backup(
                    game_title=game_title,
                    domain="note-tag-rules",
                    items=stale_items,
                )
                if backup is not None:
                    deleted_translation_backup_path = backup.backup_path
                deleted_translation_items = await session.delete_translation_items_by_paths(stale_paths)
            await session.replace_note_tag_text_rules(rule_records)
            if rule_records:
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
        imported_tag_count = sum(len(record.tag_names) for record in rule_records)
        logger.success(
            f"[tag.success]Note 标签规则导入完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 文件 [tag.count]{len(rule_records)}[/tag.count] 个，标签 [tag.count]{imported_tag_count}[/tag.count] 个，清理失效译文 [tag.count]{deleted_translation_items}[/tag.count] 条"
        )
        if deleted_translation_backup_path is not None:
            logger.warning(
                f"[tag.warning]已备份被清理的 Note 标签译文[/tag.warning] 文件 [tag.path]{deleted_translation_backup_path}[/tag.path]"
            )
        return NoteTagRuleImportSummary(
            imported_file_count=len(rule_records),
            imported_tag_count=imported_tag_count,
            deleted_translation_items=deleted_translation_items,
            deleted_translation_backup_path=deleted_translation_backup_path,
        )

    async def translate_text(
        self,
        game_title: str,
        setting_overrides: SettingOverrides | None,
        custom_placeholder_rules_text: str | None,
        run_limits: TranslationRunLimits | None,
        callbacks: tuple[
            Callable[[int, int], None],
            Callable[[int], None],
            Callable[[str], None],
        ],
    ) -> TextTranslationSummary:
        """翻译指定游戏的正文。"""
        translation_cache = TranslationCache()
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            setting = self._load_runtime_setting(
                setting_overrides,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            placeholder_rule_records: list[PlaceholderRuleRecord] | None = None
            if custom_placeholder_rules_text is None:
                placeholder_rule_records = await session.read_placeholder_rules()
            structured_placeholder_rule_records = await session.read_structured_placeholder_rules()
            text_rules = self._load_text_rules(
                setting=setting,
                custom_placeholder_rules_text=custom_placeholder_rules_text,
                placeholder_rule_records=placeholder_rule_records,
                structured_placeholder_rule_records=structured_placeholder_rule_records,
            )
            try:
                return await self._translate_text_in_session(
                    session=session,
                    setting=setting,
                    text_rules=text_rules,
                    placeholder_rule_records=placeholder_rule_records or [],
                    structured_placeholder_rule_records=structured_placeholder_rule_records,
                    custom_placeholder_rules_supplied=custom_placeholder_rules_text is not None,
                    translation_cache=translation_cache,
                    run_limits=run_limits or TranslationRunLimits(),
                    callbacks=callbacks,
                )
            except WorkflowGateError as error:
                message = str(error)
                logger.error(
                    f"[tag.failure]正文翻译前置检查未通过[/tag.failure] 游戏 [tag.count]{game_title}[/tag.count] {message}"
                )
                return TextTranslationSummary(
                    total_extracted_items=0,
                    pending_count=0,
                    deduplicated_count=0,
                    batch_count=0,
                    success_count=0,
                    error_count=0,
                    blocked_reason=message,
                    outcome="blocked",
                    stop_code=error.code,
                    stop_message=message,
                )

    async def _translate_text_in_session(
        self,
        *,
        session: TargetGameSession,
        setting: Setting,
        text_rules: TextRules,
        placeholder_rule_records: list[PlaceholderRuleRecord],
        structured_placeholder_rule_records: list[StructuredPlaceholderRuleRecord],
        custom_placeholder_rules_supplied: bool,
        translation_cache: TranslationCache,
        run_limits: TranslationRunLimits,
        callbacks: tuple[
            Callable[[int, int], None],
            Callable[[int], None],
            Callable[[str], None],
        ],
    ) -> TextTranslationSummary:
        """在单游戏数据库会话中翻译正文。"""
        set_progress, advance_progress, set_status = callbacks
        game_title = session.game_title
        game_data = await self._load_session_game_data(session)
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
            placeholder_rules=placeholder_rule_records,
            structured_placeholder_rules=structured_placeholder_rule_records,
        )
        scope = analysis_context.scope
        await assert_workflow_gate_passed(
            session=session,
            context=analysis_context,
            setting=setting,
            custom_placeholder_rules_supplied=custom_placeholder_rules_supplied,
        )
        terminology_prompt_index = await self._load_terminology_prompt_index(
            session=session,
            game_data=game_data,
        )
        translation_data_map = scope.translation_data_map
        source_residual_records = await session.read_source_residual_rules()
        source_residual_rule_set = SourceResidualRuleSet.from_records(source_residual_records)
        source_snapshot_records = await session.read_source_snapshot_records()
        translated_items = list(analysis_context.translated_items)
        freshness = await evaluate_translation_freshness(
            reuse_reader=session,
            translation_cache=translation_cache,
            scope=scope,
            translated_items=translated_items,
            terminology_prompt_index=terminology_prompt_index,
            source_language=setting.text_rules.source_language,
            additional_source_languages=setting.text_rules.additional_source_languages,
            target_language=session.target_language,
            source_snapshot_records=source_snapshot_records,
            source_residual_records=source_residual_records,
        )
        candidate_validator = TranslationCandidateValidator(
            analysis_context=analysis_context,
            translation_cache=translation_cache,
        )

        total_extracted_items = count_translation_items(translation_data_map)
        valid_translated_paths = set(freshness.valid_translated_paths)
        stale_current_paths = list(freshness.stale_current_paths)
        if stale_current_paths:
            logger.warning(
                f"[tag.warning]上下文指纹过期的译文已恢复为待翻译，历史行仅作留档且不得写回[/tag.warning] 游戏 [tag.count]{game_title}[/tag.count] [tag.count]{len(stale_current_paths)}[/tag.count] 条"
            )
        all_pending_translation_data_map = filter_pending_translation_data(
            translation_data_map=translation_data_map,
            translated_paths=valid_translated_paths,
        )
        all_pending_count = count_translation_items(all_pending_translation_data_map)
        pending_translation_data_map = limit_translation_data(
            translation_data_map=all_pending_translation_data_map,
            max_items=run_limits.max_items,
            translation_cache=translation_cache,
        )
        pending_count = count_translation_items(pending_translation_data_map)
        set_progress(0, pending_count)

        if total_extracted_items == 0:
            blocked_reason = "没有提取到任何可翻译正文"
            logger.warning(f"[tag.warning]{blocked_reason}[/tag.warning] 游戏 [tag.count]{game_title}[/tag.count]")
            return TextTranslationSummary(
                total_extracted_items=0,
                pending_count=0,
                deduplicated_count=0,
                batch_count=0,
                success_count=0,
                error_count=0,
                blocked_reason=blocked_reason,
                outcome="blocked",
                stop_code="no_translatable_text",
                stop_message=blocked_reason,
            )

        if all_pending_count == 0:
            logger.info(f"[tag.skip]正文译文已全部存在，跳过翻译[/tag.skip] 游戏 [tag.count]{game_title}[/tag.count]")
            set_progress(total_extracted_items, total_extracted_items)
            return TextTranslationSummary(
                total_extracted_items=total_extracted_items,
                pending_count=0,
                deduplicated_count=0,
                batch_count=0,
                success_count=0,
                error_count=0,
                selected_count=0,
                remaining_count=0,
            )

        try:
            saved_reuse = collect_saved_translation_reuse(
                translation_data_map=pending_translation_data_map,
                translation_cache=translation_cache,
                text_rules=text_rules,
                source_residual_rule_set=source_residual_rule_set,
                validate_candidates=candidate_validator,
            )
        except TranslationRunRecoveryRequiredError:
            raise
        except Exception as error:
            message = f"候选译文校验失败: {type(error).__name__}: {error}"
            logger.error(
                f"[tag.failure]历史译文复验失败[/tag.failure] 游戏 [tag.count]{game_title}[/tag.count] {message}"
            )
            return TextTranslationSummary(
                total_extracted_items=total_extracted_items,
                pending_count=all_pending_count,
                deduplicated_count=0,
                batch_count=0,
                success_count=0,
                error_count=0,
                blocked_reason=message,
                outcome="failed",
                stop_code="candidate_validation_failed",
                stop_message=message,
                selected_count=pending_count,
                remaining_count=all_pending_count,
            )
        pending_translation_data_map = saved_reuse.pending_translation_data_map
        deduplicated_translation_data_map = deduplicate_translation_data(
            translation_data_map=pending_translation_data_map,
            translation_cache=translation_cache,
        )
        deduplicated_count = count_translation_items(deduplicated_translation_data_map)
        try:
            all_batches = build_translation_batches(
                translation_data_map=deduplicated_translation_data_map,
                setting=setting,
                text_rules=text_rules,
                terminology_prompt_index=terminology_prompt_index,
                translation_cache=translation_cache,
            )
        except PromptItemTooLargeError as error:
            message = str(error)
            return TextTranslationSummary(
                total_extracted_items=total_extracted_items,
                pending_count=all_pending_count,
                deduplicated_count=deduplicated_count,
                batch_count=0,
                success_count=0,
                error_count=0,
                blocked_reason=message,
                outcome="failed",
                stop_code="prompt_item_too_large",
                stop_message=message,
                selected_count=pending_count,
                remaining_count=all_pending_count,
            )
        all_deduplicated_count = all_batches.item_count

        run_record = await session.start_translation_run(
            total_extracted=total_extracted_items,
            pending_count=all_pending_count,
            deduplicated_count=all_deduplicated_count,
            batch_count=len(all_batches),
        )
        try:
            max_batches_label = "全部" if run_limits.max_batches is None else str(run_limits.max_batches)
            set_status(
                f"还没成功保存译文 {pending_count} 条，相同原文合并后计划 {all_deduplicated_count} 条，批次计划 {len(all_batches)} 个，本轮最多派发 {max_batches_label} 个"
            )
            logger.info(
                f"[tag.phase]正文翻译开始[/tag.phase] 游戏 [tag.count]{game_title}[/tag.count] 提取 [tag.count]{total_extracted_items}[/tag.count] 条，还没成功保存译文 [tag.count]{pending_count}[/tag.count] 条，相同原文合并后计划 [tag.count]{all_deduplicated_count}[/tag.count] 条，批次计划 [tag.count]{len(all_batches)}[/tag.count] 个，本轮最多派发 [tag.count]{max_batches_label}[/tag.count] 个"
            )
            text_translation = TextTranslation(
                setting=setting,
                text_rules=text_rules,
                source_residual_rule_set=source_residual_rule_set,
            )
            run_result, progress_state = await self._run_text_translation_batches(
                text_translation=text_translation,
                session=session,
                batches=all_batches,
                run_record=run_record,
                advance_progress=advance_progress,
                translation_cache=translation_cache,
                source_residual_rule_set=source_residual_rule_set,
                candidate_validator=candidate_validator,
                terminology_prompt_index=terminology_prompt_index,
                saved_reuse=saved_reuse,
                max_batches=run_limits.max_batches,
                time_limit_seconds=run_limits.time_limit_seconds,
                stop_on_error_rate=run_limits.stop_on_error_rate,
            )
        except asyncio.CancelledError as error:
            partial_result = error.result if isinstance(error, TranslationRunCancelled) else None
            cancelled_message = "用户取消了正文翻译"
            cancelled_persistence = await _await_write_transaction_housekeeping(
                _finalize_interrupted_translation_run(
                    session=session,
                    run_record=run_record,
                    status="cancelled",
                    batch_count=(len(all_batches) if partial_result is None else partial_result.planned_batch_count),
                    stop_reason=cancelled_message,
                    last_error="user_cancelled",
                    physical_request_count=(None if partial_result is None else partial_result.physical_request_count),
                    retry_request_count=(None if partial_result is None else partial_result.retry_request_count),
                )
            )
            cancelled_run = cancelled_persistence.record
            terminal_error = cancelled_persistence.terminal_error
            success_count = cancelled_run.success_count
            quality_error_count = cancelled_run.quality_error_count
            terminal_outcome: Literal["cancelled", "failed"] = "cancelled" if terminal_error is None else "failed"
            terminal_stop_code = "user_cancelled" if terminal_error is None else "persistence_failed"
            terminal_message = cancelled_message if terminal_error is None else terminal_error
            return TextTranslationSummary(
                total_extracted_items=total_extracted_items,
                pending_count=all_pending_count,
                deduplicated_count=all_deduplicated_count,
                batch_count=cancelled_run.batch_count,
                success_count=success_count,
                error_count=quality_error_count,
                run_id=run_record.run_id,
                blocked_reason=terminal_message,
                outcome=terminal_outcome,
                stop_code=terminal_stop_code,
                stop_message=terminal_message,
                selected_count=pending_count,
                remaining_count=max(all_pending_count - success_count, 0),
                dispatched_batch_count=(0 if partial_result is None else partial_result.dispatched_batch_count),
                completed_batch_count=(0 if partial_result is None else partial_result.completed_batch_count),
                undispatched_batch_count=(
                    len(all_batches) if partial_result is None else partial_result.undispatched_batch_count
                ),
                cancelled_batch_count=(0 if partial_result is None else partial_result.cancelled_batch_count),
                waiting_permission_cancelled_count=(
                    0 if partial_result is None else partial_result.waiting_permission_cancelled_count
                ),
                inflight_cancelled_count=(0 if partial_result is None else partial_result.inflight_cancelled_count),
                completed_after_stop_count=(0 if partial_result is None else partial_result.completed_after_stop_count),
                reused_current_run_count=(0 if partial_result is None else partial_result.reused_current_run_count),
                reused_saved_count=saved_reuse.reused_count,
                context_conflict_count=saved_reuse.conflict_count,
                rejected_reuse_count=(
                    saved_reuse.rejected_count + (0 if partial_result is None else partial_result.rejected_reuse_count)
                ),
                physical_request_count=cancelled_run.physical_request_count,
                retry_request_count=cancelled_run.retry_request_count,
                elapsed_ms=(0 if partial_result is None else partial_result.elapsed_ms),
            )
        except TranslationRunRecoveryRequiredError:
            raise
        except Exception as error:
            message = f"保存翻译运行失败: {type(error).__name__}: {error}"
            failed_persistence = await _await_write_transaction_housekeeping(
                _finalize_interrupted_translation_run(
                    session=session,
                    run_record=run_record,
                    status="failed",
                    batch_count=len(all_batches),
                    stop_reason=message,
                    last_error="persistence_failed",
                )
            )
            failed_run = failed_persistence.record
            success_count = failed_run.success_count
            quality_error_count = failed_run.quality_error_count
            return TextTranslationSummary(
                total_extracted_items=total_extracted_items,
                pending_count=all_pending_count,
                deduplicated_count=all_deduplicated_count,
                batch_count=len(all_batches),
                success_count=success_count,
                error_count=quality_error_count,
                run_id=run_record.run_id,
                blocked_reason=message,
                outcome="failed",
                stop_code="persistence_failed",
                stop_message=message,
                reused_saved_count=saved_reuse.reused_count,
                context_conflict_count=saved_reuse.conflict_count,
                rejected_reuse_count=saved_reuse.rejected_count,
                selected_count=pending_count,
                remaining_count=max(all_pending_count - success_count, 0),
            )

        llm_failure_record = (
            None
            if run_result.llm_failure is None
            else build_llm_failure_record(
                run_id=run_record.run_id,
                failure=run_result.llm_failure,
            )
        )
        llm_failure_count = 0 if llm_failure_record is None else 1

        outcome = run_result.outcome
        stop_code = run_result.stop_code
        stop_message = run_result.stop_message
        limit_reason = run_result.limit_reason
        if (
            outcome in {"completed", "completed_with_quality_errors"}
            and run_limits.max_items is not None
            and pending_count < all_pending_count
        ):
            outcome = "stopped"
            stop_code = "run_limit_reached"
            limit_reason = "max_items"
            stop_message = "达到本轮 max-items 限制，仍有正文等待翻译"
        remaining_count = max(
            all_pending_count - progress_state.success_count,
            0,
        )
        run_status = {
            "completed": "completed",
            "completed_with_quality_errors": "blocked",
            "stopped": "stopped",
            "failed": "failed",
        }[outcome]
        finished_run = run_record.model_copy(
            update={
                "status": run_status,
                "success_count": progress_state.success_count,
                "quality_error_count": progress_state.quality_error_count,
                "physical_request_count": run_result.physical_request_count,
                "retry_request_count": run_result.retry_request_count,
                "batch_count": run_result.planned_batch_count,
                "llm_failure_count": llm_failure_count,
                "finished_at": current_timestamp_text(),
                "stop_reason": stop_message,
                "last_error": stop_code if stop_code != "none" else "",
            }
        )
        terminal_error = await _persist_terminal_translation_run(
            session=session,
            record=finished_run,
            llm_failure=llm_failure_record,
        )
        if terminal_error is not None:
            outcome = "failed"
            stop_code = "persistence_failed"
            stop_message = terminal_error
            limit_reason = ""
            llm_failure_count = 0
        return TextTranslationSummary(
            total_extracted_items=total_extracted_items,
            pending_count=all_pending_count,
            deduplicated_count=all_deduplicated_count,
            batch_count=run_result.planned_batch_count,
            success_count=progress_state.success_count,
            error_count=progress_state.quality_error_count,
            llm_failure_count=llm_failure_count,
            run_id=run_record.run_id,
            blocked_reason=(stop_message if outcome in {"stopped", "failed"} else None),
            outcome=outcome,
            stop_code=stop_code,
            stop_message=stop_message,
            dispatched_batch_count=run_result.dispatched_batch_count,
            completed_batch_count=run_result.completed_batch_count,
            undispatched_batch_count=run_result.undispatched_batch_count,
            cancelled_batch_count=run_result.cancelled_batch_count,
            waiting_permission_cancelled_count=run_result.waiting_permission_cancelled_count,
            inflight_cancelled_count=run_result.inflight_cancelled_count,
            completed_after_stop_count=run_result.completed_after_stop_count,
            reused_current_run_count=run_result.reused_current_run_count,
            reused_saved_count=saved_reuse.reused_count,
            context_conflict_count=saved_reuse.conflict_count,
            rejected_reuse_count=(saved_reuse.rejected_count + run_result.rejected_reuse_count),
            physical_request_count=run_result.physical_request_count,
            retry_request_count=run_result.retry_request_count,
            elapsed_ms=run_result.elapsed_ms,
            selected_count=pending_count,
            remaining_count=remaining_count,
            limit_reason=limit_reason,
        )

    async def write_back(
        self,
        game_title: str,
        callbacks: WriteProgressCallbacks,
        setting_overrides: SettingOverrides | None = None,
        confirm_font_overwrite: bool = False,
        force_full_restore: bool = False,
    ) -> WriteBackSummary:
        """把数据库中的有效译文回写到游戏目录。"""
        if force_full_restore:
            return await self._rebuild_active_runtime_with_native_plan(
                game_title=game_title,
                callbacks=callbacks,
                setting_overrides=setting_overrides,
                confirm_font_overwrite=confirm_font_overwrite,
            )
        return await self._write_back_with_native_fast_gate(
            game_title=game_title,
            callbacks=callbacks,
            setting_overrides=setting_overrides,
            confirm_font_overwrite=confirm_font_overwrite,
        )

    async def _write_back_with_native_fast_gate(
        self,
        *,
        game_title: str,
        callbacks: WriteProgressCallbacks,
        setting_overrides: SettingOverrides | None,
        confirm_font_overwrite: bool,
    ) -> WriteBackSummary:
        """使用 Rust 质检和写回计划执行普通写回。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            set_progress, _advance_progress, set_status = _unpack_write_progress_callbacks(callbacks)
            set_progress(0, 1)
            set_status("执行写入前检查")
            prepared = await self._prepare_write_operation(
                session=session,
                setting_overrides=setting_overrides,
                mode="write_back",
                require_complete_translation=True,
            )
            if not prepared.translated_items and not await session.read_terminology_registry():
                logger.warning(
                    f"[tag.warning]当前没有可回写译文，也没有已导入术语表[/tag.warning] 游戏 [tag.count]{game_title}[/tag.count]"
                )
                return WriteBackSummary(
                    data_item_count=0,
                    plugin_item_count=0,
                    terminology_written_count=0,
                    target_font_name=None,
                    source_font_count=0,
                    replaced_font_reference_count=0,
                    font_copied=False,
                )
            return await self.write_runtime_files_with_native_plan(
                session=session,
                game_title=game_title,
                callbacks=callbacks,
                setting=prepared.setting,
                text_rules=prepared.text_rules,
                mode="write_back",
                writable_location_paths=prepared.writable_location_paths,
                confirm_font_overwrite=confirm_font_overwrite,
                success_phase="游戏文本回写完成",
                pre_write_check_ms=prepared.pre_write_check_ms,
            )

    async def _prepare_write_operation(
        self,
        *,
        session: TargetGameSession,
        setting_overrides: SettingOverrides | None,
        mode: WriteRuntimeMode,
        require_complete_translation: bool,
    ) -> PreparedWriteOperation:
        """为写文件操作统一加载数据、规则、文本范围和质量门禁。"""
        started = time.perf_counter()
        game_data = await self._load_session_game_data(session)
        setting = self._load_setting(
            setting_overrides=setting_overrides,
            source_language=session.source_language,
            additional_source_languages=session.additional_source_languages,
        )
        placeholder_rule_records = await session.read_placeholder_rules()
        structured_placeholder_rule_records = await session.read_structured_placeholder_rules()
        text_rules = self._load_text_rules(
            setting=setting,
            placeholder_rule_records=placeholder_rule_records,
            structured_placeholder_rule_records=structured_placeholder_rule_records,
        )
        translated_items = await session.read_translated_items()
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
            translated_items=translated_items,
            placeholder_rules=placeholder_rule_records,
            structured_placeholder_rules=structured_placeholder_rule_records,
            include_write_probe=True,
        )
        scope = analysis_context.scope
        terminology_prompt_index = await self._load_terminology_prompt_index(
            session=session,
            game_data=game_data,
        )
        source_residual_records = await session.read_source_residual_rules()
        source_snapshot_records = await session.read_source_snapshot_records()
        translation_cache = TranslationCache()
        freshness = await evaluate_translation_freshness(
            reuse_reader=session,
            translation_cache=translation_cache,
            scope=scope,
            translated_items=translated_items,
            terminology_prompt_index=terminology_prompt_index,
            source_language=setting.text_rules.source_language,
            additional_source_languages=setting.text_rules.additional_source_languages,
            target_language=session.target_language,
            source_snapshot_records=source_snapshot_records,
            source_residual_records=source_residual_records,
        )
        stale_translated_paths = list(freshness.stale_current_paths)
        if stale_translated_paths:
            raise WriteBackGateError(
                f"有 {len(stale_translated_paths)} 条译文的源文件、规则、术语、语言或 prompt 上下文已变化，请先重新执行 translate；历史译文不会写回"
            )
        await assert_workflow_gate_passed(
            session=session,
            context=analysis_context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )
        await assert_write_back_quality_passed(
            session=session,
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            translated_items=translated_items,
            require_complete_translation=require_complete_translation,
            scope=scope,
            include_native_checks=False,
        )
        writable_items = self._filter_writable_translation_items(
            translated_items=translated_items,
            scope=scope,
        )
        if mode == "write_terminology" and await session.read_terminology_registry() is None:
            raise WriteBackGateError("当前游戏数据库中没有已导入术语表，请先执行 import-terminology")
        return PreparedWriteOperation(
            game_data=game_data,
            setting=setting,
            text_rules=text_rules,
            translated_items=writable_items,
            writable_location_paths=sorted(item.location_path for item in writable_items),
            scope=scope,
            pre_write_check_ms=int((time.perf_counter() - started) * 1000),
        )

    async def rebuild_active_runtime(
        self,
        game_title: str,
        callbacks: WriteProgressCallbacks,
        setting_overrides: SettingOverrides | None = None,
        confirm_font_overwrite: bool = False,
    ) -> WriteBackSummary:
        """从可信源快照和当前数据库缓存重建游戏运行文件。"""
        return await self._rebuild_active_runtime_with_native_plan(
            game_title=game_title,
            callbacks=callbacks,
            setting_overrides=setting_overrides,
            confirm_font_overwrite=confirm_font_overwrite,
        )

    async def _rebuild_active_runtime_with_native_plan(
        self,
        game_title: str,
        callbacks: WriteProgressCallbacks,
        setting_overrides: SettingOverrides | None,
        confirm_font_overwrite: bool,
    ) -> WriteBackSummary:
        """使用 Rust 热路径从可信源快照重建当前运行文件。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            set_progress, _advance_progress, set_status = _unpack_write_progress_callbacks(callbacks)
            set_progress(0, 1)
            set_status("执行写入前检查")
            prepared = await self._prepare_write_operation(
                session=session,
                setting_overrides=setting_overrides,
                mode="rebuild_active_runtime",
                require_complete_translation=True,
            )
            return await self.write_runtime_files_with_native_plan(
                session=session,
                game_title=game_title,
                callbacks=callbacks,
                setting=prepared.setting,
                text_rules=prepared.text_rules,
                mode="rebuild_active_runtime",
                writable_location_paths=prepared.writable_location_paths,
                confirm_font_overwrite=confirm_font_overwrite,
                success_phase="游戏运行文件重建完成",
                pre_write_check_ms=prepared.pre_write_check_ms,
            )

    async def write_runtime_files_with_native_plan(
        self,
        *,
        session: TargetGameSession,
        game_title: str,
        callbacks: WriteProgressCallbacks,
        setting: Setting,
        text_rules: TextRules,
        mode: WriteRuntimeMode,
        writable_location_paths: list[str],
        confirm_font_overwrite: bool,
        success_phase: str,
        pre_write_check_ms: int = 0,
    ) -> WriteBackSummary:
        """执行 Rust 写回计划，并保留 Python 侧事务替换协议。"""
        set_progress, advance_progress, set_status = _unpack_write_progress_callbacks(callbacks)
        set_status("准备 Rust 写回计划输入")
        setting_payload, source_font_path, source_font_names = self._build_native_write_back_setting_payload(
            setting=setting,
            text_rules=text_rules,
            content_root=session.content_root,
            confirm_font_overwrite=confirm_font_overwrite,
            writable_location_paths=writable_location_paths,
        )
        with tempfile.TemporaryDirectory(prefix="att_mz_native_plan_") as content_output_dir_text:
            content_output_dir = Path(content_output_dir_text)
            set_status("生成 Rust 写回计划")
            plan = build_native_write_back_plan(
                game_path=session.game_path,
                content_root=session.content_root,
                db_path=session.db_path,
                mode=mode,
                confirm_font_overwrite=confirm_font_overwrite,
                setting_payload=setting_payload,
                content_output_dir=content_output_dir,
            )
            total_count = max(plan.summary.data_item_count + plan.summary.plugin_item_count, 1)
            set_progress(0, total_count)
            font_records = list(plan.font_replacement_records)
            file_writes = [
                PlannedFileWrite.from_text(target_path=file.target_path, content=file.content)
                if file.content is not None
                else PlannedFileWrite.from_source(
                    target_path=file.target_path,
                    source_path=_require_planned_content_path(file.content_path),
                )
                for file in plan.files
            ]
            css_replaced_count, css_records, font_file_writes = _build_font_file_writes(
                content_root=session.content_root,
                source_font_path=source_font_path,
            )
            file_writes.extend(font_file_writes)
            font_records.extend(css_records)
            if file_writes:
                transaction_id = new_file_write_transaction_id()
                journal_path = file_write_transaction_journal_path(
                    content_root=session.content_root,
                    transaction_id=transaction_id,
                )
                timestamp = current_timestamp_text()
                file_transaction: DurableFileWriteTransaction | None = None
                try:
                    await session.create_write_transaction(
                        WriteTransactionRecord(
                            transaction_id=transaction_id,
                            operation=mode,
                            game_path=session.game_path,
                            state="preparing",
                            journal_path=journal_path,
                            payload=None,
                            created_at=timestamp,
                            updated_at=timestamp,
                            error="",
                        )
                    )
                    set_status("暂存并校验游戏运行文件")
                    file_transaction = DurableFileWriteTransaction.prepare(
                        mode=mode,
                        content_root=session.content_root,
                        writes=file_writes,
                        transaction_id=transaction_id,
                    )
                    payload = _write_transaction_payload(file_transaction.export_manifest())
                    await session.mark_write_transaction_prepared(transaction_id, payload)

                    post_write_audit_started = time.perf_counter()
                    set_status("审计暂存运行视图")
                    with file_transaction.staged_runtime_view(game_path=session.game_path) as staged_game_path:
                        staged_runtime_game_data = await load_active_runtime_game_data(staged_game_path)
                        self._assert_post_write_active_runtime_audit_passed(
                            game_data=staged_runtime_game_data,
                            text_rules=text_rules,
                            runtime_write_map_records=plan.plugin_source_runtime_write_maps,
                        )
                    post_write_audit_ms = int((time.perf_counter() - post_write_audit_started) * 1000)

                    file_replacement_started = time.perf_counter()
                    set_status("原子替换游戏运行文件")
                    file_transaction.replace_targets()
                    file_replacement_ms = int((time.perf_counter() - file_replacement_started) * 1000)
                    set_status("校验已替换文件哈希")
                    file_transaction.verify_replaced_targets()
                    set_status("提交写事务诊断状态")
                    await session.finalize_write_transaction_commit(
                        transaction_id=transaction_id,
                        runtime_maps=plan.plugin_source_runtime_write_maps,
                        font_records=font_records if source_font_path is not None else None,
                    )
                except BaseException as error:
                    await _await_write_transaction_housekeeping(
                        _rollback_failed_write_transaction(
                            session=session,
                            transaction_id=transaction_id,
                            file_transaction=file_transaction,
                            original_error=error,
                        )
                    )
                    raise
                try:
                    await _await_write_transaction_housekeeping(
                        _finalize_committed_write_transaction(
                            session=session,
                            transaction_id=transaction_id,
                            file_transaction=file_transaction,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise await _mark_recovery_required_error(
                        session=session,
                        transaction_id=transaction_id,
                        operation=mode,
                        message=(
                            "写事务已提交，但 journal 或备份清理未完成；"
                            f"请执行 recover-write-transaction --game {game_title}"
                        ),
                        cause=error,
                    ) from error
            else:
                file_replacement_ms = 0
                post_write_audit_started = time.perf_counter()
                set_status("审计写入后的当前运行文件")
                active_runtime_game_data = await load_active_runtime_game_data(session.game_path)
                self._assert_post_write_active_runtime_audit_passed(
                    game_data=active_runtime_game_data,
                    text_rules=text_rules,
                    runtime_write_map_records=plan.plugin_source_runtime_write_maps,
                )
                post_write_audit_ms = int((time.perf_counter() - post_write_audit_started) * 1000)
                transaction_id = new_file_write_transaction_id()
                journal_path = file_write_transaction_journal_path(
                    content_root=session.content_root,
                    transaction_id=transaction_id,
                )
                timestamp = current_timestamp_text()
                try:
                    await session.create_write_transaction(
                        WriteTransactionRecord(
                            transaction_id=transaction_id,
                            operation=mode,
                            game_path=session.game_path,
                            state="preparing",
                            journal_path=journal_path,
                            payload=None,
                            created_at=timestamp,
                            updated_at=timestamp,
                            error="",
                        )
                    )
                    await session.mark_write_transaction_prepared(
                        transaction_id,
                        WriteTransactionPayload(version=1, database_committed=False, files=()),
                    )
                    set_status("提交写事务诊断状态")
                    await session.finalize_write_transaction_commit(
                        transaction_id=transaction_id,
                        runtime_maps=plan.plugin_source_runtime_write_maps,
                        font_records=font_records if source_font_path is not None else None,
                    )
                except BaseException as error:
                    await _await_write_transaction_housekeeping(
                        _rollback_failed_write_transaction(
                            session=session,
                            transaction_id=transaction_id,
                            file_transaction=None,
                            original_error=error,
                        )
                    )
                    raise
                try:
                    await _await_write_transaction_housekeeping(
                        _finalize_committed_write_transaction(
                            session=session,
                            transaction_id=transaction_id,
                            file_transaction=None,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise await _mark_recovery_required_error(
                        session=session,
                        transaction_id=transaction_id,
                        operation=mode,
                        message=(
                            f"写事务诊断状态已提交但未完成收尾；请执行 recover-write-transaction --game {game_title}"
                        ),
                        cause=error,
                    ) from error

            if source_font_path is None and setting.write_back.replacement_font_path is not None:
                logger.info(
                    f"[tag.skip]未确认覆盖字体，已跳过字体替换[/tag.skip] 游戏 [tag.count]{game_title}[/tag.count]"
                )

        advance_progress(total_count)

        replaced_font_reference_count = plan.summary.replaced_font_reference_count + css_replaced_count
        source_font_count = len(source_font_names) if source_font_path is not None else plan.summary.source_font_count
        if plan.summary.target_font_name is not None:
            logger.info(
                f"[tag.phase]字体引用已同步[/tag.phase] 游戏 [tag.count]{game_title}[/tag.count] 目标字体 [tag.path]{plan.summary.target_font_name}[/tag.path] 原字体 [tag.count]{source_font_count}[/tag.count] 个，替换引用 [tag.count]{replaced_font_reference_count}[/tag.count] 处"
            )
        timing_text = "，".join(f"{name} {value}ms" for name, value in plan.timings_ms.items())
        logger.info(
            f"[tag.phase]写文件分段耗时[/tag.phase] 游戏 [tag.count]{game_title}[/tag.count] 模式 [tag.count]{mode}[/tag.count] 写入前检查 [tag.count]{pre_write_check_ms}[/tag.count]ms，Rust 计划 {timing_text}，文件替换 [tag.count]{file_replacement_ms}[/tag.count]ms，写后审计 [tag.count]{post_write_audit_ms}[/tag.count]ms"
        )
        logger.info(
            f"[tag.phase]Rust 写回计划完成[/tag.phase] 游戏 [tag.count]{game_title}[/tag.count] 模式 [tag.count]{mode}[/tag.count] 写入文件 [tag.count]{len(file_writes)}[/tag.count] 个，跳过 [tag.count]{plan.summary.skipped_file_count}[/tag.count] 个，插件源码源 AST 扫描 [tag.count]{plan.summary.plugin_source_ast_source_scan_file_count}[/tag.count] 个，写后 AST 验证 [tag.count]{plan.summary.plugin_source_ast_runtime_scan_file_count}[/tag.count] 个"
        )
        logger.success(
            f"[tag.success]{success_phase}[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] data 文本 [tag.count]{plan.summary.data_item_count}[/tag.count] 条，插件文本 [tag.count]{plan.summary.plugin_item_count}[/tag.count] 条，术语 [tag.count]{plan.summary.terminology_written_count}[/tag.count] 条"
        )
        return WriteBackSummary(
            data_item_count=plan.summary.data_item_count,
            plugin_item_count=plan.summary.plugin_item_count,
            terminology_written_count=plan.summary.terminology_written_count,
            target_font_name=plan.summary.target_font_name,
            source_font_count=source_font_count,
            replaced_font_reference_count=replaced_font_reference_count,
            font_copied=plan.summary.font_copied,
            planned_file_count=len(file_writes),
            skipped_file_count=plan.summary.skipped_file_count,
            plugin_source_ast_source_scan_file_count=plan.summary.plugin_source_ast_source_scan_file_count,
            plugin_source_ast_runtime_scan_file_count=plan.summary.plugin_source_ast_runtime_scan_file_count,
            plugin_source_runtime_map_count=plan.summary.plugin_source_runtime_map_count,
            pre_write_check_ms=pre_write_check_ms,
            rust_plan_ms=plan.timings_ms["total"],
            file_replacement_ms=file_replacement_ms,
            post_write_audit_ms=post_write_audit_ms,
        )

    def _assert_post_write_active_runtime_audit_passed(
        self,
        *,
        game_data: GameData,
        text_rules: TextRules,
        runtime_write_map_records: list[PluginSourceRuntimeWriteMapRecord],
    ) -> None:
        """写入后审计当前运行插件源码的可读性和 JS 语法。"""
        audit = audit_active_runtime_plugin_source(
            game_data=game_data,
            text_rules=text_rules,
            runtime_write_map_records=runtime_write_map_records,
            audit_text_issues=bool(runtime_write_map_records),
        )
        if not audit.issues:
            return
        counts = audit.issue_counts
        summary_parts: list[str] = []
        for label, code in (
            ("读取失败", "active_runtime_read_error"),
            ("JS 语法错误", "active_runtime_syntax_error"),
            ("源文残留", "active_runtime_source_residual"),
            ("控制符风险", "active_runtime_placeholder_risk"),
        ):
            count = counts.get(code, 0)
            if count > 0:
                summary_parts.append(f"{label} {count} 条")
        first_issue = audit.issues[0]
        detail = first_issue.syntax_error or first_issue.read_error or first_issue.fragment
        detail_text = f"；{detail}" if detail else ""
        summary_text = "、".join(summary_parts) if summary_parts else f"{len(audit.issues)} 条问题"
        message = (
            f"写入后当前运行文件审计未通过：{summary_text}。"
            f"首个问题：{first_issue.message}（文件 {first_issue.file_name}{detail_text}）"
        )
        raise WriteBackGateError(message)

    def _build_native_write_back_setting_payload(
        self,
        *,
        setting: Setting,
        text_rules: TextRules,
        content_root: Path,
        confirm_font_overwrite: bool,
        writable_location_paths: list[str],
    ) -> tuple[JsonObject, Path | None, list[str]]:
        """整理 Rust 写回计划需要的配置载荷。"""
        payload: JsonObject = {
            "long_text_line_width_limit": setting.text_rules.long_text_line_width_limit,
            "line_width_count_pattern": setting.text_rules.line_width_count_pattern,
            "line_split_punctuations": [punctuation for punctuation in setting.text_rules.line_split_punctuations],
            "preserve_wrapping_punctuation_pairs": [
                [left, right] for left, right in setting.text_rules.preserve_wrapping_punctuation_pairs
            ],
            "quality_text_rules": build_native_text_rules_payload(text_rules),
            "allowed_translation_paths": [path for path in writable_location_paths],
        }
        if not confirm_font_overwrite:
            return payload, None, []
        replacement_font_path = setting.write_back.replacement_font_path
        if replacement_font_path is None or not replacement_font_path.strip():
            return payload, None, []
        source_font_path = resolve_replacement_font_path(replacement_font_path)
        source_font_names = collect_replaced_source_font_names(
            font_dir=content_root / FONTS_DIRECTORY_NAME,
            replacement_font_name=source_font_path.name,
        )
        payload["replacement_font_path"] = str(source_font_path)
        payload["source_font_names"] = [font_name for font_name in source_font_names]
        return payload, source_font_path, source_font_names

    async def export_terminology(
        self,
        game_title: str,
        output_dir: Path,
    ) -> TerminologyExportSummary:
        """导出术语表工程文件。"""
        async with await self.game_registry.open_game(game_title) as session:
            game_data = await self._load_session_game_data(session)
            mv_virtual_namebox_rules = await session.read_mv_virtual_namebox_rules()
            text_rules = await self._load_session_profile_text_rules(session)
            summary = await export_terminology_artifacts(
                game_data=game_data,
                output_dir=output_dir,
                mv_virtual_namebox_rule_records=mv_virtual_namebox_rules,
                text_rules=text_rules,
            )
            logger.success(
                f"[tag.success]术语表工程导出完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 字段译名表 [tag.path]{summary.field_terms_path}[/tag.path] 正文术语表 [tag.path]{summary.glossary_path}[/tag.path] 上下文目录 [tag.path]{summary.contexts_dir}[/tag.path]"
            )
            return summary

    async def import_terminology(
        self,
        game_title: str,
        input_path: Path,
        glossary_input_path: Path,
    ) -> TerminologyImportSummary:
        """把外部 Agent 填写后的字段译名表和正文术语表导入当前游戏数据库。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            game_data = await self._load_session_game_data(session)
            registry = await load_terminology_registry(field_terms_path=input_path)
            glossary = await load_terminology_glossary(glossary_path=glossary_input_path)
            mv_virtual_namebox_rules = await session.read_mv_virtual_namebox_rules()
            text_rules = await self._load_session_profile_text_rules(session)
            expected_registry, _speaker_contexts, _database_contexts = TerminologyExtraction(
                game_data=game_data,
                mv_virtual_namebox_rule_records=mv_virtual_namebox_rules,
                text_rules=text_rules,
            ).extract_registry_and_contexts()
            validate_terminology_registry_shape(
                imported_registry=registry,
                expected_registry=expected_registry,
            )
            validate_terminology_bundle(registry=registry, glossary=glossary)
            await session.replace_terminology_bundle(registry=registry, glossary=glossary)
        imported_count = registry.total_entry_count()
        filled_count = registry.filled_entry_count()
        glossary_term_count = glossary.term_count()
        logger.success(
            f"[tag.success]术语表导入完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 字段条目 [tag.count]{imported_count}[/tag.count] 条，已填写 [tag.count]{filled_count}[/tag.count] 条，正文术语 [tag.count]{glossary_term_count}[/tag.count] 条"
        )
        return TerminologyImportSummary(
            imported_entry_count=imported_count,
            filled_entry_count=filled_count,
            glossary_term_count=glossary_term_count,
        )

    async def write_terminology(
        self,
        game_title: str,
        callbacks: WriteProgressCallbacks,
        setting_overrides: SettingOverrides | None = None,
        confirm_font_overwrite: bool = False,
    ) -> TerminologyWriteSummary:
        """根据数据库中的术语表直接写回稳定名词。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            _set_progress, _advance_progress, set_status = _unpack_write_progress_callbacks(callbacks)
            set_status("执行写入前检查")
            prepared = await self._prepare_write_operation(
                session=session,
                setting_overrides=setting_overrides,
                mode="write_terminology",
                require_complete_translation=False,
            )
            summary = await self.write_runtime_files_with_native_plan(
                session=session,
                game_title=game_title,
                callbacks=callbacks,
                setting=prepared.setting,
                text_rules=prepared.text_rules,
                mode="write_terminology",
                writable_location_paths=prepared.writable_location_paths,
                confirm_font_overwrite=confirm_font_overwrite,
                success_phase="术语写回完成",
                pre_write_check_ms=prepared.pre_write_check_ms,
            )
            return TerminologyWriteSummary(
                written_count=summary.terminology_written_count,
                preserved_translation_count=len(prepared.translated_items),
            )

    async def restore_font_replacement(
        self,
        game_title: str,
        setting_overrides: SettingOverrides | None = None,
    ) -> FontRestoreSummary:
        """按原始备份对比还原游戏数据中的字体引用。"""
        async with await open_game_for_mutation(self.game_registry, game_title) as session:
            setting = self._load_setting(
                setting_overrides=setting_overrides,
                source_language=session.source_language,
                additional_source_languages=session.additional_source_languages,
            )
            game_data = await self._load_session_game_data(session)
            records = await session.read_font_replacement_records()
            target_font_names = collect_replacement_font_names(
                replacement_font_path=setting.write_back.replacement_font_path,
                records=records,
            )
            if not target_font_names:
                logger.warning(
                    f"[tag.warning]没有候选覆盖字体名称，无法判断需要还原哪个新字体引用[/tag.warning] 游戏 [tag.count]{game_title}[/tag.count]"
                )
                return FontRestoreSummary(
                    restored_field_count=0,
                    restored_reference_count=0,
                    target_font_name=None,
                )

            restore_plan = plan_font_references_from_origin_backups(
                game_data=game_data,
                replacement_font_names=target_font_names,
            )
            text_rules = await self._load_session_profile_text_rules(session)
            runtime_write_maps = await session.read_plugin_source_runtime_write_maps()
            transaction_id = new_file_write_transaction_id()
            journal_path = file_write_transaction_journal_path(
                content_root=session.content_root,
                transaction_id=transaction_id,
            )
            timestamp = current_timestamp_text()
            file_transaction: DurableFileWriteTransaction | None = None
            try:
                await session.create_write_transaction(
                    WriteTransactionRecord(
                        transaction_id=transaction_id,
                        operation="restore_font",
                        game_path=session.game_path,
                        state="preparing",
                        journal_path=journal_path,
                        payload=None,
                        created_at=timestamp,
                        updated_at=timestamp,
                        error="",
                    )
                )
                if restore_plan.writes:
                    file_transaction = DurableFileWriteTransaction.prepare(
                        mode="restore_font",
                        content_root=session.content_root,
                        writes=list(restore_plan.writes),
                        transaction_id=transaction_id,
                    )
                    await session.mark_write_transaction_prepared(
                        transaction_id,
                        _write_transaction_payload(file_transaction.export_manifest()),
                    )
                    with file_transaction.staged_runtime_view(game_path=session.game_path) as staged_game_path:
                        staged_game_data = await load_active_runtime_game_data(staged_game_path)
                        self._assert_post_write_active_runtime_audit_passed(
                            game_data=staged_game_data,
                            text_rules=text_rules,
                            runtime_write_map_records=runtime_write_maps,
                        )
                    file_transaction.replace_targets()
                    file_transaction.verify_replaced_targets()
                else:
                    await session.mark_write_transaction_prepared(
                        transaction_id,
                        WriteTransactionPayload(version=1, database_committed=False, files=()),
                    )
                    active_game_data = await load_active_runtime_game_data(session.game_path)
                    self._assert_post_write_active_runtime_audit_passed(
                        game_data=active_game_data,
                        text_rules=text_rules,
                        runtime_write_map_records=runtime_write_maps,
                    )
                await session.finalize_write_transaction_commit(
                    transaction_id=transaction_id,
                    runtime_maps=None,
                    font_records=[],
                )
            except BaseException as error:
                await _await_write_transaction_housekeeping(
                    _rollback_failed_write_transaction(
                        session=session,
                        transaction_id=transaction_id,
                        file_transaction=file_transaction,
                        original_error=error,
                    )
                )
                raise

            try:
                await _await_write_transaction_housekeeping(
                    _finalize_committed_write_transaction(
                        session=session,
                        transaction_id=transaction_id,
                        file_transaction=file_transaction,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise await _mark_recovery_required_error(
                    session=session,
                    transaction_id=transaction_id,
                    operation="restore_font",
                    message=(f"字体还原事务已提交但未完成收尾；请执行 recover-write-transaction --game {game_title}"),
                    cause=error,
                ) from error

            restore_summary = restore_plan.summary
            target_font_name = "、".join(restore_summary.target_font_names)
            logger.success(
                f"[tag.success]字体引用还原完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 还原字段 [tag.count]{restore_summary.restored_field_count}[/tag.count] 个，引用 [tag.count]{restore_summary.restored_reference_count}[/tag.count] 处"
            )
            return FontRestoreSummary(
                restored_field_count=restore_summary.restored_field_count,
                restored_reference_count=restore_summary.restored_reference_count,
                target_font_name=target_font_name,
            )

    async def recover_write_transaction(
        self,
        game_title: str,
    ) -> WriteTransactionRecoverySummary:
        """按数据库提交状态恢复或收尾当前游戏唯一的未完成写事务。"""
        async with await open_game_for_recovery(self.game_registry, game_title) as session:
            records = await session.read_unfinished_write_transactions()
            if not records:
                return WriteTransactionRecoverySummary(
                    transaction_id=None,
                    previous_state=None,
                    final_state="none",
                    restored_file_count=0,
                    finalized_committed_file_count=0,
                )
            if len(records) != 1:
                raise RecoveryRequiredError(
                    f"当前游戏存在 {len(records)} 个未完成写事务，无法自动判断恢复顺序",
                    state="recovery_required",
                    details={"unfinished_transaction_count": len(records)},
                )

            record = records[0]
            if record.game_path.resolve() != session.game_path.resolve():
                message = f"写事务 {record.transaction_id} 绑定的游戏目录与当前游戏不一致"
                raise await _mark_recovery_required_error(
                    session=session,
                    transaction_id=record.transaction_id,
                    operation=record.operation,
                    message=message,
                )
            expected_journal_path = file_write_transaction_journal_path(
                content_root=session.content_root,
                transaction_id=record.transaction_id,
            )
            if record.journal_path.resolve() != expected_journal_path.resolve():
                message = f"写事务 {record.transaction_id} 的 journal 路径不属于当前游戏"
                raise await _mark_recovery_required_error(
                    session=session,
                    transaction_id=record.transaction_id,
                    operation=record.operation,
                    message=message,
                )

            journal_only_recovery = record.payload is None
            database_committed = False if record.payload is None else record.payload.database_committed
            try:
                if journal_only_recovery:
                    if record.state not in {"preparing", "recovery_required"}:
                        raise RuntimeError(f"缺少恢复清单的写事务状态无效: {record.state}")
                    transaction = DurableFileWriteTransaction.load(
                        journal_path=expected_journal_path,
                        content_root=session.content_root,
                    )
                    if transaction.transaction_id != record.transaction_id:
                        raise RuntimeError("写事务 journal 标识与数据库记录不匹配")
                    if transaction.mode != record.operation:
                        raise RuntimeError("写事务 journal 操作类型与数据库记录不匹配")
                    file_summary = transaction.rollback_pre_database_crash()
                else:
                    assert record.payload is not None
                    transaction = DurableFileWriteTransaction.from_manifest(
                        transaction_id=record.transaction_id,
                        mode=record.operation,
                        content_root=session.content_root,
                        journal_path=expected_journal_path,
                        database_committed=database_committed,
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                        entries=_file_manifest_from_payload(record.payload),
                    )
                    file_summary = transaction.recover(database_committed=database_committed)
            except Exception as error:
                raise await _mark_recovery_required_error(
                    session=session,
                    transaction_id=record.transaction_id,
                    operation=record.operation,
                    message=f"写事务 {record.transaction_id} 恢复失败，游戏文件保持阻断状态：{error}",
                    cause=error,
                ) from error

            try:
                if journal_only_recovery:
                    final_state = "rolled_back"
                    terminal_write = _finalize_pre_database_crash_recovery(
                        session=session,
                        transaction_id=record.transaction_id,
                        transaction=transaction,
                        error=record.error,
                    )
                elif database_committed:
                    final_state = "finalized"
                    terminal_write = session.mark_write_transaction_finalized(record.transaction_id)
                else:
                    final_state = "rolled_back"
                    terminal_write = session.mark_write_transaction_rolled_back(
                        record.transaction_id,
                        record.error,
                    )
                await _await_write_transaction_housekeeping(terminal_write)
            except Exception as error:
                raise await _mark_recovery_required_error(
                    session=session,
                    transaction_id=record.transaction_id,
                    operation=record.operation,
                    message=f"写事务 {record.transaction_id} 文件恢复完成，但数据库终态记录失败",
                    cause=error,
                ) from error
            logger.success(
                f"[tag.success]写事务恢复完成[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 事务 [tag.count]{record.transaction_id}[/tag.count] 状态 [tag.count]{final_state}[/tag.count]"
            )
            return WriteTransactionRecoverySummary(
                transaction_id=record.transaction_id,
                previous_state=record.state,
                final_state=final_state,
                restored_file_count=file_summary.restored_file_count,
                finalized_committed_file_count=file_summary.finalized_committed_file_count,
            )

    def _filter_writable_translation_items(
        self,
        *,
        translated_items: list[TranslationItem],
        scope: TextScopeResult,
    ) -> list[TranslationItem]:
        """仅保留当前提取规则仍能定位写回位置的译文条目。"""
        if scope.stale_plugin_rules:
            raise WriteBackGateError(
                f"存在 {len(scope.stale_plugin_rules)} 个过期插件规则，请重新导入插件规则后再写进游戏文件"
            )
        if scope.write_back_probe_error:
            raise WriteBackGateError(scope.write_back_probe_error)
        writable_paths = scope.writable_paths
        stale_paths = sorted(
            item.location_path for item in translated_items if item.location_path not in writable_paths
        )
        if stale_paths:
            samples = "、".join(stale_paths[:5])
            suffix = "" if len(stale_paths) <= 5 else f" 等 {len(stale_paths)} 条"
            raise WriteBackGateError(f"发现已保存译文不在当前可写文本范围内，不能继续写进游戏文件: {samples}{suffix}")
        return list(translated_items)

    async def _load_terminology_prompt_index(
        self,
        *,
        session: TargetGameSession,
        game_data: GameData,
    ) -> TerminologyPromptIndex:
        """读取数据库正文术语表，并转换为正文提示词索引。"""
        registry = await session.read_terminology_registry()
        glossary = await session.read_terminology_glossary()
        if glossary is None:
            raise RuntimeError("当前游戏尚未导入正文术语表，检查没通过，不能继续")
        if registry is None:
            raise RuntimeError("当前游戏尚未导入字段译名表，检查没通过，不能继续")
        validate_terminology_bundle(registry=registry, glossary=glossary)

        index = TerminologyPromptIndex.from_glossary(glossary, game_data=game_data)
        logger.info(
            f"[tag.phase]已加载正文术语表[/tag.phase] 游戏 [tag.count]{session.game_title}[/tag.count] 可注入译名 [tag.count]{len(index.entries)}[/tag.count] 条"
        )
        return index

    @staticmethod
    def _should_refresh_plugin_translation_items(
        old_rule: PluginTextRuleRecord | None,
        new_rule: PluginTextRuleRecord,
    ) -> bool:
        """判断插件规则变化后是否需要清理失效插件译文。"""
        if old_rule is None:
            return False
        return old_rule.plugin_hash != new_rule.plugin_hash or old_rule.path_templates != new_rule.path_templates

    @staticmethod
    def _should_refresh_event_command_translation_items(
        old_rule: EventCommandTextRuleRecord | None,
        new_rule: EventCommandTextRuleRecord,
    ) -> bool:
        """判断事件指令规则变化后是否需要清理失效译文。"""
        if old_rule is None:
            return False
        return (
            old_rule.command_code != new_rule.command_code
            or old_rule.parameter_filters != new_rule.parameter_filters
            or old_rule.path_templates != new_rule.path_templates
        )

    @staticmethod
    def _should_refresh_note_tag_translation_items(
        old_rule: NoteTagTextRuleRecord | None,
        new_rule: NoteTagTextRuleRecord,
    ) -> bool:
        """判断 Note 标签规则变化后是否需要清理失效译文。"""
        if old_rule is None:
            return False
        return old_rule.file_name != new_rule.file_name or old_rule.tag_names != new_rule.tag_names

    @staticmethod
    def _event_command_rule_prefixes(
        *,
        command_index: tuple[EventCommandAnalysisEntry, ...],
        rule_record: EventCommandTextRuleRecord,
    ) -> list[str]:
        """根据事件指令规则找出需要清理的正文路径前缀。"""
        prefixes: list[str] = []
        for entry in command_index:
            path = entry.location_path
            command = entry.command
            if command.code != rule_record.command_code:
                continue
            if not command_matches_filters(
                parameters=command.parameters,
                filters=rule_record.parameter_filters,
            ):
                continue
            prefixes.append("/".join(map(str, path)))
        return prefixes

    async def _run_text_translation_batches(
        self,
        *,
        text_translation: TextTranslation,
        session: TargetGameSession,
        batches: TranslationBatchPlan,
        run_record: TranslationRunRecord,
        advance_progress: Callable[[int], None],
        translation_cache: TranslationCache,
        source_residual_rule_set: SourceResidualRuleSet,
        candidate_validator: TranslationCandidateValidator,
        terminology_prompt_index: TerminologyPromptIndex | None,
        saved_reuse: SavedTranslationReuseResult,
        max_batches: int | None,
        time_limit_seconds: int | None,
        stop_on_error_rate: float | None,
    ) -> tuple[TranslationRunResult, TranslationProgressState]:
        """通过单一 Controller 懒派发、复验并原子保存正文批次。"""
        game_title = session.game_title
        progress_state = TranslationProgressState()
        if saved_reuse.reused_items:
            reused_count = len(saved_reuse.reused_items)
            initial_run_record = run_record.model_copy(update={"success_count": reused_count})
            await session.persist_translation_batch(
                initial_run_record,
                saved_reuse.reused_items,
                [],
                build_translation_reuse_contexts_by_path(
                    translation_cache=translation_cache,
                    items=saved_reuse.reused_items,
                ),
            )
            progress_state.success_count = reused_count
            advance_progress(reused_count)
            logger.success(
                f"[tag.success]已复用并重新检查历史译文[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] [tag.count]{reused_count}[/tag.count] 条"
            )

        async def persist_batch(
            result: BatchExecutionResult,
        ) -> PersistedBatchCounts:
            reuse_result = expand_current_run_reuse(
                right_items=result.right_items,
                error_items=result.error_items,
                translation_cache=translation_cache,
                text_rules=text_translation.text_rules,
                source_residual_rule_set=source_residual_rule_set,
                validate_candidates=candidate_validator,
            )
            success_count = len(reuse_result.right_items)
            error_count = len(reuse_result.error_items)
            updated_run = run_record.model_copy(
                update={
                    "success_count": progress_state.success_count + success_count,
                    "quality_error_count": (progress_state.quality_error_count + error_count),
                }
            )
            await session.persist_translation_batch(
                updated_run,
                reuse_result.right_items,
                reuse_result.error_items,
                build_translation_reuse_contexts_by_path(
                    translation_cache=translation_cache,
                    items=reuse_result.right_items,
                ),
                physical_request_count_delta=result.physical_request_count,
                retry_request_count_delta=result.retry_request_count,
            )
            progress_state.success_count += success_count
            progress_state.quality_error_count += error_count
            advance_progress(success_count + error_count)
            if success_count:
                logger.success(
                    f"[tag.success]已写入正文翻译结果[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] [tag.count]{success_count}[/tag.count] 条"
                )
            if error_count:
                logger.error(
                    f"[tag.failure]已记录检查没通过的译文[/tag.failure] 游戏 [tag.count]{game_title}[/tag.count] [tag.count]{error_count}[/tag.count] 条"
                )
            retranslation_batches: TranslationBatchPlan | None = None
            if reuse_result.retranslate_items:
                retranslation_data_map: dict[str, TranslationData] = {}
                for index, item in enumerate(reuse_result.retranslate_items):
                    cache_key = translation_cache.build_cache_key(item)
                    if cache_key is None:
                        raise RuntimeError(f"复用目标缺少重译上下文: {item.location_path}")
                    retranslation_data_map[f"retranslate:{index}"] = TranslationData(
                        display_name=cache_key.display_name or None,
                        translation_items=[item],
                    )
                retranslation_batches = build_translation_batches(
                    translation_data_map=retranslation_data_map,
                    setting=text_translation.setting,
                    text_rules=text_translation.text_rules,
                    terminology_prompt_index=terminology_prompt_index,
                    translation_cache=translation_cache,
                )
            return PersistedBatchCounts(
                success_count=success_count,
                quality_error_count=error_count,
                reused_current_run_count=reuse_result.reused_count,
                rejected_reuse_count=reuse_result.rejected_count,
                retranslation_batches=retranslation_batches,
            )

        async def execute_batch(
            *,
            ai_result: str,
            batch: TranslationBatch,
        ) -> BatchExecutionResult:
            verification = verify_translation_batch_result(
                ai_result=ai_result,
                batch=batch,
                text_rules=text_translation.text_rules,
                source_residual_rule_set=source_residual_rule_set,
                validate_candidates=candidate_validator,
            )
            return BatchExecutionResult(
                batch=batch,
                right_items=verification.right_items,
                error_items=verification.error_items,
            )

        run_result = await text_translation.run(
            llm_handler=self.llm_handler,
            batches=batches,
            persist_batch=persist_batch,
            max_batches=max_batches,
            time_limit_seconds=time_limit_seconds,
            stop_on_error_rate=stop_on_error_rate,
            execute_batch=execute_batch,
        )
        logger.success(
            f"[tag.success]正文翻译运行结束[/tag.success] 游戏 [tag.count]{game_title}[/tag.count] 成功 [tag.count]{progress_state.success_count}[/tag.count] 条，检查未通过 [tag.count]{progress_state.quality_error_count}[/tag.count] 条"
        )
        return run_result, progress_state


async def _persist_terminal_translation_run(
    *,
    session: TargetGameSession,
    record: TranslationRunRecord,
    llm_failure: LlmFailureRecord | None = None,
) -> str | None:
    """延迟外部取消，直到模型故障与运行终态已经原子落库。"""
    return await _await_write_transaction_housekeeping(
        _persist_terminal_translation_run_unshielded(
            session=session,
            record=record,
            llm_failure=llm_failure,
        )
    )


async def _persist_terminal_translation_run_unshielded(
    *,
    session: TargetGameSession,
    record: TranslationRunRecord,
    llm_failure: LlmFailureRecord | None = None,
) -> str | None:
    """写入终态；SQLite 连续失败时创建同目录一次性恢复日志。"""
    try:
        await session.persist_translation_run_terminal(record, llm_failure)
    except TranslationRunRecoveryRequiredError:
        raise
    except Exception as error:
        message = build_bounded_persistence_failure_text(error)
        failed_record = record.model_copy(
            update={
                "status": "failed",
                "llm_failure_count": 0,
                "finished_at": current_timestamp_text(),
                "stop_reason": message,
                "last_error": "persistence_failed",
            }
        )
        try:
            committed_record, committed_failures = await session.read_translation_terminal_snapshot(record.run_id)
        except TranslationRunRecoveryRequiredError:
            raise
        except Exception as readback_error:
            readback_message = build_bounded_persistence_failure_text(error, readback_error)
            _ = await session.write_translation_run_recovery(
                attempted_record=record,
                attempted_failure=llm_failure,
                fallback_record=failed_record,
            )
            return readback_message
        if _terminal_snapshot_matches(
            committed_record,
            committed_failures,
            expected_record=record,
            expected_failure=llm_failure,
        ):
            return None
        if _terminal_snapshot_matches(
            committed_record,
            committed_failures,
            expected_record=failed_record,
            expected_failure=None,
        ):
            return message
        if committed_record is None or committed_record.status != "running" or committed_failures:
            _ = await session.write_translation_run_recovery(
                attempted_record=record,
                attempted_failure=llm_failure,
                fallback_record=failed_record,
            )
            return message
        normalized_failed_record = _normalize_persistence_failed_record(
            attempted_record=record,
            failed_record=failed_record,
            current_running_record=committed_record,
        )
        if normalized_failed_record is None:
            _ = await session.write_translation_run_recovery(
                attempted_record=record,
                attempted_failure=llm_failure,
                fallback_record=failed_record,
            )
            return message
        failed_record = normalized_failed_record
        try:
            await session.persist_translation_run_terminal(failed_record)
        except TranslationRunRecoveryRequiredError:
            raise
        except Exception as failed_state_error:
            failed_state_message = build_bounded_persistence_failure_text(error, failed_state_error)
            try:
                readback_record, fallback_failures = await session.read_translation_terminal_snapshot(record.run_id)
            except TranslationRunRecoveryRequiredError:
                raise
            except Exception:
                readback_record = None
                fallback_failures = ()
            if _terminal_snapshot_matches(
                readback_record,
                fallback_failures,
                expected_record=failed_record,
                expected_failure=None,
            ):
                return message
            if _terminal_snapshot_matches(
                readback_record,
                fallback_failures,
                expected_record=record,
                expected_failure=llm_failure,
            ):
                return None
            if readback_record is not None and readback_record.status == "running" and not fallback_failures:
                normalized_failed_record = _normalize_persistence_failed_record(
                    attempted_record=record,
                    failed_record=failed_record,
                    current_running_record=readback_record,
                )
                if normalized_failed_record is not None:
                    failed_record = normalized_failed_record
            _ = await session.write_translation_run_recovery(
                attempted_record=record,
                attempted_failure=llm_failure,
                fallback_record=failed_record,
            )
            return failed_state_message
        return message
    return None


def _terminal_snapshot_matches(
    actual_record: TranslationRunRecord | None,
    actual_failures: tuple[LlmFailureRecord, ...],
    *,
    expected_record: TranslationRunRecord,
    expected_failure: LlmFailureRecord | None,
) -> bool:
    """用共享稳定指纹和完整故障行确认一次 commit 是否已经落盘。"""
    if actual_record is None:
        return False
    expected_failures = () if expected_failure is None else (expected_failure,)
    return (
        translation_run_stable_fingerprint(actual_record) == translation_run_stable_fingerprint(expected_record)
        and actual_failures == expected_failures
    )


def _normalize_persistence_failed_record(
    *,
    attempted_record: TranslationRunRecord,
    failed_record: TranslationRunRecord,
    current_running_record: TranslationRunRecord,
) -> TranslationRunRecord | None:
    """只从已确认 running 快照继承有对应数据库事实的结果计数。"""
    if (
        current_running_record.status != "running"
        or current_running_record.finished_at is not None
        or current_running_record.llm_failure_count != 0
        or current_running_record.stop_reason
        or current_running_record.last_error
    ):
        return None
    for field in ("run_id", "started_at", "total_extracted", "pending_count", "deduplicated_count"):
        if getattr(current_running_record, field) != getattr(attempted_record, field):
            return None
    for field in ("batch_count", "success_count", "quality_error_count"):
        if getattr(current_running_record, field) > getattr(attempted_record, field):
            return None
    if current_running_record.physical_request_count > attempted_record.physical_request_count:
        return None
    if current_running_record.retry_request_count > attempted_record.retry_request_count:
        return None
    physical_delta = attempted_record.physical_request_count - current_running_record.physical_request_count
    retry_delta = attempted_record.retry_request_count - current_running_record.retry_request_count
    if retry_delta > physical_delta:
        return None
    return failed_record.model_copy(
        update={
            "success_count": current_running_record.success_count,
            "quality_error_count": current_running_record.quality_error_count,
        }
    )


@dataclass(frozen=True, slots=True)
class _InterruptedTranslationRunPersistence:
    """异常或取消收尾后用于构造 CLI 摘要的稳定状态。"""

    record: TranslationRunRecord
    terminal_error: str | None


async def _finalize_interrupted_translation_run(
    *,
    session: TargetGameSession,
    run_record: TranslationRunRecord,
    status: Literal["cancelled", "failed"],
    batch_count: int,
    stop_reason: str,
    last_error: str,
    physical_request_count: int | None = None,
    retry_request_count: int | None = None,
) -> _InterruptedTranslationRunPersistence:
    """在同一受保护任务内读取最新计数并写入异常终态。"""
    latest_run = await session.read_translation_run(run_record.run_id)
    persisted_run = run_record if latest_run is None else latest_run
    terminal_record = run_record.model_copy(
        update={
            "status": status,
            "batch_count": batch_count,
            "success_count": persisted_run.success_count,
            "quality_error_count": persisted_run.quality_error_count,
            "llm_failure_count": 0,
            "physical_request_count": (
                persisted_run.physical_request_count if physical_request_count is None else physical_request_count
            ),
            "retry_request_count": (
                persisted_run.retry_request_count if retry_request_count is None else retry_request_count
            ),
            "finished_at": current_timestamp_text(),
            "stop_reason": stop_reason,
            "last_error": last_error,
        }
    )
    terminal_error = await _persist_terminal_translation_run_unshielded(
        session=session,
        record=terminal_record,
    )
    return _InterruptedTranslationRunPersistence(
        record=terminal_record,
        terminal_error=terminal_error,
    )


def _require_planned_content_path(content_path: Path | None) -> Path:
    """读取已经由 native plan 边界校验的 sidecar 路径。"""
    if content_path is None:
        raise RuntimeError("Rust 写回计划文件缺少 content 和 content_path")
    return content_path


def _build_font_file_writes(
    *,
    content_root: Path,
    source_font_path: Path | None,
) -> tuple[int, list[FontReplacementRecord], list[PlannedFileWrite]]:
    """把字体二进制、CSS 与首次原件留档纳入同一文件事务。"""
    if source_font_path is None:
        return 0, [], []
    font_directory = content_root / FONTS_DIRECTORY_NAME
    target_font_path = font_directory / source_font_path.name
    writes: list[PlannedFileWrite] = []
    if source_font_path.resolve() != target_font_path.resolve():
        writes.append(
            PlannedFileWrite.from_source(
                target_path=target_font_path,
                source_path=source_font_path,
            )
        )

    css_path = font_directory / GAMEFONT_CSS_FILE_NAME
    if not css_path.exists():
        return 0, [], writes
    if not css_path.is_file():
        raise FileNotFoundError(f"游戏字体样式表不是文件: {css_path}")
    css_text = css_path.read_text(encoding="utf-8")
    updated_css_text, css_records = replace_gamefont_css_text(
        css_text=css_text,
        replacement_font_name=source_font_path.name,
    )
    if not css_records:
        return 0, [], writes
    origin_css_path = font_directory / GAMEFONT_CSS_ORIGIN_FILE_NAME
    if not origin_css_path.exists():
        writes.append(
            PlannedFileWrite.from_source(
                target_path=origin_css_path,
                source_path=css_path,
            )
        )
    writes.append(
        PlannedFileWrite.from_text(
            target_path=css_path,
            content=updated_css_text,
        )
    )
    return len(css_records), css_records, writes


async def _await_write_transaction_housekeeping[T](
    operation: Coroutine[object, object, T],
) -> T:
    """延迟外部取消，直到已启动的事务回滚或收尾真正结束。"""
    operation_task = asyncio.create_task(operation)
    delayed_cancellation: asyncio.CancelledError | None = None
    while not operation_task.done():
        try:
            _ = await asyncio.shield(operation_task)
        except asyncio.CancelledError as error:
            if operation_task.cancelled():
                raise
            delayed_cancellation = error
    result = operation_task.result()
    if delayed_cancellation is not None:
        raise delayed_cancellation
    return result


async def _finalize_committed_write_transaction(
    *,
    session: TargetGameSession,
    transaction_id: str,
    file_transaction: DurableFileWriteTransaction | None,
) -> None:
    """数据库已提交后完成文件清理和终态记录。"""
    if file_transaction is not None:
        file_transaction.mark_committed_and_cleanup()
    await session.mark_write_transaction_finalized(transaction_id)


async def _finalize_pre_database_crash_recovery(
    *,
    session: TargetGameSession,
    transaction_id: str,
    transaction: DurableFileWriteTransaction,
    error: str,
) -> None:
    """清理 preparing 崩溃产物后，再把数据库记录结束为已回滚。"""
    transaction.finalize_rolled_back_cleanup()
    await session.mark_write_transaction_rolled_back(transaction_id, error)


async def _mark_recovery_required_error(
    *,
    session: TargetGameSession,
    transaction_id: str,
    operation: str,
    message: str,
    cause: BaseException | None = None,
) -> RecoveryRequiredError:
    """尽力持久化人工恢复状态，并始终返回同一结构化错误。"""
    cause_text = None if cause is None else f"{type(cause).__name__}: {cause}"
    persisted_error = message if cause_text is None else f"{message}；根因：{cause_text}"
    details: dict[str, object] = {"operation": operation}
    if cause_text is not None:
        details["cause"] = cause_text
    try:
        await session.mark_write_transaction_recovery_required(
            transaction_id,
            persisted_error,
        )
    except BaseException as state_error:
        state_error_text = f"{type(state_error).__name__}: {state_error}"
        details["state_update_error"] = state_error_text
        logger.error(
            f"[tag.failure]写事务恢复状态落盘失败[/tag.failure] 事务 [tag.count]{transaction_id}[/tag.count]：{state_error_text}"
        )
    return RecoveryRequiredError(
        message,
        transaction_id=transaction_id,
        state="recovery_required",
        details=details,
    )


async def _rollback_failed_write_transaction(
    *,
    session: TargetGameSession,
    transaction_id: str,
    file_transaction: DurableFileWriteTransaction | None,
    original_error: BaseException,
) -> None:
    """写入、审计或数据库提交失败时，恢复全部原文件并更新事务状态。"""
    error_text = f"{type(original_error).__name__}: {original_error}"
    try:
        record = await session.read_write_transaction(transaction_id)
        if record is None or record.state in {"finalized", "rolled_back"}:
            return
        if record.payload is not None and record.payload.database_committed:
            await _finalize_committed_write_transaction(
                session=session,
                transaction_id=transaction_id,
                file_transaction=file_transaction,
            )
            return
        if file_transaction is None:
            journal_path = file_write_transaction_journal_path(
                content_root=session.content_root,
                transaction_id=transaction_id,
            )
            if journal_path.exists():
                file_transaction = DurableFileWriteTransaction.load(
                    journal_path=journal_path,
                    content_root=session.content_root,
                )
            elif record.payload is not None and record.payload.files:
                file_transaction = DurableFileWriteTransaction.from_manifest(
                    transaction_id=record.transaction_id,
                    mode=record.operation,
                    content_root=session.content_root,
                    journal_path=record.journal_path,
                    database_committed=False,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    entries=_file_manifest_from_payload(record.payload),
                )
        if record.state == "preparing" and file_transaction is not None:
            await session.mark_write_transaction_prepared(
                transaction_id,
                _write_transaction_payload(file_transaction.export_manifest()),
            )
        if file_transaction is not None:
            if file_transaction.state != "rolled_back":
                _ = file_transaction.rollback()
            file_transaction.finalize_rolled_back_cleanup()
        await session.mark_write_transaction_rolled_back(
            transaction_id,
            error_text,
        )
    except BaseException as recovery_error:
        raise await _mark_recovery_required_error(
            session=session,
            transaction_id=transaction_id,
            operation="write_transaction_rollback",
            message=(
                f"写事务 {transaction_id} 自动恢复未完成；"
                "请先执行 recover-write-transaction，未恢复前不得继续修改游戏文件"
            ),
            cause=recovery_error,
        ) from recovery_error


def _write_transaction_payload(
    entries: tuple[FileWriteManifestEntry, ...],
) -> WriteTransactionPayload:
    """把文件事务清单转为数据库中的严格版本化 payload。"""
    return WriteTransactionPayload(
        version=1,
        database_committed=False,
        files=tuple(
            WriteTransactionFileRecord(
                target_relative_path=entry.target_relative_path,
                staged_relative_path=entry.staged_relative_path,
                backup_relative_path=entry.backup_relative_path,
                existed_before=entry.existed_before,
                original_sha256=entry.original_sha256,
                target_sha256=entry.target_sha256,
            )
            for entry in entries
        ),
    )


def _file_manifest_from_payload(
    payload: WriteTransactionPayload,
) -> tuple[FileWriteManifestEntry, ...]:
    """把已严格校验的数据库 payload 转为文件层恢复清单。"""
    return tuple(
        FileWriteManifestEntry(
            target_relative_path=entry.target_relative_path,
            staged_relative_path=entry.staged_relative_path,
            backup_relative_path=entry.backup_relative_path,
            existed_before=entry.existed_before,
            original_sha256=entry.original_sha256,
            target_sha256=entry.target_sha256,
        )
        for entry in payload.files
    )


def validate_terminology_registry_shape(
    *,
    imported_registry: TerminologyRegistry,
    expected_registry: TerminologyRegistry,
) -> None:
    """校验导入术语表与当前游戏可提取术语完全一致。"""
    imported_map = imported_registry.as_category_map()
    expected_map = expected_registry.as_category_map()
    errors: list[str] = []
    for category, expected_entries in expected_map.items():
        imported_entries = imported_map[category]
        missing_terms = sorted(set(expected_entries) - set(imported_entries))
        extra_terms = sorted(set(imported_entries) - set(expected_entries))
        if missing_terms:
            errors.append(f"{category} 缺少 {len(missing_terms)} 个术语")
        if extra_terms:
            errors.append(f"{category} 多出 {len(extra_terms)} 个术语")
    if errors:
        raise ValueError("；".join(errors))


__all__: list[str] = [
    "EventCommandJsonExportSummary",
    "EventCommandRuleImportSummary",
    "FontRestoreSummary",
    "PluginJsonExportSummary",
    "PluginRuleImportSummary",
    "TerminologyImportSummary",
    "TerminologyWriteSummary",
    "TextTranslationSummary",
    "TranslationHandler",
    "TranslationProgressState",
    "TranslationRunLimits",
    "WriteBackSummary",
    "WriteTransactionRecoverySummary",
    "build_translation_reuse_contexts_by_path",
    "translation_record_matches_current_target",
]
