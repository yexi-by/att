"""单命令游戏分析上下文。

重型检查必须共享这个对象中的游戏快照、插件 AST 和文本索引，
避免同一条 CLI 命令因多个门禁重复读盘、重复解析 AST。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.diagnostics import diagnostic_stage, record_scan_counts, timed_async_diagnostic_stage
from app.event_command_text.index import EventCommandAnalysisEntry, event_command_analysis_snapshots
from app.note_tag_text.exporter import collect_note_tag_candidates_from_sources
from app.persistence import TargetGameSession
from app.plugin_source_text import (
    PluginSourceRawIndex,
    PluginSourceScan,
    build_plugin_source_raw_index,
    derive_plugin_source_scan,
)
from app.rmmz.mv_namebox import (
    MvVirtualNameboxCandidate,
    collect_mv_virtual_namebox_candidates_from_command_snapshots,
    mv_virtual_namebox_candidate_details_from_candidates,
)
from app.rmmz.schema import (
    EventCommandTextRuleRecord,
    GameData,
    MvVirtualNameboxRuleRecord,
    NoteTagTextRuleRecord,
    PlaceholderRuleRecord,
    PluginSourceTextRuleRecord,
    PluginTextRuleRecord,
    StructuredPlaceholderRuleRecord,
    TranslationData,
    TranslationItem,
)
from app.rmmz.text_rules import JsonArray, JsonObject, JsonValue, TextRules
from app.text_scope import (
    TextScopeAnalysisIndex,
    TextScopeEntry,
    TextScopeResult,
    TextScopeService,
    build_text_scope_analysis_index,
)
from app.text_scope.models import StalePluginRule
from app.text_scope.plugin_rules import filter_fresh_plugin_text_rules


@dataclass(slots=True)
class GameAnalysisScanCounts:
    """单命令建立分析事实时的重型扫描计数。"""

    plugin_ast_scan_count: int
    event_index_scan_count: int
    note_index_scan_count: int
    plugin_parameter_index_scan_count: int
    text_scope_build_count: int


@dataclass(frozen=True, slots=True)
class GameAnalysisContext:
    """一条命令共享的完整游戏分析事实。"""

    game_data: GameData
    text_rules: TextRules
    plugin_source_raw_index: PluginSourceRawIndex
    plugin_ast: PluginSourceScan
    analysis_index: TextScopeAnalysisIndex
    event_commands: tuple[EventCommandAnalysisEntry, ...]
    event_command_snapshots: tuple[JsonObject, ...]
    note_candidates: tuple[JsonObject, ...]
    plugin_parameter_candidates: tuple[JsonObject, ...]
    mv_virtual_namebox_candidate_index: tuple[MvVirtualNameboxCandidate, ...]
    mv_virtual_namebox_candidates: tuple[JsonValue, ...]
    placeholder_rules: tuple[PlaceholderRuleRecord, ...]
    structured_placeholder_rules: tuple[StructuredPlaceholderRuleRecord, ...]
    plugin_rule_records: tuple[PluginTextRuleRecord, ...]
    plugin_rules: tuple[PluginTextRuleRecord, ...]
    stale_plugin_rules: tuple[StalePluginRule, ...]
    event_rules: tuple[EventCommandTextRuleRecord, ...]
    plugin_source_rules: tuple[PluginSourceTextRuleRecord, ...]
    note_tag_rules: tuple[NoteTagTextRuleRecord, ...]
    mv_virtual_namebox_rules: tuple[MvVirtualNameboxRuleRecord, ...]
    translated_items: tuple[TranslationItem, ...]
    translated_item_index: dict[str, TranslationItem]
    scope: TextScopeResult
    write_target_index: dict[str, TextScopeEntry]
    scan_counts: GameAnalysisScanCounts

    @property
    def plugin_source_scan(self) -> PluginSourceScan:
        """返回与当前文本规则绑定的唯一插件 AST 扫描。"""
        return self.plugin_ast

    @property
    def translation_data_map(self) -> dict[str, TranslationData]:
        """返回当前规则下的统一可翻译文本集。"""
        return self.scope.translation_data_map

    def build_scope_for_text_rules(
        self,
        *,
        text_rules: TextRules,
        include_write_probe: bool = False,
    ) -> TextScopeResult:
        """利用已读取事实重建规则视图，不重读数据库或重扫 AST。"""
        self.scan_counts.text_scope_build_count += 1
        record_scan_counts({"text_scope_build_count": 1})
        with diagnostic_stage("text_scope_build"):
            plugin_source_scan = derive_plugin_source_scan(
                index=self.plugin_source_raw_index,
                text_rules=text_rules,
            )
            return TextScopeService().build_from_loaded_rules(
                game_data=self.game_data,
                text_rules=text_rules,
                plugin_rules=list(self.plugin_rules),
                stale_plugin_rules=list(self.stale_plugin_rules),
                event_rules=list(self.event_rules),
                plugin_source_rule_records=list(self.plugin_source_rules),
                plugin_source_scan=plugin_source_scan,
                note_tag_rules=list(self.note_tag_rules),
                mv_virtual_namebox_rules=list(self.mv_virtual_namebox_rules),
                translated_items=list(self.translated_items),
                analysis_index=self.analysis_index,
                include_write_probe=include_write_probe,
            )


@timed_async_diagnostic_stage("game_analysis_context")
async def build_game_analysis_context(
    *,
    session: TargetGameSession,
    game_data: GameData,
    text_rules: TextRules,
    translated_items: list[TranslationItem] | None = None,
    placeholder_rules: list[PlaceholderRuleRecord] | None = None,
    structured_placeholder_rules: list[StructuredPlaceholderRuleRecord] | None = None,
    include_write_probe: bool = False,
) -> GameAnalysisContext:
    """一次加载单命令所需的规则、译文、AST 和全部索引。"""
    plugin_rule_records = await session.read_plugin_text_rules()
    plugin_rules, stale_plugin_rules = filter_fresh_plugin_text_rules(
        game_data=game_data,
        plugin_rules=plugin_rule_records,
    )
    event_rules = await session.read_event_command_text_rules()
    plugin_source_rules = await session.read_plugin_source_text_rules()
    note_tag_rules = await session.read_note_tag_text_rules()
    mv_virtual_namebox_rules = await session.read_mv_virtual_namebox_rules()
    resolved_placeholder_rules = (
        await session.read_placeholder_rules() if placeholder_rules is None else placeholder_rules
    )
    resolved_structured_placeholder_rules = (
        await session.read_structured_placeholder_rules()
        if structured_placeholder_rules is None
        else structured_placeholder_rules
    )
    resolved_translated_items = await session.read_translated_items() if translated_items is None else translated_items

    scan_counts = GameAnalysisScanCounts(
        plugin_ast_scan_count=0,
        event_index_scan_count=0,
        note_index_scan_count=0,
        plugin_parameter_index_scan_count=0,
        text_scope_build_count=0,
    )
    with diagnostic_stage("plugin_ast_scan"):
        plugin_source_raw_index = build_plugin_source_raw_index(
            game_data=game_data,
        )
        plugin_ast = derive_plugin_source_scan(
            index=plugin_source_raw_index,
            text_rules=text_rules,
        )
    scan_counts.plugin_ast_scan_count += 1
    with diagnostic_stage("text_analysis_index_build"):
        analysis_index = build_text_scope_analysis_index(game_data)
    scan_counts.event_index_scan_count += analysis_index.build_metrics.event_index_scan_count
    scan_counts.note_index_scan_count += analysis_index.build_metrics.note_index_scan_count
    scan_counts.plugin_parameter_index_scan_count += analysis_index.build_metrics.plugin_parameter_index_scan_count
    scan_counts.text_scope_build_count += 1
    with diagnostic_stage("text_scope_build"):
        scope = TextScopeService().build_from_loaded_rules(
            game_data=game_data,
            text_rules=text_rules,
            plugin_rules=plugin_rules,
            stale_plugin_rules=stale_plugin_rules,
            event_rules=event_rules,
            plugin_source_rule_records=plugin_source_rules,
            plugin_source_scan=plugin_ast,
            note_tag_rules=note_tag_rules,
            mv_virtual_namebox_rules=mv_virtual_namebox_rules,
            translated_items=resolved_translated_items,
            analysis_index=analysis_index,
            include_write_probe=include_write_probe,
        )

    event_commands = analysis_index.event_commands
    event_command_snapshots = event_command_analysis_snapshots(event_commands)
    note_candidates = tuple(
        _json_objects(
            collect_note_tag_candidates_from_sources(
                sources=analysis_index.note_sources,
                text_rules=text_rules,
            )
        )
    )
    plugin_parameter_candidates = tuple(
        candidate for entry in analysis_index.plugin_parameters for candidate in entry.json_string_leaf_candidates
    )
    command_snapshots = tuple(
        (entry.location_path, entry.display_name, entry.command) for entry in analysis_index.event_commands
    )
    mv_virtual_namebox_candidate_index = tuple(
        collect_mv_virtual_namebox_candidates_from_command_snapshots(command_snapshots)
        if game_data.layout.engine_kind == "mv"
        else ()
    )
    mv_virtual_namebox_candidates = tuple(
        mv_virtual_namebox_candidate_details_from_candidates(mv_virtual_namebox_candidate_index)
    )
    translated_item_index = {item.location_path: item for item in resolved_translated_items}
    write_target_index = {entry.location_path: entry for entry in scope.entries if entry.can_write_back}
    record_scan_counts(
        {
            "plugin_ast_scan_count": scan_counts.plugin_ast_scan_count,
            "event_index_scan_count": scan_counts.event_index_scan_count,
            "note_index_scan_count": scan_counts.note_index_scan_count,
            "plugin_parameter_index_scan_count": scan_counts.plugin_parameter_index_scan_count,
            "text_scope_build_count": scan_counts.text_scope_build_count,
        }
    )
    return GameAnalysisContext(
        game_data=game_data,
        text_rules=text_rules,
        plugin_source_raw_index=plugin_source_raw_index,
        plugin_ast=plugin_ast,
        analysis_index=analysis_index,
        event_commands=event_commands,
        event_command_snapshots=event_command_snapshots,
        note_candidates=note_candidates,
        plugin_parameter_candidates=plugin_parameter_candidates,
        mv_virtual_namebox_candidate_index=mv_virtual_namebox_candidate_index,
        mv_virtual_namebox_candidates=mv_virtual_namebox_candidates,
        placeholder_rules=tuple(resolved_placeholder_rules),
        structured_placeholder_rules=tuple(resolved_structured_placeholder_rules),
        plugin_rule_records=tuple(plugin_rule_records),
        plugin_rules=tuple(plugin_rules),
        stale_plugin_rules=tuple(stale_plugin_rules),
        event_rules=tuple(event_rules),
        plugin_source_rules=tuple(plugin_source_rules),
        note_tag_rules=tuple(note_tag_rules),
        mv_virtual_namebox_rules=tuple(mv_virtual_namebox_rules),
        translated_items=tuple(resolved_translated_items),
        translated_item_index=translated_item_index,
        scope=scope,
        write_target_index=write_target_index,
        scan_counts=scan_counts,
    )


def _json_objects(values: JsonArray) -> list[JsonObject]:
    """将 JSON 数组收窄为对象列表。"""
    return [value for value in values if isinstance(value, dict)]


__all__ = [
    "EventCommandAnalysisEntry",
    "GameAnalysisContext",
    "GameAnalysisScanCounts",
    "build_game_analysis_context",
]
