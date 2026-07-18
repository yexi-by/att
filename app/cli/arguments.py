"""命令行参数读取与日志脱敏工具。

本模块负责从 argparse 命名空间安全读取已解析参数，并统一格式化运行日志里的参数摘要。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import aiofiles

from app.cli.errors import CliBusinessError


def read_str_arg(args: argparse.Namespace, name: str) -> str:
    """从命名空间读取非空字符串参数。"""
    raw_value = read_namespace_value(args, name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise CliBusinessError(f"命令参数缺失或为空：{name}")
    return raw_value.strip()


async def read_optional_text_source_arg(
    args: argparse.Namespace,
    text_arg_name: str,
    input_arg_name: str,
) -> str | None:
    """从命令行 JSON 字符串或 JSON 文件读取可选文本。"""
    text_value = read_optional_str_arg(args, text_arg_name)
    input_path = read_optional_path_arg(args, input_arg_name)
    if text_value is not None:
        return text_value
    if input_path is None:
        return None
    return await read_text_file(input_path)


async def read_required_text_source_arg(
    args: argparse.Namespace,
    text_arg_name: str,
    input_arg_name: str,
) -> str:
    """从命令行 JSON 字符串或 JSON 文件读取必填文本。"""
    text_value = await read_optional_text_source_arg(args, text_arg_name, input_arg_name)
    if text_value is None:
        raise CliBusinessError(f"命令参数必须提供 {text_arg_name} 或 {input_arg_name}")
    if not text_value.strip():
        raise CliBusinessError(f"命令参数 {text_arg_name} 或 {input_arg_name} 内容为空")
    return text_value


async def read_text_file(path: Path) -> str:
    """以 UTF-8 读取命令行输入文件。"""
    try:
        async with aiofiles.open(path, "r", encoding="utf-8-sig") as file:
            return await file.read()
    except OSError as error:
        raise CliBusinessError(f"读取输入文件失败：{path}") from error


def read_optional_str_arg(args: argparse.Namespace, name: str) -> str | None:
    """从命名空间读取可选字符串参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise CliBusinessError(f"命令参数不是有效字符串：{name}")
    return raw_value.strip()


def read_bool_arg(args: argparse.Namespace, name: str) -> bool:
    """从命名空间读取布尔参数。"""
    raw_value = read_namespace_value(args, name)
    if not isinstance(raw_value, bool):
        raise CliBusinessError(f"命令参数不是布尔值：{name}")
    return raw_value


def read_optional_bool_arg(args: argparse.Namespace, name: str) -> bool | None:
    """从命名空间读取可选布尔参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, bool):
        raise CliBusinessError(f"命令参数不是布尔值：{name}")
    return raw_value


def read_optional_int_arg(args: argparse.Namespace, name: str) -> int | None:
    """从命名空间读取可选整数参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise CliBusinessError(f"命令参数不是整数：{name}")
    return raw_value


def read_optional_positive_int_arg(args: argparse.Namespace, name: str) -> int | None:
    """从命名空间读取可选正整数参数。"""
    value = read_optional_int_arg(args, name)
    if value is None:
        return None
    if value <= 0:
        raise CliBusinessError(f"命令参数必须是正整数：{name}")
    return value


def read_optional_float_arg(args: argparse.Namespace, name: str) -> float | None:
    """从命名空间读取可选浮点数参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if isinstance(raw_value, bool) or not isinstance(raw_value, float):
        raise CliBusinessError(f"命令参数不是数字：{name}")
    return raw_value


def read_optional_rate_arg(args: argparse.Namespace, name: str) -> float | None:
    """读取 0 到 1 之间的比例参数。"""
    value = read_optional_float_arg(args, name)
    if value is None:
        return None
    if value <= 0 or value > 1:
        raise CliBusinessError(f"命令参数必须大于 0 且小于等于 1：{name}")
    return value


def read_optional_rpm_arg(args: argparse.Namespace, name: str) -> tuple[int | None, bool]:
    """读取可选 RPM 参数，支持用 `none` 表示不限速。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None, False
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise CliBusinessError(f"命令参数不是有效 RPM：{name}")

    normalized_value = raw_value.strip().lower()
    if normalized_value in {"none", "null", "off", "unlimited", "no", "不限"}:
        return None, True

    try:
        rpm = int(normalized_value)
    except ValueError as error:
        raise CliBusinessError(f"命令参数不是有效 RPM：{name}") from error
    if rpm <= 0:
        raise CliBusinessError(f"命令参数必须是正整数或 none：{name}")
    return rpm, True


def read_optional_int_list_arg(args: argparse.Namespace, name: str) -> list[int] | None:
    """从命名空间读取可选整数数组参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, list):
        raise CliBusinessError(f"命令参数不是整数数组：{name}")

    result: list[int] = []
    raw_items = cast(list[object], raw_value)
    for item in raw_items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise CliBusinessError(f"命令参数不是整数：{name}")
        result.append(item)
    return result


def read_optional_str_list_arg(args: argparse.Namespace, name: str) -> list[str] | None:
    """从命名空间读取可选字符串数组参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, list):
        raise CliBusinessError(f"命令参数不是字符串数组：{name}")

    result: list[str] = []
    raw_items = cast(list[object], raw_value)
    for item in raw_items:
        if not isinstance(item, str) or not item:
            raise CliBusinessError(f"命令参数不是有效字符串：{name}")
        result.append(item)
    return result


def read_optional_pair_list_arg(
    args: argparse.Namespace,
    name: str,
) -> list[tuple[str, str]] | None:
    """从命名空间读取可选字符串二元组数组参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, list):
        raise CliBusinessError(f"命令参数不是字符串二元组数组：{name}")

    pairs: list[tuple[str, str]] = []
    raw_pairs = cast(list[object], raw_value)
    for raw_pair in raw_pairs:
        if not isinstance(raw_pair, list):
            raise CliBusinessError(f"命令参数不是字符串二元组：{name}")
        pair_items = cast(list[object], raw_pair)
        if len(pair_items) != 2:
            raise CliBusinessError(f"命令参数不是字符串二元组：{name}")
        left = pair_items[0]
        right = pair_items[1]
        if not isinstance(left, str) or not isinstance(right, str):
            raise CliBusinessError(f"命令参数不是字符串二元组：{name}")
        pairs.append((left, right))
    return pairs


def read_int_set_arg(args: argparse.Namespace, name: str) -> set[int] | None:
    """从命名空间读取可选整数集合参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, list):
        raise CliBusinessError(f"命令参数不是整数列表：{name}")

    result: set[int] = set()
    raw_items = cast(list[object], raw_value)
    for item in raw_items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise CliBusinessError(f"命令参数不是整数：{name}")
        result.add(item)
    return result


def read_optional_path_arg(args: argparse.Namespace, name: str) -> Path | None:
    """从命名空间读取可选路径参数。"""
    raw_value = read_namespace_value(args, name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise CliBusinessError(f"命令参数不是有效路径：{name}")
    return Path(raw_value).resolve()


def read_required_path_arg(args: argparse.Namespace, name: str) -> Path:
    """从命名空间读取必填路径参数。"""
    path = read_optional_path_arg(args, name)
    if path is None:
        raise CliBusinessError(f"命令参数缺失或为空：{name}")
    return path


SENSITIVE_OR_VERBOSE_ARGUMENTS = {
    "placeholder_rules",
    "rules",
    "system_prompt",
}


def format_namespace(args: argparse.Namespace) -> str:
    """把命令参数格式化为适合日志记录的摘要。"""
    namespace = cast(dict[str, object], vars(args))
    return ", ".join(
        f"{key}={format_log_argument_value(key=key, value=value)}" for key, value in sorted(namespace.items())
    )


def read_namespace_value(args: argparse.Namespace, name: str) -> object:
    """从 argparse 命名空间读取原始对象，并在入口处收窄动态类型。"""
    namespace = cast(dict[str, object], vars(args))
    return namespace.get(name)


def format_argv(argv: Sequence[str]) -> str:
    """格式化原始命令行参数。"""
    if not argv:
        return "<空>"
    redacted_items: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            redacted_items.append("<已省略>")
            skip_next = False
            continue
        if item.startswith("--"):
            option_body = item.removeprefix("--")
            if "=" in option_body:
                option_name, _option_value = option_body.split("=", 1)
                if option_name.replace("-", "_") in SENSITIVE_OR_VERBOSE_ARGUMENTS:
                    redacted_items.append(f"--{option_name}=<已省略>")
                    continue
            option_name = option_body.replace("-", "_")
            if option_name in SENSITIVE_OR_VERBOSE_ARGUMENTS:
                redacted_items.append(item)
                skip_next = True
                continue
        redacted_items.append(item)
    return " ".join(redacted_items)


def format_log_argument_value(*, key: str, value: object) -> object:
    """隐藏不适合写入运行首行日志的大段参数。"""
    if key in SENSITIVE_OR_VERBOSE_ARGUMENTS and value is not None:
        return "<已省略>"
    return value


__all__ = [
    "format_argv",
    "format_log_argument_value",
    "format_namespace",
    "read_bool_arg",
    "read_int_set_arg",
    "read_namespace_value",
    "read_optional_bool_arg",
    "read_optional_float_arg",
    "read_optional_int_arg",
    "read_optional_int_list_arg",
    "read_optional_pair_list_arg",
    "read_optional_path_arg",
    "read_optional_positive_int_arg",
    "read_optional_rate_arg",
    "read_optional_rpm_arg",
    "read_optional_str_arg",
    "read_optional_str_list_arg",
    "read_optional_text_source_arg",
    "read_required_path_arg",
    "read_required_text_source_arg",
    "read_str_arg",
    "read_text_file",
]
