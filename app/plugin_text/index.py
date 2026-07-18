"""插件参数单命令共享索引。"""

from dataclasses import dataclass

from app.rmmz.schema import GameData
from app.rmmz.text_rules import JsonObject, JsonValue

from .common import (
    ResolvedLeaf,
    collect_plugin_json_string_leaf_candidates_from_resolved_leaves,
    extract_plugin_name,
    resolve_plugin_leaves,
)


@dataclass(frozen=True, slots=True)
class PluginParameterAnalysisEntry:
    """单个插件及其已经展开的参数叶和候选摘要。"""

    plugin_index: int
    plugin_name: str
    plugin: dict[str, JsonValue]
    resolved_leaves: tuple[ResolvedLeaf, ...]
    json_string_leaf_candidates: tuple[JsonObject, ...]


def build_plugin_parameter_analysis_index(game_data: GameData) -> tuple[PluginParameterAnalysisEntry, ...]:
    """每个插件只展开一次参数叶，并从同一结果生成候选摘要。"""
    entries: list[PluginParameterAnalysisEntry] = []
    for plugin_index, plugin in enumerate(game_data.plugins_js):
        plugin_name = extract_plugin_name(plugin, plugin_index)
        resolved_leaves = tuple(resolve_plugin_leaves(plugin))
        entries.append(
            PluginParameterAnalysisEntry(
                plugin_index=plugin_index,
                plugin_name=plugin_name,
                plugin=plugin,
                resolved_leaves=resolved_leaves,
                json_string_leaf_candidates=tuple(
                    collect_plugin_json_string_leaf_candidates_from_resolved_leaves(
                        plugin_index=plugin_index,
                        plugin_name=plugin_name,
                        plugin=plugin,
                        resolved_leaves=resolved_leaves,
                    )
                ),
            )
        )
    return tuple(entries)


__all__ = ["PluginParameterAnalysisEntry", "build_plugin_parameter_analysis_index"]
