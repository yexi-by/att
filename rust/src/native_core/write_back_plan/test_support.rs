use super::build_write_back_plan_impl;
use super::plugin_source::{
    candidate_selector_for_span, normalize_visible_text_for_extraction, unescape_js_text,
};
use super::utils::sha256_text;
use crate::native_core::javascript_ast::parse_javascript_string_spans;
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn build_plan_generates_changed_data_file_from_sqlite() {
    let fixture = create_fixture_dir("att_mz_write_plan_success");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("Rust 写回计划应能生成");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");

    assert_eq!(value["status"], "ok");
    assert_eq!(value["summary"]["data_item_count"], 1);
    assert_eq!(value["summary"]["plugin_item_count"], 0);
    assert!(
        value["summary"]["skipped_file_count"].as_u64().unwrap_or(0) > 0,
        "未变化文件应在 diff 阶段跳过，不进入 Python 文件替换事务",
    );
    assert_eq!(value["files"].as_array().map(Vec::len), Some(1));
    assert!(
        value["files"][0]["content"]
            .as_str()
            .expect("计划文件内容应是字符串")
            .contains("测试标题"),
        "生成内容应包含写入后的译文",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_can_externalize_changed_file_content() {
    let fixture = create_fixture_dir("att_mz_write_plan_external_content");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    let content_output_dir = fixture.join("plan-content");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    let mut payload = minimal_setting_payload();
    payload["plan_content_output_dir"] = json!(content_output_dir.to_string_lossy());

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("Rust 写回计划应能把文件内容写到 sidecar");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let content_path = value["files"][0]["content_path"]
        .as_str()
        .expect("计划文件应返回 content_path");
    let sidecar_content = fs::read_to_string(content_path).expect("sidecar 文件应可读取");

    assert!(value["files"][0].get("content").is_none());
    assert!(sidecar_content.contains("测试标题"));
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_skips_plugin_source_diff_without_plugin_source_items() {
    let fixture = create_fixture_dir("att_mz_write_plan_skip_plugin_source_diff");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[{"name":"TestPlugin","status":true,"description":"","parameters":{}}]"#,
    );
    fs::write(
        game_dir.join("js").join("plugins").join("TestPlugin.js"),
        "const Messages = { title: '当前运行已改' };\n",
    )
    .expect("当前插件源码应可写入");
    fs::write(
        game_dir
            .join("js")
            .join("plugins_source_origin")
            .join("TestPlugin.js"),
        "const Messages = { title: '可信源' };\n",
    )
    .expect("可信源插件源码应可写入");

    let write_back_output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "write_back",
        false,
    )
    .expect("普通写回没有插件源码译文时不应 diff 全量插件源码");
    let write_back_value: Value =
        serde_json::from_str(&write_back_output).expect("写回计划输出应是 JSON");
    let write_back_paths: Vec<&str> = write_back_value["files"]
        .as_array()
        .expect("计划文件应是数组")
        .iter()
        .filter_map(|file| file["relative_path"].as_str())
        .collect();
    assert!(
        !write_back_paths.contains(&"js/plugins/TestPlugin.js"),
        "普通写回没有插件源码译文时不得把 origin 插件源码加入 diff 输出",
    );

    let rebuild_output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("重建当前运行文件仍应恢复插件源码可信源");
    let rebuild_value: Value =
        serde_json::from_str(&rebuild_output).expect("重建计划输出应是 JSON");
    let rebuild_content = planned_file_content(&rebuild_value, "js/plugins/TestPlugin.js");
    assert!(
        rebuild_content.contains("可信源"),
        "重建模式必须继续从可信源恢复插件源码",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_skips_unrelated_data_diff_in_write_back_mode() {
    let fixture = create_fixture_dir("att_mz_write_plan_skip_unrelated_data_diff");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    fs::write(
        game_dir.join("data_origin").join("Items.json"),
        r#"[null,{"id":1,"name":"可信源道具"}]"#,
    )
    .expect("可信源 Items.json 应可写入");
    fs::write(
        game_dir.join("data").join("Items.json"),
        r#"[null,{"id":1,"name":"当前运行被改动"}]"#,
    )
    .expect("当前 Items.json 应可写入");

    let write_back_output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "write_back",
        false,
    )
    .expect("普通写回不应隐式恢复无关 data 文件");
    let write_back_value: Value =
        serde_json::from_str(&write_back_output).expect("写回计划输出应是 JSON");
    let write_back_paths: Vec<&str> = write_back_value["files"]
        .as_array()
        .expect("计划文件应是数组")
        .iter()
        .filter_map(|file| file["relative_path"].as_str())
        .collect();
    assert!(
        !write_back_paths.contains(&"data/Items.json"),
        "普通写回不得把无关 data 文件差异升级成隐式重建",
    );

    let rebuild_output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("重建当前运行文件仍应恢复全部标准 data 文件");
    let rebuild_value: Value =
        serde_json::from_str(&rebuild_output).expect("重建计划输出应是 JSON");
    let rebuild_content = planned_file_content(&rebuild_value, "data/Items.json");
    assert!(
        rebuild_content.contains("可信源道具"),
        "重建模式必须继续从可信源恢复无关但已污染的 data 文件",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_does_not_parse_unrelated_origin_data_in_write_back_mode() {
    let fixture = create_fixture_dir("att_mz_write_plan_skip_unrelated_data_parse");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    fs::write(game_dir.join("data_origin").join("Items.json"), "{")
        .expect("损坏的可信源 Items.json 应可写入");
    fs::write(
        game_dir.join("data").join("Items.json"),
        r#"[null,{"id":1,"name":"当前运行"}]"#,
    )
    .expect("当前 Items.json 应可写入");

    let write_back_output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "write_back",
        false,
    )
    .expect("普通写回不应解析无关 origin data 文件");
    let write_back_value: Value =
        serde_json::from_str(&write_back_output).expect("写回计划输出应是 JSON");
    let write_back_paths: Vec<&str> = write_back_value["files"]
        .as_array()
        .expect("计划文件应是数组")
        .iter()
        .filter_map(|file| file["relative_path"].as_str())
        .collect();
    assert!(
        !write_back_paths.contains(&"data/Items.json"),
        "普通写回不得读取并计划无关损坏 data 文件",
    );

    let rebuild_error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("重建模式必须解析全部 origin data 并暴露损坏文件");
    assert!(
        rebuild_error.contains("解析 data JSON 失败"),
        "重建模式错误应说明 origin data JSON 损坏",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_supports_mv_virtual_namebox_rules() {
    let fixture = create_fixture_dir("att_mz_write_plan_mv_namebox");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_mv_virtual_namebox_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_mv_virtual_namebox_rules_and_items(&db_path);

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("MV 虚拟名字框应由 Rust 写回计划支持");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let files = value["files"].as_array().expect("计划文件应是数组");
    let common_events_content = files
        .iter()
        .find(|file| file["relative_path"] == "data/CommonEvents.json")
        .and_then(|file| file["content"].as_str())
        .expect("CommonEvents.json 应出现在计划文件中");

    assert!(
        common_events_content.contains("向导："),
        "独立虚拟名字框说话人应被术语表译名重建",
    );
    assert!(
        common_events_content.contains("向导「你好」"),
        "内联虚拟名字框应把说话人和译文正文合并回同一行",
    );
    assert!(
        common_events_content.contains("勇者:勇者正文"),
        "actor_name 虚拟名字框应按角色名术语重建",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_write_back_loads_actor_names_for_mv_actor_name_rule() {
    let fixture = create_fixture_dir("att_mz_write_plan_mv_namebox_write_back");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_mv_virtual_namebox_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_mv_virtual_namebox_rules_and_items(&db_path);

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "write_back",
        false,
    )
    .expect("普通写回应为 MV actor_name 规则加载 Actors.json");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");

    assert!(
        planned_file_content(&value, "data/CommonEvents.json").contains("勇者:勇者正文"),
        "actor_name 虚拟名字框在普通写回模式也应按角色名术语重建",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_writes_mz_terminology_fields() {
    let fixture = create_fixture_dir("att_mz_write_plan_mz_terminology");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "Map001.json",
        r#"{"displayName":"始まりの町","events":[null,{"id":1,"pages":[{"list":[{"code":101,"parameters":[0,0,0,2,"村人"]},{"code":401,"parameters":["こんにちは"]},{"code":0,"parameters":[]}]}]}]}"#,
    );
    write_data_origin_and_active(
        &game_dir,
        "Actors.json",
        r#"[null,{"id":1,"name":"勇者","nickname":"ニック"}]"#,
    );
    write_data_origin_and_active(
        &game_dir,
        "Skills.json",
        r#"[null,{"id":1,"name":"火の術"}]"#,
    );
    write_data_origin_and_active(
        &game_dir,
        "Items.json",
        r#"[null,{"id":1,"name":"回復薬"}]"#,
    );
    write_data_origin_and_active(
        &game_dir,
        "System.json",
        r#"{"gameTitle":"原标题","elements":["","炎"],"skillTypes":[],"weaponTypes":[],"armorTypes":[],"equipTypes":[]}"#,
    );
    for (category, source_text, translated_text) in [
        ("speaker_names", "村人", "村民"),
        ("map_display_names", "始まりの町", "起始之镇"),
        ("actor_names", "勇者", "勇者甲"),
        ("actor_nicknames", "ニック", "绰号"),
        ("skill_names", "火の術", "火术"),
        ("item_names", "回復薬", "回复药"),
        ("system_elements", "炎", "火焰"),
    ] {
        insert_field_term(&db_path, category, source_text, translated_text);
    }

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("MZ 字段术语应由 Rust 写回计划写入");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");

    assert_eq!(value["summary"]["terminology_written_count"], 7);
    assert!(planned_file_content(&value, "data/Map001.json").contains("村民"));
    assert!(planned_file_content(&value, "data/Actors.json").contains("勇者甲"));
    assert!(planned_file_content(&value, "data/Actors.json").contains("绰号"));
    assert!(planned_file_content(&value, "data/Skills.json").contains("火术"));
    assert!(planned_file_content(&value, "data/Items.json").contains("回复药"));
    assert!(planned_file_content(&value, "data/System.json").contains("火焰"));

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_writes_mv_terminology_virtual_namebox() {
    let fixture = create_fixture_dir("att_mz_write_plan_mv_terminology");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_mv_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_mv_data_origin_and_active(
        &game_dir,
        "CommonEvents.json",
        r#"[null,{"id":1,"list":[{"code":101,"parameters":[0,0,0,2]},{"code":401,"parameters":["案内人「こんにちは」"]},{"code":0,"parameters":[]}]}]"#,
    );
    insert_mv_virtual_namebox_rules(&db_path);
    insert_field_term(&db_path, "speaker_names", "案内人", "向导");

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("MV 虚拟名字框术语应由 Rust 写回计划写入");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");

    assert_eq!(value["summary"]["terminology_written_count"], 1);
    assert!(planned_file_content(&value, "data/CommonEvents.json").contains("向导「こんにちは」"));

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_mv_speaker_terms_without_virtual_namebox_rules() {
    let fixture = create_fixture_dir("att_mz_write_plan_mv_missing_namebox_rules");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_mv_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_field_term(&db_path, "speaker_names", "案内人", "向导");

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("MV speaker_names 没有虚拟名字框规则时必须直接失败");

    assert!(
        error.contains("MV 术语写回缺少 MV 虚拟名字框规则"),
        "错误文案应说明缺少 MV 虚拟名字框规则",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_preserves_plugin_json_string_container_shell() {
    let fixture = create_fixture_dir("att_mz_write_plan_plugin_json_shell");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[{"name":"TestPlugin","status":true,"description":"","parameters":{"MainEvents":"[\"{\\\"MainEventNote\\\":\\\"\\\\\\\"原始说明\\\\\\\"\\\"}\"]"}}]"#,
    );
    fs::write(
        game_dir.join("js").join("plugins").join("TestPlugin.js"),
        "const TestPlugin = {};\n",
    )
    .expect("启用插件源码应可写入");
    insert_translation_item(
        &db_path,
        "plugins.js/0/MainEvents/0/MainEventNote",
        "short_text",
        "[\"原始说明\"]",
        "[]",
        "[\"中文说明\"]",
    );

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("插件 JSON 字符串容器写回应成功");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let plugins_content = planned_file_content(&value, "js/plugins.js");
    let plugins = parse_plugins_js(plugins_content);
    let note = plugins[0]["parameters"]["MainEvents"]
        .as_str()
        .and_then(|text| serde_json::from_str::<Value>(text).ok())
        .and_then(|events| events.get(0).and_then(Value::as_str).map(str::to_string))
        .and_then(|event_text| serde_json::from_str::<Value>(&event_text).ok())
        .and_then(|event| {
            event
                .get("MainEventNote")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .expect("插件 JSON 字符串容器中的目标字段应存在");

    assert_eq!(
        serde_json::from_str::<String>(&note).expect("可见文本外壳应保持 JSON 字符串"),
        "中文说明",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_preserves_note_tag_json_string_shell() {
    let fixture = create_fixture_dir("att_mz_write_plan_note_json_shell");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "Items.json",
        r#"[null,{"id":1,"name":"Item","note":"<拡張説明:\"原始说明\">\n<keep:1>"}]"#,
    );
    insert_translation_item(
        &db_path,
        "Items.json/1/note/拡張説明",
        "short_text",
        "[\"原始说明\"]",
        "[]",
        "[\"中文说明\"]",
    );

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("Note 标签 JSON 字符串外壳写回应成功");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let items_content = planned_file_content(&value, "data/Items.json");
    let items: Value = serde_json::from_str(items_content).expect("Items.json 应是 JSON");
    let note = items[1]["note"].as_str().expect("note 应是字符串");
    let tag_value = note
        .strip_prefix("<拡張説明:")
        .and_then(|text| text.split('>').next())
        .expect("Note 标签值应存在");

    assert_eq!(
        serde_json::from_str::<String>(tag_value).expect("Note 标签值应保持 JSON 字符串外壳"),
        "中文说明",
    );
    assert!(note.ends_with("\n<keep:1>"), "其他 Note 标签必须保持不变",);

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_plugin_config_multiline_short_text() {
    let fixture = create_fixture_dir("att_mz_write_plan_plugin_multiline_short");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[{"name":"TestPlugin","status":true,"description":"","parameters":{"Message":"原文"}}]"#,
    );
    fs::write(
        game_dir.join("js").join("plugins").join("TestPlugin.js"),
        "const TestPlugin = {};\n",
    )
    .expect("启用插件源码应可写入");
    insert_translation_item(
        &db_path,
        "plugins.js/0/Message",
        "short_text",
        "[\"原文\"]",
        "[]",
        "[\"第一行\",\"第二行\"]",
    );

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("插件配置单字段译文多行时必须直接失败");

    assert!(
        error.contains("单字段文本必须只提供 1 条中文译文行"),
        "错误文案应说明插件配置单字段结构被破坏，实际为 {error}",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_event_parameter_multiline_short_text() {
    let fixture = create_fixture_dir("att_mz_write_plan_event_param_multiline_short");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "CommonEvents.json",
        r#"[null,{"id":1,"list":[{"code":357,"parameters":["TestPlugin","Show",0,{"message":"原文"}]},{"code":0,"parameters":[]}]}]"#,
    );
    let location_path = "CommonEvents.json/1/0/parameters/3/message";
    insert_translation_item(
        &db_path,
        location_path,
        "short_text",
        "[\"原文\"]",
        "[]",
        "[\"第一行\",\"第二行\"]",
    );
    let mut payload = minimal_setting_payload();
    payload["allowed_translation_paths"]
        .as_array_mut()
        .expect("allowed_translation_paths 应是数组")
        .push(json!(location_path));

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("事件参数单字段译文多行时必须直接失败");

    assert!(
        error.contains("单字段文本必须只提供 1 条中文译文行"),
        "错误文案应说明事件参数单字段结构被破坏，实际为 {error}",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_note_tag_multiline_short_text() {
    let fixture = create_fixture_dir("att_mz_write_plan_note_multiline_short");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "Items.json",
        r#"[null,{"id":1,"name":"Item","note":"<拡張説明:原文>\n<keep:1>"}]"#,
    );
    insert_translation_item(
        &db_path,
        "Items.json/1/note/拡張説明",
        "short_text",
        "[\"原文\"]",
        "[]",
        "[\"第一行\",\"第二行\"]",
    );

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("Note 标签单字段译文多行时必须直接失败");

    assert!(
        error.contains("单字段文本必须只提供 1 条中文译文行"),
        "错误文案应说明 Note 标签单字段结构被破坏，实际为 {error}",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_returns_plugin_source_runtime_map_verified_against_final_ast() {
    let fixture = create_fixture_dir("att_mz_write_plan_plugin_source_runtime_map");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[{"name":"TestPlugin","status":true,"description":"","parameters":{}}]"#,
    );
    let source = "const Messages = { title: '原文', label: '長い本文' };\n";
    fs::write(
        game_dir.join("js").join("plugins").join("TestPlugin.js"),
        source,
    )
    .expect("当前插件源码应可写入");
    fs::write(
        game_dir
            .join("js")
            .join("plugins_source_origin")
            .join("TestPlugin.js"),
        source,
    )
    .expect("原始插件源码备份应可写入");
    let source_selector = plugin_source_selector_for_visible_text(source, "長い本文");
    let location_path = format!("js/plugins/TestPlugin.js/{source_selector}");
    insert_translation_item(
        &db_path,
        &location_path,
        "short_text",
        "[\"長い本文\"]",
        "[]",
        "[\"短\"]",
    );
    let mut payload = minimal_setting_payload();
    payload["allowed_translation_paths"]
        .as_array_mut()
        .expect("allowed_translation_paths 应是数组")
        .push(json!(location_path));

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("插件源码写回计划应生成 runtime map");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let runtime_maps = value["plugin_source_runtime_write_maps"]
        .as_array()
        .expect("runtime maps 应是数组");
    let runtime_map = runtime_maps.first().expect("应生成插件源码 runtime map");
    let runtime_selector = runtime_map["runtime_selector"]
        .as_str()
        .expect("runtime selector 应是字符串");
    let planned_content = planned_file_content(&value, "js/plugins/TestPlugin.js");
    let runtime_visible_text =
        plugin_source_visible_text_by_selector(planned_content, runtime_selector);

    assert_eq!(runtime_maps.len(), 1);
    assert_eq!(runtime_map["location_path"], location_path);
    assert_eq!(runtime_visible_text, "短");
    assert_eq!(
        value["summary"]["plugin_source_ast_source_scan_file_count"],
        json!(1)
    );
    assert_eq!(
        value["summary"]["plugin_source_ast_runtime_scan_file_count"],
        json!(1)
    );
    assert_eq!(
        value["summary"]["plugin_source_runtime_map_count"],
        json!(1)
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_maps_excluded_plugin_source_selector_after_runtime_shift() {
    let fixture = create_fixture_dir("att_mz_write_plan_plugin_source_excluded_runtime_map");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[{"name":"TestPlugin","status":true,"description":"","parameters":{}}]"#,
    );
    let source = "const Messages = { title: '長い本文', category: 'カテゴリ' };\n";
    fs::write(
        game_dir.join("js").join("plugins").join("TestPlugin.js"),
        source,
    )
    .expect("当前插件源码应可写入");
    fs::write(
        game_dir
            .join("js")
            .join("plugins_source_origin")
            .join("TestPlugin.js"),
        source,
    )
    .expect("原始插件源码备份应可写入");
    let title_selector = plugin_source_selector_for_visible_text(source, "長い本文");
    let excluded_selector = plugin_source_selector_for_visible_text(source, "カテゴリ");
    let title_location_path = format!("js/plugins/TestPlugin.js/{title_selector}");
    insert_translation_item(
        &db_path,
        &title_location_path,
        "short_text",
        "[\"長い本文\"]",
        "[]",
        "[\"短\"]",
    );
    insert_plugin_source_text_rule(
        &db_path,
        "TestPlugin.js",
        &sha256_text(source),
        &excluded_selector,
        "excluded",
    );
    let mut payload = minimal_setting_payload();
    payload["allowed_translation_paths"]
        .as_array_mut()
        .expect("allowed_translation_paths 应是数组")
        .push(json!(title_location_path));

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "write_back",
        false,
    )
    .expect("插件源码写回计划应生成已排除 selector runtime map");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let runtime_maps = value["plugin_source_runtime_write_maps"]
        .as_array()
        .expect("runtime maps 应是数组");
    let translated_map = runtime_maps
        .iter()
        .find(|map| map["mapping_kind"] == "translated")
        .expect("应生成已翻译 runtime map");
    let excluded_map = runtime_maps
        .iter()
        .find(|map| map["mapping_kind"] == "excluded")
        .expect("应生成已排除 runtime map");
    let excluded_runtime_selector = excluded_map["runtime_selector"]
        .as_str()
        .expect("已排除 runtime selector 应是字符串");
    let planned_content = planned_file_content(&value, "js/plugins/TestPlugin.js");
    let excluded_runtime_text =
        plugin_source_visible_text_by_selector(planned_content, excluded_runtime_selector);

    assert_eq!(runtime_maps.len(), 2);
    assert_eq!(translated_map["location_path"], title_location_path);
    assert_eq!(excluded_map["source_selector"], excluded_selector);
    assert_ne!(excluded_runtime_selector, excluded_selector);
    assert_eq!(excluded_runtime_text, "カテゴリ");
    assert_eq!(
        value["summary"]["plugin_source_runtime_map_count"],
        json!(2)
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_plugin_config_protocol_shell_damage() {
    let fixture = create_fixture_dir("att_mz_write_plan_plugin_protocol_damage");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[{"name":"TestPlugin","status":true,"description":"","parameters":{"Message":"\"\\\\\\\\V[1]\""}}]"#,
    );
    fs::write(
        game_dir.join("js").join("plugins").join("TestPlugin.js"),
        "const TestPlugin = {};\n",
    )
    .expect("启用插件源码应可写入");
    insert_translation_item(
        &db_path,
        "plugins.js/0/Message",
        "short_text",
        r#"["\\\\V[1]"]"#,
        "[]",
        r#"["\\\\V[1]"]"#,
    );
    let setting_payload = quality_payload_allowing_backslash_control_literal();

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &setting_payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("插件配置协议外壳被破坏时必须直接失败");

    assert!(
        error.contains("文本协议写回失败"),
        "错误文案应说明插件配置文本协议失败，实际为 {error}",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_note_tag_protocol_shell_damage() {
    let fixture = create_fixture_dir("att_mz_write_plan_note_protocol_damage");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "Items.json",
        r#"[null,{"id":1,"name":"Item","note":"<拡張説明:\"\\\\\\\\V[1]\">"}]"#,
    );
    insert_translation_item(
        &db_path,
        "Items.json/1/note/拡張説明",
        "short_text",
        r#"["\\\\V[1]"]"#,
        "[]",
        r#"["\\\\V[1]"]"#,
    );
    let setting_payload = quality_payload_allowing_backslash_control_literal();

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &setting_payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("Note 标签协议外壳被破坏时必须直接失败");

    assert!(
        error.contains("文本协议写回失败"),
        "错误文案应说明 Note 标签文本协议失败，实际为 {error}",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_normalizes_multibyte_wrapping_punctuation_without_byte_panic() {
    let fixture = create_fixture_dir("att_mz_write_plan_multibyte_wrapping");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "CommonEvents.json",
        r#"[null,{"id":1,"list":[{"code":101,"parameters":[0,0,0,2]},{"code":401,"parameters":["if『s[140]」「Lonely」（幽冥裂谷）"]},{"code":0,"parameters":[]}]}]"#,
    );
    insert_translation_item(
        &db_path,
        "CommonEvents.json/1/0",
        "long_text",
        "[\"if『s[140]」「Lonely」（幽冥裂谷）\"]",
        "[\"CommonEvents.json/1/1\"]",
        "[\"if‘s[140]’‘Lonely’（幽冥裂谷）\"]",
    );
    let setting_payload = serde_json::json!({
        "quality_text_rules": minimal_setting_payload()["quality_text_rules"].clone(),
        "allowed_translation_paths": minimal_setting_payload()["allowed_translation_paths"].clone(),
        "long_text_line_width_limit": 99,
        "line_width_count_pattern": "\\S",
        "line_split_punctuations": ["，", "。", "）", "」", "』"],
        "preserve_wrapping_punctuation_pairs": [["「", "」"], ["『", "』"]]
    });

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &setting_payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("多字节包裹标点修复和长文本切行不应 panic");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let common_events_content = planned_file_content(&value, "data/CommonEvents.json");

    assert!(
        common_events_content.contains("if『s[140]」"),
        "译文左侧包裹标点应按源文槽位修复",
    );
    assert!(
        common_events_content.contains("「Lonely」"),
        "同一行第二组多字节包裹标点也应安全修复",
    );
    assert!(
        common_events_content.contains("\"code\": 401"),
        "长文本重建后仍应使用正文行指令",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_splits_overwide_long_text() {
    let fixture = create_fixture_dir("att_mz_write_plan_split_long_text");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "CommonEvents.json",
        r#"[null,{"id":1,"list":[{"code":101,"parameters":[0,0,0,2]},{"code":401,"parameters":["こんにちは"]},{"code":0,"parameters":[]}]}]"#,
    );
    insert_translation_item(
        &db_path,
        "CommonEvents.json/1/0",
        "long_text",
        "[\"こんにちは\"]",
        "[\"CommonEvents.json/1/1\"]",
        "[\"甲乙丙丁戊己庚辛\"]",
    );
    let setting_payload = serde_json::json!({
        "quality_text_rules": minimal_setting_payload()["quality_text_rules"].clone(),
        "allowed_translation_paths": minimal_setting_payload()["allowed_translation_paths"].clone(),
        "long_text_line_width_limit": 3,
        "line_width_count_pattern": "\\S",
        "line_split_punctuations": ["，", "。"],
        "preserve_wrapping_punctuation_pairs": [["「", "」"], ["『", "』"]]
    });

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &setting_payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("长文本超宽时 Rust 写回计划应能切行");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let common_events_content = planned_file_content(&value, "data/CommonEvents.json");

    assert!(
        common_events_content.contains("\"甲乙丙\""),
        "第一行应按行宽上限切分",
    );
    assert!(
        common_events_content.contains("\"丁戊己\""),
        "第二行应按行宽上限切分",
    );

    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_bad_setting_payload() {
    let error = build_write_back_plan_impl(
        "C:/missing-game",
        "C:/missing.db",
        "{",
        "rebuild_active_runtime",
        false,
    )
    .expect_err("配置 JSON 损坏时必须失败");

    assert!(
        error.contains("写回计划配置 JSON 无效"),
        "错误文案应说明配置 JSON 无效",
    );
}

#[test]
fn build_plan_rejects_invalid_mode_before_running_hot_path() {
    let error =
        build_write_back_plan_impl("C:/missing-game", "C:/missing.db", "{}", "legacy", false)
            .expect_err("写回计划 mode 非法时必须直接失败");

    assert!(
        error.contains("写回计划模式无效"),
        "错误文案应说明 mode 非法"
    );
}

#[test]
fn build_plan_rejects_missing_quality_text_rules() {
    let fixture = create_fixture_dir("att_mz_write_plan_missing_quality_rules");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    let mut payload = minimal_setting_payload();
    payload
        .as_object_mut()
        .expect("测试配置载荷应为对象")
        .remove("quality_text_rules");

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("缺少 Rust 质检文本规则时必须直接失败");

    assert!(
        error.contains("写回计划缺少 Rust 质检文本规则"),
        "错误文案应说明缺少 Rust 质检规则",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_missing_allowed_translation_paths() {
    let fixture = create_fixture_dir("att_mz_write_plan_missing_allowed_paths");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    let mut payload = minimal_setting_payload();
    payload
        .as_object_mut()
        .expect("测试配置载荷应为对象")
        .remove("allowed_translation_paths");

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("缺少可写文本范围时必须直接失败");

    assert!(
        error.contains("写回计划缺少 allowed_translation_paths"),
        "错误文案应说明缺少当前可写文本范围",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_missing_text_plan_layout_fields() {
    let fixture = create_fixture_dir("att_mz_write_plan_missing_text_layout");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);

    for field_name in [
        "long_text_line_width_limit",
        "line_width_count_pattern",
        "line_split_punctuations",
        "preserve_wrapping_punctuation_pairs",
    ] {
        let mut payload = minimal_setting_payload();
        payload
            .as_object_mut()
            .expect("测试配置载荷应为对象")
            .remove(field_name);
        let error = build_write_back_plan_impl(
            &game_dir.to_string_lossy(),
            &db_path.to_string_lossy(),
            &payload.to_string(),
            "rebuild_active_runtime",
            false,
        )
        .expect_err("缺少文本布局字段时必须直接失败");

        assert!(
            error.contains(&format!("写回计划缺少 {field_name}")),
            "错误文案应说明缺少 {field_name}，实际为 {error}",
        );
    }
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_reads_source_residual_rules_from_sqlite() {
    let fixture = create_fixture_dir("att_mz_write_plan_source_residual_rule");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_translation_item(
        &db_path,
        "System.json/gameTitle",
        "short_text",
        "[\"原标题\"]",
        "[]",
        "[\"テストタイトル\"]",
    );
    insert_source_residual_rule(&db_path, "System.json/gameTitle", "[\"テストタイトル\"]");

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("Rust 写回计划应读取数据库中的源文残留例外规则");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let system_content = planned_file_content(&value, "data/System.json");

    assert!(
        system_content.contains("テストタイトル"),
        "数据库中的位置源文残留例外规则应允许指定片段写入",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_incomplete_plugin_config_path() {
    let fixture = create_fixture_dir("att_mz_write_plan_bad_plugin_path");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_translation_item(
        &db_path,
        "plugins.js/0",
        "short_text",
        "[\"原文\"]",
        "[]",
        "[\"译文\"]",
    );

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("插件配置路径不完整时必须直接失败");

    assert!(
        error.contains("插件配置路径不完整"),
        "错误文案应说明插件配置路径不完整",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_incomplete_plugin_source_path() {
    let fixture = create_fixture_dir("att_mz_write_plan_bad_plugin_source_path");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_translation_item(
        &db_path,
        "js/plugins/TestPlugin.js",
        "short_text",
        "[\"原文\"]",
        "[]",
        "[\"译文\"]",
    );

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("插件源码路径不完整时必须直接失败");

    assert!(
        error.contains("插件源码路径缺少 selector"),
        "错误文案应说明插件源码路径缺少 selector",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_unparseable_event_command_sort_path() {
    let fixture = create_fixture_dir("att_mz_write_plan_bad_event_sort_path");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "CommonEvents.json",
        r#"[null,{"id":1,"list":[{"code":357,"parameters":["TestPlugin","Show",0,{"message":"原文"}]},{"code":0,"parameters":[]}]}]"#,
    );
    let bad_path = "CommonEvents.json/not-number/0/parameters/3/message";
    insert_translation_item(
        &db_path,
        bad_path,
        "short_text",
        "[\"原文\"]",
        "[]",
        "[\"译文\"]",
    );
    let mut payload = minimal_setting_payload();
    payload["allowed_translation_paths"]
        .as_array_mut()
        .expect("allowed_translation_paths 应是数组")
        .push(json!(bad_path));

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("事件指令排序路径解析失败时必须直接失败");

    assert!(
        error.contains("数字解析失败 not-number"),
        "错误文案应说明事件指令路径数字解析失败，实际为 {error}",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_empty_translation_lines() {
    let fixture = create_fixture_dir("att_mz_write_plan_empty_translation");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_translation_item(
        &db_path,
        "System.json/gameTitle",
        "short_text",
        "[\"原标题\"]",
        "[]",
        "[]",
    );

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("译文行为空时必须直接失败");

    assert!(error.contains("译文行为空"), "错误文案应说明译文行为空",);
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_blank_translation_lines() {
    let fixture = create_fixture_dir("att_mz_write_plan_blank_translation");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    insert_translation_item(
        &db_path,
        "System.json/gameTitle",
        "short_text",
        "[\"原标题\"]",
        "[]",
        "[\"   \"]",
    );

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("译文行全为空白时必须直接失败");

    assert!(error.contains("译文行为空"), "错误文案应说明译文行为空");
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_writes_system_switches_without_silent_skip() {
    let fixture = create_fixture_dir("att_mz_write_plan_system_switches");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "System.json",
        r#"{"gameTitle":"原标题","switches":["","元スイッチ"],"variables":["","元変数"]}"#,
    );
    insert_translation_item(
        &db_path,
        "System.json/switches/1",
        "short_text",
        "[\"元スイッチ\"]",
        "[]",
        "[\"开关译名\"]",
    );

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("System.switches 译文应由 Rust 写回计划写入");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let system_content = planned_file_content(&value, "data/System.json");

    assert!(
        system_content.contains("开关译名"),
        "System.switches 不得被静默跳过",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_splits_long_text_without_breaking_control_codes() {
    let fixture = create_fixture_dir("att_mz_write_plan_split_control_code");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_data_origin_and_active(
        &game_dir,
        "CommonEvents.json",
        r#"[null,{"id":1,"list":[{"code":101,"parameters":[0,0,0,2]},{"code":401,"parameters":["_ENEMY_は\\nn[Ayumu]と_HERO_の名前は\\v[1](\\v[2])だ……\\."]},{"code":0,"parameters":[]}]}]"#,
    );
    insert_translation_item(
        &db_path,
        "CommonEvents.json/1/0",
        "long_text",
        r#"["_ENEMY_は\\nn[Ayumu]と_HERO_の名前は\\v[1](\\v[2])だ……\\."]"#,
        r#"["CommonEvents.json/1/1"]"#,
        r#"["_ENEMY_和\\nn[Ayumu]、_HERO_的名字是\\v[1](\\v[2])……\\."]"#,
    );
    let mut payload = minimal_setting_payload();
    payload["long_text_line_width_limit"] = json!(4);

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &payload.to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("长文本切行不能破坏控制符");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let content = planned_file_content(&value, "data/CommonEvents.json");

    assert!(content.contains(r"\\v[1]"));
    assert!(content.contains(r"\\v[2]"));
    assert!(content.contains(r"\\nn[Ayumu]"));
    assert!(content.contains("_ENEMY_"));
    assert!(content.contains("_HERO_"));
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_quality_gate_checks_without_planned_files() {
    let fixture = create_fixture_dir("att_mz_write_plan_quality_gate");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "quality_gate",
        false,
    )
    .expect("quality_gate 应执行完整可写检查");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");

    assert_eq!(value["mode"], "quality_gate");
    assert_eq!(value["summary"]["data_item_count"], 1);
    assert_eq!(value["summary"]["planned_file_count"], 0);
    assert_eq!(
        value["files"].as_array().map(Vec::len),
        Some(0),
        "quality_gate 不应返回待替换文件",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_ignores_non_standard_origin_data_files() {
    let fixture = create_fixture_dir("att_mz_write_plan_skip_unknown_data");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    fs::write(
        game_dir.join("data_origin").join("UnknownPluginData.json"),
        r#"{"name":"可信源"}"#,
    )
    .expect("非标准可信源 data 应可写入");
    fs::write(
        game_dir.join("data").join("UnknownPluginData.json"),
        r#"{"name":"当前运行"}"#,
    )
    .expect("非标准当前 data 应可写入");

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("非标准 data 文件不应阻断写回计划");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");
    let planned_paths: Vec<&str> = value["files"]
        .as_array()
        .expect("计划文件应是数组")
        .iter()
        .filter_map(|file| file["relative_path"].as_str())
        .collect();

    assert!(
        !planned_paths.contains(&"data/UnknownPluginData.json"),
        "非标准插件私有 data JSON 不得进入 Rust 写回计划",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_accepts_json5_plugins_origin() {
    let fixture = create_fixture_dir("att_mz_write_plan_json5_plugins");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[
            {
                name: 'LoosePlugin',
                status: false,
                parameters: {
                    Message: '原文',
                },
            },
        ]"#,
    );

    let output = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect("plugins_origin.js 应支持 RPG Maker 常见宽松 JS 写法");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");

    assert_eq!(value["status"], "ok");
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_rejects_enabled_plugin_without_name() {
    let fixture = create_fixture_dir("att_mz_write_plan_plugin_missing_name");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);
    write_plugins_origin(
        &game_dir,
        r#"[{"status":true,"description":"","parameters":{}}]"#,
    );

    let error = build_write_back_plan_impl(
        &game_dir.to_string_lossy(),
        &db_path.to_string_lossy(),
        &minimal_setting_payload().to_string(),
        "rebuild_active_runtime",
        false,
    )
    .expect_err("启用插件缺少 name 时必须直接失败");

    assert!(
        error.contains("启用插件缺少 name"),
        "错误文案应说明启用插件缺少 name，实际为 {error}",
    );
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

#[test]
fn build_plan_accepts_thread_pool_env_on_write_plan_path() {
    let fixture = create_fixture_dir("att_mz_write_plan_thread_env");
    let game_dir = fixture.join("game");
    let db_path = fixture.join("game.db");
    create_minimal_game_files(&game_dir);
    create_minimal_database(&db_path);

    let output = crate::native_core::pool::with_thread_count_override_for_test(Some("1"), || {
        build_write_back_plan_impl(
            &game_dir.to_string_lossy(),
            &db_path.to_string_lossy(),
            &minimal_setting_payload().to_string(),
            "rebuild_active_runtime",
            false,
        )
    })
    .expect("写回计划路径应接受 ATT_MZ_RUST_THREADS");
    let value: Value = serde_json::from_str(&output).expect("写回计划输出应是 JSON");

    assert_eq!(value["status"], "ok");
    fs::remove_dir_all(fixture).expect("测试目录应可清理");
}

fn planned_file_content<'a>(plan: &'a Value, relative_path: &str) -> &'a str {
    plan["files"]
        .as_array()
        .and_then(|files| {
            files
                .iter()
                .find(|file| file["relative_path"] == relative_path)
        })
        .and_then(|file| file["content"].as_str())
        .expect("指定计划文件应存在并包含字符串内容")
}

fn parse_plugins_js(content: &str) -> Value {
    let start = content.find('[').expect("plugins.js 应包含数组起点");
    let end = content.rfind(']').expect("plugins.js 应包含数组终点");
    serde_json::from_str(&content[start..=end]).expect("plugins.js 数组应可解析")
}

fn plugin_source_selector_for_visible_text(source: &str, expected_visible_text: &str) -> String {
    let scan = parse_javascript_string_spans(source).expect("测试插件源码应可解析");
    let span = scan
        .spans
        .into_iter()
        .find(|span| {
            let raw_text = source
                .get(span.content_start_byte_index..span.content_end_byte_index)
                .expect("测试字符串范围应有效");
            normalize_visible_text_for_extraction(&unescape_js_text(raw_text))
                == expected_visible_text
        })
        .expect("测试插件源码应包含指定可见文本");
    let raw_text = source
        .get(span.content_start_byte_index..span.content_end_byte_index)
        .expect("测试字符串范围应有效");
    candidate_selector_for_span(span.start_index, span.end_index, raw_text)
}

fn plugin_source_visible_text_by_selector(source: &str, selector: &str) -> String {
    let scan = parse_javascript_string_spans(source).expect("测试插件源码应可解析");
    for span in scan.spans {
        let raw_text = source
            .get(span.content_start_byte_index..span.content_end_byte_index)
            .expect("测试字符串范围应有效");
        let current_selector =
            candidate_selector_for_span(span.start_index, span.end_index, raw_text);
        if current_selector == selector {
            return normalize_visible_text_for_extraction(&unescape_js_text(raw_text));
        }
    }
    panic!("runtime selector 应能在最终 AST 中找到: {selector}");
}

fn minimal_setting_payload() -> Value {
    json!({
        "allowed_translation_paths": [
            "System.json/gameTitle",
            "System.json/switches/1",
            "plugins.js/0",
            "plugins.js/0/MainEvents/0/MainEventNote",
            "plugins.js/0/Message",
            "js/plugins/TestPlugin.js",
            "CommonEvents.json/1/0",
            "CommonEvents.json/2/0",
            "CommonEvents.json/3/0",
            "CommonEvents.json/4/0",
            "Items.json/1/note/拡張説明"
        ],
        "long_text_line_width_limit": 999,
        "line_width_count_pattern": r"\S",
        "line_split_punctuations": ["，", "。", "、", "；", "：", "！", "？", "…", "～", "—", "♪", "♡", "）", "】", "」", "』", ",", ".", ";", ":", "!", "?"],
        "preserve_wrapping_punctuation_pairs": [["「", "」"], ["『", "』"]],
        "quality_text_rules": {
            "custom_placeholder_rules": [],
            "structured_placeholder_rules": [],
            "source_residual_allowed_chars": [],
            "source_residual_allowed_tail_chars": [],
            "source_residual_segment_pattern": r"[\p{Hiragana}\p{Katakana}ー]+",
            "source_residual_label": "日文",
            "allowed_source_residual_terms": [],
            "source_residual_terms_ignore_case": false,
            "line_width_count_pattern": r"\S",
            "residual_escape_sequence_pattern": r"\\[A-Za-z0-9_]+\[[^\]]*\]",
            "long_text_line_width_limit": 999
        }
    })
}

fn quality_payload_allowing_backslash_control_literal() -> Value {
    let mut payload = minimal_setting_payload();
    payload["quality_text_rules"]["custom_placeholder_rules"] = json!([
        {
            "pattern_text": r"\\+V\[\d+\]",
            "placeholder_template": "[CUSTOM_BACKSLASH_CONTROL_{index}]"
        }
    ]);
    payload
}

fn create_fixture_dir(prefix: &str) -> PathBuf {
    let unique_id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("系统时间应晚于 UNIX_EPOCH")
        .as_nanos();
    let path = std::env::temp_dir().join(format!("{prefix}_{unique_id}"));
    fs::create_dir_all(&path).expect("测试目录应可创建");
    path
}

fn create_minimal_game_files(game_dir: &Path) {
    let data_dir = game_dir.join("data");
    let data_origin_dir = game_dir.join("data_origin");
    let js_dir = game_dir.join("js");
    let plugin_source_dir = js_dir.join("plugins");
    let plugin_source_origin_dir = js_dir.join("plugins_source_origin");
    fs::create_dir_all(&data_dir).expect("data 目录应可创建");
    fs::create_dir_all(&data_origin_dir).expect("data_origin 目录应可创建");
    fs::create_dir_all(&plugin_source_dir).expect("插件源码目录应可创建");
    fs::create_dir_all(&plugin_source_origin_dir).expect("插件源码备份目录应可创建");
    fs::write(data_dir.join("System.json"), "{\"gameTitle\":\"原标题\"}\n")
        .expect("当前 System.json 应可写入");
    fs::write(
        data_origin_dir.join("System.json"),
        "{\"gameTitle\":\"原标题\"}\n",
    )
    .expect("原始 System.json 应可写入");
    fs::write(js_dir.join("plugins.js"), "var $plugins = [];\n").expect("当前 plugins.js 应可写入");
    fs::write(js_dir.join("plugins_origin.js"), "var $plugins = [];\n")
        .expect("原始 plugins.js 应可写入");
}

fn create_minimal_mv_game_files(game_dir: &Path) {
    let content_root = game_dir.join("www");
    let data_dir = content_root.join("data");
    let data_origin_dir = content_root.join("data_origin");
    let js_dir = content_root.join("js");
    let plugin_source_dir = js_dir.join("plugins");
    let plugin_source_origin_dir = js_dir.join("plugins_source_origin");
    fs::create_dir_all(&data_dir).expect("MV data 目录应可创建");
    fs::create_dir_all(&data_origin_dir).expect("MV data_origin 目录应可创建");
    fs::create_dir_all(&plugin_source_dir).expect("MV 插件源码目录应可创建");
    fs::create_dir_all(&plugin_source_origin_dir).expect("MV 插件源码备份目录应可创建");
    fs::write(data_dir.join("System.json"), "{\"gameTitle\":\"原标题\"}\n")
        .expect("MV 当前 System.json 应可写入");
    fs::write(
        data_origin_dir.join("System.json"),
        "{\"gameTitle\":\"原标题\"}\n",
    )
    .expect("MV 原始 System.json 应可写入");
    fs::write(js_dir.join("plugins.js"), "var $plugins = [];\n")
        .expect("MV 当前 plugins.js 应可写入");
    fs::write(js_dir.join("plugins_origin.js"), "var $plugins = [];\n")
        .expect("MV 原始 plugins.js 应可写入");
}

fn write_plugins_origin(game_dir: &Path, plugins_array_text: &str) {
    let js_dir = game_dir.join("js");
    let content = format!("var $plugins = {plugins_array_text};\n");
    fs::write(js_dir.join("plugins.js"), &content).expect("当前 plugins.js 应可写入");
    fs::write(js_dir.join("plugins_origin.js"), content).expect("原始 plugins.js 应可写入");
}

fn write_data_origin_and_active(game_dir: &Path, file_name: &str, content: &str) {
    for dir_name in ["data", "data_origin"] {
        fs::write(game_dir.join(dir_name).join(file_name), content)
            .expect("测试 data 文件应可写入");
    }
}

fn write_mv_data_origin_and_active(game_dir: &Path, file_name: &str, content: &str) {
    for dir_name in ["data", "data_origin"] {
        fs::write(game_dir.join("www").join(dir_name).join(file_name), content)
            .expect("测试 MV data 文件应可写入");
    }
}

fn create_minimal_database(db_path: &Path) {
    let connection = Connection::open(db_path).expect("测试数据库应可创建");
    connection
        .execute_batch(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../app/persistence/schema/current.sql"
        )))
        .expect("测试数据库 schema 应可创建");
    connection
        .execute(
            "INSERT INTO translation_items \
             (location_path, item_type, role, original_lines, source_line_paths, translation_lines) \
             VALUES (?1, ?2, NULL, ?3, ?4, ?5)",
            params![
                "System.json/gameTitle",
                "short_text",
                "[\"原标题\"]",
                "[]",
                "[\"测试标题\"]",
            ],
        )
        .expect("测试译文应可写入");
    connection
        .execute(
            "INSERT INTO translation_runs \
             (run_id, status, total_extracted, pending_count, deduplicated_count, batch_count, \
              success_count, quality_error_count, llm_failure_count, physical_request_count, \
              retry_request_count, started_at, updated_at, finished_at, stop_reason, last_error) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
            params![
                "run_test",
                "completed",
                1,
                0,
                1,
                1,
                1,
                0,
                0,
                0,
                0,
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:01",
                "2026-01-01T00:00:01",
                "",
                "",
            ],
        )
        .expect("测试运行记录应可写入");
}

fn insert_translation_item(
    db_path: &Path,
    location_path: &str,
    item_type: &str,
    original_lines: &str,
    source_line_paths: &str,
    translation_lines: &str,
) {
    let connection = Connection::open(db_path).expect("测试数据库应可打开");
    connection
        .execute(
            "INSERT OR REPLACE INTO translation_items \
             (location_path, item_type, role, original_lines, source_line_paths, translation_lines) \
             VALUES (?1, ?2, NULL, ?3, ?4, ?5)",
            params![
                location_path,
                item_type,
                original_lines,
                source_line_paths,
                translation_lines,
            ],
        )
        .expect("测试译文应可写入");
}

fn insert_plugin_source_text_rule(
    db_path: &Path,
    file_name: &str,
    file_hash: &str,
    selector: &str,
    selector_kind: &str,
) {
    let connection = Connection::open(db_path).expect("测试数据库应可打开");
    connection
        .execute(
            "INSERT INTO plugin_source_text_rules \
             (file_name, file_hash, selector, selector_kind) VALUES (?1, ?2, ?3, ?4)",
            params![file_name, file_hash, selector, selector_kind],
        )
        .expect("测试插件源码规则应可写入");
}

fn insert_source_residual_rule(db_path: &Path, location_path: &str, allowed_terms: &str) {
    let connection = Connection::open(db_path).expect("测试数据库应可打开");
    connection
        .execute(
            "INSERT INTO source_residual_rules \
             (rule_id, rule_type, location_path, pattern_text, allowed_terms, check_group, reason) \
             VALUES (?1, 'position', ?2, '', ?3, '', '测试允许片段')",
            params![
                format!("position:{location_path}"),
                location_path,
                allowed_terms,
            ],
        )
        .expect("测试源文残留例外规则应可写入");
}

fn insert_field_term(db_path: &Path, category: &str, source_text: &str, translated_text: &str) {
    let connection = Connection::open(db_path).expect("测试数据库应可打开");
    connection
        .execute(
            "INSERT INTO terminology_field_terms (category, source_text, translated_text) \
             VALUES (?1, ?2, ?3)",
            params![category, source_text, translated_text],
        )
        .expect("测试字段术语应可写入");
}

fn create_mv_virtual_namebox_game_files(game_dir: &Path) {
    let common_events = r#"
[
  null,
  {
"id": 1,
"list": [
  {"code": 0, "parameters": []}
]
  },
  {
"id": 2,
"list": [
  {"code": 101, "parameters": [0, 0, 0, 2]},
  {"code": 401, "parameters": ["案内人："]},
  {"code": 401, "parameters": ["次の本文です"]},
  {"code": 0, "parameters": []}
]
  },
  {
"id": 3,
"list": [
  {"code": 101, "parameters": [0, 0, 0, 2]},
  {"code": 401, "parameters": ["案内人「こんにちは」"]},
  {"code": 0, "parameters": []}
]
  },
  {
"id": 4,
"list": [
  {"code": 101, "parameters": [0, 0, 0, 2]},
  {"code": 401, "parameters": ["\\N[1]:役者の本文です"]},
  {"code": 0, "parameters": []}
]
  }
]
"#;
    let actors = r#"[null, {"id": 1, "name": "MV勇者"}]"#;
    for dir_name in ["data", "data_origin"] {
        let data_dir = game_dir.join(dir_name);
        fs::write(data_dir.join("CommonEvents.json"), common_events)
            .expect("MV CommonEvents.json 应可写入");
        fs::write(data_dir.join("Actors.json"), actors).expect("MV Actors.json 应可写入");
    }
}

fn insert_mv_virtual_namebox_rules(db_path: &Path) {
    let connection = Connection::open(db_path).expect("测试数据库应可打开");
    let rules = [
        (
            0,
            "quote-inline",
            r"^(?P<speaker>[^\\「（:：<>\r\n]{1,40})\s*(?P<connector>[:：]?「)(?P<body>.*)$",
            "speaker",
            "body",
            "translate",
            "{speaker}{connector}{body}",
        ),
        (
            1,
            "standalone-colon",
            r"^(?P<speaker>[^\\「『【\[\]()（）:：\r\n]{1,40})\s*[:：]\s*$",
            "speaker",
            "",
            "translate",
            "{speaker}：",
        ),
        (
            2,
            "actor-inline",
            r"^(?P<speaker>\\[Nn]\[(?P<actor_id>1)\])(?P<separator>[:：])(?P<body>.*)$",
            "speaker",
            "body",
            "actor_name",
            "{speaker}{separator}{body}",
        ),
    ];
    for rule in rules {
        connection
            .execute(
                "INSERT INTO mv_virtual_namebox_rules \
                 (rule_order, rule_name, pattern_text, speaker_group, body_group, speaker_policy, render_template) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![rule.0, rule.1, rule.2, rule.3, rule.4, rule.5, rule.6],
            )
            .expect("MV 虚拟名字框规则应可写入");
    }
}

fn insert_mv_virtual_namebox_rules_and_items(db_path: &Path) {
    insert_mv_virtual_namebox_rules(db_path);
    let connection = Connection::open(db_path).expect("测试数据库应可打开");
    for (source_text, translated_text) in [("案内人", "向导"), ("MV勇者", "勇者")] {
        connection
            .execute(
                "INSERT INTO terminology_field_terms (category, source_text, translated_text) \
                 VALUES ('speaker_names', ?1, ?2)",
                params![source_text, translated_text],
            )
            .expect("MV 说话人术语应可写入");
    }
    let items = [
        (
            "CommonEvents.json/2/0",
            "[\"次の本文です\"]",
            "[\"CommonEvents.json/2/2\"]",
            "[\"你好\"]",
        ),
        (
            "CommonEvents.json/3/0",
            "[\"こんにちは\"]",
            "[\"CommonEvents.json/3/1\"]",
            "[\"你好」\"]",
        ),
        (
            "CommonEvents.json/4/0",
            "[\"役者の本文です\"]",
            "[\"CommonEvents.json/4/1\"]",
            "[\"勇者正文\"]",
        ),
    ];
    for (location_path, original_lines, source_line_paths, translation_lines) in items {
        connection
            .execute(
                "INSERT INTO translation_items \
                 (location_path, item_type, role, original_lines, source_line_paths, translation_lines) \
                 VALUES (?1, 'long_text', NULL, ?2, ?3, ?4)",
                params![location_path, original_lines, source_line_paths, translation_lines],
            )
            .expect("MV 虚拟名字框译文应可写入");
    }
}
