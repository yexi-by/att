use crate::native_core::javascript_ast::JavaScriptStringSpan;
use crate::native_core::models::NativeTextRules;
use fancy_regex::Regex as FancyRegex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

pub(super) const PLUGINS_FILE_NAME: &str = "plugins.js";
pub(super) const COMMON_EVENTS_FILE_NAME: &str = "CommonEvents.json";
pub(super) const TROOPS_FILE_NAME: &str = "Troops.json";
pub(super) const SYSTEM_FILE_NAME: &str = "System.json";
pub(super) const DATA_ORIGIN_DIRECTORY_NAME: &str = "data_origin";
pub(super) const PLUGINS_ORIGIN_FILE_NAME: &str = "plugins_origin.js";
pub(super) const PLUGIN_SOURCE_ORIGIN_DIRECTORY_NAME: &str = "plugins_source_origin";

#[derive(Clone, Debug)]
pub(super) struct TranslationItem {
    pub(super) location_path: String,
    pub(super) item_type: String,
    pub(super) role: Option<String>,
    pub(super) original_lines: Vec<String>,
    pub(super) source_line_paths: Vec<String>,
    pub(super) translation_lines: Vec<String>,
}

#[derive(Clone, Debug)]
pub(super) struct PluginSourceReplacement {
    pub(super) selector: String,
    pub(super) item: TranslationItem,
    pub(super) span: JavaScriptStringSpan,
    pub(super) raw_text: String,
    pub(super) visible_text: String,
    pub(super) written_text: String,
    pub(super) source_file_hash: String,
}

#[derive(Clone, Debug)]
pub(super) struct PluginSourceReplacementResult {
    pub(super) replacement: PluginSourceReplacement,
    pub(super) runtime_selector: String,
    pub(super) runtime_line: i64,
}

#[derive(Clone, Debug)]
pub(super) struct PluginSourceTextRule {
    pub(super) file_name: String,
    pub(super) file_hash: String,
    pub(super) selectors: Vec<String>,
    pub(super) excluded_selectors: Vec<String>,
}

#[derive(Serialize)]
pub(super) struct PlannedFile {
    pub(super) target_path: String,
    pub(super) relative_path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(super) content: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(super) content_path: Option<String>,
}

#[derive(Serialize)]
pub(super) struct RuntimeWriteMap {
    pub(super) mapping_kind: String,
    pub(super) location_path: String,
    pub(super) source_file_name: String,
    pub(super) source_selector: String,
    pub(super) source_file_hash: String,
    pub(super) source_text_hash: String,
    pub(super) translation_lines_hash: String,
    pub(super) runtime_file_name: String,
    pub(super) runtime_selector: String,
    pub(super) runtime_file_hash: String,
    pub(super) runtime_text_hash: String,
    pub(super) runtime_line: i64,
    pub(super) created_at: String,
}

#[derive(Serialize)]
pub(super) struct FontReplacementRecordOut {
    pub(super) file_name: String,
    pub(super) value_path: String,
    pub(super) original_text: String,
    pub(super) replaced_text: String,
    pub(super) replacement_font_name: String,
}

#[derive(Serialize)]
pub(super) struct PlanSummary {
    pub(super) data_item_count: usize,
    pub(super) plugin_item_count: usize,
    pub(super) terminology_written_count: usize,
    pub(super) target_font_name: Option<String>,
    pub(super) source_font_count: usize,
    pub(super) replaced_font_reference_count: usize,
    pub(super) font_copied: bool,
    pub(super) planned_file_count: usize,
    pub(super) skipped_file_count: usize,
    pub(super) plugin_source_ast_source_scan_file_count: usize,
    pub(super) plugin_source_ast_runtime_scan_file_count: usize,
    pub(super) plugin_source_runtime_map_count: usize,
}

#[derive(Serialize)]
pub(super) struct WriteBackPlan {
    pub(super) status: String,
    pub(super) mode: String,
    pub(super) files: Vec<PlannedFile>,
    pub(super) plugin_source_runtime_write_maps: Vec<RuntimeWriteMap>,
    pub(super) font_replacement_records: Vec<FontReplacementRecordOut>,
    pub(super) summary: PlanSummary,
    pub(super) timings_ms: BTreeMap<String, u128>,
}

pub(super) struct Layout {
    pub(super) engine_kind: EngineKind,
    pub(super) content_root: PathBuf,
    pub(super) data_dir: PathBuf,
    pub(super) data_origin_dir: PathBuf,
    pub(super) plugins_path: PathBuf,
    pub(super) plugins_origin_path: PathBuf,
    pub(super) plugin_source_dir: PathBuf,
    pub(super) plugin_source_origin_dir: PathBuf,
}

#[derive(Clone, Copy)]
pub(super) enum EngineKind {
    Mz,
    Mv,
}

pub(super) struct MvVirtualNameboxRule {
    pub(super) rule_name: String,
    pub(super) pattern: FancyRegex,
    pub(super) speaker_group: String,
    pub(super) body_group: String,
    pub(super) speaker_policy: MvVirtualSpeakerPolicy,
    pub(super) render_template: String,
    pub(super) group_names: Vec<String>,
}

#[derive(Clone, Copy)]
pub(super) enum MvVirtualSpeakerPolicy {
    Translate,
    Preserve,
    ActorName,
}

pub(super) struct MvVirtualSpeaker {
    pub(super) speaker_line_path: String,
    pub(super) speaker: String,
    pub(super) body_text: String,
    pub(super) matched_text: String,
    pub(super) rule_name: String,
    pub(super) speaker_policy: MvVirtualSpeakerPolicy,
    pub(super) source_speaker_text: String,
    pub(super) render_template: String,
    pub(super) group_values: HashMap<String, String>,
    pub(super) speaker_group: String,
    pub(super) body_group: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct SettingPayload {
    pub(super) quality_text_rules: Option<NativeTextRules>,
    pub(super) replacement_font_path: Option<String>,
    pub(super) source_font_names: Option<Vec<String>>,
    pub(super) allowed_translation_paths: Option<Vec<String>>,
    pub(super) long_text_line_width_limit: Option<usize>,
    pub(super) line_width_count_pattern: Option<String>,
    pub(super) line_split_punctuations: Option<Vec<String>>,
    pub(super) preserve_wrapping_punctuation_pairs: Option<Vec<(String, String)>>,
    pub(super) plan_content_output_dir: Option<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum WritePlanMode {
    WriteBack,
    RebuildActiveRuntime,
    WriteTerminology,
    QualityGate,
}

impl WritePlanMode {
    pub(super) fn parse(value: &str) -> Result<Self, String> {
        match value {
            "write_back" => Ok(Self::WriteBack),
            "rebuild_active_runtime" => Ok(Self::RebuildActiveRuntime),
            "write_terminology" => Ok(Self::WriteTerminology),
            "quality_gate" => Ok(Self::QualityGate),
            _ => Err(format!("写回计划模式无效: {value}")),
        }
    }

    pub(super) fn as_str(self) -> &'static str {
        match self {
            Self::WriteBack => "write_back",
            Self::RebuildActiveRuntime => "rebuild_active_runtime",
            Self::WriteTerminology => "write_terminology",
            Self::QualityGate => "quality_gate",
        }
    }
}

pub(super) struct TextPlanRules {
    pub(super) long_text_line_width_limit: usize,
    pub(super) line_width_count_pattern: regex::Regex,
    pub(super) line_split_punctuations: Vec<String>,
    pub(super) preserve_wrapping_punctuation_pairs: Vec<(String, String)>,
    pub(super) protected_macro_pattern: regex::Regex,
}

impl TextPlanRules {
    pub(super) fn from_payload(payload: &SettingPayload) -> Result<Self, String> {
        let long_text_line_width_limit = payload
            .long_text_line_width_limit
            .ok_or_else(|| "写回计划缺少 long_text_line_width_limit".to_string())?;
        let line_width_count_pattern = payload
            .line_width_count_pattern
            .as_deref()
            .ok_or_else(|| "写回计划缺少 line_width_count_pattern".to_string())?;
        let line_split_punctuations = payload
            .line_split_punctuations
            .clone()
            .ok_or_else(|| "写回计划缺少 line_split_punctuations".to_string())?;
        let preserve_wrapping_punctuation_pairs = payload
            .preserve_wrapping_punctuation_pairs
            .clone()
            .ok_or_else(|| "写回计划缺少 preserve_wrapping_punctuation_pairs".to_string())?;
        Ok(Self {
            long_text_line_width_limit,
            line_width_count_pattern: regex::Regex::new(line_width_count_pattern)
                .map_err(|error| format!("文本行宽计数字符正则无效: {error}"))?,
            line_split_punctuations,
            preserve_wrapping_punctuation_pairs,
            protected_macro_pattern: regex::Regex::new(r"_[A-Z][A-Z0-9]+_")
                .map_err(|error| format!("文本宏保护正则无效: {error}"))?,
        })
    }

    pub(super) fn is_line_width_counted_char(&self, character: char) -> bool {
        self.line_width_count_pattern
            .is_match(&character.to_string())
    }
}

pub(super) struct FontPlanSummary {
    pub(super) target_font_name: Option<String>,
    pub(super) source_font_count: usize,
    pub(super) replaced_reference_count: usize,
    pub(super) copied: bool,
    pub(super) records: Vec<FontReplacementRecordOut>,
}

impl FontPlanSummary {
    pub(super) fn empty() -> Self {
        Self {
            target_font_name: None,
            source_font_count: 0,
            replaced_reference_count: 0,
            copied: false,
            records: Vec::new(),
        }
    }
}
