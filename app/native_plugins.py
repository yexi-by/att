"""RPG Maker `plugins.js` 原生解析适配层。"""

from __future__ import annotations

from app.native_runtime import JsonArray, invoke_native


def parse_native_plugins_array(array_text: str) -> JsonArray:
    """用 Rust/json5 解析已从标准 `plugins.js` 捕获的数组文本。"""
    result = invoke_native("plugins.parse", {"array_text": array_text})
    if not isinstance(result, list):
        raise TypeError("native_plugins_result 必须是 JSON 数组")
    return result


__all__ = ["parse_native_plugins_array"]
