"""RPG Maker MV/MZ 标准数据处理公共导出入口。"""

from .extraction import DataTextExtraction
from .game_file_view import GameFileView, parse_game_file_view
from .loader import (
    GameDataManager,
    load_active_game_data,
    load_active_runtime_game_data,
    load_game_data,
    load_game_data_for_view,
    load_translation_source_game_data,
    read_game_title,
    resolve_game_directory,
    resolve_game_layout,
    resolve_game_source_paths,
)

__all__: list[str] = [
    "DataTextExtraction",
    "GameDataManager",
    "GameFileView",
    "load_active_runtime_game_data",
    "load_active_game_data",
    "load_game_data",
    "load_game_data_for_view",
    "load_translation_source_game_data",
    "parse_game_file_view",
    "read_game_title",
    "resolve_game_directory",
    "resolve_game_layout",
    "resolve_game_source_paths",
]
