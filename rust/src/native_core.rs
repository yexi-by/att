//! Rust 原生核心门面。
//!
//! 本模块只声明内部功能域并向 PyO3 绑定层暴露稳定入口，具体扫描逻辑分布在
//! 质量检查、写入协议、Note 标签来源和字体替换等子模块中。

mod controls;
mod details;
mod file_hashing;
mod font_replacement;
mod javascript_ast;
mod models;
mod note_sources;
mod placeholder_candidates;
mod placeholders;
mod plugins;
mod pool;
mod quality;
mod rules;
mod write_back_plan;
mod write_protocol;

pub(crate) use file_hashing::FileHashError;

/// 扫描译文质量问题并返回 JSON 明细。
pub fn scan_quality_impl(payload_json: &str) -> Result<String, String> {
    quality::scan_quality_impl(payload_json)
}

/// 扫描译文质量问题并返回 JSON 计数。
pub fn scan_quality_counts_impl(payload_json: &str) -> Result<String, String> {
    quality::scan_quality_counts_impl(payload_json)
}

/// 检查写回协议风险并返回 JSON 明细。
pub fn scan_write_protocol_impl(payload_json: &str) -> Result<String, String> {
    write_protocol::scan_write_protocol_impl(payload_json)
}

/// 检查写回协议风险并返回 JSON 计数。
pub fn scan_write_protocol_count_impl(payload_json: &str) -> Result<String, String> {
    write_protocol::scan_write_protocol_count_impl(payload_json)
}

/// 收集 Note 标签文本来源并返回 JSON 数组。
pub fn collect_note_tag_sources_impl(payload_json: &str) -> Result<String, String> {
    note_sources::collect_note_tag_sources_impl(payload_json)
}

/// 在受信任根目录内批量计算普通文件 SHA-256。
pub(crate) fn hash_files_impl(payload_json: &str) -> Result<String, FileHashError> {
    file_hashing::hash_files_impl(payload_json)
}

/// 批量扫描疑似控制符 occurrence 并返回覆盖事实。
pub fn scan_placeholder_candidates_impl(payload_json: &str) -> Result<String, String> {
    placeholder_candidates::scan_placeholder_candidates_impl(payload_json)
}

/// 解析 JavaScript 字符串字面量跨度并返回 JSON 明细。
pub fn parse_javascript_string_spans_impl(payload_json: &str) -> Result<String, String> {
    javascript_ast::parse_javascript_string_spans_impl(payload_json)
}

/// 批量解析 JavaScript 字符串字面量跨度并返回 JSON 明细。
pub fn parse_javascript_string_spans_batch_impl(payload_json: &str) -> Result<String, String> {
    javascript_ast::parse_javascript_string_spans_batch_impl(payload_json)
}

/// 构建写回或重建运行文件计划并返回 JSON 计划。
pub fn build_write_back_plan_impl(
    game_path: &str,
    db_path: &str,
    setting_payload_json: &str,
    mode: &str,
    confirm_font_overwrite: bool,
) -> Result<String, String> {
    write_back_plan::build_write_back_plan_impl(
        game_path,
        db_path,
        setting_payload_json,
        mode,
        confirm_font_overwrite,
    )
}

/// 返回当前进程共享原生线程池的实际线程数。
pub fn executor_thread_count() -> Result<usize, String> {
    pool::executor_thread_count()
}

/// 解析 `plugins.js` 中已捕获的 `$plugins` 数组文本。
pub fn parse_plugins_array_impl(payload_json: &str) -> Result<String, String> {
    plugins::parse_plugins_array_impl(payload_json)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn minimal_text_rules() -> Value {
        json!({
            "custom_placeholder_rules": [],
            "source_residual_allowed_chars": [],
            "source_residual_allowed_tail_chars": [],
            "source_residual_segment_pattern": r"[\p{Hiragana}\p{Katakana}\p{Han}ー]+",
            "source_residual_label": "日文",
            "allowed_source_residual_terms": [],
            "source_residual_terms_ignore_case": false,
            "line_width_count_pattern": r"[^\s]",
            "residual_escape_sequence_pattern": r"\\[A-Za-z0-9_]+\[[^\]]*\]",
            "long_text_line_width_limit": 999
        })
    }

    fn english_text_rules() -> Value {
        json!({
            "custom_placeholder_rules": [],
            "source_residual_allowed_chars": [],
            "source_residual_allowed_tail_chars": [],
            "source_residual_segment_pattern": r"[A-Za-z][A-Za-z0-9'’_-]*",
            "source_residual_label": "英文",
            "allowed_source_residual_terms": ["HP", "MP", "TP", "OK"],
            "source_residual_terms_ignore_case": true,
            "line_width_count_pattern": r"[^\s]",
            "residual_escape_sequence_pattern": r"\\[A-Za-z0-9_]+\[[^\]]*\]",
            "long_text_line_width_limit": 999
        })
    }

    #[test]
    fn quality_scan_reports_source_residual_as_segments() {
        let payload = json!({
            "items": [
                {
                    "location_path": "Map001.json/1/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": ["Hello Alice"],
                    "translation_lines": ["你好 Alice"]
                }
            ],
            "text_rules": english_text_rules(),
            "source_residual_rules": []
        });
        let output = scan_quality_impl(&payload.to_string()).expect("质检应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        let reason = value["source_residual_items"][0]["reason"]
            .as_str()
            .expect("残留明细应包含原因");
        assert!(reason.contains("Alice"));
        assert!(!reason.contains("'A', 'l'"));
    }

    #[test]
    fn quality_count_scan_returns_only_counts() {
        let payload = json!({
            "items": [
                {
                    "location_path": "Map001.json/1/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": ["Hello Alice"],
                    "translation_lines": ["你好 Alice"]
                }
            ],
            "text_rules": english_text_rules(),
            "source_residual_rules": []
        });
        let output = scan_quality_counts_impl(&payload.to_string()).expect("质检计数应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value["source_residual_count"], json!(1));
        assert_eq!(value["text_structure_count"], json!(0));
        assert_eq!(value["placeholder_risk_count"], json!(0));
        assert_eq!(value["overwide_line_count"], json!(0));
        assert!(value.get("source_residual_items").is_none());
    }

    #[test]
    fn quality_output_is_identical_with_one_two_and_four_threads() {
        let payload = json!({
            "items": [
                {
                    "location_path": "Map001.json/1/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": ["Hello Alice"],
                    "translation_lines": ["你好 Alice"]
                },
                {
                    "location_path": "Map001.json/2/0/0",
                    "item_type": "short_text",
                    "role": null,
                    "original_lines": [r"\V[1]Text"],
                    "translation_lines": [r"\V[2]译文"]
                },
                {
                    "location_path": "Map001.json/3/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": ["Text"],
                    "translation_lines": ["一二三四五"]
                }
            ],
            "text_rules": {
                "custom_placeholder_rules": [],
                "source_residual_allowed_chars": [],
                "source_residual_allowed_tail_chars": [],
                "source_residual_segment_pattern": r"[A-Za-z][A-Za-z0-9'’_-]*",
                "source_residual_label": "英文",
                "allowed_source_residual_terms": [],
                "source_residual_terms_ignore_case": true,
                "line_width_count_pattern": r"[^\s]",
                "residual_escape_sequence_pattern": r"\\[A-Za-z0-9_]+\[[^\]]*\]",
                "long_text_line_width_limit": 2
            },
            "source_residual_rules": []
        });
        let payload_json = payload.to_string();
        let scan = |thread_count: &str| {
            pool::with_thread_count_override_for_test(Some(thread_count), || {
                scan_quality_impl(&payload_json).expect("质检应成功")
            })
        };
        let counts = |thread_count: &str| {
            pool::with_thread_count_override_for_test(Some(thread_count), || {
                scan_quality_counts_impl(&payload_json).expect("质检计数应成功")
            })
        };

        let one_thread_output = scan("1");
        assert_eq!(scan("2"), one_thread_output);
        assert_eq!(scan("4"), one_thread_output);
        let one_thread_counts = counts("1");
        assert_eq!(counts("2"), one_thread_counts);
        assert_eq!(counts("4"), one_thread_counts);

        let details: Value = serde_json::from_str(&one_thread_output).expect("明细应为 JSON");
        let count_values: Value = serde_json::from_str(&one_thread_counts).expect("计数应为 JSON");
        assert_eq!(
            count_values["source_residual_count"],
            json!(
                details["source_residual_items"]
                    .as_array()
                    .expect("源文残留明细应为数组")
                    .len()
            )
        );
        assert_eq!(
            count_values["text_structure_count"],
            json!(
                details["text_structure_items"]
                    .as_array()
                    .expect("结构明细应为数组")
                    .len()
            )
        );
        assert_eq!(
            count_values["placeholder_risk_count"],
            json!(
                details["placeholder_risk_items"]
                    .as_array()
                    .expect("占位符明细应为数组")
                    .len()
            )
        );
        assert_eq!(
            count_values["overwide_line_count"],
            json!(
                details["overwide_line_items"]
                    .as_array()
                    .expect("行宽明细应为数组")
                    .len()
            )
        );
    }

    #[test]
    fn quality_scan_rejects_unknown_source_residual_rule_type() {
        let payload = json!({
            "items": [],
            "text_rules": english_text_rules(),
            "source_residual_rules": [
                {
                    "rule_id": "broken:1",
                    "rule_type": "unknown",
                    "location_path": "Map001.json/1/0/0",
                    "pattern_text": "",
                    "allowed_terms": [],
                    "check_group": "",
                    "reason": "损坏测试"
                }
            ]
        });
        let error = scan_quality_impl(&payload.to_string()).expect_err("未知规则类型必须报错");
        assert!(error.contains("源文保留规则类型无效"));
    }

    #[test]
    fn quality_scan_rejects_corrupt_position_source_residual_rule() {
        let missing_path_payload = json!({
            "items": [],
            "text_rules": english_text_rules(),
            "source_residual_rules": [
                {
                    "rule_id": "position:missing_path",
                    "rule_type": "position",
                    "location_path": "",
                    "pattern_text": "",
                    "allowed_terms": ["Alice"],
                    "check_group": "",
                    "reason": "损坏测试"
                }
            ]
        });
        let missing_path_error = scan_quality_impl(&missing_path_payload.to_string())
            .expect_err("位置规则缺少内部位置必须报错");
        assert!(missing_path_error.contains("位置源文保留规则缺少内部位置"));

        let empty_terms_payload = json!({
            "items": [],
            "text_rules": english_text_rules(),
            "source_residual_rules": [
                {
                    "rule_id": "position:empty_terms",
                    "rule_type": "position",
                    "location_path": "Map001.json/1/0/0",
                    "pattern_text": "",
                    "allowed_terms": [],
                    "check_group": "",
                    "reason": "损坏测试"
                }
            ]
        });
        let empty_terms_error = scan_quality_impl(&empty_terms_payload.to_string())
            .expect_err("位置规则缺少允许片段必须报错");
        assert!(empty_terms_error.contains("位置源文保留规则缺少允许保留的源文片段"));
    }

    #[test]
    fn quality_scan_keeps_real_line_breaks_inside_short_text() {
        let payload = json!({
            "items": [
                {
                    "location_path": "Items.json/1/description",
                    "item_type": "short_text",
                    "role": null,
                    "original_lines": ["武器スキル\n\\C[14]敵単体に毒を付与\\C[0]"],
                    "translation_lines": ["武器技能\n\\C[14]对敌方单体施加毒\\C[0]"]
                }
            ],
            "text_rules": minimal_text_rules(),
            "source_residual_rules": []
        });
        let output = scan_quality_impl(&payload.to_string()).expect("质检应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value["text_structure_items"], json!([]));
        assert_eq!(value["placeholder_risk_items"], json!([]));
    }

    #[test]
    fn quality_scan_rejects_changed_long_control_candidate_hidden_by_standard_prefix() {
        let payload = json!({
            "items": [
                {
                    "location_path": "Map001.json/1/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": [r"\nn[Name]OK"],
                    "translation_lines": [r"\nn[Other]OK"]
                }
            ],
            "text_rules": minimal_text_rules(),
            "source_residual_rules": []
        });
        let output = scan_quality_impl(&payload.to_string()).expect("质检应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        let reason = value["placeholder_risk_items"][0]["reason"]
            .as_str()
            .expect("占位符风险明细应包含原因");
        assert!(reason.contains(r"\nn[Name]"));
        assert!(reason.contains(r"\nn[Other]"));
    }

    #[test]
    fn quality_scan_rejects_changed_candidate_hidden_by_custom_prefix() {
        let mut text_rules = minimal_text_rules();
        text_rules["custom_placeholder_rules"] = json!([{
            "pattern_text": r"\\X",
            "placeholder_template": "[CUSTOM_X_PREFIX_{index}]"
        }]);
        let payload = json!({
            "items": [
                {
                    "location_path": "Map001.json/1/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": [r"\X[1]こんにちは"],
                    "translation_lines": [r"\X[2]你好"]
                }
            ],
            "text_rules": text_rules,
            "source_residual_rules": []
        });

        let output = scan_quality_impl(&payload.to_string()).expect("质检应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        let reason = value["placeholder_risk_items"][0]["reason"]
            .as_str()
            .expect("占位符风险明细应包含原因");

        assert!(reason.contains(r"\X[1]"));
        assert!(reason.contains("译文: 无"));
    }

    #[test]
    fn structured_placeholder_requires_and_accepts_complete_paired_shell() {
        let mut text_rules = minimal_text_rules();
        text_rules["structured_placeholder_rules"] = json!([
            {
                "rule_name": "mv_name_window",
                "rule_type": "paired_shell",
                "pattern_text": r"(?P<open>\\NW\[)(?P<text>[^\]]+)(?P<close>\])",
                "translatable_group": "text",
                "protected_groups": {
                    "open": "[CUSTOM_MV_NW_OPEN_{index}]",
                    "close": "[CUSTOM_MV_NW_CLOSE_{index}]"
                }
            }
        ]);
        let payload = json!({
            "items": [
                {
                    "location_path": "Map001.json/1/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": [r"\NW[神父]"],
                    "translation_lines": [r"\NW[神父]"]
                }
            ],
            "text_rules": text_rules,
            "source_residual_rules": []
        });
        let output = scan_quality_impl(&payload.to_string()).expect("完整 paired shell 应通过");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value["placeholder_risk_items"], json!([]));
    }

    #[test]
    fn structured_placeholder_rejects_unprotected_gap_inside_full_match() {
        let mut text_rules = minimal_text_rules();
        text_rules["structured_placeholder_rules"] = json!([
            {
                "rule_name": "bad_gap",
                "rule_type": "paired_shell",
                "pattern_text": r"(?P<open>\\NW\[):(?P<text>[^\]]+)(?P<close>\])",
                "translatable_group": "text",
                "protected_groups": {
                    "open": "[CUSTOM_MV_NW_OPEN_{index}]",
                    "close": "[CUSTOM_MV_NW_CLOSE_{index}]"
                }
            }
        ]);
        let payload = json!({
            "items": [
                {
                    "location_path": "Map001.json/1/0/0",
                    "item_type": "long_text",
                    "role": null,
                    "original_lines": [r"\NW[:神父]"],
                    "translation_lines": [r"\NW[:神父]"]
                }
            ],
            "text_rules": text_rules,
            "source_residual_rules": []
        });
        let output = scan_quality_impl(&payload.to_string()).expect("质检应返回结构化问题");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        for bucket in [
            "source_residual_items",
            "text_structure_items",
            "placeholder_risk_items",
        ] {
            assert!(
                value[bucket][0]["reason"]
                    .as_str()
                    .is_some_and(|reason| reason.contains("没有连续覆盖完整匹配"))
            );
        }
    }

    #[test]
    fn protocol_scan_reports_invalid_mode() {
        let payload = json!({
            "entries": [
                {
                    "item": {
                        "location_path": "plugins.js",
                        "item_type": "short_text",
                        "role": null,
                        "original_lines": ["旧"],
                        "translation_lines": ["新"]
                    },
                    "mode": "none",
                    "current_value": null,
                    "path_parts": [],
                    "note_text": null,
                    "tag_name": null
                }
            ]
        });
        let output = scan_write_protocol_impl(&payload.to_string()).expect("协议检查应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value.as_array().map(Vec::len), Some(1));
        assert_eq!(value[0]["location_path"], json!("plugins.js"));
        assert!(
            value[0]["reason"]
                .as_str()
                .is_some_and(|reason| reason.contains("写入协议模式无效"))
        );
    }

    #[test]
    fn protocol_count_scan_returns_only_count() {
        let payload = json!({
            "entries": [
                {
                    "item": {
                        "location_path": "plugins.js",
                        "item_type": "short_text",
                        "role": null,
                        "original_lines": ["旧"],
                        "translation_lines": ["新"]
                    },
                    "mode": "none",
                    "current_value": null,
                    "path_parts": [],
                    "note_text": null,
                    "tag_name": null
                }
            ]
        });
        let output = scan_write_protocol_count_impl(&payload.to_string()).expect("协议计数应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value["write_protocol_count"], json!(1));
        assert!(value.as_object().is_some_and(|object| object.len() == 1));
    }

    #[test]
    fn protocol_scan_uses_real_plugin_translation_text() {
        let payload = json!({
            "entries": [
                {
                    "item": {
                        "location_path": "plugins.js/0/Message",
                        "item_type": "short_text",
                        "role": null,
                        "original_lines": ["原文"],
                        "translation_lines": [r"\\V[1]"]
                    },
                    "mode": "nested",
                    "current_value": "\"原文\"",
                    "path_parts": [],
                    "note_text": null,
                    "tag_name": null
                }
            ]
        });
        let output = scan_write_protocol_impl(&payload.to_string()).expect("协议检查应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value.as_array().map(Vec::len), Some(1));
        assert_eq!(value[0]["location_path"], json!("plugins.js/0/Message"));
        assert!(
            value[0]["reason"]
                .as_str()
                .is_some_and(|reason| reason.contains("控制符被写成会直接显示的字面量"))
        );
    }

    #[test]
    fn protocol_scan_uses_real_note_translation_text() {
        let payload = json!({
            "entries": [
                {
                    "item": {
                        "location_path": "Items.json/1/note/说明",
                        "item_type": "short_text",
                        "role": null,
                        "original_lines": ["原文"],
                        "translation_lines": [r"\\V[1]"]
                    },
                    "mode": "note",
                    "current_value": null,
                    "path_parts": [],
                    "note_text": r#"<说明:"原文">"#,
                    "tag_name": "说明"
                }
            ]
        });
        let output = scan_write_protocol_impl(&payload.to_string()).expect("协议检查应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value.as_array().map(Vec::len), Some(1));
        assert_eq!(value[0]["location_path"], json!("Items.json/1/note/说明"));
        assert!(
            value[0]["reason"]
                .as_str()
                .is_some_and(|reason| reason.contains("控制符被写成会直接显示的字面量"))
        );
    }

    #[test]
    fn note_source_scan_collects_nested_note_fields() {
        let payload = json!({
            "data": {
                "Items.json": [
                    null,
                    {
                        "id": 1,
                        "note": "<说明:旧文本>",
                        "effects": [
                            {"note": "<效果:旧文本>"}
                        ]
                    }
                ],
                "plugins.js": "var $plugins = [];"
            },
            "file_pattern": null
        });
        let output = collect_note_tag_sources_impl(&payload.to_string()).expect("扫描应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value.as_array().map(Vec::len), Some(2));
        assert_eq!(value[0]["location_prefix"], "Items.json/1");
        assert_eq!(value[0]["matches"][0]["tag_name"], "说明");
        assert_eq!(value[0]["matches"][0]["value"], "旧文本");
        assert_eq!(value[0]["matches"][0]["has_value"], true);
        assert_eq!(value[1]["location_prefix"], "Items.json/1/effects/0");
    }
}
