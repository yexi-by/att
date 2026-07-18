"""Rust 批量文件哈希的唯一 Python 适配层。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.native_runtime import JsonObject, invoke_native


@dataclass(frozen=True, slots=True)
class NativeFileHashInput:
    """单个受根目录约束的文件哈希请求。"""

    id: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class NativeFileHashResult:
    """单个文件的原生 SHA-256 结果。"""

    id: str
    relative_path: str
    sha256: str
    byte_size: int


def hash_native_files(
    *,
    root: Path,
    files: Sequence[NativeFileHashInput],
) -> list[NativeFileHashResult]:
    """在一次版本化 native 调用中哈希全部文件。"""
    requested_files = list(files)
    result = _ensure_object(
        invoke_native(
            "hash.files",
            {
                "root": str(root),
                "files": [
                    {
                        "id": item.id,
                        "relative_path": item.relative_path,
                    }
                    for item in requested_files
                ],
            },
        ),
        "native_file_hash_result",
    )
    _require_exact_keys(result, {"files"}, "native_file_hash_result")
    raw_files = result["files"]
    if not isinstance(raw_files, list):
        raise TypeError("native_file_hash_result.files 必须是数组")
    if len(raw_files) != len(requested_files):
        raise RuntimeError(f"Rust 文件哈希结果数量与请求不一致：请求={len(requested_files)}，返回={len(raw_files)}")

    parsed: list[NativeFileHashResult] = []
    for index, (raw_file, requested) in enumerate(zip(raw_files, requested_files, strict=True)):
        context = f"native_file_hash_result.files[{index}]"
        file_result = _ensure_object(raw_file, context)
        _require_exact_keys(
            file_result,
            {"id", "relative_path", "sha256", "byte_size"},
            context,
        )
        result_id = _read_string(file_result, "id", context)
        relative_path = _read_string(file_result, "relative_path", context)
        if result_id != requested.id or relative_path != requested.relative_path:
            raise RuntimeError(f"{context} 未按请求顺序绑定原始 id 和 relative_path")
        sha256 = _read_string(file_result, "sha256", context)
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise TypeError(f"{context}.sha256 必须是 64 位小写十六进制 SHA-256")
        byte_size = file_result["byte_size"]
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
            raise TypeError(f"{context}.byte_size 必须是非负整数")
        parsed.append(
            NativeFileHashResult(
                id=result_id,
                relative_path=relative_path,
                sha256=sha256,
                byte_size=byte_size,
            )
        )
    return parsed


def _ensure_object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict):
        raise TypeError(f"{context} 必须是 JSON 对象")
    return cast(JsonObject, value)


def _require_exact_keys(payload: JsonObject, expected: set[str], context: str) -> None:
    actual = set(payload)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise RuntimeError(f"{context} 字段不匹配：缺少={missing}，未知={unexpected}")


def _read_string(payload: JsonObject, key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{context}.{key} 必须是字符串")
    return value


__all__ = [
    "NativeFileHashInput",
    "NativeFileHashResult",
    "hash_native_files",
]
