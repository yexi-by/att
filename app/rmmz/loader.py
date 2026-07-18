"""
游戏加载统一入口模块。

本模块加载 RPG Maker MV/MZ 标准数据文件与 `js/plugins.js`。未知 `data/*.json`
会被跳过并记录 DEBUG 日志。
"""

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import cast

import aiofiles
from pydantic import TypeAdapter

from app.native_plugins import parse_native_plugins_array
from app.observability.logging import logger
from app.rmmz.game_data import BaseItem, CommonEvent, MapData, System, Troop
from app.rmmz.game_file_view import GameFileView
from app.rmmz.probe import run_dialogue_probe
from app.rmmz.schema import (
    COMMON_EVENTS_FILE_NAME,
    DATA_DIRECTORY_NAME,
    DATA_ORIGIN_DIRECTORY_NAME,
    FIXED_FILE_NAMES,
    JS_DIRECTORY_NAME,
    MAP_INFOS_FILE_NAME,
    MAP_PATTERN,
    PLUGIN_SOURCE_ORIGIN_DIRECTORY_NAME,
    PLUGINS_FILE_NAME,
    PLUGINS_JS_PATTERN,
    PLUGINS_ORIGIN_FILE_NAME,
    SYSTEM_FILE_NAME,
    TROOPS_FILE_NAME,
    EngineKind,
    GameData,
    GameLayout,
)
from app.rmmz.text_rules import JsonValue, ensure_json_object

PACKAGE_FILE_NAME = "package.json"
WWW_DIRECTORY_NAME = "www"
RMMZ_CORE_FILE_NAME = "rmmz_core.js"
RPG_CORE_FILE_NAME = "rpg_core.js"
ENGINE_NAME_PATTERN: re.Pattern[str] = re.compile(r"RPGMAKER_NAME\s*=\s*['\"](?P<name>MV|MZ)['\"]")
ENGINE_VERSION_PATTERN: re.Pattern[str] = re.compile(r"RPGMAKER_VERSION\s*=\s*['\"](?P<version>[^'\"]+)['\"]")


async def load_game_data(
    game_path: str | Path,
    *,
    include_plugin_source_files: bool = True,
    include_writable_copies: bool = True,
) -> GameData:
    """加载游戏数据；业务服务层必须使用显式视图入口。"""
    return await _load_game_data(
        game_path,
        use_origin_backups=True,
        require_origin_backups=False,
        include_plugin_source_files=include_plugin_source_files,
        include_writable_copies=include_writable_copies,
    )


async def load_game_data_for_view(
    game_path: str | Path,
    *,
    source_view: GameFileView,
    include_plugin_source_files: bool = True,
    include_writable_copies: bool = True,
) -> GameData:
    """按指定文件视图加载游戏数据。"""
    return await _load_game_data(
        game_path,
        use_origin_backups=source_view == GameFileView.TRANSLATION_SOURCE,
        require_origin_backups=source_view == GameFileView.TRANSLATION_SOURCE,
        include_plugin_source_files=include_plugin_source_files,
        include_writable_copies=include_writable_copies,
    )


async def load_active_game_data(
    game_path: str | Path,
    *,
    include_plugin_source_files: bool = True,
    include_writable_copies: bool = True,
) -> GameData:
    """从 RPG Maker 游戏根目录加载当前激活文件，不读取完整原始备份。"""
    return await load_game_data_for_view(
        game_path,
        source_view=GameFileView.ACTIVE_RUNTIME,
        include_plugin_source_files=include_plugin_source_files,
        include_writable_copies=include_writable_copies,
    )


async def load_active_runtime_game_data(
    game_path: str | Path,
    *,
    include_plugin_source_files: bool = True,
    include_writable_copies: bool = False,
) -> GameData:
    """从 RPG Maker 游戏根目录加载当前运行视图文件。"""
    return await load_game_data_for_view(
        game_path,
        source_view=GameFileView.ACTIVE_RUNTIME,
        include_plugin_source_files=include_plugin_source_files,
        include_writable_copies=include_writable_copies,
    )


async def load_translation_source_game_data(
    game_path: str | Path,
    *,
    include_plugin_source_files: bool = True,
    include_writable_copies: bool = True,
) -> GameData:
    """从 RPG Maker 游戏根目录加载翻译源视图文件。"""
    return await _load_game_data(
        game_path,
        use_origin_backups=True,
        require_origin_backups=True,
        include_plugin_source_files=include_plugin_source_files,
        include_writable_copies=include_writable_copies,
    )


async def _load_game_data(
    game_path: str | Path,
    *,
    use_origin_backups: bool,
    require_origin_backups: bool,
    include_plugin_source_files: bool,
    include_writable_copies: bool,
) -> GameData:
    """按指定来源策略加载标准数据文件并构造 `GameData`。"""
    layout = resolve_game_layout(game_path)
    source_data_dir = resolve_data_source_dir(
        layout=layout,
        use_origin_backups=use_origin_backups,
        require_origin_backups=require_origin_backups,
    )
    source_plugins_path = resolve_plugins_source_file(
        layout=layout,
        use_origin_backups=use_origin_backups,
        require_origin_backups=require_origin_backups,
    )

    valid_files = sorted(
        (
            file_path
            for file_path in source_data_dir.iterdir()
            if file_path.is_file() and _is_standard_rmmz_filename(file_path.name)
        ),
        key=lambda file_path: file_path.name,
    )
    _log_skipped_data_files(source_data_dir=source_data_dir, valid_files=valid_files)

    file_contents = await asyncio.gather(*(_read_text_file(file_path) for file_path in valid_files))

    data: dict[str, JsonValue] = {}
    map_data: dict[str, MapData] = {}
    system: System | None = None
    common_events: list[CommonEvent | None] | None = None
    troops: list[Troop | None] | None = None
    base_data: dict[str, list[BaseItem | None]] = {}

    common_events_adapter: TypeAdapter[list[CommonEvent | None]] = TypeAdapter(list[CommonEvent | None])
    troops_adapter: TypeAdapter[list[Troop | None]] = TypeAdapter(list[Troop | None])
    base_data_adapter: TypeAdapter[list[BaseItem | None]] = TypeAdapter(list[BaseItem | None])

    for file_path, content in zip(valid_files, file_contents, strict=True):
        file_name = file_path.name
        json_value = _decode_json_value(content=content, source=file_path)
        data[file_name] = json_value

        if MAP_PATTERN.fullmatch(file_name):
            map_data[file_name] = MapData.model_validate(json_value)
            continue

        if file_name == SYSTEM_FILE_NAME:
            system = System.model_validate(json_value)
        elif file_name == COMMON_EVENTS_FILE_NAME:
            common_events = common_events_adapter.validate_python(json_value)
        elif file_name == TROOPS_FILE_NAME:
            troops = troops_adapter.validate_python(json_value)
        else:
            base_data[file_name] = base_data_adapter.validate_python(json_value)

    plugins_content = await _read_text_file(source_plugins_path)
    data[PLUGINS_FILE_NAME] = plugins_content
    plugins_js = _parse_plugins_js_text(plugins_content)
    if include_plugin_source_files:
        plugin_source_files, plugin_source_read_errors = await _read_plugin_source_files(
            layout=layout,
            use_origin_backups=use_origin_backups,
            require_origin_backups=require_origin_backups,
        )
    else:
        plugin_source_files, plugin_source_read_errors = {}, {}

    if system is None or common_events is None or troops is None:
        raise ValueError("游戏缺少 System.json、CommonEvents.json 或 Troops.json，禁止启动")

    run_dialogue_probe(map_data=map_data, common_events=common_events, troops=troops)

    return GameData(
        layout=layout,
        data=data,
        writable_data=copy.deepcopy(data) if include_writable_copies else {},
        map_data=map_data,
        system=system,
        common_events=common_events,
        troops=troops,
        base_data=base_data,
        plugins_js=plugins_js,
        writable_plugins_js=copy.deepcopy(plugins_js) if include_writable_copies else [],
        plugin_source_files=plugin_source_files,
        plugin_source_read_errors=plugin_source_read_errors,
        writable_plugin_source_files=dict(plugin_source_files) if include_writable_copies else {},
    )


def resolve_game_directory(game_path: str | Path) -> Path:
    """解析并校验游戏根目录路径。"""
    resolved_path = Path(game_path).resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"游戏目录不存在: {resolved_path}")
    if not resolved_path.is_dir():
        raise NotADirectoryError(f"游戏路径不是目录: {resolved_path}")
    return resolved_path


def read_game_title(game_path: Path) -> str:
    """按 package.json、System.json、目录名顺序读取游戏标题。"""
    layout = resolve_game_layout(game_path)
    package_title = read_game_title_from_package(layout.package_path)
    if package_title is not None:
        return package_title

    system_title = read_game_title_from_system(layout.data_dir / SYSTEM_FILE_NAME)
    if system_title is not None:
        return system_title

    directory_title = layout.game_root.name.strip()
    if directory_title:
        return directory_title

    raise ValueError(
        f"游戏标题为空，请确认 package.json.window.title、data/System.json.gameTitle 或游戏目录名有效: {layout.game_root}"
    )


def read_game_title_from_package(package_path: Path) -> str | None:
    """从 package.json 读取窗口标题，缺失或空标题时返回 None。"""
    if not package_path.exists():
        return None

    raw_text = package_path.read_text(encoding="utf-8")
    package_data = _decode_json_value(content=raw_text, source=package_path)
    package_object = ensure_json_object(package_data, f"{package_path} 顶层")

    window_config = package_object.get("window")
    if not isinstance(window_config, dict):
        return None

    title = window_config.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    return title.strip()


def read_game_title_from_system(system_path: Path) -> str | None:
    """从 System.json 读取游戏标题，缺失或空标题时返回 None。"""
    if not system_path.exists():
        return None

    raw_text = system_path.read_text(encoding="utf-8")
    system_data = _decode_json_value(content=raw_text, source=system_path)
    system_object = ensure_json_object(system_data, f"{system_path} 顶层")
    title = system_object.get("gameTitle")
    if not isinstance(title, str) or not title.strip():
        return None
    return title.strip()


def resolve_game_layout(game_path: str | Path) -> GameLayout:
    """解析 RPG Maker MV/MZ 的真实数据目录与插件配置位置。"""
    game_root = resolve_game_directory(game_path)
    candidate_roots = [game_root, game_root / WWW_DIRECTORY_NAME]
    for content_root in candidate_roots:
        data_dir = content_root / DATA_DIRECTORY_NAME
        plugins_path = content_root / JS_DIRECTORY_NAME / PLUGINS_FILE_NAME
        if not data_dir.is_dir() or not plugins_path.is_file():
            continue
        return build_game_layout(
            game_root=game_root,
            content_root=content_root,
            is_www_layout=content_root != game_root,
        )

    raise FileNotFoundError(
        f"未找到可识别的 RPG Maker MV/MZ 游戏结构，请确认目录内存在 data/js 或 www/data/www/js: {game_root}"
    )


def build_game_layout(*, game_root: Path, content_root: Path, is_www_layout: bool) -> GameLayout:
    """根据真实内容目录构造游戏布局对象。"""
    js_dir = content_root / JS_DIRECTORY_NAME
    engine_kind, engine_version = detect_engine_kind_and_version(
        js_dir=js_dir,
        is_www_layout=is_www_layout,
    )
    package_path = resolve_package_path(game_root=game_root, content_root=content_root)
    return GameLayout(
        game_root=game_root,
        content_root=content_root,
        data_dir=content_root / DATA_DIRECTORY_NAME,
        data_origin_dir=content_root / DATA_ORIGIN_DIRECTORY_NAME,
        js_dir=js_dir,
        plugins_path=js_dir / PLUGINS_FILE_NAME,
        plugins_origin_path=js_dir / PLUGINS_ORIGIN_FILE_NAME,
        plugin_source_origin_dir=js_dir / PLUGIN_SOURCE_ORIGIN_DIRECTORY_NAME,
        package_path=package_path,
        engine_kind=engine_kind,
        engine_version=engine_version,
        is_www_layout=is_www_layout,
    )


def resolve_package_path(*, game_root: Path, content_root: Path) -> Path:
    """解析优先用于读取标题的 package.json 路径。"""
    root_package_path = game_root / PACKAGE_FILE_NAME
    if root_package_path.exists():
        return root_package_path
    return content_root / PACKAGE_FILE_NAME


def detect_engine_kind_and_version(*, js_dir: Path, is_www_layout: bool) -> tuple[EngineKind, str]:
    """从核心脚本识别 RPG Maker 引擎类型与版本。"""
    core_candidates = [js_dir / RMMZ_CORE_FILE_NAME, js_dir / RPG_CORE_FILE_NAME]
    for core_path in core_candidates:
        if not core_path.exists():
            continue
        core_text = core_path.read_text(encoding="utf-8", errors="ignore")
        name_match = ENGINE_NAME_PATTERN.search(core_text)
        version_match = ENGINE_VERSION_PATTERN.search(core_text)
        version = version_match.group("version").strip() if version_match is not None else "unknown"
        if name_match is not None:
            engine_name = name_match.group("name")
            if engine_name == "MV":
                return "mv", version
            return "mz", version
        if core_path.name == RMMZ_CORE_FILE_NAME:
            return "mz", version
        if core_path.name == RPG_CORE_FILE_NAME:
            return "mv", version

    if is_www_layout:
        return "mv", "unknown"
    return "mz", "unknown"


def resolve_game_source_paths(game_root: Path) -> tuple[Path, Path, bool]:
    """根据是否存在原件备份解析本次应读取的源数据路径。"""
    layout = resolve_game_layout(game_root)
    source_data_dir = resolve_data_source_dir(
        layout=layout,
        use_origin_backups=True,
        require_origin_backups=True,
    )
    if not layout.source_plugins_path.exists():
        raise FileNotFoundError(f"插件配置文件不存在: {layout.plugins_origin_path}")
    return source_data_dir, layout.plugins_origin_path, layout.has_origin_backup


def resolve_data_source_file(*, active_file_path: Path, origin_data_dir: Path, use_origin_backup: bool = True) -> Path:
    """解析单个 data 文件来源；存在完整原始备份时不回退激活文件。"""
    if not use_origin_backup:
        return active_file_path
    if origin_data_dir.exists() and not origin_data_dir.is_dir():
        raise NotADirectoryError(f"原始 data 备份不是目录: {origin_data_dir}")
    if origin_data_dir.is_dir():
        return origin_data_dir / active_file_path.name
    return active_file_path


def resolve_data_source_dir(
    *,
    layout: GameLayout,
    use_origin_backups: bool,
    require_origin_backups: bool = False,
) -> Path:
    """解析本轮读取的 data 源目录，并校验激活目录和原始备份完整性。"""
    if not use_origin_backups:
        validate_data_directory_integrity(data_dir=layout.data_dir, role="激活数据目录")
        return layout.data_dir
    if layout.data_origin_dir.exists() and not layout.data_origin_dir.is_dir():
        raise NotADirectoryError(f"原始 data 备份不是目录: {layout.data_origin_dir}")
    if layout.data_origin_dir.is_dir():
        validate_data_directory_integrity(data_dir=layout.data_origin_dir, role="原始 data 备份")
        return layout.data_origin_dir
    if require_origin_backups:
        raise FileNotFoundError(f"缺少原始 data 备份，不能读取翻译源视图: {layout.data_origin_dir}")
    validate_data_directory_integrity(data_dir=layout.data_dir, role="激活数据目录")
    return layout.data_dir


def resolve_plugins_source_file(
    *,
    layout: GameLayout,
    use_origin_backups: bool,
    require_origin_backups: bool = False,
) -> Path:
    """按视图解析插件配置文件路径。"""
    if not use_origin_backups:
        if not layout.plugins_path.is_file():
            raise FileNotFoundError(f"激活插件配置文件不存在: {layout.plugins_path}")
        return layout.plugins_path
    if not layout.plugins_origin_path.is_file() and require_origin_backups:
        raise FileNotFoundError(f"缺少原始插件配置备份，不能读取翻译源视图: {layout.plugins_origin_path}")
    if not layout.plugins_origin_path.is_file():
        return layout.plugins_path
    return layout.plugins_origin_path


def validate_data_directory_integrity(*, data_dir: Path, role: str) -> None:
    """校验 RPG Maker 标准 data 目录包含完整标准文件和地图文件。"""
    if not data_dir.is_dir():
        raise NotADirectoryError(f"{role}不是目录: {data_dir}")
    file_names = {file_path.name for file_path in data_dir.iterdir() if file_path.is_file()}
    missing_fixed_files = sorted(FIXED_FILE_NAMES.difference(file_names))
    if missing_fixed_files:
        raise FileNotFoundError(
            f"{role}缺少 RPG Maker 标准 data 文件: {', '.join(missing_fixed_files)}。"
            + "data_origin 必须是完整原始 data 备份，请使用干净游戏目录重新注册。"
        )
    missing_map_files = collect_missing_map_files_from_map_infos(data_dir=data_dir)
    if missing_map_files:
        raise FileNotFoundError(f"{role}的 MapInfos.json 引用了缺失地图文件: {', '.join(missing_map_files)}")


def collect_missing_map_files_from_map_infos(*, data_dir: Path) -> list[str]:
    """读取 MapInfos.json 并返回缺失的地图文件名。"""
    map_infos_path = data_dir / MAP_INFOS_FILE_NAME
    map_infos_value = _decode_json_value(
        content=map_infos_path.read_text(encoding="utf-8"),
        source=map_infos_path,
    )
    if not isinstance(map_infos_value, list):
        raise TypeError(f"{MAP_INFOS_FILE_NAME} 顶层必须是数组: {map_infos_path}")
    expected_map_names: set[str] = set()
    for index, item in enumerate(map_infos_value):
        if item is None:
            continue
        if not isinstance(item, dict):
            raise TypeError(f"{MAP_INFOS_FILE_NAME}[{index}] 必须是对象或 null")
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise TypeError(f"{MAP_INFOS_FILE_NAME}[{index}].id 必须是整数")
        if raw_id <= 0:
            continue
        expected_map_names.add(f"Map{raw_id:03d}.json")
    return sorted(map_name for map_name in expected_map_names if not (data_dir / map_name).is_file())


class GameDataManager:
    """全局游戏数据管理器。"""

    def __init__(self) -> None:
        """初始化空的游戏数据内存记录。"""
        self.items: dict[str, GameData] = {}

    async def load_game_data(self, game_path: str | Path) -> None:
        """读取指定游戏目录，并以游戏标题为键写入内存记录。"""
        resolved_game_path = resolve_game_directory(game_path)
        game_title = read_game_title(resolved_game_path)
        layout = resolve_game_layout(resolved_game_path)
        source_data_dir = resolve_data_source_dir(layout=layout, use_origin_backups=True)
        source_plugins_path = layout.source_plugins_path
        has_origin_backup = layout.has_origin_backup
        game_data = await load_game_data_for_view(
            resolved_game_path,
            source_view=GameFileView.TRANSLATION_SOURCE,
        )

        if has_origin_backup:
            logger.warning(
                f"[tag.warning]检测到该游戏已经执行过激活版回写，后续会优先读取完整原始 data 备份[/tag.warning] 游戏 [tag.count]{game_title}[/tag.count] 数据来源 [tag.path]{source_data_dir}[/tag.path] 插件来源 [tag.path]{source_plugins_path}[/tag.path]"
            )

        self.items[game_title] = game_data


async def _read_text_file(file_path: Path) -> str:
    """使用 UTF-8 异步读取文本文件。"""
    async with aiofiles.open(file_path, "r", encoding="utf-8") as file:
        return await file.read()


async def _read_plugin_source_files(
    *,
    layout: GameLayout,
    use_origin_backups: bool,
    require_origin_backups: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    """读取 `js/plugins` 直接源码文件，读取失败只记录错误不阻断普通流程。"""
    if not use_origin_backups:
        return await _read_direct_plugin_source_files(layout.js_dir / "plugins")
    if not layout.plugin_source_origin_dir.is_dir():
        if require_origin_backups:
            raise NotADirectoryError(f"缺少原始插件源码备份目录，不能读取翻译源视图: {layout.plugin_source_origin_dir}")
        return await _read_direct_plugin_source_files(layout.js_dir / "plugins")
    return await _read_direct_plugin_source_files(layout.plugin_source_origin_dir)


async def _read_direct_plugin_source_files(source_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """读取一个目录下的直接插件源码文件，按文件拆分成功内容和读取错误。"""
    if not source_dir.is_dir():
        return {}, {}
    file_paths = sorted(
        (file_path for file_path in source_dir.glob("*.js") if file_path.is_file()),
        key=lambda file_path: file_path.name,
    )
    results = await asyncio.gather(*(_read_plugin_source_file(file_path) for file_path in file_paths))
    files: dict[str, str] = {}
    errors: dict[str, str] = {}
    for file_name, content, error_text in results:
        if error_text is None:
            files[file_name] = content
        else:
            errors[file_name] = error_text
    return files, errors


async def _read_plugin_source_file(file_path: Path) -> tuple[str, str, str | None]:
    """读取单个插件源码文件；非 UTF-8 文件不进入 AST 流程。"""
    async with aiofiles.open(file_path, "rb") as file:
        content_bytes = await file.read()
    try:
        return file_path.name, content_bytes.decode("utf-8"), None
    except UnicodeDecodeError as error:
        return file_path.name, "", f"插件源码文件不是 UTF-8 文本: {error}"


def _is_standard_rmmz_filename(file_name: str) -> bool:
    """判断文件名是否属于标准 RPG Maker MV/MZ 数据文件。"""
    return file_name in FIXED_FILE_NAMES or MAP_PATTERN.fullmatch(file_name) is not None


def _log_skipped_data_files(*, source_data_dir: Path, valid_files: list[Path]) -> None:
    """把未知 data JSON 文件记录到 DEBUG 日志。"""
    valid_names = {file_path.name for file_path in valid_files}
    for file_path in sorted(source_data_dir.glob("*.json"), key=lambda path: path.name):
        if file_path.name in valid_names:
            continue
        logger.debug(f"[tag.skip]跳过非标准 data 文件[/tag.skip] [tag.path]{file_path}[/tag.path]")


def _decode_json_value(*, content: str, source: Path) -> JsonValue:
    """把 JSON 文本解析并校验为项目允许的 JSON 值。"""
    try:
        decoded = cast(object, json.loads(content))
        # 标准库 JSON 解码器只会产生项目 JsonValue 覆盖的基本类型、列表和字符串键对象。
        # JSON 解码结果在本函数内直接完成类型收窄，避免大体量游戏数据加载阶段重复消耗 CPU。
        return cast(JsonValue, decoded)
    except TypeError as error:
        raise TypeError(f"JSON 内容不是项目允许的值类型: {source}") from error


def _parse_plugins_js_text(plugins_content: str) -> list[dict[str, JsonValue]]:
    """从 `plugins.js` 解析 `$plugins` 数组。"""
    match = PLUGINS_JS_PATTERN.search(plugins_content)
    if match is None:
        raise ValueError("plugins.js 中未找到 `var $plugins = [...]` 标准结构")

    json_value = parse_native_plugins_array(match.group(1))

    plugins: list[dict[str, JsonValue]] = []
    for index, plugin in enumerate(json_value):
        if not isinstance(plugin, dict):
            raise ValueError(f"plugins.js 第 {index} 个插件不是对象")
        plugins.append(plugin)
    return plugins


__all__: list[str] = [
    "GameDataManager",
    "load_active_runtime_game_data",
    "load_active_game_data",
    "load_game_data",
    "load_game_data_for_view",
    "load_translation_source_game_data",
    "read_game_title",
    "read_game_title_from_package",
    "read_game_title_from_system",
    "resolve_data_source_file",
    "resolve_data_source_dir",
    "resolve_game_directory",
    "resolve_game_layout",
    "resolve_game_source_paths",
    "resolve_plugins_source_file",
    "validate_data_directory_integrity",
]
