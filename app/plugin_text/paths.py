"""JSON 参数树展开与受限 JSONPath 工具。"""

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from app.rmmz.text_protocol import decode_json_container_text
from app.rmmz.text_rules import JsonObject, JsonValue

JSON_INDEX_SEGMENT_PATTERN: re.Pattern[str] = re.compile(r"\[\d+\]")
JSON_PATH_PATTERN: re.Pattern[str] = re.compile(r"^\$(?:\['(?:[^'\\]|\\.)+'\]|\[(?:\d+|\*)\])+$")
JSON_PATH_SEGMENT_PATTERN: re.Pattern[str] = re.compile(r"\['((?:[^'\\]|\\.)+)'\]|\[(\d+|\*)\]")


class ResolvedLeaf(BaseModel):
    """参数树展开后的叶子节点。"""

    path: str
    value: str | int | float | bool | None
    value_type: Literal["string", "number", "boolean", "null"]
    from_json_string: bool


def resolve_plugin_leaves(plugin: dict[str, JsonValue]) -> list[ResolvedLeaf]:
    """递归展开单个插件参数树，提取全部叶子节点。"""
    parameters = plugin.get("parameters")
    if not isinstance(parameters, dict):
        return []

    return resolve_json_leaves(
        value=parameters,
        root_path="$['parameters']",
    )


def resolve_event_command_leaves(parameters: list[JsonValue]) -> list[ResolvedLeaf]:
    """递归展开事件指令参数数组，提取全部叶子节点。"""
    return resolve_json_leaves(
        value={"parameters": parameters},
        root_path="$",
    )


def resolve_json_leaves(*, value: JsonValue, root_path: str) -> list[ResolvedLeaf]:
    """从任意 JSON 值根节点递归展开叶子路径。"""
    leaves: list[ResolvedLeaf] = []
    walk_plugin_value(
        value=value,
        current_path=root_path,
        from_json_string=False,
        leaves=leaves,
    )
    return leaves


def walk_plugin_value(
    *,
    value: JsonValue,
    current_path: str,
    from_json_string: bool,
    leaves: list[ResolvedLeaf],
) -> None:
    """递归遍历插件值，并把叶子节点写入结果列表。"""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{current_path}[{quote_jsonpath_key(key)}]"
            walk_plugin_value(
                value=child,
                current_path=child_path,
                from_json_string=from_json_string,
                leaves=leaves,
            )
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            walk_plugin_value(
                value=child,
                current_path=f"{current_path}[{index}]",
                from_json_string=from_json_string,
                leaves=leaves,
            )
        return

    if isinstance(value, str):
        parsed_container = try_parse_container_string(value)
        if parsed_container is not None:
            walk_plugin_value(
                value=parsed_container,
                current_path=current_path,
                from_json_string=True,
                leaves=leaves,
            )
            return
        leaves.append(
            ResolvedLeaf(
                path=current_path,
                value=value,
                value_type="string",
                from_json_string=from_json_string,
            )
        )
        return

    if isinstance(value, bool):
        leaves.append(
            ResolvedLeaf(
                path=current_path,
                value=value,
                value_type="boolean",
                from_json_string=from_json_string,
            )
        )
        return

    if value is None:
        leaves.append(
            ResolvedLeaf(
                path=current_path,
                value=None,
                value_type="null",
                from_json_string=from_json_string,
            )
        )
        return

    leaves.append(
        ResolvedLeaf(
            path=current_path,
            value=value,
            value_type="number",
            from_json_string=from_json_string,
        )
    )


def try_parse_container_string(value: str) -> dict[str, JsonValue] | list[JsonValue] | None:
    """尝试把字符串解析成 JSON 容器。"""
    decoded = decode_json_container_text(value)
    if decoded is None:
        return None
    return decoded.value


def quote_jsonpath_key(key: str) -> str:
    """把对象键安全包装成 JSONPath 片段。"""
    escaped_key = key.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped_key}'"


def build_allowed_templates(resolved_leaves: Sequence[ResolvedLeaf]) -> set[str]:
    """根据字符串叶子路径构造允许的模板路径集合。"""
    allowed_templates: set[str] = set()
    for leaf in resolved_leaves:
        if leaf.value_type != "string":
            continue

        index_matches = list(JSON_INDEX_SEGMENT_PATTERN.finditer(leaf.path))
        if not index_matches:
            allowed_templates.add(leaf.path)
            continue

        for mask in range(1 << len(index_matches)):
            fragments: list[str] = []
            last_end = 0
            for match_index, match in enumerate(index_matches):
                fragments.append(leaf.path[last_end : match.start()])
                fragments.append("[*]" if mask & (1 << match_index) else match.group(0))
                last_end = match.end()
            fragments.append(leaf.path[last_end:])
            allowed_templates.add("".join(fragments))
    return allowed_templates


def expand_rule_to_leaf_paths(
    *,
    path_template: str,
    resolved_leaves: Sequence[ResolvedLeaf],
) -> list[str]:
    """把模板路径展开为命中的精确叶子路径列表。"""
    matched_paths = [
        leaf.path
        for leaf in resolved_leaves
        if leaf.value_type == "string" and jsonpath_matches_template(template_path=path_template, actual_path=leaf.path)
    ]
    matched_paths.sort()
    return matched_paths


def build_json_string_leaf_path_hint(
    *,
    path_template: str,
    resolved_leaves: Sequence[ResolvedLeaf],
) -> str | None:
    """为误指向 JSON 字符串容器的规则生成内部叶子路径提示。"""
    descendant_leaves = [
        leaf
        for leaf in resolved_leaves
        if leaf.value_type == "string"
        and leaf.from_json_string
        and jsonpath_template_is_ancestor(template_path=path_template, actual_path=leaf.path)
    ]
    if not descendant_leaves:
        return None

    candidate_templates = sorted(build_allowed_templates(descendant_leaves))
    preview = "、".join(candidate_templates[:5])
    suffix = "" if len(candidate_templates) <= 5 else f" 等 {len(candidate_templates)} 条候选"
    return f"该字段疑似是 JSON 字符串容器，请把规则写到解析后的内部字符串叶子，例如: {preview}{suffix}"


def jsonpath_matches_template(*, template_path: str, actual_path: str) -> bool:
    """判断精确路径是否匹配某条模板路径。"""
    template_parts = jsonpath_to_path_parts(template_path)
    actual_parts = jsonpath_to_path_parts(actual_path)
    if len(template_parts) != len(actual_parts):
        return False

    for template_part, actual_part in zip(template_parts, actual_parts, strict=True):
        if template_part == "*":
            if not isinstance(actual_part, int):
                return False
            continue
        if template_part != actual_part:
            return False
    return True


def jsonpath_template_is_ancestor(*, template_path: str, actual_path: str) -> bool:
    """判断模板路径是否是某个实际叶子路径的祖先。"""
    try:
        template_parts = jsonpath_to_path_parts(template_path)
        actual_parts = jsonpath_to_path_parts(actual_path)
    except ValueError:
        return False
    if len(template_parts) >= len(actual_parts):
        return False

    for template_part, actual_part in zip(template_parts, actual_parts, strict=False):
        if template_part == "*":
            if not isinstance(actual_part, int):
                return False
            continue
        if template_part != actual_part:
            return False
    return True


def jsonpath_to_path_parts(path: str) -> list[str | int]:
    """把受限 JSONPath 解析为路径片段列表。"""
    if not JSON_PATH_PATTERN.fullmatch(path):
        raise ValueError(f"JSONPath 超出当前规则范围: {path}")

    parts: list[str | int] = []
    for match in JSON_PATH_SEGMENT_PATTERN.finditer(path):
        key_segment = match.group(1)
        index_segment = match.group(2)
        if key_segment is not None:
            parts.append(unescape_jsonpath_key(key_segment))
            continue
        if index_segment == "*":
            parts.append("*")
            continue
        parts.append(int(index_segment))
    return parts


def jsonpath_to_location_path(*, json_path: str, plugin_index: int) -> str:
    """把插件级 JSONPath 转成回写使用的 `location_path`。"""
    path_parts = jsonpath_to_path_parts(json_path)
    if not path_parts or path_parts[0] != "parameters":
        raise ValueError(f"插件路径必须从 parameters 开始: {json_path}")

    normalized_parts = ["plugins.js", str(plugin_index)]
    normalized_parts.extend(str(part) for part in path_parts[1:])
    return "/".join(normalized_parts)


def jsonpath_to_event_command_location_path(
    *,
    json_path: str,
    command_location_path: str,
) -> str:
    """把事件指令 JSONPath 转成可回写的正文定位路径。"""
    path_parts = jsonpath_to_path_parts(json_path)
    if not path_parts or path_parts[0] != "parameters":
        raise ValueError(f"事件指令路径必须从 parameters 开始: {json_path}")

    normalized_parts = [command_location_path]
    normalized_parts.extend(str(part) for part in path_parts)
    return "/".join(normalized_parts)


def unescape_jsonpath_key(key: str) -> str:
    """反转义 JSONPath 里的对象键。"""
    return key.replace("\\'", "'").replace("\\\\", "\\")


def collect_plugin_json_string_leaf_candidates(
    *,
    plugin_index: int,
    plugin_name: str,
    plugin: dict[str, JsonValue],
) -> list[JsonObject]:
    """列出插件参数 JSON 字符串容器内的字符串叶子候选。"""
    return collect_plugin_json_string_leaf_candidates_from_resolved_leaves(
        plugin_index=plugin_index,
        plugin_name=plugin_name,
        plugin=plugin,
        resolved_leaves=resolve_plugin_leaves(plugin),
    )


def collect_plugin_json_string_leaf_candidates_from_resolved_leaves(
    *,
    plugin_index: int,
    plugin_name: str,
    plugin: dict[str, JsonValue],
    resolved_leaves: Sequence[ResolvedLeaf],
) -> list[JsonObject]:
    """基于已经展开的插件参数叶索引生成 JSON 容器候选。"""
    parameters = plugin.get("parameters")
    if not isinstance(parameters, dict):
        return []

    candidates: list[JsonObject] = []
    for parameter_name, raw_value in parameters.items():
        if not isinstance(raw_value, str):
            continue
        container_path = f"$['parameters'][{quote_jsonpath_key(parameter_name)}]"
        leaves = [
            leaf
            for leaf in resolved_leaves
            if leaf.value_type == "string"
            and leaf.from_json_string
            and jsonpath_template_is_ancestor(
                template_path=container_path,
                actual_path=leaf.path,
            )
        ]
        if not leaves:
            continue
        path_candidates: list[JsonValue] = [candidate for candidate in sorted(build_allowed_templates(leaves))]
        samples: list[JsonValue] = [
            {
                "path": leaf.path,
                "text": leaf.value,
            }
            for leaf in leaves[:10]
            if isinstance(leaf.value, str)
        ]
        candidates.append(
            {
                "plugin_index": plugin_index,
                "plugin_name": plugin_name,
                "container_path": container_path,
                "leaf_count": len(leaves),
                "string_leaf_path_candidates": path_candidates,
                "samples": samples,
            }
        )
    return candidates


__all__: list[str] = [
    "ResolvedLeaf",
    "build_allowed_templates",
    "build_json_string_leaf_path_hint",
    "collect_plugin_json_string_leaf_candidates",
    "collect_plugin_json_string_leaf_candidates_from_resolved_leaves",
    "expand_rule_to_leaf_paths",
    "jsonpath_matches_template",
    "jsonpath_template_is_ancestor",
    "jsonpath_to_location_path",
    "jsonpath_to_event_command_location_path",
    "jsonpath_to_path_parts",
    "resolve_event_command_leaves",
    "resolve_json_leaves",
    "resolve_plugin_leaves",
]
