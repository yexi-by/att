//! Rust 原生核心数据模型。
//!
//! 本模块定义 Python JSON 边界进入 Rust 后使用的载荷、输出和内部共享结构。

use fancy_regex::Regex as FancyRegex;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct QualityPayload {
    pub(crate) items: Vec<NativeTranslationItem>,
    pub(crate) text_rules: NativeTextRules,
    pub(crate) source_residual_rules: Vec<NativeSourceResidualRule>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProtocolPayload {
    pub(crate) entries: Vec<ProtocolEntry>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NoteSourcesPayload {
    pub(crate) data: HashMap<String, Value>,
    pub(crate) file_pattern: Option<String>,
}

#[derive(Debug, Serialize)]
pub(crate) struct NoteTagSourceOutput {
    pub(crate) file_name: String,
    pub(crate) owner_path: Vec<String>,
    pub(crate) note_text: String,
    pub(crate) location_prefix: String,
    pub(crate) matches: Vec<NoteTagMatchOutput>,
}

#[derive(Debug, Serialize)]
pub(crate) struct NoteTagMatchOutput {
    pub(crate) tag_name: String,
    pub(crate) value: String,
    pub(crate) has_value: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProtocolEntry {
    pub(crate) item: NativeTranslationItem,
    pub(crate) mode: String,
    pub(crate) current_value: Option<Value>,
    pub(crate) path_parts: Vec<String>,
    pub(crate) note_text: Option<String>,
    pub(crate) tag_name: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativeTranslationItem {
    pub(crate) location_path: String,
    pub(crate) item_type: String,
    pub(crate) role: Option<String>,
    pub(crate) original_lines: Vec<String>,
    pub(crate) translation_lines: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativeTextRules {
    pub(crate) custom_placeholder_rules: Vec<NativeCustomPlaceholderRule>,
    #[serde(default)]
    pub(crate) structured_placeholder_rules: Vec<NativeStructuredPlaceholderRule>,
    pub(crate) source_residual_allowed_chars: Vec<String>,
    pub(crate) source_residual_allowed_tail_chars: Vec<String>,
    pub(crate) source_residual_segment_pattern: String,
    pub(crate) source_residual_label: String,
    pub(crate) allowed_source_residual_terms: Vec<String>,
    pub(crate) source_residual_terms_ignore_case: bool,
    pub(crate) line_width_count_pattern: String,
    pub(crate) residual_escape_sequence_pattern: String,
    pub(crate) long_text_line_width_limit: usize,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativeCustomPlaceholderRule {
    pub(crate) pattern_text: String,
    pub(crate) placeholder_template: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativeStructuredPlaceholderRule {
    pub(crate) rule_name: String,
    pub(crate) rule_type: String,
    pub(crate) pattern_text: String,
    pub(crate) translatable_group: String,
    pub(crate) protected_groups: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativeSourceResidualRule {
    pub(crate) rule_id: String,
    pub(crate) rule_type: String,
    pub(crate) location_path: String,
    pub(crate) pattern_text: String,
    pub(crate) allowed_terms: Vec<String>,
    pub(crate) check_group: String,
    pub(crate) reason: String,
}

#[derive(Debug, Serialize)]
pub(crate) struct QualityScanOutput {
    pub(crate) source_residual_items: Vec<Value>,
    pub(crate) text_structure_items: Vec<Value>,
    pub(crate) placeholder_risk_items: Vec<Value>,
    pub(crate) overwide_line_items: Vec<Value>,
}

#[derive(Debug, Serialize)]
pub(crate) struct QualityScanCountOutput {
    pub(crate) source_residual_count: usize,
    pub(crate) text_structure_count: usize,
    pub(crate) placeholder_risk_count: usize,
    pub(crate) overwide_line_count: usize,
}

#[derive(Debug, Clone)]
pub(crate) struct CompiledRules {
    pub(crate) custom_placeholder_rules: Vec<CompiledCustomRule>,
    pub(crate) structured_placeholder_rules: Vec<CompiledStructuredRule>,
    pub(crate) source_residual_allowed_chars: HashSet<char>,
    pub(crate) source_residual_allowed_tail_chars: HashSet<char>,
    pub(crate) allowed_source_residual_terms: Vec<String>,
    pub(crate) source_residual_terms_ignore_case: bool,
    pub(crate) source_residual_label: String,
    pub(crate) source_residual_segment_re: Regex,
    pub(crate) line_width_count_re: Regex,
    pub(crate) residual_escape_sequence_re: Regex,
    pub(crate) long_text_line_width_limit: usize,
}

#[derive(Debug, Clone)]
pub(crate) struct CompiledCustomRule {
    pub(crate) pattern: FancyRegex,
    pub(crate) pattern_text: String,
    pub(crate) placeholder_template: String,
}

#[derive(Debug, Clone)]
pub(crate) struct CompiledStructuredRule {
    pub(crate) rule_name: String,
    pub(crate) pattern: FancyRegex,
    pub(crate) translatable_group: String,
    pub(crate) protected_groups: HashMap<String, String>,
}

#[derive(Debug, Clone)]
pub(crate) struct ControlSpan {
    pub(crate) start: usize,
    pub(crate) end: usize,
    pub(crate) original: String,
    pub(crate) placeholder: Option<String>,
    pub(crate) custom_template: Option<String>,
    pub(crate) custom_index_key: Option<String>,
    pub(crate) source: SpanSource,
    pub(crate) priority: i32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum SpanSource {
    Standard,
    Custom,
    Structured,
}

#[derive(Debug)]
pub(crate) struct PlaceholderBuild {
    pub(crate) original_lines_with_placeholders: Vec<String>,
    pub(crate) placeholder_map: HashMap<String, String>,
    pub(crate) placeholder_counts: HashMap<String, usize>,
}

#[cfg(test)]
mod strict_payload_tests;
