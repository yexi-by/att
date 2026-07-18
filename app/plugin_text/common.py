"""
插件文本规则共用工具。

本模块提供插件对象哈希与路径展开工具，用于校验外部导入规则是否匹配当前
`plugins.js` 的字符串叶子。
"""

import hashlib
import json

from app.plugin_text.paths import (
    ResolvedLeaf,
    build_json_string_leaf_path_hint,
    collect_plugin_json_string_leaf_candidates,
    collect_plugin_json_string_leaf_candidates_from_resolved_leaves,
    expand_rule_to_leaf_paths,
    jsonpath_matches_template,
    jsonpath_to_location_path,
    jsonpath_to_path_parts,
    resolve_plugin_leaves,
)
from app.rmmz.text_rules import JsonValue


def extract_plugin_name(plugin: dict[str, JsonValue], plugin_index: int) -> str:
    """读取插件名称；缺失时返回稳定兜底名。"""
    plugin_name = plugin.get("name")
    if isinstance(plugin_name, str) and plugin_name.strip():
        return plugin_name.strip()
    return f"unnamed_plugin_{plugin_index}"


def build_plugin_hash(plugin: dict[str, JsonValue]) -> str:
    """计算单个插件对象的稳定结构哈希。"""
    payload = json.dumps(plugin, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plugins_file_hash(plugins: list[dict[str, JsonValue]]) -> str:
    """计算整份 `plugins.js` 的结构哈希。"""
    payload = json.dumps(plugins, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__: list[str] = [
    "ResolvedLeaf",
    "build_json_string_leaf_path_hint",
    "build_plugin_hash",
    "build_plugins_file_hash",
    "collect_plugin_json_string_leaf_candidates",
    "collect_plugin_json_string_leaf_candidates_from_resolved_leaves",
    "expand_rule_to_leaf_paths",
    "extract_plugin_name",
    "jsonpath_matches_template",
    "jsonpath_to_location_path",
    "jsonpath_to_path_parts",
    "resolve_plugin_leaves",
]
