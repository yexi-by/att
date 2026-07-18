"""事件指令单命令共享索引。"""

from dataclasses import dataclass

from app.plugin_text.paths import ResolvedLeaf, resolve_event_command_leaves
from app.rmmz.commands import iter_all_commands
from app.rmmz.game_data import EventCommand
from app.rmmz.schema import GameData
from app.rmmz.text_rules import JsonArray, JsonObject


@dataclass(frozen=True, slots=True)
class EventCommandAnalysisEntry:
    """事件指令及其已经展开的参数叶索引。"""

    location_path: tuple[str | int, ...]
    display_name: str
    command: EventCommand
    resolved_leaves: tuple[ResolvedLeaf, ...]


def build_event_command_analysis_index(game_data: GameData) -> tuple[EventCommandAnalysisEntry, ...]:
    """遍历一次全部事件指令，并为每条指令展开一次参数叶。"""
    return tuple(
        EventCommandAnalysisEntry(
            location_path=tuple(path),
            display_name=display_name,
            command=command,
            resolved_leaves=tuple(resolve_event_command_leaves(command.parameters)),
        )
        for path, display_name, command in iter_all_commands(game_data)
    )


def event_command_analysis_snapshots(
    command_index: tuple[EventCommandAnalysisEntry, ...],
) -> tuple[JsonObject, ...]:
    """把事件索引转换成稳定的范围哈希载荷。"""
    snapshots: list[JsonObject] = []
    for entry in command_index:
        path: JsonArray = [part for part in entry.location_path]
        parameters: JsonArray = [parameter for parameter in entry.command.parameters]
        snapshots.append(
            {
                "path": path,
                "code": entry.command.code,
                "parameters": parameters,
            }
        )
    return tuple(snapshots)


__all__ = [
    "EventCommandAnalysisEntry",
    "build_event_command_analysis_index",
    "event_command_analysis_snapshots",
]
