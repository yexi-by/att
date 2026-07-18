//! 疑似控制符 occurrence 批量扫描。
//!
//! 本模块在一次 native 调用中编译规则，并使用共享 Rayon 池并行扫描全部正文行。

use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::sync::Arc;

use super::controls::iter_control_sequence_candidate_coverages;
use super::models::{CompiledRules, NativeTextRules};
use super::pool::run_with_optional_pool;
use super::rules::compile_rules;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PlaceholderCandidateScanPayload {
    texts: Vec<PlaceholderCandidateTextInput>,
    text_rules: NativeTextRules,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PlaceholderCandidateTextInput {
    location_path: String,
    line_number: usize,
    text: String,
}

#[derive(Debug, Serialize)]
struct PlaceholderCandidateScanOutput {
    occurrences: Vec<PlaceholderCandidateOccurrence>,
}

#[derive(Debug, Serialize)]
struct PlaceholderCandidateOccurrence {
    location_path: String,
    line_number: usize,
    start_index: usize,
    end_index: usize,
    raw_marker: String,
    marker: String,
    coverage_kind: &'static str,
    matched_rule_ids: Vec<String>,
}

pub(crate) fn scan_placeholder_candidates_impl(payload_json: &str) -> Result<String, String> {
    let payload: PlaceholderCandidateScanPayload = serde_json::from_str(payload_json)
        .map_err(|error| format!("疑似控制符扫描输入 JSON 无效: {error}"))?;
    let rules = Arc::new(compile_rules(payload.text_rules)?);
    let per_text = run_with_optional_pool(|| {
        payload
            .texts
            .par_iter()
            .map(|input| scan_text(input, &rules))
            .collect::<Result<Vec<_>, String>>()
    })??;
    let output = PlaceholderCandidateScanOutput {
        occurrences: per_text.into_iter().flatten().collect(),
    };
    serde_json::to_string(&output)
        .map_err(|error| format!("疑似控制符扫描输出 JSON 序列化失败: {error}"))
}

fn scan_text(
    input: &PlaceholderCandidateTextInput,
    rules: &CompiledRules,
) -> Result<Vec<PlaceholderCandidateOccurrence>, String> {
    if input.location_path.is_empty() {
        return Err("疑似控制符扫描 location_path 不能为空".to_string());
    }
    if input.line_number == 0 {
        return Err("疑似控制符扫描 line_number 必须大于零".to_string());
    }
    let coverages = iter_control_sequence_candidate_coverages(&input.text, rules)
        .map_err(|error| format!("{}#{}: {error}", input.location_path, input.line_number))?;
    Ok(coverages
        .into_iter()
        .map(|coverage| PlaceholderCandidateOccurrence {
            location_path: input.location_path.clone(),
            line_number: input.line_number,
            start_index: byte_to_char_index(&input.text, coverage.start),
            end_index: byte_to_char_index(&input.text, coverage.end),
            raw_marker: coverage.raw_marker,
            marker: coverage.marker,
            coverage_kind: coverage.coverage_kind.as_str(),
            matched_rule_ids: coverage.matched_rule_ids,
        })
        .collect())
}

fn byte_to_char_index(text: &str, byte_index: usize) -> usize {
    text[..byte_index].chars().count()
}

#[cfg(test)]
mod tests {
    use super::scan_placeholder_candidates_impl;
    use crate::native_core::pool;
    use serde_json::{Value, json};

    fn text_rules() -> Value {
        json!({
            "custom_placeholder_rules": [
                {
                    "pattern_text": r"\\NW\[\\N\[\d+\]\]",
                    "placeholder_template": "[CUSTOM_DYNAMIC_NW_MARKER_{index}]"
                },
                {
                    "pattern_text": r"\\SV\[[^\]\r\n]+\]",
                    "placeholder_template": "[CUSTOM_PLUGIN_SV_MARKER_{index}]"
                }
            ],
            "structured_placeholder_rules": [
                {
                    "rule_name": "MV_NW",
                    "rule_type": "paired_shell",
                    "pattern_text": r"(?P<open>\\NW\[)(?P<text>[^\\\]\r\n]+?)(?P<close>\])",
                    "translatable_group": "text",
                    "protected_groups": {
                        "open": "[CUSTOM_MV_NW_OPEN_{index}]",
                        "close": "[CUSTOM_MV_NW_CLOSE_{index}]"
                    }
                }
            ],
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

    fn scan(thread_count: &str) -> String {
        let payload = json!({
            "texts": [
                {
                    "location_path": "CommonEvents.json/1/0",
                    "line_number": 1,
                    "text": r"\NW[神父]"
                },
                {
                    "location_path": "CommonEvents.json/1/0",
                    "line_number": 2,
                    "text": r"\NW[\N[1]]"
                },
                {
                    "location_path": "CommonEvents.json/1/0",
                    "line_number": 3,
                    "text": r"\NW[神父]\SV[A0001]"
                }
            ],
            "text_rules": text_rules()
        });
        pool::with_thread_count_override_for_test(Some(thread_count), || {
            scan_placeholder_candidates_impl(&payload.to_string()).expect("批量扫描应成功")
        })
    }

    #[test]
    fn output_is_identical_with_one_two_and_four_threads() {
        let one_thread = scan("1");
        assert_eq!(scan("2"), one_thread);
        assert_eq!(scan("4"), one_thread);

        let output: Value = serde_json::from_str(&one_thread).expect("输出应为 JSON");
        let occurrences = output["occurrences"].as_array().expect("应返回 occurrence");
        assert_eq!(occurrences.len(), 4);
        assert_eq!(occurrences[0]["coverage_kind"], "structured");
        assert_eq!(occurrences[0]["matched_rule_ids"], json!(["MV_NW"]));
        assert_eq!(occurrences[1]["coverage_kind"], "custom");
        assert_eq!(occurrences[3]["raw_marker"], r"\SV[A0001]");
    }

    #[test]
    fn ranges_use_unicode_character_indices() {
        let payload = json!({
            "texts": [{
                "location_path": "Map001.json/1/0",
                "line_number": 7,
                "text": r"前😀\X[1]后"
            }],
            "text_rules": {
                "custom_placeholder_rules": [],
                "structured_placeholder_rules": [],
                "source_residual_allowed_chars": [],
                "source_residual_allowed_tail_chars": [],
                "source_residual_segment_pattern": r"[\p{Han}]+",
                "source_residual_label": "日文",
                "allowed_source_residual_terms": [],
                "source_residual_terms_ignore_case": false,
                "line_width_count_pattern": r"[^\s]",
                "residual_escape_sequence_pattern": r"\\[A-Za-z0-9_]+\[[^\]]*\]",
                "long_text_line_width_limit": 999
            }
        });
        let raw = scan_placeholder_candidates_impl(&payload.to_string()).expect("扫描应成功");
        let output: Value = serde_json::from_str(&raw).expect("输出应为 JSON");
        let occurrence = &output["occurrences"][0];
        assert_eq!(occurrence["start_index"], 2);
        assert_eq!(occurrence["end_index"], 7);
        assert_eq!(occurrence["raw_marker"], r"\X[1]");
        assert_eq!(occurrence["coverage_kind"], "uncovered");
    }

    #[test]
    fn mixed_structured_coverage_keeps_uncovered_occurrence() {
        let mut rules = text_rules();
        rules["custom_placeholder_rules"] = json!([]);
        rules["structured_placeholder_rules"] = json!([{
            "rule_name": "SAFE_X",
            "rule_type": "paired_shell",
            "pattern_text": r"(?<=ALLOW:)(?P<open>\\X\[)(?P<text>1)(?P<close>\])",
            "translatable_group": "text",
            "protected_groups": {
                "open": "[CUSTOM_SAFE_X_OPEN_{index}]",
                "close": "[CUSTOM_SAFE_X_CLOSE_{index}]"
            }
        }]);
        let payload = json!({
            "texts": [
                {"location_path": "CommonEvents.json/1/0", "line_number": 1, "text": r"ALLOW:\X[1]"},
                {"location_path": "CommonEvents.json/1/0", "line_number": 2, "text": r"DENY:\X[1]"}
            ],
            "text_rules": rules
        });
        let raw = scan_placeholder_candidates_impl(&payload.to_string()).expect("扫描应成功");
        let output: Value = serde_json::from_str(&raw).expect("输出应为 JSON");
        assert_eq!(output["occurrences"][0]["coverage_kind"], "structured");
        assert_eq!(output["occurrences"][1]["coverage_kind"], "uncovered");
    }

    #[test]
    fn custom_prefix_overlap_does_not_cover_complete_candidate() {
        let mut rules = text_rules();
        rules["custom_placeholder_rules"] = json!([{
            "pattern_text": r"\\X",
            "placeholder_template": "[CUSTOM_X_PREFIX_{index}]"
        }]);
        rules["structured_placeholder_rules"] = json!([]);
        let payload = json!({
            "texts": [{
                "location_path": "CommonEvents.json/1/0",
                "line_number": 1,
                "text": r"\X[1]"
            }],
            "text_rules": rules
        });

        let raw = scan_placeholder_candidates_impl(&payload.to_string()).expect("扫描应成功");
        let output: Value = serde_json::from_str(&raw).expect("输出应为 JSON");
        let occurrence = &output["occurrences"][0];

        assert_eq!(occurrence["raw_marker"], r"\X[1]");
        assert_eq!(occurrence["coverage_kind"], "uncovered");
        assert_eq!(occurrence["matched_rule_ids"], json!([]));
    }

    #[test]
    fn complete_custom_match_covers_candidate() {
        let mut rules = text_rules();
        rules["custom_placeholder_rules"] = json!([{
            "pattern_text": r"\\X\[[^\]\r\n]+\]",
            "placeholder_template": "[CUSTOM_X_MARKER_{index}]"
        }]);
        rules["structured_placeholder_rules"] = json!([]);
        let payload = json!({
            "texts": [{
                "location_path": "CommonEvents.json/1/0",
                "line_number": 1,
                "text": r"\X[1]"
            }],
            "text_rules": rules
        });

        let raw = scan_placeholder_candidates_impl(&payload.to_string()).expect("扫描应成功");
        let output: Value = serde_json::from_str(&raw).expect("输出应为 JSON");
        let occurrence = &output["occurrences"][0];

        assert_eq!(occurrence["raw_marker"], r"\X[1]");
        assert_eq!(occurrence["coverage_kind"], "custom");
        assert_eq!(
            occurrence["matched_rule_ids"],
            json!([r"\\X\[[^\]\r\n]+\]"])
        );
    }

    #[test]
    fn structured_inner_match_does_not_hide_outer_candidate() {
        let mut rules = text_rules();
        rules["custom_placeholder_rules"] = json!([]);
        rules["structured_placeholder_rules"] = json!([{
            "rule_name": "INNER_NW",
            "rule_type": "paired_shell",
            "pattern_text": r"(?P<open>\\NW\[)(?P<text>[^\]\r\n]+)(?P<close>\])",
            "translatable_group": "text",
            "protected_groups": {
                "open": "[CUSTOM_INNER_NW_OPEN_{index}]",
                "close": "[CUSTOM_INNER_NW_CLOSE_{index}]"
            }
        }]);
        let payload = json!({
            "texts": [{
                "location_path": "CommonEvents.json/1/0",
                "line_number": 1,
                "text": r"\OUT[\NW[神父]]"
            }],
            "text_rules": rules
        });
        let raw = scan_placeholder_candidates_impl(&payload.to_string()).expect("扫描应成功");
        let output: Value = serde_json::from_str(&raw).expect("输出应为 JSON");
        assert_eq!(output["occurrences"][0]["raw_marker"], r"\OUT[\NW[神父]");
        assert_eq!(output["occurrences"][0]["coverage_kind"], "uncovered");
    }
}
