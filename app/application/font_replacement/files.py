"""字体替换相关文件读写与路径解析。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.native_plugins import parse_native_plugins_array
from app.rmmz.schema import PLUGINS_JS_PATTERN
from app.rmmz.text_rules import JsonValue, coerce_json_value
from app.runtime_paths import resolve_app_home_path

from .constants import FONT_FILE_SUFFIXES
from .css import collect_gamefont_css_font_names
from .references import normalize_font_name_list


def resolve_replacement_font_path(font_path_text: str) -> Path:
    """解析配置中的字体路径。"""
    resolved_path = resolve_app_home_path(font_path_text)
    if not resolved_path.exists():
        raise FileNotFoundError(f"替换字体文件不存在: {resolved_path}")
    if not resolved_path.is_file():
        raise FileNotFoundError(f"替换字体路径不是文件: {resolved_path}")
    if resolved_path.suffix.lower() not in FONT_FILE_SUFFIXES:
        raise ValueError(f"替换字体文件扩展名不受支持: {resolved_path}")
    return resolved_path


def collect_existing_font_names(*, font_dir: Path, replacement_font_name: str) -> list[str]:
    """收集游戏字体目录中需要被替换的字体文件名。"""
    if not font_dir.exists():
        return []
    if not font_dir.is_dir():
        raise NotADirectoryError(f"游戏字体路径不是目录: {font_dir}")

    replacement_font_name_lower = replacement_font_name.lower()
    font_names: list[str] = []
    for font_path in sorted(font_dir.iterdir(), key=lambda path: path.name.lower()):
        if not font_path.is_file():
            continue
        if font_path.suffix.lower() not in FONT_FILE_SUFFIXES:
            continue
        if font_path.name.lower() == replacement_font_name_lower:
            continue
        font_names.append(font_path.name)
    return font_names


def collect_replaced_source_font_names(*, font_dir: Path, replacement_font_name: str) -> list[str]:
    """合并字体目录和 `gamefont.css` 中声明的旧字体文件名。"""
    return normalize_font_name_list(
        [
            *collect_existing_font_names(
                font_dir=font_dir,
                replacement_font_name=replacement_font_name,
            ),
            *collect_gamefont_css_font_names(
                font_dir=font_dir,
                replacement_font_name=replacement_font_name,
            ),
        ]
    )


def read_json_value_file(file_path: Path) -> JsonValue:
    """读取 JSON 文件并收窄为项目 JSON 值。"""
    raw_text = file_path.read_text(encoding="utf-8")
    decoded = cast(object, json.loads(raw_text))
    return coerce_json_value(decoded)


def read_plugins_js_file(file_path: Path) -> list[dict[str, JsonValue]]:
    """读取并解析 RPG Maker MV/MZ 的 `plugins.js`。"""
    plugins_text = file_path.read_text(encoding="utf-8")
    match = PLUGINS_JS_PATTERN.search(plugins_text)
    if match is None:
        raise ValueError(f"plugins.js 中未找到标准 $plugins 数组: {file_path}")
    decoded = parse_native_plugins_array(match.group(1))
    plugins: list[dict[str, JsonValue]] = []
    for index, plugin_value in enumerate(decoded):
        if not isinstance(plugin_value, dict):
            raise TypeError(f"plugins.js 第 {index} 个插件不是对象: {file_path}")
        plugins.append(plugin_value)
    return plugins


def serialize_plugins_js(plugins: list[dict[str, JsonValue]]) -> str:
    """序列化插件配置为 RPG Maker MV/MZ 使用的 JavaScript 文本。"""
    plugins_text = json.dumps(plugins, ensure_ascii=False, indent=2)
    return f"var $plugins = {plugins_text};\n"
