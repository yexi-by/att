"""Rust 写回计划适配层。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from app.native_runtime import invoke_native
from app.rmmz.schema import (
    FontReplacementRecord,
    PluginSourceRuntimeMappingKind,
    PluginSourceRuntimeWriteMapRecord,
)
from app.rmmz.text_rules import JsonObject

type RawJsonObject = dict[str, object]


@dataclass(frozen=True, slots=True)
class NativePlannedFile:
    """Rust 生成的单个待替换文本文件。"""

    target_path: Path
    relative_path: str
    content: str | None = None
    content_path: Path | None = None


@dataclass(frozen=True, slots=True)
class NativeWriteBackSummary:
    """Rust 写回计划摘要。"""

    data_item_count: int
    plugin_item_count: int
    terminology_written_count: int
    target_font_name: str | None
    source_font_count: int
    replaced_font_reference_count: int
    font_copied: bool
    planned_file_count: int
    skipped_file_count: int
    plugin_source_ast_source_scan_file_count: int = 0
    plugin_source_ast_runtime_scan_file_count: int = 0
    plugin_source_runtime_map_count: int = 0


@dataclass(frozen=True, slots=True)
class NativeWriteBackPlan:
    """Rust 写回计划结果。"""

    files: list[NativePlannedFile]
    plugin_source_runtime_write_maps: list[PluginSourceRuntimeWriteMapRecord]
    font_replacement_records: list[FontReplacementRecord]
    summary: NativeWriteBackSummary
    timings_ms: dict[str, int]


def build_native_write_back_plan(
    *,
    game_path: Path,
    content_root: Path,
    db_path: Path,
    mode: str,
    confirm_font_overwrite: bool,
    setting_payload: JsonObject | None = None,
    content_output_dir: Path | None = None,
) -> NativeWriteBackPlan:
    """调用 Rust 构建写回计划。"""
    native_payload = dict(setting_payload or {})
    if content_output_dir is not None:
        native_payload["plan_content_output_dir"] = str(content_output_dir)
    result = _ensure_object(
        invoke_native(
            "write_back.plan",
            {
                "game_path": str(game_path),
                "db_path": str(db_path),
                "setting": native_payload,
                "mode": mode,
                "confirm_font_overwrite": confirm_font_overwrite,
            },
        ),
        "native_write_back_plan",
    )
    status = result.get("status")
    if status == "error":
        raise RuntimeError(_format_native_error_result(result))
    if status != "ok":
        raise RuntimeError(f"Rust 写回计划返回未知状态：{status!r}")
    return NativeWriteBackPlan(
        files=_parse_planned_files(
            result,
            content_root=content_root,
            content_output_dir=content_output_dir,
        ),
        plugin_source_runtime_write_maps=_parse_runtime_write_maps(result),
        font_replacement_records=_parse_font_replacement_records(result),
        summary=_parse_summary(result),
        timings_ms=_parse_timings(result),
    )


def _parse_planned_files(
    result: RawJsonObject,
    *,
    content_root: Path,
    content_output_dir: Path | None,
) -> list[NativePlannedFile]:
    """解析待替换文件清单。"""
    files: list[NativePlannedFile] = []
    for index, raw_file in enumerate(_ensure_array(result.get("files"), "native_write_back_plan.files")):
        file_object = _ensure_object(raw_file, f"native_write_back_plan.files[{index}]")
        target_path = _read_str(file_object, "target_path", f"files[{index}]")
        relative_path = _read_str(file_object, "relative_path", f"files[{index}]")
        content = _read_optional_str(file_object, "content", f"files[{index}]")
        content_path_text = _read_optional_str(file_object, "content_path", f"files[{index}]")
        if (content is None) == (content_path_text is None):
            raise RuntimeError(f"Rust 写回计划 files[{index}] 必须且只能包含 content 或 content_path")
        content_path = (
            _validate_content_path(
                content_path=Path(content_path_text),
                content_output_dir=content_output_dir,
                context=f"files[{index}]",
            )
            if content_path_text is not None
            else None
        )
        planned_target_path = _validate_planned_file_path(
            target_path=Path(target_path),
            relative_path=relative_path,
            content_root=content_root,
            context=f"files[{index}]",
        )
        files.append(
            NativePlannedFile(
                target_path=planned_target_path,
                relative_path=relative_path,
                content=content,
                content_path=content_path,
            )
        )
    return files


def _validate_planned_file_path(
    *,
    target_path: Path,
    relative_path: str,
    content_root: Path,
    context: str,
) -> Path:
    """校验 Rust 计划返回的目标路径必须位于游戏内容目录内。"""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Rust 写回计划 {context}.relative_path 不是安全相对路径")
    resolved_content_root = content_root.resolve(strict=False)
    resolved_target_path = target_path.resolve(strict=False)
    expected_target_path = (resolved_content_root / relative).resolve(strict=False)
    if not resolved_target_path.is_relative_to(resolved_content_root):
        raise RuntimeError("Rust 写回计划目标路径不在游戏内容目录内")
    if resolved_target_path != expected_target_path:
        raise RuntimeError("Rust 写回计划目标路径与相对路径不一致")
    return resolved_target_path


def _validate_content_path(
    *,
    content_path: Path,
    content_output_dir: Path | None,
    context: str,
) -> Path:
    """校验 Rust sidecar 内容文件必须位于本次临时输出目录内。"""
    if content_output_dir is None:
        raise RuntimeError(f"Rust 写回计划 {context}.content_path 缺少可信临时输出目录")
    resolved_output_dir = content_output_dir.resolve(strict=False)
    resolved_content_path = content_path.resolve(strict=False)
    if not resolved_content_path.is_relative_to(resolved_output_dir):
        raise RuntimeError("Rust 写回计划 content_path 不在临时输出目录内")
    if not resolved_content_path.is_file():
        raise RuntimeError(f"Rust 写回计划 content_path 不存在: {resolved_content_path}")
    return resolved_content_path


def _format_native_error_result(result: RawJsonObject) -> str:
    """提取 Rust error 状态中的用户可读原因。"""
    raw_errors = result.get("errors")
    if isinstance(raw_errors, list) and raw_errors:
        messages: list[str] = []
        for index, raw_error in enumerate(cast(list[object], raw_errors)):
            error_object = _ensure_object(raw_error, f"errors[{index}]")
            message = error_object.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
        if messages:
            return "；".join(messages)
    message = result.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "Rust 写回计划返回 error 状态但没有提供错误原因"


def _parse_runtime_write_maps(result: RawJsonObject) -> list[PluginSourceRuntimeWriteMapRecord]:
    """解析插件源码当前运行映射。"""
    records: list[PluginSourceRuntimeWriteMapRecord] = []
    raw_records = _ensure_array(
        result.get("plugin_source_runtime_write_maps"),
        "native_write_back_plan.plugin_source_runtime_write_maps",
    )
    for index, raw_record in enumerate(raw_records):
        record = _ensure_object(raw_record, f"plugin_source_runtime_write_maps[{index}]")
        records.append(
            PluginSourceRuntimeWriteMapRecord(
                mapping_kind=_read_mapping_kind(record, f"runtime_map[{index}]"),
                location_path=_read_str(record, "location_path", f"runtime_map[{index}]"),
                source_file_name=_read_str(record, "source_file_name", f"runtime_map[{index}]"),
                source_selector=_read_str(record, "source_selector", f"runtime_map[{index}]"),
                source_file_hash=_read_str(record, "source_file_hash", f"runtime_map[{index}]"),
                source_text_hash=_read_str(record, "source_text_hash", f"runtime_map[{index}]"),
                translation_lines_hash=_read_str(record, "translation_lines_hash", f"runtime_map[{index}]"),
                runtime_file_name=_read_str(record, "runtime_file_name", f"runtime_map[{index}]"),
                runtime_selector=_read_str(record, "runtime_selector", f"runtime_map[{index}]"),
                runtime_file_hash=_read_str(record, "runtime_file_hash", f"runtime_map[{index}]"),
                runtime_text_hash=_read_str(record, "runtime_text_hash", f"runtime_map[{index}]"),
                runtime_line=_read_int(record, "runtime_line", f"runtime_map[{index}]"),
                created_at=_read_str(record, "created_at", f"runtime_map[{index}]"),
            )
        )
    return records


def _read_mapping_kind(record: RawJsonObject, context: str) -> PluginSourceRuntimeMappingKind:
    """读取 Rust 返回的插件源码运行映射类型。"""
    value = _read_str(record, "mapping_kind", context)
    if value not in ("translated", "excluded"):
        raise RuntimeError(f"Rust 写回计划 {context}.mapping_kind 必须是 translated 或 excluded")
    return value


def _parse_font_replacement_records(result: RawJsonObject) -> list[FontReplacementRecord]:
    """解析字体引用替换记录。"""
    records: list[FontReplacementRecord] = []
    raw_records = _ensure_array(
        result.get("font_replacement_records"),
        "native_write_back_plan.font_replacement_records",
    )
    for index, raw_record in enumerate(raw_records):
        record = _ensure_object(raw_record, f"font_replacement_records[{index}]")
        records.append(
            FontReplacementRecord(
                file_name=_read_str(record, "file_name", f"font_record[{index}]"),
                value_path=_read_str(record, "value_path", f"font_record[{index}]"),
                original_text=_read_str(record, "original_text", f"font_record[{index}]"),
                replaced_text=_read_str(record, "replaced_text", f"font_record[{index}]"),
                replacement_font_name=_read_str(record, "replacement_font_name", f"font_record[{index}]"),
            )
        )
    return records


def _parse_summary(result: RawJsonObject) -> NativeWriteBackSummary:
    """解析写回计划摘要。"""
    summary = _ensure_object(result.get("summary"), "native_write_back_plan.summary")
    target_font_value = summary.get("target_font_name")
    if target_font_value is not None and not isinstance(target_font_value, str):
        raise TypeError("summary.target_font_name 必须是字符串或 null")
    target_font_name = target_font_value
    return NativeWriteBackSummary(
        data_item_count=_read_int(summary, "data_item_count", "summary"),
        plugin_item_count=_read_int(summary, "plugin_item_count", "summary"),
        terminology_written_count=_read_int(summary, "terminology_written_count", "summary"),
        target_font_name=target_font_name,
        source_font_count=_read_int(summary, "source_font_count", "summary"),
        replaced_font_reference_count=_read_int(summary, "replaced_font_reference_count", "summary"),
        font_copied=_read_bool(summary, "font_copied", "summary"),
        planned_file_count=_read_int(summary, "planned_file_count", "summary"),
        skipped_file_count=_read_int(summary, "skipped_file_count", "summary"),
        plugin_source_ast_source_scan_file_count=_read_int(
            summary,
            "plugin_source_ast_source_scan_file_count",
            "summary",
        ),
        plugin_source_ast_runtime_scan_file_count=_read_int(
            summary,
            "plugin_source_ast_runtime_scan_file_count",
            "summary",
        ),
        plugin_source_runtime_map_count=_read_int(
            summary,
            "plugin_source_runtime_map_count",
            "summary",
        ),
    )


def _parse_timings(result: RawJsonObject) -> dict[str, int]:
    """解析 Rust 分段耗时。"""
    timings = _ensure_object(result.get("timings_ms"), "native_write_back_plan.timings_ms")
    parsed: dict[str, int] = {}
    for key, value in timings.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"timings_ms.{key} 必须是整数")
        parsed[key] = value
    if "total" not in parsed:
        raise TypeError("timings_ms.total 必须存在")
    return parsed


def _read_str(payload: RawJsonObject, field_name: str, context: str) -> str:
    """读取字符串字段。"""
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise TypeError(f"{context}.{field_name} 必须是字符串")
    return value


def _read_optional_str(payload: RawJsonObject, field_name: str, context: str) -> str | None:
    """读取可选字符串字段。"""
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{context}.{field_name} 必须是字符串")
    return value


def _read_int(payload: RawJsonObject, field_name: str, context: str) -> int:
    """读取整数字段。"""
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{context}.{field_name} 必须是整数")
    return value


def _read_bool(payload: RawJsonObject, field_name: str, context: str) -> bool:
    """读取布尔字段。"""
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise TypeError(f"{context}.{field_name} 必须是布尔值")
    return value


def _ensure_object(value: object, context: str) -> RawJsonObject:
    """确认 JSON 值是对象，不递归复制大型子对象。"""
    if not isinstance(value, dict):
        raise TypeError(f"{context} 必须是 JSON 对象")
    return cast(RawJsonObject, value)


def _ensure_array(value: object, context: str) -> list[object]:
    """确认 JSON 值是数组，不递归复制大型子对象。"""
    if not isinstance(value, list):
        raise TypeError(f"{context} 必须是 JSON 数组")
    return cast(list[object], value)


__all__ = [
    "NativePlannedFile",
    "NativeWriteBackPlan",
    "NativeWriteBackSummary",
    "build_native_write_back_plan",
]
