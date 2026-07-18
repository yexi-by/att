//! 翻译质量检查编排。
//!
//! 本模块负责解析质量检查输入、并行调度各类检查，并保持 PyO3 门面的输出协议稳定。

mod line_width;
mod placeholder;
mod residual;
mod structure;

use rayon::prelude::*;
use std::sync::Arc;

use super::details::collect_sorted_details;
use super::models::{PlaceholderBuild, QualityPayload, QualityScanCountOutput, QualityScanOutput};
use super::placeholders::{build_placeholders, mask_translation_controls};
use super::pool::run_with_optional_pool;
use super::rules::compile_rules;
use line_width::{collect_overwide_details, count_overwide_lines};
use placeholder::{collect_placeholder_detail, has_placeholder_issue};
use residual::{collect_residual_detail, has_residual_issue, index_residual_rules};
use structure::{collect_text_structure_detail, has_text_structure_issue};

pub(super) struct PreparedControlState {
    placeholder_build: PlaceholderBuild,
    translation_lines_with_placeholders: Vec<String>,
}

struct ItemQualityFindings {
    source_residual: Option<serde_json::Value>,
    text_structure: Option<serde_json::Value>,
    placeholder_risk: Option<serde_json::Value>,
    overwide_lines: Vec<serde_json::Value>,
}

struct ItemQualityCounts {
    source_residual: usize,
    text_structure: usize,
    placeholder_risk: usize,
    overwide_lines: usize,
}

fn prepare_control_state(
    item: &super::models::NativeTranslationItem,
    rules: &super::models::CompiledRules,
) -> Result<PreparedControlState, String> {
    let placeholder_build = build_placeholders(item, rules)?;
    let translation_lines_with_placeholders =
        mask_translation_controls(item, rules, &placeholder_build)?;
    Ok(PreparedControlState {
        placeholder_build,
        translation_lines_with_placeholders,
    })
}

/// 扫描翻译质量问题并返回稳定 JSON 字符串。
pub fn scan_quality_impl(payload_json: &str) -> Result<String, String> {
    let payload: QualityPayload = serde_json::from_str(payload_json)
        .map_err(|error| format!("Rust 质检输入 JSON 解析失败: {error}"))?;
    let output = scan_quality_items(
        payload.items,
        payload.text_rules,
        payload.source_residual_rules,
    )?;

    serde_json::to_string(&output)
        .map_err(|error| format!("Rust 质检输出 JSON 序列化失败: {error}"))
}

/// 扫描翻译质量问题并只返回计数。
pub fn scan_quality_counts_impl(payload_json: &str) -> Result<String, String> {
    let payload: QualityPayload = serde_json::from_str(payload_json)
        .map_err(|error| format!("Rust 质检计数输入 JSON 解析失败: {error}"))?;
    let counts = scan_quality_counts(
        payload.items,
        payload.text_rules,
        payload.source_residual_rules,
    )?;
    serde_json::to_string(&counts)
        .map_err(|error| format!("Rust 质检计数输出 JSON 序列化失败: {error}"))
}

fn scan_quality_counts(
    items: Vec<super::models::NativeTranslationItem>,
    text_rules: super::models::NativeTextRules,
    source_residual_rules: Vec<super::models::NativeSourceResidualRule>,
) -> Result<QualityScanCountOutput, String> {
    let rules = Arc::new(compile_rules(text_rules)?);
    let residual_rules = Arc::new(index_residual_rules(source_residual_rules)?);
    let items = Arc::new(items);

    run_with_optional_pool(|| {
        items
            .par_iter()
            .map(|item| {
                let prepared = prepare_control_state(item, &rules);
                ItemQualityCounts {
                    source_residual: usize::from(has_residual_issue(
                        item,
                        &rules,
                        &residual_rules,
                        &prepared,
                    )),
                    text_structure: usize::from(has_text_structure_issue(item, &rules, &prepared)),
                    placeholder_risk: usize::from(has_placeholder_issue(item, &rules, &prepared)),
                    overwide_lines: count_overwide_lines(item, &rules),
                }
            })
            .reduce(
                || ItemQualityCounts {
                    source_residual: 0,
                    text_structure: 0,
                    placeholder_risk: 0,
                    overwide_lines: 0,
                },
                |left, right| ItemQualityCounts {
                    source_residual: left.source_residual + right.source_residual,
                    text_structure: left.text_structure + right.text_structure,
                    placeholder_risk: left.placeholder_risk + right.placeholder_risk,
                    overwide_lines: left.overwide_lines + right.overwide_lines,
                },
            )
    })
    .map(|counts| QualityScanCountOutput {
        source_residual_count: counts.source_residual,
        text_structure_count: counts.text_structure,
        placeholder_risk_count: counts.placeholder_risk,
        overwide_line_count: counts.overwide_lines,
    })
}

/// 扫描翻译质量问题并返回结构化明细。
pub(crate) fn scan_quality_items(
    items: Vec<super::models::NativeTranslationItem>,
    text_rules: super::models::NativeTextRules,
    source_residual_rules: Vec<super::models::NativeSourceResidualRule>,
) -> Result<QualityScanOutput, String> {
    let rules = Arc::new(compile_rules(text_rules)?);
    let residual_rules = Arc::new(index_residual_rules(source_residual_rules)?);
    let items = Arc::new(items);

    run_with_optional_pool(|| {
        let findings: Vec<ItemQualityFindings> = items
            .par_iter()
            .map(|item| {
                let prepared = prepare_control_state(item, &rules);
                ItemQualityFindings {
                    source_residual: collect_residual_detail(
                        item,
                        &rules,
                        &residual_rules,
                        &prepared,
                    ),
                    text_structure: collect_text_structure_detail(item, &rules, &prepared),
                    placeholder_risk: collect_placeholder_detail(item, &rules, &prepared),
                    overwide_lines: collect_overwide_details(item, &rules),
                }
            })
            .collect();
        let mut source_residual_items = Vec::new();
        let mut text_structure_items = Vec::new();
        let mut placeholder_risk_items = Vec::new();
        let mut overwide_line_items = Vec::new();
        for finding in findings {
            source_residual_items.extend(finding.source_residual);
            text_structure_items.extend(finding.text_structure);
            placeholder_risk_items.extend(finding.placeholder_risk);
            overwide_line_items.extend(finding.overwide_lines);
        }

        QualityScanOutput {
            source_residual_items: collect_sorted_details(source_residual_items),
            text_structure_items: collect_sorted_details(text_structure_items),
            placeholder_risk_items: collect_sorted_details(placeholder_risk_items),
            overwide_line_items: collect_sorted_details(overwide_line_items),
        }
    })
}
