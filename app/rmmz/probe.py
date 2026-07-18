"""
对话探针工具模块。

负责在游戏数据加载完成后，检查所有对话文本指令的前置结构是否符合预期。
当前规则保持简单直接：
1. 所有 `Code.TEXT(401)` 的前一条指令必须是 `Code.NAME(101)` 或连续的 `Code.TEXT(401)`。
2. 一旦发现孤立的 `401`，立即抛出异常，阻止后续翻译流程启动。
"""

from app.observability.logging import logger
from app.rmmz.game_data import CommonEvent, EventCommand, MapData, Troop
from app.rmmz.schema import Code


def run_dialogue_probe(
    *,
    map_data: dict[str, MapData],
    common_events: list[CommonEvent | None],
    troops: list[Troop | None],
) -> None:
    """
    执行游戏对话结构的探针检查。

    由于 RPG Maker 中完整的对话块通常由 101 和后续连续的 401 文本指令组成，
    如果在没有 101 或者前置不是 401 的情况下突然出现了 401 文本指令，意味着该游戏
    使用了非常规的插件命令或是未支持的结构。
    该函数会在项目启动并完成游戏数据解析后立即运行。如果探针不通过，则抛出异常并阻止启动。

    Args:
        map_data: 已解析的全部地图数据字典。
        common_events: 已解析的公共事件列表（稀疏数组）。
        troops: 已解析的敌群事件列表（稀疏数组）。

    Raises:
        ValueError: 发现孤立的 `Code.TEXT(401)` 时抛出，表示检测到异常的对话结构。
    """
    logger.info("[tag.phase]对话探针[/tag.phase] 开始执行检查")

    for file_name, map_item in map_data.items():
        for event in map_item.events:
            if event is None:
                continue

            for page_index, page in enumerate(event.pages, start=1):
                _check_command_list(
                    commands=page.commands,
                    location_info=(f"{file_name} -> Event {event.id} -> Page {page_index}"),
                )

    for common_event in common_events:
        if common_event is None:
            continue

        _check_command_list(
            commands=common_event.commands,
            location_info=f"CommonEvents.json -> CommonEvent {common_event.id}",
        )

    for troop in troops:
        if troop is None:
            continue

        for page_index, page in enumerate(troop.pages, start=1):
            _check_command_list(
                commands=page.commands,
                location_info=f"Troops.json -> Troop {troop.id} -> Page {page_index}",
            )

    logger.success("[tag.success]对话探针检查通过[/tag.success]")


def _check_command_list(commands: list[EventCommand], location_info: str) -> None:
    """
    对单组事件指令列表进行序列检查，判断是否存在孤立的 `Code.TEXT(401)`。

    具体规则：
    当当前指令是 401(TEXT) 时，其前一条指令必须是 101(NAME) 或者 401(TEXT)。
    否则就认为是孤立的 401 文本块，无法正常推断角色或连续段落。

    Args:
        commands: 从某个事件页或公共事件中提取出的完整指令列表。
        location_info: 用于报错时输出的人类可读的详细定位信息。

    Raises:
        ValueError: 一旦检测到孤立文本块，立刻抛出异常并携带精准的定位。
    """
    previous_code: int | None = None

    for command_index, command in enumerate(commands):
        if command.code == Code.TEXT and previous_code not in (Code.NAME, Code.TEXT):
            raise ValueError(
                f"对话探针检查失败: {location_info} 中发现孤立的 RPG Maker 401 文本指令，当前指令索引 {command_index}，前一条指令编码为 {previous_code}"
            )

        previous_code = command.code


__all__: list[str] = ["run_dialogue_probe"]
