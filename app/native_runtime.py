"""Python/Rust 原生扩展的唯一版本化适配层。"""

from __future__ import annotations

import json
from functools import cache
from importlib import import_module
from typing import Protocol, cast, final
from uuid import uuid4

from app.version import application_version

type JsonValue = str | int | float | bool | None | JsonArray | JsonObject
type JsonArray = list[JsonValue]
type JsonObject = dict[str, JsonValue]

EXPECTED_ABI_VERSION = 1
EXPECTED_ENVELOPE_VERSION = 1
EXPECTED_SCHEMA_VERSION = 1


class NativeModule(Protocol):
    """PyO3 扩展必须提供的最小稳定入口。"""

    def native_contract(self) -> str:
        """返回原生扩展契约。"""
        raise NotImplementedError

    def invoke(self, request_json: str) -> str:
        """执行版本化原生请求。"""
        raise NotImplementedError


@final
class NativeRuntimeError(RuntimeError):
    """原生错误 envelope。"""

    def __init__(
        self,
        *,
        code: str,
        stage: str,
        message: str,
        retryable: bool,
        details: JsonObject,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.details = details


@cache
def load_native_module() -> NativeModule:
    """加载并严格校验原生扩展版本，禁止静默使用旧 pyd。"""
    try:
        module = cast(NativeModule, cast(object, import_module("app._native")))
    except ImportError as error:
        raise RuntimeError("Rust 原生扩展不可用，请执行 uv run --locked maturin develop --release") from error
    for name in ("native_contract", "invoke"):
        if not hasattr(module, name):
            raise RuntimeError("Rust 原生扩展契约过旧，请重新安装当前版本或执行 maturin develop --release")
    contract = native_contract(module=module)
    _require_exact_keys(
        contract,
        {"package_version", "abi_version", "envelope_version", "schemas"},
        "native_contract",
    )
    expected_package = application_version()
    package_version = _read_string(contract, "package_version", "native_contract")
    if package_version != expected_package:
        raise RuntimeError(f"Python 与 Rust 原生扩展版本不一致：Python={expected_package}, Rust={package_version}")
    _require_int(contract, "abi_version", EXPECTED_ABI_VERSION, "native_contract")
    _require_int(contract, "envelope_version", EXPECTED_ENVELOPE_VERSION, "native_contract")
    schemas = _ensure_json_object(contract["schemas"], "native_contract.schemas")
    required_operations = {
        "quality.scan",
        "quality.counts",
        "write_protocol.scan",
        "write_protocol.counts",
        "note_sources.collect",
        "hash.files",
        "placeholder_candidates.scan",
        "javascript.parse",
        "javascript.parse_batch",
        "plugins.parse",
        "runtime.thread_count",
        "write_back.plan",
    }
    missing = sorted(required_operations - set(schemas))
    if missing:
        raise RuntimeError(f"Rust 原生扩展缺少当前操作：{missing}")
    unexpected = sorted(set(schemas) - required_operations)
    if unexpected:
        raise RuntimeError(f"Rust 原生扩展包含未知操作：{unexpected}")
    for operation in required_operations:
        _require_int(schemas, operation, EXPECTED_SCHEMA_VERSION, "native_contract.schemas")
    return module


def native_contract(*, module: NativeModule | None = None) -> JsonObject:
    """读取并解析原生契约。"""
    active_module = module if module is not None else load_native_module()
    raw = cast(object, json.loads(active_module.native_contract()))
    return _ensure_json_object(_coerce_json_value(raw), "native_contract")


def invoke_native(operation: str, payload: JsonValue) -> JsonValue:
    """调用原生操作并验证完整响应 envelope。"""
    module = load_native_module()
    request_id = uuid4().hex
    request = {
        "envelope_version": EXPECTED_ENVELOPE_VERSION,
        "abi_version": EXPECTED_ABI_VERSION,
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "operation": operation,
        "request_id": request_id,
        "payload": payload,
    }
    raw_response = cast(
        object, json.loads(module.invoke(json.dumps(request, ensure_ascii=False, separators=(",", ":"))))
    )
    response = _ensure_json_object(_coerce_json_value(raw_response), "native_response")
    _require_int(response, "abi_version", EXPECTED_ABI_VERSION, "native_response")
    _require_int(response, "envelope_version", EXPECTED_ENVELOPE_VERSION, "native_response")
    _require_int(response, "schema_version", EXPECTED_SCHEMA_VERSION, "native_response")
    if response.get("request_id") != request_id:
        raise RuntimeError("Rust 原生响应 request_id 与请求不一致")
    status = _read_string(response, "status", "native_response")
    if status == "ok":
        _require_exact_keys(
            response,
            {"envelope_version", "abi_version", "schema_version", "request_id", "status", "data"},
            "native_response",
        )
        return response["data"]
    if status != "error":
        raise RuntimeError(f"Rust 原生响应状态无效：{status}")
    error = _ensure_json_object(response["error"], "native_response.error")
    _require_exact_keys(
        response,
        {"envelope_version", "abi_version", "schema_version", "request_id", "status", "error"},
        "native_response",
    )
    _require_exact_keys(
        error,
        {"code", "stage", "message", "retryable", "details"},
        "native_response.error",
    )
    retryable = error.get("retryable")
    if not isinstance(retryable, bool):
        raise TypeError("native_response.error.retryable 必须是布尔值")
    raise NativeRuntimeError(
        code=_read_string(error, "code", "native_response.error"),
        stage=_read_string(error, "stage", "native_response.error"),
        message=_read_string(error, "message", "native_response.error"),
        retryable=retryable,
        details=_ensure_json_object(error["details"], "native_response.error.details"),
    )


def native_thread_count() -> int:
    """通过版本化 native envelope 返回共享线程池的实际线程数。"""
    result = _ensure_json_object(invoke_native("runtime.thread_count", {}), "native_thread_count_result")
    _require_exact_keys(result, {"thread_count"}, "native_thread_count_result")
    count = result.get("thread_count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise RuntimeError("Rust 原生扩展返回了无效线程数")
    return count


def _read_string(payload: JsonObject, key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{context}.{key} 必须是字符串")
    return value


def _require_int(payload: JsonObject, key: str, expected: int, context: str) -> None:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        raise RuntimeError(f"{context}.{key} 必须为 {expected}，实际为 {value!r}")


def _require_exact_keys(payload: JsonObject, expected: set[str], context: str) -> None:
    """校验协议对象没有缺失或未知字段。"""
    actual = set(payload)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise RuntimeError(f"{context} 字段不匹配：缺少={missing}，未知={unexpected}")


def _coerce_json_value(value: object) -> JsonValue:
    """把原生边界解码值递归收窄为 JSON 值。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_coerce_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError("原生 JSON 对象键必须是字符串")
            result[key] = _coerce_json_value(item)
        return result
    raise TypeError(f"原生响应包含非 JSON 值：{type(value).__name__}")


def _ensure_json_object(value: JsonValue, context: str) -> JsonObject:
    """校验 JSON 值是对象。"""
    if not isinstance(value, dict):
        raise TypeError(f"{context} 必须是 JSON 对象")
    return value


__all__ = [
    "NativeRuntimeError",
    "JsonArray",
    "JsonObject",
    "JsonValue",
    "invoke_native",
    "load_native_module",
    "native_contract",
    "native_thread_count",
]
