"""插件文本外部规则导入、提取和回写测试。"""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from app.config.schemas import TextRulesSetting
from app.language_profiles import build_text_rules_setting_for_language_profile
from app.plugin_text import (
    PluginTextExtraction,
    build_plugin_rule_records_from_import,
    export_plugins_json_file,
    load_plugin_rule_import_file,
    parse_plugin_rule_import_text,
    resolve_plugin_leaves,
)
from app.rmmz import load_game_data
from app.rmmz.schema import PluginTextRuleRecord
from app.rmmz.text_rules import JsonValue, TextRules, coerce_json_value, ensure_json_array, ensure_json_object
from tests._native_write_plan_helper import reset_writable_copies, write_plugin_text


@pytest.mark.asyncio
async def test_plugin_json_export_writes_raw_plugins_array(minimal_game_dir: Path, tmp_path: Path) -> None:
    """插件配置导出文件的顶层结构是原始 `$plugins` 数组。"""
    game_data = await load_game_data(minimal_game_dir)
    output_path = tmp_path / "plugins.json"

    await export_plugins_json_file(game_data=game_data, output_path=output_path)

    json_value_adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
    exported_value = json_value_adapter.validate_json(output_path.read_text(encoding="utf-8"))
    exported_plugins = ensure_json_array(exported_value, "plugins.json")
    first_plugin = ensure_json_object(exported_plugins[0], "plugins.json[0]")
    assert first_plugin["name"] == "TestPlugin"
    assert "parameters" in first_plugin


@pytest.mark.asyncio
async def test_plugin_rule_import_validates_external_file(minimal_game_dir: Path, tmp_path: Path) -> None:
    """外部插件规则文件使用插件索引、插件名和路径数组定位插件。"""
    game_data = await load_game_data(minimal_game_dir)
    input_path = tmp_path / "plugin-rules.json"
    _ = input_path.write_text(
        json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "TestPlugin",
                    "paths": ["$['parameters']['Message']"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    import_file = await load_plugin_rule_import_file(input_path)
    records = build_plugin_rule_records_from_import(
        game_data=game_data,
        import_file=import_file,
    )

    assert len(records) == 1
    assert records[0].plugin_index == 0
    assert records[0].plugin_name == "TestPlugin"
    assert records[0].path_templates == ["$['parameters']['Message']"]


@pytest.mark.asyncio
async def test_plugin_rule_import_ignores_duplicate_names_outside_referenced_index(minimal_game_dir: Path) -> None:
    """重复插件名不再阻断校验，规则只按显式 plugin_index 定位。"""
    game_data = await load_game_data(minimal_game_dir)
    duplicate_plugin: dict[str, JsonValue] = {
        "name": "DuplicatePlugin",
        "status": False,
        "description": "关闭的重复插件",
        "parameters": {"Message": "重复插件文本"},
    }
    game_data.plugins_js.extend([dict(duplicate_plugin), dict(duplicate_plugin)])

    empty_records = build_plugin_rule_records_from_import(
        game_data=game_data,
        import_file=parse_plugin_rule_import_text("[]"),
    )
    records = build_plugin_rule_records_from_import(
        game_data=game_data,
        import_file=parse_plugin_rule_import_text(
            json.dumps(
                [
                    {
                        "plugin_index": 0,
                        "plugin_name": "TestPlugin",
                        "paths": ["$['parameters']['Message']"],
                    }
                ],
                ensure_ascii=False,
            )
        ),
    )

    assert empty_records == []
    assert len(records) == 1
    assert records[0].plugin_index == 0


@pytest.mark.asyncio
async def test_plugin_rule_import_rejects_plugin_index_name_mismatch(minimal_game_dir: Path) -> None:
    """插件索引和插件名必须同时匹配当前 plugins.js，避免规则写到错误插件。"""
    game_data = await load_game_data(minimal_game_dir)
    import_file = parse_plugin_rule_import_text(
        json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "WrongPlugin",
                    "paths": ["$['parameters']['Message']"],
                }
            ],
            ensure_ascii=False,
        )
    )

    with pytest.raises(ValueError, match="名称与当前 plugins.js 不匹配"):
        _ = build_plugin_rule_records_from_import(game_data=game_data, import_file=import_file)


@pytest.mark.asyncio
async def test_plugin_rule_import_rejects_paths_without_english_source_text(
    minimal_english_game_dir: Path,
) -> None:
    """英文插件规则不能把协议值当成可导入的可见文本路径。"""
    game_data = await load_game_data(minimal_english_game_dir)
    import_file = parse_plugin_rule_import_text(
        json.dumps(
            [
                {
                    "plugin_index": 0,
                    "plugin_name": "VisiblePlugin",
                    "paths": ["$['parameters']['Enabled']"],
                }
            ],
            ensure_ascii=False,
        )
    )
    text_rules = TextRules.from_setting(build_text_rules_setting_for_language_profile("en"))

    with pytest.raises(ValueError, match="没有命中玩家可见可翻译文本"):
        _ = build_plugin_rule_records_from_import(
            game_data=game_data,
            import_file=import_file,
            text_rules=text_rules,
        )


def test_plugin_rule_import_rejects_non_integer_plugin_index() -> None:
    """插件索引只接受 JSON 整数，避免字符串或布尔值被隐式转换成下标。"""
    invalid_payloads = [
        [
            {
                "plugin_index": "0",
                "plugin_name": "TestPlugin",
                "paths": ["$['parameters']['Message']"],
            }
        ],
        [
            {
                "plugin_index": True,
                "plugin_name": "TestPlugin",
                "paths": ["$['parameters']['Message']"],
            }
        ],
    ]

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            _ = parse_plugin_rule_import_text(json.dumps(payload, ensure_ascii=False))


def test_plugin_rule_import_rejects_name_mapping_schema() -> None:
    """插件名映射对象不是插件规则外部协议。"""
    payload = {
        "TestPlugin": ["$['parameters']['Message']"],
    }

    with pytest.raises(TypeError, match="顶层必须是数组"):
        _ = parse_plugin_rule_import_text(json.dumps(payload, ensure_ascii=False))


@pytest.mark.asyncio
async def test_plugin_text_extracts_rule_matched_leaves(minimal_game_dir: Path) -> None:
    """插件文本提取只使用已确认的规则路径，不扫描无关资源路径。"""
    game_data = await load_game_data(minimal_game_dir)
    leaves = resolve_plugin_leaves(game_data.plugins_js[0])
    leaf_paths = {leaf.path for leaf in leaves}
    assert "$['parameters']['Message']" in leaf_paths
    assert "$['parameters']['Nested']['text']" in leaf_paths
    assert "$['parameters']['List'][0]['text']" in leaf_paths
    assert "$['parameters']['List'][1]['text']" in leaf_paths

    rule_record = PluginTextRuleRecord(
        plugin_index=0,
        plugin_name="TestPlugin",
        plugin_hash="hash",
        path_templates=[
            "$['parameters']['Message']",
            "$['parameters']['Nested']['text']",
            "$['parameters']['List'][0]['text']",
            "$['parameters']['List'][1]['text']",
            "$['parameters']['Count']",
        ],
    )
    extracted = PluginTextExtraction(game_data, [rule_record]).extract_all_text()
    items = extracted["plugins.js"].translation_items

    assert {item.location_path for item in items} == {
        "plugins.js/0/Message",
        "plugins.js/0/Nested/text",
        "plugins.js/0/List/0/text",
        "plugins.js/0/List/1/text",
    }


@pytest.mark.asyncio
async def test_plugin_text_english_ui_tokens_are_not_protocol_noise(
    minimal_english_game_dir: Path,
) -> None:
    """英文插件规则命中的短 UI 文本不能被协议噪音过滤静默跳过。"""
    game_data = await load_game_data(minimal_english_game_dir)
    plugin = ensure_json_object(game_data.plugins_js[0], "plugins[0]")
    parameters = ensure_json_object(plugin["parameters"], "plugins[0].parameters")
    parameters["AutoSaveLabel"] = "AutoSave"
    parameters["GameOverLabel"] = "GameOver"
    parameters["RouteLabel"] = "Route66"
    parameters["SaveFileLabel"] = "Save_File"
    rule_record = PluginTextRuleRecord(
        plugin_index=0,
        plugin_name="VisiblePlugin",
        plugin_hash="hash",
        path_templates=[
            "$['parameters']['AutoSaveLabel']",
            "$['parameters']['GameOverLabel']",
            "$['parameters']['RouteLabel']",
            "$['parameters']['SaveFileLabel']",
            "$['parameters']['Formula']",
            "$['parameters']['Enabled']",
        ],
    )
    text_rules = TextRules.from_setting(
        TextRulesSetting(
            source_language="en",
            source_residual_label="英文",
            source_text_required_pattern=r"[A-Za-z][A-Za-z0-9'’_-]*",
            source_text_exclusion_profile="english_protocol_noise",
            source_residual_segment_pattern=r"[A-Za-z][A-Za-z0-9'’_-]*",
        )
    )

    extracted = PluginTextExtraction(
        game_data,
        plugin_rule_records=[rule_record],
        text_rules=text_rules,
    ).extract_all_text()
    items = extracted["plugins.js"].translation_items

    assert {item.location_path for item in items} == {
        "plugins.js/0/AutoSaveLabel",
        "plugins.js/0/GameOverLabel",
        "plugins.js/0/RouteLabel",
        "plugins.js/0/SaveFileLabel",
    }


@pytest.mark.asyncio
async def test_plugin_text_write_back_updates_nested_json_string(minimal_game_dir: Path) -> None:
    """插件文本回写能更新普通参数，也能更新 JSON 字符串里的嵌套文本。"""
    game_data = await load_game_data(minimal_game_dir)
    rule_record = PluginTextRuleRecord(
        plugin_index=0,
        plugin_name="TestPlugin",
        plugin_hash="hash",
        path_templates=[
            "$['parameters']['Message']",
            "$['parameters']['Nested']['text']",
        ],
    )
    items = PluginTextExtraction(game_data, [rule_record]).extract_all_text()["plugins.js"].translation_items
    for item in items:
        if item.location_path.endswith("/Message"):
            item.translation_lines = ["插件译文"]
        else:
            item.translation_lines = ["嵌套译文"]

    reset_writable_copies(game_data)
    write_plugin_text(game_data, items)

    parameters = game_data.writable_plugins_js[0]["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["Message"] == "插件译文"
    nested_value = parameters["Nested"]
    assert isinstance(nested_value, str)
    assert json.loads(nested_value)["text"] == "嵌套译文"
    assert isinstance(game_data.writable_data["plugins.js"], str)


@pytest.mark.asyncio
async def test_plugin_text_write_back_rejects_internal_placeholder_leak(minimal_game_dir: Path) -> None:
    """插件文本写回前必须拒绝项目内部占位符。"""
    game_data = await load_game_data(minimal_game_dir)
    rule_record = PluginTextRuleRecord(
        plugin_index=0,
        plugin_name="TestPlugin",
        plugin_hash="hash",
        path_templates=["$['parameters']['Message']"],
    )
    item = PluginTextExtraction(game_data, [rule_record]).extract_all_text()["plugins.js"].translation_items[0]
    item.translation_lines = ["插件译文[RMMZ_TEXT_COLOR_0]"]

    reset_writable_copies(game_data)
    with pytest.raises(ValueError, match="译文残留项目内部占位符"):
        write_plugin_text(game_data, [item])


@pytest.mark.asyncio
async def test_plugin_text_json_string_leaf_uses_visible_text_protocol(minimal_game_dir: Path) -> None:
    """插件 JSON 容器里的 JSON 字符串叶子按玩家可见文本提取和写回。"""
    game_data = await load_game_data(minimal_game_dir)
    parameters = game_data.plugins_js[0]["parameters"]
    assert isinstance(parameters, dict)
    source_note = "\n　" + r"\C[2]目標人物の場所\C[0]\n村の中央へ向かう。" + "　\n"
    event_object = {
        "MainEventNote": json.dumps(source_note, ensure_ascii=False),
        "MainEventName": "討伐依頼",
    }
    parameters["MainEvents"] = json.dumps(
        [json.dumps(event_object, ensure_ascii=False)],
        ensure_ascii=False,
    )
    rule_record = PluginTextRuleRecord(
        plugin_index=0,
        plugin_name="TestPlugin",
        plugin_hash="hash",
        path_templates=[
            "$['parameters']['MainEvents'][0]['MainEventNote']",
        ],
    )

    item = PluginTextExtraction(game_data, [rule_record]).extract_all_text()["plugins.js"].translation_items[0]

    assert item.location_path == "plugins.js/0/MainEvents/0/MainEventNote"
    assert item.original_lines == [source_note.strip()]

    translated_note = "\n　" + r"\C[2]目标人物的位置\C[0]\n前往村子中央。" + "　\n"
    item.translation_lines = [translated_note.strip()]
    reset_writable_copies(game_data)
    write_plugin_text(game_data, [item])

    writable_parameters = game_data.writable_plugins_js[0]["parameters"]
    assert isinstance(writable_parameters, dict)
    writable_main_events = writable_parameters["MainEvents"]
    assert isinstance(writable_main_events, str)
    writable_main_events_raw = cast(object, json.loads(writable_main_events))
    writable_events = ensure_json_array(
        coerce_json_value(writable_main_events_raw),
        "MainEvents",
    )
    writable_event_text = writable_events[0]
    assert isinstance(writable_event_text, str)
    writable_event_raw = cast(object, json.loads(writable_event_text))
    writable_event = ensure_json_object(
        coerce_json_value(writable_event_raw),
        "MainEvents[0]",
    )
    writable_note = writable_event["MainEventNote"]
    assert isinstance(writable_note, str)
    writable_note_raw = cast(object, json.loads(writable_note))
    assert coerce_json_value(writable_note_raw) == translated_note.strip()
