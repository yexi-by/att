"""文本范围构建所需的单命令共享索引。"""

from dataclasses import dataclass

from app.event_command_text.index import EventCommandAnalysisEntry, build_event_command_analysis_index
from app.note_tag_text.sources import NoteTagSource, collect_note_tag_sources
from app.plugin_text.index import PluginParameterAnalysisEntry, build_plugin_parameter_analysis_index
from app.rmmz.schema import GameData


@dataclass(frozen=True, slots=True)
class TextScopeIndexBuildMetrics:
    """本次索引构建实际执行的底层扫描次数。"""

    event_index_scan_count: int
    note_index_scan_count: int
    plugin_parameter_index_scan_count: int


@dataclass(frozen=True, slots=True)
class TextScopeAnalysisIndex:
    """同一命令中被正文提取、规则命中和候选摘要共享的事实。"""

    event_commands: tuple[EventCommandAnalysisEntry, ...]
    note_sources: tuple[NoteTagSource, ...]
    plugin_parameters: tuple[PluginParameterAnalysisEntry, ...]
    build_metrics: TextScopeIndexBuildMetrics


def build_text_scope_analysis_index(game_data: GameData) -> TextScopeAnalysisIndex:
    """依次建立三类索引，并记录实际发生的构建调用。"""
    event_scan_count = 0
    note_scan_count = 0
    plugin_scan_count = 0

    event_commands = build_event_command_analysis_index(game_data)
    event_scan_count += 1
    note_sources = tuple(collect_note_tag_sources(game_data=game_data))
    note_scan_count += 1
    plugin_parameters = build_plugin_parameter_analysis_index(game_data)
    plugin_scan_count += 1
    return TextScopeAnalysisIndex(
        event_commands=event_commands,
        note_sources=note_sources,
        plugin_parameters=plugin_parameters,
        build_metrics=TextScopeIndexBuildMetrics(
            event_index_scan_count=event_scan_count,
            note_index_scan_count=note_scan_count,
            plugin_parameter_index_scan_count=plugin_scan_count,
        ),
    )


__all__ = [
    "TextScopeAnalysisIndex",
    "TextScopeIndexBuildMetrics",
    "build_text_scope_analysis_index",
]
