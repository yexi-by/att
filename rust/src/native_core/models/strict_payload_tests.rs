use crate::protocol::invoke_json;
use serde_json::{Value, json};

fn minimal_text_rules() -> Value {
    json!({
        "custom_placeholder_rules": [],
        "structured_placeholder_rules": [],
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

fn translation_item_with_unknown_field() -> Value {
    json!({
        "location_path": "data/Actors.json/1/name",
        "item_type": "short_text",
        "role": null,
        "original_lines": ["原文"],
        "translation_lines": ["译文"],
        "unexpected_nested_field": true
    })
}

fn valid_translation_item() -> Value {
    json!({
        "location_path": "data/Actors.json/1/name",
        "item_type": "short_text",
        "role": null,
        "original_lines": ["原文"],
        "translation_lines": ["译文"]
    })
}

fn versioned_request(operation: &str, request_id: &str, payload: Value) -> String {
    json!({
        "envelope_version": 1,
        "abi_version": 1,
        "schema_version": 1,
        "operation": operation,
        "request_id": request_id,
        "payload": payload
    })
    .to_string()
}

#[test]
fn versioned_operations_reject_unknown_request_fields_at_every_external_dto_boundary() {
    let text_rules = minimal_text_rules();
    let cases = [
        (
            "quality-payload",
            "quality.counts",
            json!({
                "items": [],
                "text_rules": text_rules.clone(),
                "source_residual_rules": [],
                "unexpected_top_level_field": true
            }),
        ),
        (
            "protocol-payload",
            "write_protocol.counts",
            json!({
                "entries": [],
                "unexpected_top_level_field": true
            }),
        ),
        (
            "note-sources-payload",
            "note_sources.collect",
            json!({
                "data": {},
                "file_pattern": null,
                "unexpected_top_level_field": true
            }),
        ),
        (
            "protocol-entry",
            "write_protocol.counts",
            json!({
                "entries": [{
                    "item": valid_translation_item(),
                    "mode": "data",
                    "current_value": null,
                    "path_parts": [],
                    "note_text": null,
                    "tag_name": null,
                    "unexpected_nested_field": true
                }]
            }),
        ),
        (
            "translation-item",
            "quality.counts",
            json!({
                "items": [translation_item_with_unknown_field()],
                "text_rules": text_rules.clone(),
                "source_residual_rules": []
            }),
        ),
        (
            "source-residual-rule",
            "quality.counts",
            json!({
                "items": [],
                "text_rules": text_rules,
                "source_residual_rules": [{
                    "rule_id": "rule-1",
                    "rule_type": "regex",
                    "location_path": "data/Actors.json/1/name",
                    "pattern_text": "原文",
                    "allowed_terms": [],
                    "check_group": "",
                    "reason": "测试",
                    "unexpected_nested_field": true
                }]
            }),
        ),
        (
            "write-back-setting",
            "write_back.plan",
            json!({
                "game_path": "C:/unused-game",
                "db_path": "C:/unused.db",
                "setting": {
                    "unexpected_nested_field": true
                },
                "mode": "quality_gate",
                "confirm_font_overwrite": false
            }),
        ),
    ];

    for (request_id, operation, payload) in cases {
        let response_text = invoke_json(&versioned_request(operation, request_id, payload))
            .expect("版本化 native 调用必须返回 JSON envelope");
        let response: Value = serde_json::from_str(&response_text).expect("native 响应必须是 JSON");

        assert_eq!(response["status"], "error", "case={request_id}");
        assert_eq!(response["request_id"], request_id, "case={request_id}");
        assert_eq!(
            response["error"]["code"], "native_operation_failed",
            "case={request_id}"
        );
        assert_eq!(response["error"]["stage"], "execute", "case={request_id}");
        assert_eq!(response["error"]["retryable"], false, "case={request_id}");
        assert_eq!(response["error"]["details"], json!({}), "case={request_id}");
        let message = response["error"]["message"]
            .as_str()
            .expect("native error.message 必须是字符串");
        assert!(
            message.contains("unknown field") && message.contains("unexpected_"),
            "case={request_id}, message={message}"
        );
    }
}
