"""插件源码文本风险扫描、规则提取和写回测试。"""

import json
from pathlib import Path
from typing import cast

import pytest

import app.plugin_source_text.runtime_audit as runtime_audit_module
from app.agent_toolkit import AgentToolkitService
from app.application.flow_gate import collect_workflow_gate_errors
from app.config.schemas import TextRulesSetting
from app.game_analysis import build_game_analysis_context
from app.native_file_hashing import NativeFileHashInput, NativeFileHashResult, hash_native_files
from app.native_javascript_ast import (
    NativeJavaScriptStringScan,
    parse_native_javascript_string_spans,
    parse_native_javascript_string_spans_batch,
)
from app.native_runtime import NativeRuntimeError
from app.persistence import GameRegistry
from app.plugin_source_text import (
    PluginSourceBatchTextScan,
    PluginSourceTextExtraction,
    RuntimePluginSourceScanError,
    audit_active_runtime_plugin_source,
    build_plugin_source_raw_index,
    build_plugin_source_rule_records_from_import,
    derive_plugin_source_scan,
    iter_plugin_source_string_literals,
    parse_plugin_source_rule_import_text,
    plugin_source_rule_records_to_import_json,
    scan_plugin_source_files_text_strict,
    scan_plugin_source_files_text_strict_with_cache,
)
from app.rmmz import load_active_game_data, load_game_data
from app.rmmz.schema import PluginSourceRuntimeScanCacheRecord, PluginSourceTextRuleRecord, TranslationItem
from app.rmmz.text_rules import JsonValue, TextRules, coerce_json_value, ensure_json_array, ensure_json_object
from app.rule_review import plugin_source_rule_scope_hash, plugin_source_text_rules_hash
from app.text_scope.write_probe import collect_write_back_probe_reasons
from app.utils.config_loader_utils import load_setting
from tests._native_write_plan_helper import (
    reset_writable_copies,
    write_data_text,
    write_game_files,
    write_plugin_source_text,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


def _rewrite_plugins_js(path: Path, plugins: list[JsonValue]) -> None:
    """把插件数组写回测试用 plugins.js。"""
    _ = path.write_text(
        f"var $plugins = {json.dumps(plugins, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )


def test_native_javascript_ast_hashes_exact_unicode_utf8_source_consistently() -> None:
    """单文件和批量 AST 调用必须返回精确 UTF-8 字节的同一 SHA-256。"""
    source = "const 文本 = 'こんにちは😀';"
    expected_sha256 = "960f4efaac990fa56cf2650aa5773734aad3b386309b044cde7b6813a14fd939"

    single = parse_native_javascript_string_spans(source)
    batch = parse_native_javascript_string_spans_batch({"Unicode.js": source})["Unicode.js"]

    assert single.source_sha256 == expected_sha256
    assert batch.source_sha256 == expected_sha256
    assert batch == single


@pytest.mark.parametrize(
    "invalid_sha256",
    ["a" * 63, "A" * 64, "g" * 64],
)
def test_native_javascript_ast_rejects_invalid_single_source_sha256(
    monkeypatch: pytest.MonkeyPatch,
    invalid_sha256: str,
) -> None:
    """Python 适配层必须严格拒绝长度、大小写或字符集错误的哈希。"""

    def invalid_native_result(operation: str, payload: object) -> object:
        _ = (operation, payload)
        return {
            "source_sha256": invalid_sha256,
            "has_error": False,
            "spans": [],
        }

    monkeypatch.setattr("app.native_javascript_ast.invoke_native", invalid_native_result)

    with pytest.raises(TypeError, match="64 位小写十六进制 SHA-256"):
        _ = parse_native_javascript_string_spans("const text = '日文';")


def test_native_javascript_ast_rejects_invalid_batch_source_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批量 AST 返回值也必须执行同一 SHA-256 契约校验。"""

    def invalid_native_result(operation: str, payload: object) -> object:
        _ = (operation, payload)
        return cast(
            object,
            {
                "files": [
                    {
                        "file_name": "A.js",
                        "source_sha256": "A" * 64,
                        "has_error": False,
                        "spans": [],
                    }
                ]
            },
        )

    monkeypatch.setattr("app.native_javascript_ast.invoke_native", invalid_native_result)

    with pytest.raises(TypeError, match="64 位小写十六进制 SHA-256"):
        _ = parse_native_javascript_string_spans_batch({"A.js": "const text = '日文';"})


def test_strict_plugin_source_batch_reuses_native_hash_without_second_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫描器必须直接使用批量 AST 哈希，不在 Python 内重新哈希或二次解析。"""
    native_sha256 = "ab" * 32
    batch_calls = 0

    def native_batch(files: dict[str, str]) -> dict[str, NativeJavaScriptStringScan]:
        nonlocal batch_calls
        batch_calls += 1
        return {
            file_name: NativeJavaScriptStringScan(
                source_sha256=native_sha256,
                has_error=False,
                spans=(),
            )
            for file_name in files
        }

    def forbidden_single_scan(source: str) -> NativeJavaScriptStringScan:
        _ = source
        raise AssertionError("批量扫描后不应二次解析源码")

    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans_batch",
        native_batch,
    )
    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans",
        forbidden_single_scan,
    )
    result = scan_plugin_source_files_text_strict(
        files={"A.js": "const answer = 42;"},
        active_file_names=frozenset({"A.js"}),
    )

    assert batch_calls == 1
    assert result.file_scans["A.js"].file_hash == native_sha256


def test_runtime_scan_cache_full_hit_hashes_disk_once_without_ast_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存全命中时只批量哈希当前运行文件，不进入 AST 批扫。"""
    source = "const 文本 = 'こんにちは😀';"
    expected_sha256 = "960f4efaac990fa56cf2650aa5773734aad3b386309b044cde7b6813a14fd939"
    plugin_dir = tmp_path / "js" / "plugins"
    plugin_dir.mkdir(parents=True)
    _ = (plugin_dir / "Unicode.js").write_text(source, encoding="utf-8")
    hash_calls: list[tuple[Path, tuple[str, ...]]] = []
    real_hash_native_files = hash_native_files

    def counting_hash_batch(
        *,
        root: Path,
        files: list[NativeFileHashInput],
    ) -> list[NativeFileHashResult]:
        hash_calls.append((root, tuple(item.relative_path for item in files)))
        return real_hash_native_files(root=root, files=files)

    def forbidden_ast_batch(
        *,
        files: dict[str, str],
        active_file_names: frozenset[str],
        text_rules: TextRules | None = None,
    ) -> PluginSourceBatchTextScan:
        _ = (files, active_file_names, text_rules)
        raise AssertionError("缓存全命中不应进入 AST 批扫")

    monkeypatch.setattr(runtime_audit_module, "hash_native_files", counting_hash_batch)
    monkeypatch.setattr(runtime_audit_module, "scan_plugin_source_files_text_strict", forbidden_ast_batch)

    batch_scan, refreshed_records, stats = scan_plugin_source_files_text_strict_with_cache(
        content_root=tmp_path,
        files={"Unicode.js": source},
        active_file_names=frozenset({"Unicode.js"}),
        cache_records=[
            PluginSourceRuntimeScanCacheRecord(
                file_name="Unicode.js",
                file_hash=expected_sha256,
                syntax_error="",
                literals=[],
                created_at="2026-07-18T00:00:00",
            )
        ],
        created_at="2026-07-18T00:01:00",
    )

    assert hash_calls == [(tmp_path, ("js/plugins/Unicode.js",))]
    assert stats.hit_file_count == 1
    assert stats.rescan_file_count == 0
    assert batch_scan.file_hashes == {"Unicode.js": expected_sha256}
    assert refreshed_records[0].file_hash == expected_sha256


def test_runtime_scan_cache_miss_uses_one_ast_batch_and_matches_unicode_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存未命中只执行一次 AST 批扫，且解析哈希必须与磁盘预检一致。"""
    source = "const 文本 = 'こんにちは😀';"
    expected_sha256 = "960f4efaac990fa56cf2650aa5773734aad3b386309b044cde7b6813a14fd939"
    plugin_dir = tmp_path / "js" / "plugins"
    plugin_dir.mkdir(parents=True)
    _ = (plugin_dir / "Unicode.js").write_text(source, encoding="utf-8")
    ast_batches: list[tuple[str, ...]] = []
    real_ast_batch = scan_plugin_source_files_text_strict

    def counting_ast_batch(
        *,
        files: dict[str, str],
        active_file_names: frozenset[str],
        text_rules: TextRules | None = None,
    ) -> PluginSourceBatchTextScan:
        ast_batches.append(tuple(sorted(files)))
        return real_ast_batch(
            files=files,
            active_file_names=active_file_names,
            text_rules=text_rules,
        )

    monkeypatch.setattr(runtime_audit_module, "scan_plugin_source_files_text_strict", counting_ast_batch)

    batch_scan, refreshed_records, stats = scan_plugin_source_files_text_strict_with_cache(
        content_root=tmp_path,
        files={"Unicode.js": source},
        active_file_names=frozenset({"Unicode.js"}),
        cache_records=[],
        created_at="2026-07-18T00:00:00",
    )

    assert ast_batches == [("Unicode.js",)]
    assert stats.miss_file_count == 1
    assert stats.rescan_file_count == 1
    assert batch_scan.file_scans["Unicode.js"].file_hash == expected_sha256
    assert batch_scan.file_hashes == {"Unicode.js": expected_sha256}
    assert refreshed_records[0].file_hash == expected_sha256


def test_runtime_scan_cache_rejects_hash_parse_race(tmp_path: Path) -> None:
    """磁盘字节与已加载文本不一致时必须显式失败，不得保存失效 AST 缓存。"""
    disk_source = "const Messages = { title: '磁盘文本' };"
    loaded_source = "const Messages = { title: '已加载旧文本' };"
    plugin_dir = tmp_path / "js" / "plugins"
    plugin_dir.mkdir(parents=True)
    _ = (plugin_dir / "Race.js").write_text(disk_source, encoding="utf-8")

    with pytest.raises(RuntimePluginSourceScanError) as error_info:
        _ = scan_plugin_source_files_text_strict_with_cache(
            content_root=tmp_path,
            files={"Race.js": loaded_source},
            active_file_names=frozenset({"Race.js"}),
            cache_records=[],
            created_at="2026-07-18T00:00:00",
        )

    assert error_info.value.code == "runtime_plugin_source_changed_during_scan"
    assert error_info.value.details["file_name"] == "Race.js"
    assert error_info.value.details["precheck_sha256"] != error_info.value.details["parsed_sha256"]


def test_runtime_scan_cache_preserves_native_missing_file_error(tmp_path: Path) -> None:
    """预检期间文件消失时保留 native 稳定错误码和文件细节。"""
    (tmp_path / "js" / "plugins").mkdir(parents=True)

    with pytest.raises(NativeRuntimeError) as error_info:
        _ = scan_plugin_source_files_text_strict_with_cache(
            content_root=tmp_path,
            files={"Missing.js": "const text = '日文';"},
            active_file_names=frozenset({"Missing.js"}),
            cache_records=[],
            created_at="2026-07-18T00:00:00",
        )

    assert error_info.value.code == "hash_files_file_not_found"
    assert error_info.value.details["id"] == "Missing.js"
    assert error_info.value.details["relative_path"] == "js/plugins/Missing.js"


@pytest.mark.asyncio
async def test_plugin_source_rule_scope_hash_reuses_passed_scan_without_ast(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规则审查范围只能读取命令上下文的扫描事实，不得再次解析或哈希源码。"""
    game_data = await load_game_data(minimal_game_dir)
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=TextRules.from_setting(setting.text_rules),
    )
    expected_scope_hash = plugin_source_rule_scope_hash(scan=scan)

    def forbidden_native_batch(files: dict[str, str]) -> dict[str, NativeJavaScriptStringScan]:
        _ = files
        raise AssertionError("规则审查范围不得重新执行插件源码 AST 批扫")

    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans_batch",
        forbidden_native_batch,
    )
    game_data.plugin_source_files["TestPlugin.js"] += "\nconst changed = '变化后';"

    assert plugin_source_rule_scope_hash(scan=scan) == expected_scope_hash


@pytest.mark.asyncio
async def test_plugin_source_scope_hash_binds_each_enabled_missing_file_and_empty_set(
    minimal_game_dir: Path,
) -> None:
    """空集合、不同文件名和同文件三种读取状态必须得到不同范围哈希。"""
    game_data = await load_game_data(minimal_game_dir)
    text_rules = TextRules.from_setting(TextRulesSetting())

    def enabled_plugin(plugin_name: str) -> dict[str, JsonValue]:
        return {
            "name": plugin_name,
            "status": True,
            "description": "",
            "parameters": {},
        }

    game_data.plugins_js = []
    empty_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    game_data.plugins_js = [enabled_plugin("MissingA")]
    missing_a_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    game_data.plugin_source_read_errors["MissingA.js"] = "测试读取失败"
    read_error_a_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    del game_data.plugin_source_read_errors["MissingA.js"]
    game_data.plugin_source_files["MissingA.js"] = "const title = '当前源码';\n"
    present_a_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    del game_data.plugin_source_files["MissingA.js"]
    game_data.plugins_js = [enabled_plugin("MissingB")]
    missing_b_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )

    assert empty_scan.enabled_file_states == ()
    assert empty_scan.risk.read_error_file_count == 0
    assert [(state.file_name, state.status) for state in missing_a_scan.enabled_file_states] == [
        ("MissingA.js", "missing")
    ]
    assert [(state.file_name, state.status) for state in missing_b_scan.enabled_file_states] == [
        ("MissingB.js", "missing")
    ]
    assert [(state.file_name, state.status) for state in read_error_a_scan.enabled_file_states] == [
        ("MissingA.js", "read_error")
    ]
    assert [(state.file_name, state.status) for state in present_a_scan.enabled_file_states] == [
        ("MissingA.js", "present")
    ]
    assert missing_a_scan.risk.read_error_file_count == 1
    assert read_error_a_scan.risk.read_error_file_count == 1
    assert present_a_scan.risk.read_error_file_count == 0
    assert missing_b_scan.risk.read_error_file_count == 1
    assert (
        len(
            {
                plugin_source_rule_scope_hash(scan=empty_scan),
                plugin_source_rule_scope_hash(scan=missing_a_scan),
                plugin_source_rule_scope_hash(scan=read_error_a_scan),
                plugin_source_rule_scope_hash(scan=present_a_scan),
                plugin_source_rule_scope_hash(scan=missing_b_scan),
            }
        )
        == 5
    )


@pytest.mark.asyncio
async def test_plugin_source_raw_index_derives_multiple_rule_views_with_one_native_batch(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原始索引可以按多套语言规则派生视图，且全程只调用一次原生批量 AST。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "RuleViews", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "RuleViews.js").write_text(
        "const Messages = { title: '販売中', soldOut: 'SOLD OUT' };",
        encoding="utf-8",
    )
    batch_calls = 0

    def counting_native_batch(files: dict[str, str]) -> dict[str, NativeJavaScriptStringScan]:
        nonlocal batch_calls
        batch_calls += 1
        return parse_native_javascript_string_spans_batch(files)

    def forbidden_single_scan(source: str) -> NativeJavaScriptStringScan:
        _ = source
        raise AssertionError("从原始索引派生视图不应再调用单文件 AST")

    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans_batch",
        counting_native_batch,
    )
    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans",
        forbidden_single_scan,
    )
    game_data = await load_game_data(minimal_game_dir)

    raw_index = build_plugin_source_raw_index(game_data=game_data)
    japanese_view = derive_plugin_source_scan(
        index=raw_index,
        text_rules=TextRules.from_setting(TextRulesSetting()),
    )
    japanese_english_view = derive_plugin_source_scan(
        index=raw_index,
        text_rules=TextRules.from_setting(TextRulesSetting(additional_source_languages=["en"])),
    )

    raw_file = next(file for file in raw_index.files if file.file_name == "RuleViews.js")
    japanese_texts = {candidate.text for candidate in japanese_view.candidates if candidate.file_name == "RuleViews.js"}
    multilingual_texts = {
        candidate.text for candidate in japanese_english_view.candidates if candidate.file_name == "RuleViews.js"
    }
    assert batch_calls == 1
    assert raw_file.source_sha256 == raw_file.native_scan.source_sha256
    assert {literal.text for literal in raw_file.literals} == {"販売中", "SOLD OUT"}
    assert japanese_texts == {"販売中"}
    assert multilingual_texts == {"販売中", "SOLD OUT"}
    assert next(file for file in japanese_view.files if file.file_name == "RuleViews.js").file_hash == (
        raw_file.source_sha256
    )


@pytest.mark.asyncio
async def test_plugin_source_scan_only_counts_enabled_direct_plugin_files(minimal_game_dir: Path) -> None:
    """插件源码风险只用启用插件的直接源码文件触发高风险。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.extend(
        [
            {
                "name": "EnabledSource",
                "status": True,
                "description": "",
                "parameters": {},
            },
            {
                "name": "DisabledSource",
                "status": False,
                "description": "",
                "parameters": {},
            },
        ]
    )
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    enabled_lines = [f"Window_Base.prototype.drawText('有効テキスト{i}', 0, 0, 320);" for i in range(301)]
    disabled_lines = [f"Window_Base.prototype.drawText('無効テキスト{i}', 0, 0, 320);" for i in range(301)]
    _ = (plugin_source_dir / "EnabledSource.js").write_text("\n".join(enabled_lines), encoding="utf-8")
    _ = (plugin_source_dir / "DisabledSource.js").write_text("\n".join(disabled_lines), encoding="utf-8")
    nested_dir = plugin_source_dir / "nested"
    nested_dir.mkdir()
    _ = (nested_dir / "EnabledSource.js").write_text(
        "Window_Base.prototype.drawText('入れ子は対象外', 0, 0, 320);",
        encoding="utf-8",
    )

    game_data = await load_game_data(minimal_game_dir)
    text_rules = TextRules.from_setting(TextRulesSetting())
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )

    assert scan.risk.high_risk
    assert scan.risk.strong_context_text_count == 301
    assert scan.risk.ignored_file_count == 1
    assert scan.risk.scanned_file_count == 4
    assert all(candidate.file_name != "nested/EnabledSource.js" for candidate in scan.candidates)


@pytest.mark.asyncio
async def test_plugin_source_scan_batches_native_ast_parse_for_source_files(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件源码总扫描必须批量调用 Rust AST，而不是按文件逐个进入 Python/Rust 边界。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.extend(
        [
            {"name": "BatchA", "status": True, "description": "", "parameters": {}},
            {"name": "BatchB", "status": True, "description": "", "parameters": {}},
        ]
    )
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "BatchA.js").write_text(
        "Window_Base.prototype.drawText('一括テキストA', 0, 0, 320);",
        encoding="utf-8",
    )
    _ = (plugin_source_dir / "BatchB.js").write_text(
        "Window_Base.prototype.drawText('一括テキストB', 0, 0, 320);",
        encoding="utf-8",
    )
    batch_calls: list[tuple[str, ...]] = []

    def counting_batch_scan(files: dict[str, str]) -> object:
        """记录批量入口，并用真实单文件解析器构造等价返回值。"""
        batch_calls.append(tuple(sorted(files)))
        from app.native_javascript_ast import parse_native_javascript_string_spans

        return {file_name: parse_native_javascript_string_spans(source) for file_name, source in files.items()}

    def forbidden_single_scan(source: str) -> object:
        """总扫描不应按文件反复调用单文件适配入口。"""
        _ = source
        raise AssertionError("插件源码总扫描不应调用单文件 AST 入口")

    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans_batch",
        counting_batch_scan,
    )
    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans",
        forbidden_single_scan,
    )
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=TextRules.from_setting(TextRulesSetting()),
    )

    assert len(batch_calls) == 1
    assert {"BatchA.js", "BatchB.js"} <= set(batch_calls[0])
    assert {candidate.file_name for candidate in scan.candidates} >= {"BatchA.js", "BatchB.js"}


@pytest.mark.asyncio
async def test_plugin_source_rule_import_requires_and_reuses_explicit_scan(minimal_game_dir: Path) -> None:
    """规则导入只使用调用者显式提供的 AST 扫描事实。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "ImportHash", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "ImportHash.js").write_text(
        "const Messages = { title: '导入哈希' };",
        encoding="utf-8",
    )
    game_data = await load_game_data(minimal_game_dir)
    text_rules = TextRules.from_setting(TextRulesSetting())
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    file_scan = next(item for item in scan.files if item.file_name == "ImportHash.js")
    candidate = next(item for item in scan.candidates if item.file_name == "ImportHash.js")
    import_file = parse_plugin_source_rule_import_text(
        json.dumps(
            [{"file": "ImportHash.js", "selectors": [candidate.selector]}],
            ensure_ascii=False,
        )
    )
    records = build_plugin_source_rule_records_from_import(
        import_file=import_file,
        scan=scan,
    )

    assert records[0].file_hash == file_scan.file_hash


@pytest.mark.asyncio
async def test_plugin_source_rules_extract_and_write_back_ast_string(minimal_game_dir: Path) -> None:
    """插件源码规则按 AST selector 提取文本，并按同一 selector 写回源码。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append(
        {
            "name": "HardcodedText",
            "status": True,
            "description": "",
            "parameters": {},
        }
    )
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    source_path = plugin_source_dir / "HardcodedText.js"
    _ = source_path.write_text(
        "\n".join(
            [
                "const Messages = {",
                "  title: 'プラグイン直書き',",
                "  helpLines: ['一行目', '二行目'],",
                "};",
                "Window_Base.prototype.drawText(Messages.title, 0, 0, 320);",
            ]
        ),
        encoding="utf-8",
    )
    game_data = await load_game_data(minimal_game_dir)
    text_rules = TextRules.from_setting(TextRulesSetting())
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    title_candidate = next(candidate for candidate in scan.candidates if candidate.text == "プラグイン直書き")
    rule_text = json.dumps(
        [
            {
                "file": "HardcodedText.js",
                "selectors": [title_candidate.selector],
            }
        ],
        ensure_ascii=False,
    )
    records = build_plugin_source_rule_records_from_import(
        import_file=parse_plugin_source_rule_import_text(rule_text),
        scan=scan,
    )

    extracted = PluginSourceTextExtraction(game_data, records, text_rules, scan=scan).extract_all_text()
    items = extracted["js/plugins/HardcodedText.js"].translation_items
    assert [item.original_lines for item in items] == [["プラグイン直書き"]]

    items[0].translation_lines = ["插件直写"]
    reset_writable_copies(game_data)
    _ = write_plugin_source_text(
        game_data,
        items,
        text_rules=text_rules,
    )
    write_game_files(game_data)

    assert "title: '插件直写'" in source_path.read_text(encoding="utf-8")
    backup_path = minimal_game_dir / "js" / "plugins_source_origin" / "HardcodedText.js"
    assert backup_path.exists()
    assert "title: 'プラグイン直書き'" in backup_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_plugin_source_write_back_returns_runtime_write_maps_after_length_changes(
    minimal_game_dir: Path,
) -> None:
    """插件源码写回返回已翻译与已审查排除 selector 的诊断映射。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    source_path = plugin_source_dir / "HardcodedText.js"
    _ = source_path.write_text(
        "\n".join(
            [
                "const Messages = {",
                "  first: '一番目',",
                "  second: '二番目',",
                "  category: 'カテゴリ',",
                "  protocol: '\\\\TRP',",
                "};",
            ]
        ),
        encoding="utf-8",
    )
    game_data = await load_game_data(minimal_game_dir)
    text_rules = TextRules.from_setting(TextRulesSetting())
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    file_scan = next(file_scan for file_scan in scan.files if file_scan.file_name == "HardcodedText.js")
    first_candidate = next(candidate for candidate in scan.candidates if candidate.text == "一番目")
    second_candidate = next(candidate for candidate in scan.candidates if candidate.text == "二番目")
    category_candidate = next(candidate for candidate in scan.candidates if candidate.text == "カテゴリ")
    records = [
        PluginSourceTextRuleRecord(
            file_name="HardcodedText.js",
            file_hash=file_scan.file_hash,
            selectors=[first_candidate.selector, second_candidate.selector],
            excluded_selectors=[category_candidate.selector],
        )
    ]
    items = (
        PluginSourceTextExtraction(game_data, records, text_rules, scan=scan)
        .extract_all_text()["js/plugins/HardcodedText.js"]
        .translation_items
    )
    for item in items:
        if item.original_lines == ["一番目"]:
            item.translation_lines = ["很长的第一个译文"]
        else:
            item.translation_lines = ["短"]

    reset_writable_copies(game_data)
    runtime_write_maps = write_plugin_source_text(
        game_data,
        items,
        text_rules=text_rules,
        plugin_source_rule_records=records,
    )
    final_source = game_data.writable_plugin_source_files["HardcodedText.js"]
    runtime_literals = {
        literal.selector: literal
        for literal in iter_plugin_source_string_literals(
            file_name="HardcodedText.js",
            source=final_source,
            active=True,
        )
    }

    assert len(runtime_write_maps) == 3
    assert all(record.runtime_selector in runtime_literals for record in runtime_write_maps)
    translated_maps = [record for record in runtime_write_maps if record.mapping_kind == "translated"]
    excluded_maps = [record for record in runtime_write_maps if record.mapping_kind == "excluded"]
    assert {record.location_path for record in translated_maps} == {item.location_path for item in items}
    assert {record.source_selector for record in translated_maps} == {
        first_candidate.selector,
        second_candidate.selector,
    }
    assert [record.source_selector for record in excluded_maps] == [category_candidate.selector]
    assert runtime_literals[excluded_maps[0].runtime_selector].text == "カテゴリ"
    assert "\\TRP" not in {runtime_literals[record.runtime_selector].text for record in translated_maps}
    second_record = next(record for record in translated_maps if record.source_selector == second_candidate.selector)
    assert second_record.runtime_selector != second_candidate.selector
    assert runtime_literals[second_record.runtime_selector].text == "短"


@pytest.mark.asyncio
async def test_plugin_source_write_back_treats_runtime_map_as_optional(
    minimal_game_dir: Path,
) -> None:
    """Rust 插件源码写回会生成当前运行映射。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { title: 'プラグイン直書き' };\n",
        encoding="utf-8",
    )
    game_data = await load_game_data(minimal_game_dir)
    text_rules = TextRules.from_setting(TextRulesSetting())
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    file_scan = next(file_scan for file_scan in scan.files if file_scan.file_name == "HardcodedText.js")
    candidate = next(candidate for candidate in scan.candidates if candidate.file_name == "HardcodedText.js")
    records = [
        PluginSourceTextRuleRecord(
            file_name="HardcodedText.js",
            file_hash=file_scan.file_hash,
            selectors=[candidate.selector],
        )
    ]
    item = (
        PluginSourceTextExtraction(game_data, records, text_rules, scan=scan)
        .extract_all_text()["js/plugins/HardcodedText.js"]
        .translation_items[0]
    )
    item.translation_lines = ["插件直写"]

    reset_writable_copies(game_data)
    runtime_write_maps = write_plugin_source_text(
        game_data,
        [item],
        text_rules=text_rules,
    )

    assert len(runtime_write_maps) == 1
    assert runtime_write_maps[0].location_path == item.location_path
    assert "title: '插件直写'" in game_data.writable_plugin_source_files["HardcodedText.js"]


@pytest.mark.asyncio
async def test_plugin_source_ast_map_exports_short_source_text_with_ast_context(
    minimal_game_dir: Path,
) -> None:
    """插件源码 AST 地图必须导出所有源语言短文本，并附加事实 AST 上下文。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "\n".join(
            [
                "const Commands = {",
                "  param2: ['プフクスッ', '終わりね。', '早いですねー。'],",
                "  icon: 'img/日本語.png',",
                "};",
                "function termSecondPerson(targetId) {",
                "  return 'キミ';",
                "}",
                "Window_Base.prototype.drawText('短い', 0, 0, 320);",
            ]
        ),
        encoding="utf-8",
    )
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=TextRules.from_setting(TextRulesSetting()),
    )
    candidates_by_text = {candidate.text: candidate for candidate in scan.candidates}

    for text in ["プフクスッ", "終わりね。", "早いですねー。", "img/日本語.png", "キミ", "短い"]:
        assert text in candidates_by_text

    param_candidate = candidates_by_text["プフクスッ"]
    assert param_candidate.ast_context["property_key"] == "param2"
    assert param_candidate.ast_context["property_path"] == ["param2"]
    return_candidate = candidates_by_text["キミ"]
    assert return_candidate.ast_context["return_function_name"] == "termSecondPerson"
    call_candidate = candidates_by_text["短い"]
    call_name = call_candidate.ast_context["call_name"]
    assert isinstance(call_name, str)
    assert call_name.endswith("drawText")
    assert call_candidate.ast_context["call_argument_index"] == 0
    resource_candidate = candidates_by_text["img/日本語.png"]
    assert "resource_path_like" in resource_candidate.structural_flags


@pytest.mark.asyncio
async def test_plugin_source_extraction_never_rescans_explicit_ast(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一插件源码文件的多条 selector 只消费显式 AST，不在提取阶段重扫。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "\n".join(f"Window_Base.prototype.drawText('抽出テキスト{i}', 0, 0, 320);" for i in range(5)),
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    file_scan = next(file_scan for file_scan in scan.files if file_scan.file_name == "HardcodedText.js")
    selectors = [candidate.selector for candidate in scan.candidates if candidate.file_name == "HardcodedText.js"]
    record = PluginSourceTextRuleRecord(
        file_name="HardcodedText.js",
        file_hash=file_scan.file_hash,
        selectors=selectors,
    )

    def unexpected_native_scan(_source: str) -> object:
        raise AssertionError("插件源码提取不得绕过显式 PluginSourceScan 重扫 AST")

    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans",
        unexpected_native_scan,
    )

    extracted = PluginSourceTextExtraction(game_data, [record], text_rules, scan=scan).extract_all_text()

    assert len(extracted["js/plugins/HardcodedText.js"].translation_items) == len(selectors)


@pytest.mark.asyncio
async def test_plugin_source_write_back_scans_changed_file_for_runtime_write_maps(
    minimal_game_dir: Path,
) -> None:
    """插件源码写回只为实际写入的文件生成当前运行映射。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "\n".join(f"Window_Base.prototype.drawText('回写テキスト{i}', 0, 0, 320);" for i in range(3)),
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    file_scan = next(file_scan for file_scan in scan.files if file_scan.file_name == "HardcodedText.js")
    selectors = [candidate.selector for candidate in scan.candidates if candidate.file_name == "HardcodedText.js"]
    record = PluginSourceTextRuleRecord(
        file_name="HardcodedText.js",
        file_hash=file_scan.file_hash,
        selectors=selectors,
    )
    items = (
        PluginSourceTextExtraction(game_data, [record], text_rules, scan=scan)
        .extract_all_text()["js/plugins/HardcodedText.js"]
        .translation_items
    )
    for index, item in enumerate(items):
        item.translation_lines = [f"写回文本{index}"]
    reset_writable_copies(game_data)
    runtime_write_maps = write_plugin_source_text(
        game_data,
        items,
        text_rules=text_rules,
    )

    assert len(runtime_write_maps) == len(items)
    assert {record.source_file_name for record in runtime_write_maps} == {"HardcodedText.js"}


@pytest.mark.asyncio
async def test_data_write_back_ignores_plugin_source_location_paths(minimal_game_dir: Path) -> None:
    """Rust 写回计划遇到失效插件源码路径必须直接报错。"""
    game_data = await load_game_data(minimal_game_dir)
    item = TranslationItem(
        location_path="js/plugins/HardcodedText.js/ast:string:1:2:abcdef",
        item_type="short_text",
        original_lines=["プラグイン直書き"],
        source_line_paths=["js/plugins/HardcodedText.js/ast:string:1:2:abcdef"],
        translation_lines=["插件直写"],
    )

    with pytest.raises(ValueError, match="插件源码文件不存在"):
        write_data_text(game_data, [item], text_rules=TextRules.from_setting(TextRulesSetting()))


@pytest.mark.asyncio
async def test_non_utf8_plugin_source_does_not_break_default_game_loading(minimal_game_dir: Path) -> None:
    """非 UTF-8 插件源码不能让普通游戏加载和低成本扫描直接失败。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "ShiftJisSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "ShiftJisSource.js").write_bytes(
        "Window_Base.prototype.drawText('シフトJIS本文', 0, 0, 320);".encode("cp932")
    )

    game_data = await load_game_data(minimal_game_dir)
    light_game_data = await load_game_data(minimal_game_dir, include_plugin_source_files=False)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=TextRules.from_setting(TextRulesSetting()),
    )

    assert light_game_data.plugin_source_files == {}
    assert light_game_data.plugin_source_read_errors == {}
    assert "ShiftJisSource.js" not in game_data.plugin_source_files
    assert game_data.plugin_source_read_errors["ShiftJisSource.js"]
    assert scan.risk.read_error_file_count == 1
    assert [(state.file_name, state.status) for state in scan.enabled_file_states if state.status != "present"] == [
        ("ShiftJisSource.js", "read_error")
    ]


def test_plugin_source_rule_import_rejects_extra_fields() -> None:
    """插件源码规则 JSON 只能包含最小 schema 字段。"""
    rule_text = json.dumps(
        [
            {
                "file": "HardcodedText.js",
                "selectors": ["ast:string:1:2:abcdef"],
                "source": "不允许把源码内容写进规则",
            }
        ],
        ensure_ascii=False,
    )

    with pytest.raises(Exception, match="source"):
        _ = parse_plugin_source_rule_import_text(rule_text)


@pytest.mark.asyncio
async def test_plugin_source_rules_support_excluded_selectors(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """插件源码规则可保存已审查但不翻译的 selector，且不进入正文提取。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "\n".join(
            [
                "const Messages = { title: '翻訳する本文', icon: 'img/日本語.png' };",
            ]
        ),
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    title_candidate = next(candidate for candidate in scan.candidates if candidate.text == "翻訳する本文")
    icon_candidate = next(candidate for candidate in scan.candidates if candidate.text == "img/日本語.png")
    rule_text = json.dumps(
        [
            {
                "file": "HardcodedText.js",
                "selectors": [title_candidate.selector],
                "excluded_selectors": [icon_candidate.selector],
            }
        ],
        ensure_ascii=False,
    )

    records = build_plugin_source_rule_records_from_import(
        import_file=parse_plugin_source_rule_import_text(rule_text),
        scan=scan,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_plugin_source_text_rules(records)
        persisted_records = await session.read_plugin_source_text_rules()
    extracted = PluginSourceTextExtraction(game_data, persisted_records, text_rules, scan=scan).extract_all_text()
    exported_rules = plugin_source_rule_records_to_import_json(persisted_records)
    exported_rule = ensure_json_object(ensure_json_array(exported_rules, "rules")[0], "rules[0]")

    assert persisted_records[0].selectors == [title_candidate.selector]
    assert persisted_records[0].excluded_selectors == [icon_candidate.selector]
    assert extracted["js/plugins/HardcodedText.js"].translation_items[0].original_lines == ["翻訳する本文"]
    assert exported_rule["excluded_selectors"] == [icon_candidate.selector]


@pytest.mark.asyncio
async def test_plugin_source_rules_allow_file_with_only_excluded_selectors(
    minimal_game_dir: Path,
) -> None:
    """插件源码文件可以只保存排除 selector，用来表示已审查但不翻译。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { icon: 'img/日本語.png' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    candidate = scan.candidates[0]
    rule_text = json.dumps(
        [
            {
                "file": "HardcodedText.js",
                "selectors": [],
                "excluded_selectors": [candidate.selector],
            }
        ],
        ensure_ascii=False,
    )

    records = build_plugin_source_rule_records_from_import(
        import_file=parse_plugin_source_rule_import_text(rule_text),
        scan=scan,
    )

    assert records[0].selectors == []
    assert records[0].excluded_selectors == [candidate.selector]


@pytest.mark.asyncio
async def test_active_runtime_audit_reports_excluded_residual_and_plugin_controls(
    minimal_game_dir: Path,
) -> None:
    """当前运行验收只看真实运行文件问题，不按规则排除状态降级。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { category: 'カテゴリ', protocol: '\\\\TRP' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)

    reset_writable_copies(game_data)
    _ = write_plugin_source_text(game_data, [], text_rules=text_rules)
    audit = audit_active_runtime_plugin_source(
        game_data=game_data,
        text_rules=text_rules,
        plugin_source_files=game_data.writable_plugin_source_files,
    )

    summary = audit.summary_json()
    source_residual_count = summary["active_runtime_source_residual_count"]
    placeholder_risk_count = summary["active_runtime_placeholder_risk_count"]
    assert isinstance(source_residual_count, int)
    assert isinstance(placeholder_risk_count, int)
    assert source_residual_count >= 1
    assert placeholder_risk_count >= 1
    assert {issue.code for issue in audit.issues} >= {
        "active_runtime_source_residual",
        "active_runtime_placeholder_risk",
    }


@pytest.mark.asyncio
async def test_active_runtime_audit_can_limit_to_read_and_syntax_checks(
    minimal_game_dir: Path,
) -> None:
    """普通写回后验收未开启插件源码支线时，只检查启用源码文件可读和语法。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { category: 'カテゴリ', protocol: '\\\\TRP' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)

    audit = audit_active_runtime_plugin_source(
        game_data=game_data,
        text_rules=text_rules,
        audit_text_issues=False,
    )

    assert audit.summary_json()["active_runtime_text_issue_audit_enabled"] is False
    assert audit.issue_counts["active_runtime_source_residual"] == 0
    assert audit.issue_counts["active_runtime_placeholder_risk"] == 0


@pytest.mark.asyncio
async def test_active_runtime_audit_batches_native_ast_scan(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前运行审计批量扫描插件源码，避免逐文件跨 Python/Rust 边界。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.extend(
        [
            {"name": "AuditBatchA", "status": True, "description": "", "parameters": {}},
            {"name": "AuditBatchB", "status": True, "description": "", "parameters": {}},
        ]
    )
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "AuditBatchA.js").write_text(
        "const Messages = { title: 'カテゴリA' };\n",
        encoding="utf-8",
    )
    _ = (plugin_source_dir / "AuditBatchB.js").write_text(
        "const Messages = { title: 'カテゴリB' };\n",
        encoding="utf-8",
    )
    batch_calls: list[tuple[str, ...]] = []

    def counting_batch_scan(files: dict[str, str]) -> object:
        """记录批量 AST 入口调用，并用真实解析器返回结果。"""
        batch_calls.append(tuple(sorted(files)))
        from app.native_javascript_ast import parse_native_javascript_string_spans

        return {file_name: parse_native_javascript_string_spans(source) for file_name, source in files.items()}

    def forbidden_single_scan(source: str) -> object:
        """当前运行审计不应逐文件调用单文件 AST 入口。"""
        _ = source
        raise AssertionError("当前运行审计不应调用单文件 AST 入口")

    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans_batch",
        counting_batch_scan,
    )
    monkeypatch.setattr(
        "app.plugin_source_text.scanner.parse_native_javascript_string_spans",
        forbidden_single_scan,
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)

    audit = audit_active_runtime_plugin_source(
        game_data=game_data,
        text_rules=text_rules,
    )

    assert len(batch_calls) == 1
    assert {"AuditBatchA.js", "AuditBatchB.js"} <= set(batch_calls[0])
    assert audit.issue_counts["active_runtime_source_residual"] >= 2


@pytest.mark.asyncio
async def test_active_runtime_audit_errors_for_unreviewed_source_candidate(
    minimal_game_dir: Path,
) -> None:
    """未审查的插件源码源语言候选仍然阻塞当前运行验收。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { title: '未審査テキスト' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)

    reset_writable_copies(game_data)
    _ = write_plugin_source_text(game_data, [], text_rules=text_rules)
    audit = audit_active_runtime_plugin_source(
        game_data=game_data,
        text_rules=text_rules,
        plugin_source_files=game_data.writable_plugin_source_files,
    )

    assert audit.issue_counts["active_runtime_source_residual"] == 1
    assert audit.issues[0].code == "active_runtime_source_residual"


@pytest.mark.asyncio
async def test_active_runtime_audit_reports_current_runtime_control_risk(
    minimal_game_dir: Path,
) -> None:
    """当前运行源码变化后，验收直接报告真实控制符风险。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { protocol: '\\\\TRP' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)

    reset_writable_copies(game_data)
    _ = write_plugin_source_text(game_data, [], text_rules=text_rules)
    game_data.writable_plugin_source_files["HardcodedText.js"] = "const Messages = { protocol: '\\\\ii[1]' };\n"
    audit = audit_active_runtime_plugin_source(
        game_data=game_data,
        text_rules=text_rules,
        plugin_source_files=game_data.writable_plugin_source_files,
    )

    assert audit.issue_counts["active_runtime_placeholder_risk"] == 1
    issue = next(issue for issue in audit.issues if issue.code == "active_runtime_placeholder_risk")
    assert issue.fragment == "\\ii[1]"


@pytest.mark.asyncio
async def test_plugin_source_rules_reject_selector_included_and_excluded(
    minimal_game_dir: Path,
) -> None:
    """同一插件源码 selector 不能同时标记为翻译和排除。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { title: '翻訳する本文' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    candidate = scan.candidates[0]
    rule_text = json.dumps(
        [
            {
                "file": "HardcodedText.js",
                "selectors": [candidate.selector],
                "excluded_selectors": [candidate.selector],
            }
        ],
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="不能同时"):
        _ = build_plugin_source_rule_records_from_import(
            import_file=parse_plugin_source_rule_import_text(rule_text),
            scan=scan,
        )


@pytest.mark.asyncio
async def test_plugin_source_write_back_requires_native_ast(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件源码写回阶段没有原生 AST 解析器时必须停止。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HardcodedText", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HardcodedText.js").write_text(
        "const Messages = { title: 'プラグイン直書き' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    candidate = next(candidate for candidate in scan.candidates if candidate.text == "プラグイン直書き")
    records = build_plugin_source_rule_records_from_import(
        import_file=parse_plugin_source_rule_import_text(
            json.dumps([{"file": "HardcodedText.js", "selectors": [candidate.selector]}], ensure_ascii=False)
        ),
        scan=scan,
    )
    item = (
        PluginSourceTextExtraction(game_data, records, text_rules, scan=scan)
        .extract_all_text()["js/plugins/HardcodedText.js"]
        .translation_items[0]
    )
    item.translation_lines = ["插件直写"]

    def missing_native_module(operation: str, payload: object) -> object:
        """模拟发行包或开发环境缺少 Rust 原生扩展。"""
        _ = (operation, payload)
        raise RuntimeError("Rust 原生扩展不可用")

    monkeypatch.setattr("app.native_write_plan.invoke_native", missing_native_module)

    with pytest.raises(ValueError, match="Rust 原生扩展不可用"):
        _ = write_plugin_source_text(
            game_data,
            [item],
            text_rules=text_rules,
        )


@pytest.mark.asyncio
async def test_plugin_source_partial_backup_keeps_unmodified_files_visible(minimal_game_dir: Path) -> None:
    """插件源码只备份改动文件后，再次加载仍能看到未改动直接源码文件。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.extend(
        [
            {"name": "SourceA", "status": True, "description": "", "parameters": {}},
            {"name": "SourceB", "status": True, "description": "", "parameters": {}},
        ]
    )
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "SourceA.js").write_text(
        "const Messages = { title: '一つ目の本文' };\n",
        encoding="utf-8",
    )
    _ = (plugin_source_dir / "SourceB.js").write_text(
        "const Messages = { title: '二つ目の本文' };\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    candidate = next(item for item in scan.candidates if item.file_name == "SourceA.js")
    rule_text = json.dumps([{"file": "SourceA.js", "selectors": [candidate.selector]}], ensure_ascii=False)
    records = build_plugin_source_rule_records_from_import(
        import_file=parse_plugin_source_rule_import_text(rule_text),
        scan=scan,
    )
    item = (
        PluginSourceTextExtraction(game_data, records, text_rules, scan=scan)
        .extract_all_text()["js/plugins/SourceA.js"]
        .translation_items[0]
    )
    item.translation_lines = ["第一个正文"]

    reset_writable_copies(game_data)
    _ = write_plugin_source_text(
        game_data,
        [item],
        text_rules=text_rules,
    )
    write_game_files(game_data)
    reloaded = await load_game_data(minimal_game_dir)

    assert {"SourceA.js", "SourceB.js"}.issubset(reloaded.plugin_source_files)
    assert "一つ目の本文" in reloaded.plugin_source_files["SourceA.js"]
    assert "二つ目の本文" in reloaded.plugin_source_files["SourceB.js"]


@pytest.mark.asyncio
async def test_plugin_source_loader_splits_translation_source_and_active_runtime(
    minimal_game_dir: Path,
) -> None:
    """翻译源视图读取 origin，当前运行视图读取真实激活源码。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "SourceA", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    origin_source_dir = minimal_game_dir / "js" / "plugins_source_origin"
    origin_source_dir.mkdir()
    _ = (plugin_source_dir / "SourceA.js").write_text(
        "const Messages = { title: '当前运行文本' };\n",
        encoding="utf-8",
    )
    _ = (origin_source_dir / "SourceA.js").write_text(
        "const Messages = { title: '原始翻译源' };\n",
        encoding="utf-8",
    )

    translation_source = await load_game_data(minimal_game_dir)
    active_runtime = await load_active_game_data(minimal_game_dir)

    assert "原始翻译源" in translation_source.plugin_source_files["SourceA.js"]
    assert "当前运行文本" in active_runtime.plugin_source_files["SourceA.js"]


@pytest.mark.asyncio
async def test_export_plugin_source_ast_map_view_selects_translation_source_or_active_runtime(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """AST 地图命令必须显式按视图导出 origin 或当前运行源码。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "SourceA", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    source_path = plugin_source_dir / "SourceA.js"
    _ = source_path.write_text(
        "const Messages = { title: '原始ソース' };\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    _ = source_path.write_text(
        "const Messages = { title: '現在実行中' };\n",
        encoding="utf-8",
    )
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    translation_source_path = tmp_path / "translation-source.json"
    active_runtime_path = tmp_path / "active-runtime.json"

    translation_source_report = await service.export_plugin_source_ast_map(
        game_title="テストゲーム",
        output_path=translation_source_path,
        source_view="translation-source",
    )
    active_runtime_report = await service.export_plugin_source_ast_map(
        game_title="テストゲーム",
        output_path=active_runtime_path,
        source_view="active-runtime",
    )
    translation_source_payload = ensure_json_object(
        coerce_json_value(cast(object, json.loads(translation_source_path.read_text(encoding="utf-8")))),
        "translation-source AST",
    )
    active_runtime_payload = ensure_json_object(
        coerce_json_value(cast(object, json.loads(active_runtime_path.read_text(encoding="utf-8")))),
        "active-runtime AST",
    )

    assert translation_source_report.summary["source_view"] == "translation-source"
    assert active_runtime_report.summary["source_view"] == "active-runtime"
    assert translation_source_payload["source_view"] == "translation-source"
    assert active_runtime_payload["source_view"] == "active-runtime"
    assert "原始ソース" in json.dumps(translation_source_payload, ensure_ascii=False)
    assert "現在実行中" in json.dumps(active_runtime_payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_plugin_source_scan_decodes_doubled_control_literal_without_real_line_break(
    minimal_game_dir: Path,
) -> None:
    """源码里的双反斜杠控制符不能被误解成真实换行加裸控制片段。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "ControlSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "ControlSource.js").write_text(
        "const Messages = { title: '頑張って\\\\nn[0]くん' };\n",
        encoding="utf-8",
    )

    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=TextRules.from_setting(TextRulesSetting()),
    )
    candidate = next(item for item in scan.candidates if item.file_name == "ControlSource.js")

    assert "\n" not in candidate.text
    assert r"\nn[0]" in candidate.text


@pytest.mark.asyncio
async def test_plugin_source_high_risk_pauses_workflow_until_rules_are_confirmed(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """显式插件源码风险扫描为高风险且没有规则时，正文流程必须停止。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append(
        {
            "name": "HighRiskSource",
            "status": True,
            "description": "",
            "parameters": {},
        }
    )
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HighRiskSource.js").write_text(
        "\n".join(f"Window_Base.prototype.drawText('高リスク{i}', 0, 0, 320);" for i in range(301)),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        game_data = await load_game_data(session.game_path)
        session.set_game_data(game_data)
        text_rules = TextRules.from_setting(setting.text_rules)
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=game_data,
            text_rules=text_rules,
        )
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=analysis_context.plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=analysis_context.plugin_source_scan.risk.high_risk,
            candidate_count=len(analysis_context.plugin_source_scan.candidates),
            summary=analysis_context.plugin_source_scan.risk_report_json(),
        )

        errors = await collect_workflow_gate_errors(
            session=session,
            context=analysis_context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )

    assert errors
    assert errors[0].code == "plugin_source_text_high_risk"
    assert "插件源码" in errors[0].message


@pytest.mark.asyncio
async def test_plugin_source_stale_rule_hash_blocks_workflow(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """当前运行插件源码变化不能污染翻译源规则身份。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HighRiskSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    source_path = plugin_source_dir / "HighRiskSource.js"
    _ = source_path.write_text(
        "\n".join(f"Window_Base.prototype.drawText('高リスク{i}', 0, 0, 320);" for i in range(301)),
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    initial_game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=initial_game_data),
        text_rules=text_rules,
    )
    candidate = scan.candidates[0]
    records = build_plugin_source_rule_records_from_import(
        import_file=parse_plugin_source_rule_import_text(
            json.dumps([{"file": "HighRiskSource.js", "selectors": [candidate.selector]}], ensure_ascii=False)
        ),
        scan=scan,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_plugin_source_text_rules(records)

    _ = source_path.write_text(
        "\n".join(f"Window_Base.prototype.drawText('変更後高リスク{i}', 0, 0, 320);" for i in range(301)),
        encoding="utf-8",
    )
    async with await registry.open_game("テストゲーム") as session:
        setting = load_setting(EXAMPLE_SETTING_PATH, source_language=session.source_language)
        changed_game_data = await load_game_data(session.game_path)
        session.set_game_data(changed_game_data)
        analysis_context = await build_game_analysis_context(
            session=session,
            game_data=changed_game_data,
            text_rules=text_rules,
        )
        errors = await collect_workflow_gate_errors(
            session=session,
            context=analysis_context,
            setting=setting,
            custom_placeholder_rules_supplied=False,
        )

    assert not any(error.code == "stale_plugin_source_rules" for error in errors)


@pytest.mark.asyncio
async def test_prepare_workspace_writes_plugin_source_risk_summary_without_ast_map(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """默认 Agent 工作区只写插件源码风险摘要，不提前写完整 AST 候选地图。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "RiskSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "RiskSource.js").write_text(
        "Window_Base.prototype.drawText('风险候选', 0, 0, 320);\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    workspace = tmp_path / "workspace"

    service = AgentToolkitService(
        game_registry=registry,
        setting_path=EXAMPLE_SETTING_PATH,
    )
    _ = await service.prepare_agent_workspace(
        game_title="テストゲーム",
        output_dir=workspace,
        command_codes=None,
    )
    validation_report = await service.validate_agent_workspace(
        game_title="テストゲーム",
        workspace=workspace,
    )
    payload = ensure_json_object(
        coerce_json_value(
            cast(
                object,
                json.loads((workspace / "plugin-source-risk-report.json").read_text(encoding="utf-8")),
            )
        ),
        "plugin-source-risk-report.json",
    )

    assert "risk" in payload
    assert "candidate_count" in payload
    assert "files" not in payload
    assert "candidates" not in payload
    assert not (workspace / "plugin-source-rules.json").exists()
    assert "plugin_source_rules_missing" not in {error.code for error in validation_report.errors}


@pytest.mark.asyncio
async def test_quality_report_hides_plugin_source_review_fields_until_branch_started(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """低风险且未启动支线时，质量报告不暴露插件源码审查状态。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "RiskSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "RiskSource.js").write_text(
        "Window_Base.prototype.drawText('风险候选', 0, 0, 320);\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")

    report = await AgentToolkitService(
        game_registry=registry,
        setting_path=EXAMPLE_SETTING_PATH,
    ).quality_report(game_title="テストゲーム")

    assert "plugin_source_unreviewed_count" not in report.summary
    assert "plugin_source_unreviewed_candidates" not in report.details


@pytest.mark.asyncio
async def test_export_plugin_source_ast_map_report_keeps_full_map_only_in_output_file(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """AST 地图导出命令的 JSON 报告不能把完整候选地图再写到 stdout。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "RiskSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "RiskSource.js").write_text(
        "const Messages = { title: '风险候选' };\n",
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    output_path = tmp_path / "plugin-source-ast-map.json"

    report = await AgentToolkitService(
        game_registry=registry,
        setting_path=EXAMPLE_SETTING_PATH,
    ).export_plugin_source_ast_map(
        game_title="テストゲーム",
        output_path=output_path,
    )
    payload = ensure_json_object(
        coerce_json_value(cast(object, json.loads(output_path.read_text(encoding="utf-8")))),
        "plugin-source-ast-map.json",
    )

    assert report.status == "ok"
    assert report.summary["candidate_count"] == 1
    assert "files" in payload
    assert "candidates" not in payload
    files = ensure_json_array(payload["files"], "plugin-source-ast-map.files")
    first_file = ensure_json_object(files[0], "plugin-source-ast-map.files[0]")
    assert "candidates" in first_file
    assert "files" not in report.details
    assert "candidates" not in report.details
    assert report.details["output"] == str(output_path)


@pytest.mark.asyncio
async def test_plugin_source_rule_validation_rejects_invalid_js_directly(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """插件源码无法通过 JS AST 解析时，规则校验直接失败。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "BrokenSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "BrokenSource.js").write_text(
        "const Messages = { title: '壊れた本文',\n",
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    rule_text = json.dumps(
        [{"file": "BrokenSource.js", "selectors": ["ast:string:0:1:invalid"]}],
        ensure_ascii=False,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")

    with pytest.raises(RuntimeError, match="JS 语法错误"):
        raw_index = build_plugin_source_raw_index(game_data=game_data)
        _ = derive_plugin_source_scan(index=raw_index, text_rules=text_rules)

    validation_report = await AgentToolkitService(
        game_registry=registry,
        setting_path=EXAMPLE_SETTING_PATH,
    ).validate_plugin_source_rules(
        game_title="テストゲーム",
        rules_text=rule_text,
    )

    assert validation_report.status == "error"
    assert validation_report.summary["unwritable_count"] == 0
    assert validation_report.errors[0].code == "plugin_source_rules_invalid"
    assert "JS 语法错误" in validation_report.errors[0].message
    assert validation_report.details["rules"] == []


@pytest.mark.asyncio
async def test_quality_report_errors_when_high_risk_plugin_source_review_is_incomplete(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """高风险插件源码只审查部分 selector 时，质量报告必须提示源码审查未完成。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HighRiskSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HighRiskSource.js").write_text(
        "\n".join(f"Window_Base.prototype.drawText('高リスク{i}', 0, 0, 320);" for i in range(301)),
        encoding="utf-8",
    )
    setting = load_setting(EXAMPLE_SETTING_PATH, source_language="ja")
    text_rules = TextRules.from_setting(setting.text_rules)
    game_data = await load_game_data(minimal_game_dir)
    plugin_source_scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    first_candidate = plugin_source_scan.candidates[0]
    records = build_plugin_source_rule_records_from_import(
        import_file=parse_plugin_source_rule_import_text(
            json.dumps(
                [
                    {
                        "file": "HighRiskSource.js",
                        "selectors": [first_candidate.selector],
                        "excluded_selectors": [],
                    }
                ],
                ensure_ascii=False,
            )
        ),
        scan=plugin_source_scan,
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    async with await registry.open_game("テストゲーム") as session:
        await session.replace_plugin_source_text_rules(records)
        await session.replace_plugin_source_assessment(
            source_hash=plugin_source_rule_scope_hash(scan=plugin_source_scan),
            text_rules_hash=plugin_source_text_rules_hash(text_rules),
            high_risk=plugin_source_scan.risk.high_risk,
            candidate_count=len(plugin_source_scan.candidates),
            summary=plugin_source_scan.risk_report_json(),
        )
    report = await AgentToolkitService(
        game_registry=registry,
        setting_path=EXAMPLE_SETTING_PATH,
    ).quality_report(game_title="テストゲーム")

    assert "plugin_source_review_incomplete" in {error.code for error in report.errors}
    assert report.summary["plugin_source_unreviewed_count"] == 300


@pytest.mark.asyncio
async def test_validate_plugin_source_rules_errors_when_review_is_incomplete(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """插件源码规则没有覆盖全部候选时，校验命令必须报 error。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "TwoCandidates", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "TwoCandidates.js").write_text(
        "\n".join(
            [
                "Window_Base.prototype.drawText('候補一つ目', 0, 0, 320);",
                "Window_Base.prototype.drawText('候補二つ目', 0, 0, 320);",
            ]
        ),
        encoding="utf-8",
    )
    text_rules = TextRules.from_setting(TextRulesSetting())
    game_data = await load_game_data(minimal_game_dir)
    scan = derive_plugin_source_scan(
        index=build_plugin_source_raw_index(game_data=game_data),
        text_rules=text_rules,
    )
    first_candidate = scan.candidates[0]
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")
    report = await AgentToolkitService(
        game_registry=registry,
        setting_path=EXAMPLE_SETTING_PATH,
    ).validate_plugin_source_rules(
        game_title="テストゲーム",
        rules_text=json.dumps(
            [
                {
                    "file": "TwoCandidates.js",
                    "selectors": [first_candidate.selector],
                    "excluded_selectors": [],
                }
            ],
            ensure_ascii=False,
        ),
    )

    assert report.status == "error"
    assert "plugin_source_review_incomplete" in {error.code for error in report.errors}
    assert report.summary["unreviewed_selector_count"] == 1


@pytest.mark.asyncio
async def test_import_plugin_source_rules_rejects_high_risk_empty_review(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """高风险插件源码不能通过空规则确认绕过源码候选审查。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.append({"name": "HighRiskSource", "status": True, "description": "", "parameters": {}})
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    _ = (plugin_source_dir / "HighRiskSource.js").write_text(
        "\n".join(f"Window_Base.prototype.drawText('高リスク{i}', 0, 0, 320);" for i in range(301)),
        encoding="utf-8",
    )
    registry = GameRegistry(tmp_path / "db")
    _ = await registry.register_game(minimal_game_dir, source_language="ja")

    report = await AgentToolkitService(
        game_registry=registry,
        setting_path=EXAMPLE_SETTING_PATH,
    ).import_plugin_source_rules(
        game_title="テストゲーム",
        rules_text="[]",
        confirm_empty=True,
    )

    assert report.status == "error"
    assert "plugin_source_review_incomplete" in {error.code for error in report.errors}


@pytest.mark.asyncio
async def test_plugin_source_write_probe_uses_batch_preview(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件源码写回预演成功路径必须按文件批量扫描，避免逐条重复解析 JS。"""
    plugins_path = minimal_game_dir / "js" / "plugins.js"
    plugins = ensure_json_array(_read_test_json_from_plugins_js(plugins_path), "plugins")
    plugins.extend(
        [
            {"name": "HardcodedText", "status": True, "description": "", "parameters": {}},
            {"name": "HardcodedTextExtra", "status": True, "description": "", "parameters": {}},
        ]
    )
    _rewrite_plugins_js(plugins_path, plugins)
    plugin_source_dir = minimal_game_dir / "js" / "plugins"
    plugin_source_dir.mkdir(exist_ok=True)
    sources = {
        "HardcodedText.js": "\n".join(
            f"Window_Base.prototype.drawText('探针原文{index}', 0, 0, 320);" for index in range(2)
        ),
        "HardcodedTextExtra.js": "Window_Base.prototype.drawText('额外探针原文', 0, 0, 320);",
    }
    for file_name, source in sources.items():
        _ = (plugin_source_dir / file_name).write_text(source, encoding="utf-8")
    loaded_sources = {file_name: (plugin_source_dir / file_name).read_bytes().decode("utf-8") for file_name in sources}
    game_data = await load_game_data(minimal_game_dir)
    from app.plugin_source_text.scanner import scan_plugin_source_file_text_strict as real_scan
    from app.plugin_source_text.scanner import scan_plugin_source_files_text_strict as real_batch_scan

    items: list[TranslationItem] = []
    for file_name, loaded_source in loaded_sources.items():
        literals = list(
            real_scan(
                file_name=file_name,
                source=loaded_source,
                active=True,
                text_rules=None,
            ).literals
        )
        items.extend(
            TranslationItem(
                location_path=f"js/plugins/{file_name}/{literal.selector}",
                item_type="short_text",
                original_lines=[literal.text],
                source_line_paths=[f"js/plugins/{file_name}/{literal.selector}"],
                translation_lines=[f"探针译文{index}"],
            )
            for index, literal in enumerate(literals)
        )
    scanned_batches: list[tuple[str, ...]] = []

    def successful_native_probe(
        *,
        game_data: JsonValue,
        plugins_js: list[JsonValue],
        items: list[TranslationItem],
    ) -> list[JsonValue]:
        """模拟普通写入协议探针通过。"""
        _ = (game_data, plugins_js, items)
        return []

    def counting_plugin_source_batch_scan(
        *,
        files: dict[str, str],
        active_file_names: frozenset[str],
        text_rules: TextRules | None,
    ) -> PluginSourceBatchTextScan:
        """记录插件源码写回预演扫描过的文件。"""
        scanned_batches.append(tuple(sorted(files)))
        return real_batch_scan(
            files=files,
            active_file_names=active_file_names,
            text_rules=text_rules,
        )

    monkeypatch.setattr(
        "app.text_scope.write_probe.collect_native_write_protocol_details",
        successful_native_probe,
    )
    monkeypatch.setattr(
        "app.text_scope.write_probe.scan_plugin_source_files_text_strict",
        counting_plugin_source_batch_scan,
    )

    reasons = collect_write_back_probe_reasons(game_data=game_data, active_items=items)

    assert reasons == {}
    assert scanned_batches == [("HardcodedText.js", "HardcodedTextExtra.js")]


def _read_test_json_from_plugins_js(path: Path) -> JsonValue:
    """从测试 `plugins.js` 读取 `$plugins` 数组。"""
    text = path.read_text(encoding="utf-8")
    start = text.index("[")
    end = text.rindex("]") + 1
    return coerce_json_value(cast(object, json.loads(text[start:end])))
