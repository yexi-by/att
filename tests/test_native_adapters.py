"""Rust 单入口契约与 Python 适配层测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast, final

import pytest

from app import (
    native_file_hashing,
    native_javascript_ast,
    native_placeholder_candidates,
    native_plugins,
    native_quality,
    native_runtime,
    native_write_plan,
)
from app.config.schemas import TextRulesSetting
from app.language_profiles import apply_language_profile_to_raw_config
from app.rmmz.schema import TranslationItem
from app.rmmz.text_rules import JsonArray, JsonObject, JsonValue, TextRules
from app.version import application_version

_REQUIRED_OPERATIONS = {
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


@final
class _EnvelopeModule:
    """按新单入口协议返回版本化响应的测试模块。"""

    def __init__(self, *, data: JsonValue = None, error: JsonObject | None = None) -> None:
        self.data: JsonValue = data
        self.error: JsonObject | None = error
        self.requests: list[JsonObject] = []

    def native_contract(self) -> str:
        """返回完整当前契约。"""
        return json.dumps(
            {
                "package_version": application_version(),
                "abi_version": 1,
                "envelope_version": 1,
                "schemas": {operation: 1 for operation in _REQUIRED_OPERATIONS},
            }
        )

    def invoke(self, request_json: str) -> str:
        """回显 request_id，并返回预置成功或错误响应。"""
        request = cast(JsonObject, cast(object, json.loads(request_json)))
        self.requests.append(request)
        response: JsonObject = {
            "envelope_version": 1,
            "abi_version": 1,
            "schema_version": 1,
            "request_id": request["request_id"],
        }
        if self.error is None:
            response.update({"status": "ok", "data": self.data})
        else:
            response.update({"status": "error", "error": self.error})
        return json.dumps(response, ensure_ascii=False)


@final
class _OperationInvoker:
    """记录上层适配器提交给唯一 native 入口的操作。"""

    def __init__(self, responses: dict[str, JsonValue]) -> None:
        self.responses: dict[str, JsonValue] = responses
        self.calls: list[tuple[str, JsonValue]] = []

    def __call__(self, operation: str, payload: JsonValue) -> JsonValue:
        self.calls.append((operation, payload))
        if operation not in self.responses:
            raise AssertionError(f"不应调用原生操作: {operation}")
        return self.responses[operation]


def test_native_runtime_builds_versioned_request_and_validates_success_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唯一适配层必须生成版本化请求，并只返回成功响应的 data。"""
    module = _EnvelopeModule(data={"count": 3})
    monkeypatch.setattr(native_runtime, "load_native_module", lambda: module)

    result = native_runtime.invoke_native("quality.counts", {"items": []})

    assert result == {"count": 3}
    assert len(module.requests) == 1
    request = module.requests[0]
    assert request["envelope_version"] == 1
    assert request["abi_version"] == 1
    assert request["schema_version"] == 1
    assert request["operation"] == "quality.counts"
    assert request["payload"] == {"items": []}
    assert isinstance(request["request_id"], str) and request["request_id"]


def test_native_runtime_preserves_typed_native_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误 envelope 的稳定错误码、阶段和 retryable 必须完整保留。"""
    module = _EnvelopeModule(
        error={
            "code": "native_operation_failed",
            "stage": "execute",
            "message": "规则损坏",
            "retryable": False,
            "details": {},
        }
    )
    monkeypatch.setattr(native_runtime, "load_native_module", lambda: module)

    with pytest.raises(native_runtime.NativeRuntimeError, match="规则损坏") as error_info:
        _ = native_runtime.invoke_native("quality.scan", {"items": []})

    assert error_info.value.code == "native_operation_failed"
    assert error_info.value.stage == "execute"
    assert error_info.value.retryable is False
    assert error_info.value.details == {}


def test_native_thread_count_uses_versioned_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程数查询也必须经过唯一 envelope 入口。"""
    module = _EnvelopeModule(data={"thread_count": 2})
    monkeypatch.setattr(native_runtime, "load_native_module", lambda: module)

    assert native_runtime.native_thread_count() == 2
    assert len(module.requests) == 1
    request = module.requests[0]
    assert request["operation"] == "runtime.thread_count"
    assert request["payload"] == {}
    assert request["schema_version"] == 1


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"thread_count": 0},
        {"thread_count": True},
        {"thread_count": 2, "legacy_count": 2},
    ],
)
def test_native_thread_count_rejects_malformed_data(
    monkeypatch: pytest.MonkeyPatch,
    data: JsonObject,
) -> None:
    """线程数响应不得缺字段、返回非法值或夹带旧协议字段。"""
    module = _EnvelopeModule(data=data)
    monkeypatch.setattr(native_runtime, "load_native_module", lambda: module)

    with pytest.raises(RuntimeError):
        _ = native_runtime.native_thread_count()


def test_native_file_hash_adapter_submits_one_bound_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """文件哈希适配层必须只调用一次，并严格保留请求顺序与绑定。"""
    invoker = _OperationInvoker(
        {
            "hash.files": {
                "files": [
                    {
                        "id": "second",
                        "relative_path": "data/二.json",
                        "sha256": "2" * 64,
                        "byte_size": 22,
                    },
                    {
                        "id": "first",
                        "relative_path": "data/first.json",
                        "sha256": "1" * 64,
                        "byte_size": 11,
                    },
                ]
            }
        }
    )
    monkeypatch.setattr(native_file_hashing, "invoke_native", invoker)
    inputs = [
        native_file_hashing.NativeFileHashInput(id="second", relative_path="data/二.json"),
        native_file_hashing.NativeFileHashInput(id="first", relative_path="data/first.json"),
    ]

    results = native_file_hashing.hash_native_files(root=Path("C:/game"), files=inputs)

    assert results == [
        native_file_hashing.NativeFileHashResult(
            id="second",
            relative_path="data/二.json",
            sha256="2" * 64,
            byte_size=22,
        ),
        native_file_hashing.NativeFileHashResult(
            id="first",
            relative_path="data/first.json",
            sha256="1" * 64,
            byte_size=11,
        ),
    ]
    assert invoker.calls == [
        (
            "hash.files",
            {
                "root": str(Path("C:/game")),
                "files": [{"id": item.id, "relative_path": item.relative_path} for item in inputs],
            },
        )
    ]


@pytest.mark.parametrize(
    ("response", "error_pattern"),
    [
        ({"files": []}, "结果数量与请求不一致"),
        (
            {
                "files": [
                    {
                        "id": "wrong",
                        "relative_path": "data/a.json",
                        "sha256": "a" * 64,
                        "byte_size": 1,
                    }
                ]
            },
            "未按请求顺序绑定",
        ),
        (
            {
                "files": [
                    {
                        "id": "a",
                        "relative_path": "data/a.json",
                        "sha256": "A" * 64,
                        "byte_size": 1,
                    }
                ]
            },
            "64 位小写十六进制",
        ),
        (
            {
                "files": [
                    {
                        "id": "a",
                        "relative_path": "data/a.json",
                        "sha256": "a" * 64,
                        "byte_size": True,
                    }
                ]
            },
            "byte_size 必须是非负整数",
        ),
    ],
)
def test_native_file_hash_adapter_rejects_malformed_results(
    monkeypatch: pytest.MonkeyPatch,
    response: JsonObject,
    error_pattern: str,
) -> None:
    """缺项、错序、坏摘要和错误字节数都不能越过 Python 边界。"""
    monkeypatch.setattr(
        native_file_hashing,
        "invoke_native",
        _OperationInvoker({"hash.files": response}),
    )

    with pytest.raises((RuntimeError, TypeError), match=error_pattern):
        _ = native_file_hashing.hash_native_files(
            root=Path("C:/game"),
            files=[native_file_hashing.NativeFileHashInput(id="a", relative_path="data/a.json")],
        )


def test_native_file_hash_end_to_end_preserves_typed_duplicate_error(tmp_path: Path) -> None:
    """真实扩展必须返回 `hash.files` 专用错误码和可诊断详情。"""
    target = tmp_path / "a.txt"
    _ = target.write_text("内容", encoding="utf-8")

    with pytest.raises(native_runtime.NativeRuntimeError) as error_info:
        _ = native_file_hashing.hash_native_files(
            root=tmp_path,
            files=[
                native_file_hashing.NativeFileHashInput(id="same", relative_path="a.txt"),
                native_file_hashing.NativeFileHashInput(id="same", relative_path="a.txt"),
            ],
        )

    assert error_info.value.code == "hash_files_duplicate_id"
    assert error_info.value.stage == "validate"
    assert error_info.value.details["id"] == "same"
    assert error_info.value.retryable is False


def test_native_runtime_rejects_mismatched_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """响应不能冒用其他请求的 request_id。"""
    module = _EnvelopeModule(data={})
    original_invoke = module.invoke

    def invoke_with_wrong_request_id(request_json: str) -> str:
        response = cast(JsonObject, cast(object, json.loads(original_invoke(request_json))))
        response["request_id"] = "wrong"
        return json.dumps(response)

    monkeypatch.setattr(module, "invoke", invoke_with_wrong_request_id)
    monkeypatch.setattr(native_runtime, "load_native_module", lambda: module)

    with pytest.raises(RuntimeError, match="request_id 与请求不一致"):
        _ = native_runtime.invoke_native("quality.scan", {})


def test_native_runtime_rejects_operation_schema_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """任一操作 schema 漂移时不得加载原生扩展。"""
    module = _EnvelopeModule()
    original_contract = module.native_contract

    def incompatible_contract() -> str:
        contract = cast(JsonObject, cast(object, json.loads(original_contract())))
        schemas = cast(JsonObject, contract["schemas"])
        schemas["quality.scan"] = 2
        return json.dumps(contract)

    monkeypatch.setattr(module, "native_contract", incompatible_contract)

    def import_test_module(_: str) -> _EnvelopeModule:
        return module

    monkeypatch.setattr(native_runtime, "import_module", import_test_module)
    native_runtime.load_native_module.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=r"quality\.scan.*必须为 1"):
            _ = native_runtime.load_native_module()
    finally:
        native_runtime.load_native_module.cache_clear()


def test_native_thread_pool_configuration_is_frozen_for_one_hundred_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """线程池首次使用后必须复用，后续环境变量变化只能在重启后生效。"""
    native_runtime.load_native_module.cache_clear()
    initial_count = native_runtime.native_thread_count()
    changed_count = "2" if initial_count == 1 else "1"
    monkeypatch.setenv("ATT_MZ_RUST_THREADS", changed_count)

    observed = [native_runtime.native_thread_count() for _ in range(100)]

    assert observed == [initial_count] * 100


def test_native_write_plan_reports_native_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """写回计划业务 error 必须保留具体原因。"""
    invoker = _OperationInvoker(
        {
            "write_back.plan": {
                "status": "error",
                "errors": [{"code": "write_gate", "message": "写进游戏文件前检查没通过"}],
            }
        }
    )
    monkeypatch.setattr(native_write_plan, "invoke_native", invoker)

    with pytest.raises(RuntimeError, match="写进游戏文件前检查没通过"):
        _ = _build_write_plan()


def test_native_write_plan_rejects_target_path_outside_content_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 适配层必须拦截 Rust 返回的越界目标路径。"""
    payload = _minimal_write_plan_payload()
    payload["files"] = [
        {
            "target_path": str(Path("outside") / "System.json"),
            "relative_path": "data/System.json",
            "content": "{}\n",
        }
    ]
    monkeypatch.setattr(native_write_plan, "invoke_native", _OperationInvoker({"write_back.plan": payload}))

    with pytest.raises(RuntimeError, match="目标路径不在游戏内容目录内"):
        _ = _build_write_plan()


def test_native_write_plan_requires_total_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """写回计划缺少 total 耗时时必须直接报错。"""
    payload = _minimal_write_plan_payload()
    payload["timings_ms"] = {}
    monkeypatch.setattr(native_write_plan, "invoke_native", _OperationInvoker({"write_back.plan": payload}))

    with pytest.raises(TypeError, match="timings_ms.total 必须存在"):
        _ = _build_write_plan()


def test_native_write_plan_rejects_bad_target_font_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """写回计划 target_font_name 类型错误时必须直接报错。"""
    payload = _minimal_write_plan_payload()
    cast(JsonObject, payload["summary"])["target_font_name"] = 123
    monkeypatch.setattr(native_write_plan, "invoke_native", _OperationInvoker({"write_back.plan": payload}))

    with pytest.raises(TypeError, match="summary.target_font_name 必须是字符串或 null"):
        _ = _build_write_plan()


def test_native_write_plan_accepts_content_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写回计划可通过受信任 sidecar 文件返回大文本内容。"""
    content_root = tmp_path / "game"
    content_output_dir = tmp_path / "plan-content"
    content_output_dir.mkdir()
    sidecar_path = content_output_dir / "000000.txt"
    _ = sidecar_path.write_text('{"gameTitle":"测试"}\n', encoding="utf-8")
    payload = _minimal_write_plan_payload()
    payload["files"] = [
        {
            "target_path": str(content_root / "data" / "System.json"),
            "relative_path": "data/System.json",
            "content_path": str(sidecar_path),
        }
    ]
    invoker = _OperationInvoker({"write_back.plan": payload})
    monkeypatch.setattr(native_write_plan, "invoke_native", invoker)

    plan = native_write_plan.build_native_write_back_plan(
        game_path=content_root,
        content_root=content_root,
        db_path=tmp_path / "game.db",
        mode="rebuild_active_runtime",
        confirm_font_overwrite=False,
        setting_payload={"text_rules": {}},
        content_output_dir=content_output_dir,
    )

    assert invoker.calls == [
        (
            "write_back.plan",
            {
                "game_path": str(content_root),
                "db_path": str(tmp_path / "game.db"),
                "setting": {
                    "text_rules": {},
                    "plan_content_output_dir": str(content_output_dir),
                },
                "mode": "rebuild_active_runtime",
                "confirm_font_overwrite": False,
            },
        )
    ]
    assert plan.files[0].content is None
    assert plan.files[0].content_path == sidecar_path.resolve(strict=False)


def test_native_write_plan_rejects_content_sidecar_outside_trusted_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rust 返回的 sidecar 文件必须位于本次临时输出目录内。"""
    content_root = tmp_path / "game"
    content_output_dir = tmp_path / "plan-content"
    content_output_dir.mkdir()
    outside_path = tmp_path / "outside.txt"
    _ = outside_path.write_text("{}", encoding="utf-8")
    payload = _minimal_write_plan_payload()
    payload["files"] = [
        {
            "target_path": str(content_root / "data" / "System.json"),
            "relative_path": "data/System.json",
            "content_path": str(outside_path),
        }
    ]
    monkeypatch.setattr(native_write_plan, "invoke_native", _OperationInvoker({"write_back.plan": payload}))

    with pytest.raises(RuntimeError, match="content_path 不在临时输出目录内"):
        _ = native_write_plan.build_native_write_back_plan(
            game_path=content_root,
            content_root=content_root,
            db_path=tmp_path / "game.db",
            mode="rebuild_active_runtime",
            confirm_font_overwrite=False,
            content_output_dir=content_output_dir,
        )


def test_native_javascript_ast_requires_bool_has_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """AST 结果 has_error 类型错误时必须直接报错。"""
    monkeypatch.setattr(
        native_javascript_ast,
        "invoke_native",
        _OperationInvoker({"javascript.parse": {"has_error": "false", "spans": []}}),
    )

    with pytest.raises(TypeError, match="has_error 必须是布尔值"):
        _ = native_javascript_ast.parse_native_javascript_string_spans("'文本'")


def test_native_javascript_ast_requires_ast_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """AST 字符串节点缺少 ast_context 时必须直接报错。"""
    monkeypatch.setattr(
        native_javascript_ast,
        "invoke_native",
        _OperationInvoker(
            {
                "javascript.parse": {
                    "has_error": False,
                    "spans": [
                        {
                            "kind": "string",
                            "quote": "'",
                            "start_index": 0,
                            "end_index": 4,
                            "content_start_index": 1,
                            "content_end_index": 3,
                        }
                    ],
                }
            }
        ),
    )

    with pytest.raises(TypeError, match="ast_context 必须存在"):
        _ = native_javascript_ast.parse_native_javascript_string_spans("'文本'")


def test_native_plugins_uses_versioned_parse_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件数组只能经由 `plugins.parse` 操作解析。"""
    invoker = _OperationInvoker({"plugins.parse": [{"name": "Example", "status": True, "parameters": {}}]})
    monkeypatch.setattr(native_plugins, "invoke_native", invoker)

    parsed = native_plugins.parse_native_plugins_array("[{name: 'Example'}]")

    assert parsed[0] == {"name": "Example", "status": True, "parameters": {}}
    assert invoker.calls == [("plugins.parse", {"array_text": "[{name: 'Example'}]"})]


def test_native_quality_counts_parse_count_only_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """计数路径只消费轻量计数协议。"""
    invoker = _OperationInvoker(
        {
            "quality.counts": {
                "source_residual_count": 1,
                "text_structure_count": 2,
                "placeholder_risk_count": 3,
                "overwide_line_count": 4,
            },
            "write_protocol.counts": {"write_protocol_count": 5},
        }
    )
    monkeypatch.setattr(native_quality, "invoke_native", invoker)

    counts = native_quality.collect_native_quality_counts(
        items=[_sample_translation_item()],
        text_rules=TextRules.from_setting(TextRulesSetting()),
        source_residual_rules=[],
    )
    protocol_count = native_quality.count_native_write_protocol_issues(
        game_data=cast(JsonObject, {}),
        plugins_js=cast(JsonArray, []),
        items=[],
    )

    assert counts == native_quality.NativeQualityCounts(1, 2, 3, 4)
    assert protocol_count == 5
    assert [operation for operation, _ in invoker.calls] == ["quality.counts", "write_protocol.counts"]


def test_native_quality_counts_reject_bad_count_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """计数结果类型错误时必须直接报错。"""
    invoker = _OperationInvoker(
        {
            "quality.counts": {
                "source_residual_count": True,
                "text_structure_count": 0,
                "placeholder_risk_count": 0,
                "overwide_line_count": 0,
            },
            "write_protocol.counts": {"write_protocol_count": -1},
        }
    )
    monkeypatch.setattr(native_quality, "invoke_native", invoker)

    with pytest.raises(TypeError, match="source_residual_count 必须是非负整数"):
        _ = native_quality.collect_native_quality_counts(
            items=[_sample_translation_item()],
            text_rules=TextRules.from_setting(TextRulesSetting()),
            source_residual_rules=[],
        )
    with pytest.raises(TypeError, match="write_protocol_count 必须是非负整数"):
        _ = native_quality.count_native_write_protocol_issues(
            game_data=cast(JsonObject, {}),
            plugins_js=cast(JsonArray, []),
            items=[],
        )


def test_native_quality_detects_explicit_additional_english_source_residual() -> None:
    """日文游戏显式追加英文后，Rust 质检必须把未翻译的英文 UI 判为源文残留。"""
    raw_config: dict[str, object] = {}
    apply_language_profile_to_raw_config(
        raw_config=raw_config,
        source_language="ja",
        additional_source_languages=("en",),
    )
    raw_text_rules = raw_config.get("text_rules")
    assert isinstance(raw_text_rules, dict)
    text_rules = TextRules.from_setting(TextRulesSetting.model_validate(raw_text_rules))
    item = TranslationItem(
        location_path="System.json/currencyUnit",
        item_type="short_text",
        original_lines=["SOLD OUT"],
        translation_lines=["SOLD OUT"],
    )

    details = native_quality.collect_native_quality_details(
        items=[item],
        text_rules=text_rules,
        source_residual_rules=[],
    )

    assert len(details.source_residual_items) == 1
    issue = details.source_residual_items[0]
    assert isinstance(issue, dict)
    assert issue["location_path"] == item.location_path


def test_native_placeholder_adapter_submits_one_batch_and_parses_character_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """占位符 occurrence 适配层只提交一个批次并保留 Unicode 字符索引。"""
    invoker = _OperationInvoker(
        {
            "placeholder_candidates.scan": {
                "occurrences": [
                    {
                        "location_path": "Map001.json/1/0",
                        "line_number": 1,
                        "start_index": 2,
                        "end_index": 7,
                        "raw_marker": r"\X[1]",
                        "marker": r"\X[1]",
                        "coverage_kind": "uncovered",
                        "matched_rule_ids": [],
                    },
                    {
                        "location_path": "Map001.json/1/0",
                        "line_number": 2,
                        "start_index": 0,
                        "end_index": 5,
                        "raw_marker": r"\V[1]",
                        "marker": r"\V[1]",
                        "coverage_kind": "standard",
                        "matched_rule_ids": ["standard"],
                    },
                ]
            }
        }
    )
    monkeypatch.setattr(native_placeholder_candidates, "invoke_native", invoker)
    texts = [
        native_placeholder_candidates.NativePlaceholderScanText(
            location_path="Map001.json/1/0",
            line_number=1,
            text=r"前😀\X[1]后",
        ),
        native_placeholder_candidates.NativePlaceholderScanText(
            location_path="Map001.json/1/0",
            line_number=2,
            text=r"\V[1]本文",
        ),
    ]

    occurrences = native_placeholder_candidates.scan_native_placeholder_occurrences(
        texts=texts,
        text_rules=TextRules.from_setting(TextRulesSetting()),
    )

    assert len(occurrences) == 2
    assert occurrences[0].start_index == 2
    assert occurrences[0].end_index == 7
    assert occurrences[1].coverage_kind == "standard"
    assert len(invoker.calls) == 1
    operation, payload = invoker.calls[0]
    assert operation == "placeholder_candidates.scan"
    payload_object = cast(JsonObject, payload)
    assert payload_object["texts"] == [
        {"location_path": item.location_path, "line_number": item.line_number, "text": item.text} for item in texts
    ]


def test_native_placeholder_adapter_rejects_unbound_or_malformed_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原生结果不得返回未请求位置或与原文不一致的字符范围。"""
    invoker = _OperationInvoker(
        {
            "placeholder_candidates.scan": {
                "occurrences": [
                    {
                        "location_path": "Map999.json/1/0",
                        "line_number": 1,
                        "start_index": 0,
                        "end_index": 5,
                        "raw_marker": r"\X[1]",
                        "marker": r"\X[1]",
                        "coverage_kind": "uncovered",
                        "matched_rule_ids": [],
                    }
                ]
            }
        }
    )
    monkeypatch.setattr(native_placeholder_candidates, "invoke_native", invoker)

    with pytest.raises(RuntimeError, match="返回了未请求的位置"):
        _ = native_placeholder_candidates.scan_native_placeholder_occurrences(
            texts=[
                native_placeholder_candidates.NativePlaceholderScanText(
                    location_path="Map001.json/1/0",
                    line_number=1,
                    text=r"\X[1]",
                )
            ],
            text_rules=TextRules.from_setting(TextRulesSetting()),
        )


def _sample_translation_item() -> TranslationItem:
    """构造原生适配层测试用译文条目。"""
    return TranslationItem(
        location_path="Items.json/1/name",
        item_type="short_text",
        original_lines=["薬草"],
        source_line_paths=["Items.json/1/name"],
        translation_lines=["草药"],
    )


def _build_write_plan() -> native_write_plan.NativeWriteBackPlan:
    """用固定路径调用写回计划适配器。"""
    return native_write_plan.build_native_write_back_plan(
        game_path=Path("game"),
        content_root=Path("game"),
        db_path=Path("game.db"),
        mode="rebuild_active_runtime",
        confirm_font_overwrite=False,
    )


def _minimal_write_plan_payload() -> JsonObject:
    """构造满足适配层解析的最小写回计划。"""
    return {
        "status": "ok",
        "files": [],
        "plugin_source_runtime_write_maps": [],
        "font_replacement_records": [],
        "summary": {
            "data_item_count": 0,
            "plugin_item_count": 0,
            "terminology_written_count": 0,
            "target_font_name": None,
            "source_font_count": 0,
            "replaced_font_reference_count": 0,
            "font_copied": False,
            "planned_file_count": 0,
            "skipped_file_count": 0,
            "plugin_source_ast_source_scan_file_count": 0,
            "plugin_source_ast_runtime_scan_file_count": 0,
            "plugin_source_runtime_map_count": 0,
        },
        "timings_ms": {"total": 1},
    }
