"""Rust 原生 JavaScript AST 解析适配层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.native_runtime import invoke_native
from app.rmmz.text_rules import JsonValue, ensure_json_array, ensure_json_object


@dataclass(frozen=True, slots=True)
class NativeJavaScriptAstContext:
    """Rust AST 返回的字符串节点事实语境。"""

    node_kind: str
    property_key: str
    property_path: tuple[str, ...]
    call_name: str
    call_argument_index: int | None
    return_function_name: str
    assignment_name: str


@dataclass(frozen=True, slots=True)
class NativeJavaScriptStringSpan:
    """Rust AST 返回的源码字符串节点范围。"""

    kind: str
    quote: str
    start_index: int
    end_index: int
    content_start_index: int
    content_end_index: int
    ast_context: NativeJavaScriptAstContext


@dataclass(frozen=True, slots=True)
class NativeJavaScriptStringScan:
    """Rust AST 字符串节点扫描结果。"""

    source_sha256: str
    has_error: bool
    spans: tuple[NativeJavaScriptStringSpan, ...]


def parse_native_javascript_string_spans(source: str) -> NativeJavaScriptStringScan:
    """调用 Rust AST 解析器收集普通字符串节点范围。"""
    result = ensure_json_object(
        invoke_native("javascript.parse", {"source": source}),
        "native_javascript_ast_result",
    )
    spans = tuple(
        _parse_native_span(span, index)
        for index, span in enumerate(ensure_json_array(result["spans"], "native_javascript_ast_result.spans"))
    )
    has_error = result["has_error"]
    if not isinstance(has_error, bool):
        raise TypeError("native_javascript_ast_result.has_error 必须是布尔值")
    return NativeJavaScriptStringScan(
        source_sha256=_ensure_sha256(
            result.get("source_sha256"),
            "native_javascript_ast_result.source_sha256",
        ),
        has_error=has_error,
        spans=spans,
    )


def parse_native_javascript_string_spans_batch(
    files: Mapping[str, str],
) -> dict[str, NativeJavaScriptStringScan]:
    """批量调用 Rust AST 解析器收集多个源码文件的字符串节点范围。"""
    result = ensure_json_object(
        invoke_native(
            "javascript.parse_batch",
            {"files": [{"file_name": file_name, "source": source} for file_name, source in sorted(files.items())]},
        ),
        "native_javascript_ast_batch_result",
    )
    scans: dict[str, NativeJavaScriptStringScan] = {}
    for index, raw_file in enumerate(ensure_json_array(result["files"], "native_javascript_ast_batch_result.files")):
        file_result = ensure_json_object(raw_file, f"native_javascript_ast_batch_result.files[{index}]")
        file_name = _ensure_string(
            file_result["file_name"],
            f"native_javascript_ast_batch_result.files[{index}].file_name",
        )
        has_error = file_result["has_error"]
        if not isinstance(has_error, bool):
            raise TypeError(f"native_javascript_ast_batch_result.files[{index}].has_error 必须是布尔值")
        spans = tuple(
            _parse_native_span(span, span_index)
            for span_index, span in enumerate(
                ensure_json_array(
                    file_result["spans"],
                    f"native_javascript_ast_batch_result.files[{index}].spans",
                )
            )
        )
        scans[file_name] = NativeJavaScriptStringScan(
            source_sha256=_ensure_sha256(
                file_result.get("source_sha256"),
                f"native_javascript_ast_batch_result.files[{index}].source_sha256",
            ),
            has_error=has_error,
            spans=spans,
        )
    missing_files = set(files) - set(scans)
    if missing_files:
        samples = "、".join(sorted(missing_files)[:5])
        raise RuntimeError(f"批量 JS AST 结果缺少文件: {samples}")
    return scans


def _parse_native_span(value: JsonValue, index: int) -> NativeJavaScriptStringSpan:
    """把单个 AST 范围从 JSON 收窄成 Python 结构。"""
    span = ensure_json_object(value, f"native_javascript_ast_result.spans[{index}]")
    return NativeJavaScriptStringSpan(
        kind=_ensure_string(span["kind"], f"native_javascript_ast_result.spans[{index}].kind"),
        quote=_ensure_string(span["quote"], f"native_javascript_ast_result.spans[{index}].quote"),
        start_index=_ensure_int(span["start_index"], f"native_javascript_ast_result.spans[{index}].start_index"),
        end_index=_ensure_int(span["end_index"], f"native_javascript_ast_result.spans[{index}].end_index"),
        content_start_index=_ensure_int(
            span["content_start_index"],
            f"native_javascript_ast_result.spans[{index}].content_start_index",
        ),
        content_end_index=_ensure_int(
            span["content_end_index"],
            f"native_javascript_ast_result.spans[{index}].content_end_index",
        ),
        ast_context=_parse_native_ast_context(
            span.get("ast_context"),
            f"native_javascript_ast_result.spans[{index}].ast_context",
        ),
    )


def _parse_native_ast_context(value: JsonValue | None, label: str) -> NativeJavaScriptAstContext:
    """把 AST 上下文 JSON 收窄成 Python 结构。"""
    if value is None:
        raise TypeError(f"{label} 必须存在")
    context = ensure_json_object(value, label)
    return NativeJavaScriptAstContext(
        node_kind=_ensure_string(context["node_kind"], f"{label}.node_kind"),
        property_key=_ensure_string(context["property_key"], f"{label}.property_key"),
        property_path=tuple(
            _ensure_string(item, f"{label}.property_path[{index}]")
            for index, item in enumerate(ensure_json_array(context["property_path"], f"{label}.property_path"))
        ),
        call_name=_ensure_string(context["call_name"], f"{label}.call_name"),
        call_argument_index=_ensure_optional_int(context["call_argument_index"], f"{label}.call_argument_index"),
        return_function_name=_ensure_string(
            context["return_function_name"],
            f"{label}.return_function_name",
        ),
        assignment_name=_ensure_string(context["assignment_name"], f"{label}.assignment_name"),
    )


def _ensure_string(value: object, label: str) -> str:
    """校验 JSON 字段是字符串。"""
    if not isinstance(value, str):
        raise TypeError(f"{label} 必须是字符串")
    return value


def _ensure_sha256(value: object, label: str) -> str:
    """校验 JSON 字段是 64 位小写十六进制 SHA-256。"""
    text = _ensure_string(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise TypeError(f"{label} 必须是 64 位小写十六进制 SHA-256")
    return text


def _ensure_int(value: object, label: str) -> int:
    """校验 JSON 字段是整数。"""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{label} 必须是整数")
    return value


def _ensure_optional_int(value: object, label: str) -> int | None:
    """校验 JSON 字段是整数或 null。"""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{label} 必须是整数或 null")
    return value


__all__ = [
    "NativeJavaScriptAstContext",
    "NativeJavaScriptStringScan",
    "NativeJavaScriptStringSpan",
    "parse_native_javascript_string_spans",
    "parse_native_javascript_string_spans_batch",
]
