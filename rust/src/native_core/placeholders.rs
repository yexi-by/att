//! 文本占位符构建与校验。
//!
//! 本模块负责把游戏控制符映射为占位符，并校验译文是否完整保留这些协议片段。

use std::collections::{HashMap, HashSet, VecDeque};

use super::controls::{
    collect_unprotected_control_sequences, format_control_counts, format_custom_placeholder,
    iter_control_sequence_spans, replace_control_sequences,
};
use super::models::{CompiledRules, NativeTranslationItem, PlaceholderBuild, SpanSource};
use super::rules::PLACEHOLDER_RE;

pub(crate) const REAL_LINE_BREAK_MARKER: &str = "\n";

pub(crate) const REAL_LINE_BREAK_PLACEHOLDER: &str = "[RMMZ_REAL_LINE_BREAK]";

pub(crate) const LITERAL_LINE_BREAK_MARKER: &str = r"\n";

pub(crate) const LITERAL_LINE_BREAK_PLACEHOLDER: &str = "[RMMZ_LITERAL_LINE_BREAK]";

pub(crate) fn build_placeholders(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
) -> Result<PlaceholderBuild, String> {
    let mut original_lines_with_placeholders = Vec::new();
    let mut placeholder_map: HashMap<String, String> = HashMap::new();
    let mut placeholder_sources: HashMap<String, SpanSource> = HashMap::new();
    let mut placeholder_counts: HashMap<String, usize> = HashMap::new();
    let mut custom_placeholder_counter = 0usize;
    let mut custom_placeholder_map: HashMap<String, String> = HashMap::new();
    let mut custom_index_by_key: HashMap<String, usize> = HashMap::new();

    for line in &item.original_lines {
        let spans = iter_control_sequence_spans(line, rules)?;
        let mut output = String::new();
        let mut last_end = 0usize;
        for span in spans {
            output.push_str(&line[last_end..span.start]);
            let placeholder = if let Some(standard_placeholder) = &span.placeholder {
                standard_placeholder.clone()
            } else if let Some(custom_template) = &span.custom_template {
                if let Some(custom_index_key) = &span.custom_index_key {
                    let index =
                        if let Some(existing_index) = custom_index_by_key.get(custom_index_key) {
                            *existing_index
                        } else {
                            custom_placeholder_counter += 1;
                            custom_index_by_key
                                .insert(custom_index_key.clone(), custom_placeholder_counter);
                            custom_placeholder_counter
                        };
                    format_custom_placeholder(custom_template, index)
                } else if let Some(existing_placeholder) =
                    custom_placeholder_map.get(&span.original)
                {
                    existing_placeholder.clone()
                } else {
                    custom_placeholder_counter += 1;
                    let formatted =
                        format_custom_placeholder(custom_template, custom_placeholder_counter);
                    custom_placeholder_map.insert(span.original.clone(), formatted.clone());
                    formatted
                }
            } else {
                return Err(format!("无法为控制符生成占位符: {}", span.original));
            };

            if let Some(existing_original) = placeholder_map.get(&placeholder) {
                let existing_source = placeholder_sources.get(&placeholder);
                if existing_original != &span.original
                    && (matches!(
                        existing_source,
                        Some(SpanSource::Custom | SpanSource::Structured)
                    ) || matches!(span.source, SpanSource::Custom | SpanSource::Structured))
                {
                    return Err(format!(
                        "自定义占位符 {} 同时匹配了多个不同片段: {} / {}",
                        placeholder, existing_original, span.original
                    ));
                }
            } else {
                placeholder_map.insert(placeholder.clone(), span.original.clone());
                placeholder_sources.insert(placeholder.clone(), span.source.clone());
            }
            *placeholder_counts.entry(placeholder.clone()).or_insert(0) += 1;
            output.push_str(&placeholder);
            last_end = span.end;
        }
        output.push_str(&line[last_end..]);
        original_lines_with_placeholders.push(replace_real_line_breaks(
            &output,
            &mut placeholder_map,
            &mut placeholder_sources,
            &mut placeholder_counts,
        )?);
    }

    Ok(PlaceholderBuild {
        original_lines_with_placeholders,
        placeholder_map,
        placeholder_counts,
    })
}

pub(crate) fn replace_real_line_breaks(
    line: &str,
    placeholder_map: &mut HashMap<String, String>,
    placeholder_sources: &mut HashMap<String, SpanSource>,
    placeholder_counts: &mut HashMap<String, usize>,
) -> Result<String, String> {
    let real_break_count = line.matches(REAL_LINE_BREAK_MARKER).count();
    if real_break_count == 0 {
        return Ok(line.to_string());
    }
    if let Some(existing_original) = placeholder_map.get(REAL_LINE_BREAK_PLACEHOLDER) {
        if existing_original != REAL_LINE_BREAK_MARKER {
            return Err(format!(
                "占位符 {} 同时匹配了多个不同片段",
                REAL_LINE_BREAK_PLACEHOLDER
            ));
        }
    } else {
        placeholder_map.insert(
            REAL_LINE_BREAK_PLACEHOLDER.to_string(),
            REAL_LINE_BREAK_MARKER.to_string(),
        );
        placeholder_sources.insert(
            REAL_LINE_BREAK_PLACEHOLDER.to_string(),
            SpanSource::Standard,
        );
    }
    *placeholder_counts
        .entry(REAL_LINE_BREAK_PLACEHOLDER.to_string())
        .or_insert(0) += real_break_count;
    Ok(line.replace(REAL_LINE_BREAK_MARKER, REAL_LINE_BREAK_PLACEHOLDER))
}

pub(crate) fn verify_placeholders(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    placeholder_build: &PlaceholderBuild,
    translation_lines_with_placeholders: &[String],
) -> Result<(), String> {
    let mut errors = Vec::new();
    let original_placeholders =
        collect_placeholder_tokens(&placeholder_build.original_lines_with_placeholders);
    let translated_placeholders = collect_placeholder_tokens(translation_lines_with_placeholders);

    let mut extra_placeholders: Vec<String> = translated_placeholders
        .difference(&original_placeholders)
        .cloned()
        .collect();
    if !extra_placeholders.is_empty() {
        extra_placeholders.sort();
        let message = if original_placeholders.is_empty() {
            "原文不包含任何占位符，但译文新增了以下占位符"
        } else {
            "译文新增了原文没有的占位符"
        };
        errors.push(format!("{}: {}", message, extra_placeholders.join("、")));
    }

    if !placeholder_build.placeholder_map.is_empty() {
        let combined_text = translation_lines_with_placeholders.join("").to_lowercase();
        for (placeholder, expected_count) in &placeholder_build.placeholder_counts {
            let actual_count = combined_text.matches(&placeholder.to_lowercase()).count();
            if placeholder.eq_ignore_ascii_case(LITERAL_LINE_BREAK_PLACEHOLDER) {
                if actual_count < *expected_count {
                    errors.push(format!(
                        "占位符 {} 数量不足 (至少需要: {}, 实际: {})",
                        placeholder, expected_count, actual_count
                    ));
                }
                continue;
            }
            if actual_count != *expected_count {
                errors.push(format!(
                    "占位符 {} 数量错误 (期望: {}, 实际: {})",
                    placeholder, expected_count, actual_count
                ));
            }
        }
    }

    let original_raw_controls = collect_unprotected_control_sequences(&item.original_lines, rules)?;
    let translated_raw_controls =
        collect_unprotected_control_sequences(translation_lines_with_placeholders, rules)?;
    if original_raw_controls != translated_raw_controls {
        errors.push(format!(
            "疑似控制符不一致，未被占位符规则覆盖的反斜杠控制片段必须原样保留 (原文: {}; 译文: {})",
            format_control_counts(&original_raw_controls),
            format_control_counts(&translated_raw_controls)
        ));
    }

    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join(";\n"))
    }
}

/// 检查占位符是否一致，但不构造面向报告的错误明细。
pub(crate) fn placeholders_match(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    placeholder_build: &PlaceholderBuild,
    translation_lines_with_placeholders: &[String],
) -> Result<bool, String> {
    let original_placeholders =
        collect_placeholder_tokens(&placeholder_build.original_lines_with_placeholders);
    let translated_placeholders = collect_placeholder_tokens(translation_lines_with_placeholders);
    if translated_placeholders
        .difference(&original_placeholders)
        .next()
        .is_some()
    {
        return Ok(false);
    }

    if !placeholder_build.placeholder_map.is_empty() {
        let combined_text = translation_lines_with_placeholders.join("").to_lowercase();
        for (placeholder, expected_count) in &placeholder_build.placeholder_counts {
            let actual_count = combined_text.matches(&placeholder.to_lowercase()).count();
            if placeholder.eq_ignore_ascii_case(LITERAL_LINE_BREAK_PLACEHOLDER) {
                if actual_count < *expected_count {
                    return Ok(false);
                }
            } else if actual_count != *expected_count {
                return Ok(false);
            }
        }
    }

    let original_raw_controls = collect_unprotected_control_sequences(&item.original_lines, rules)?;
    let translated_raw_controls =
        collect_unprotected_control_sequences(translation_lines_with_placeholders, rules)?;
    Ok(original_raw_controls == translated_raw_controls)
}

pub(crate) fn collect_placeholder_tokens(lines: &[String]) -> HashSet<String> {
    let mut tokens = HashSet::new();
    for line in lines {
        for matched in PLACEHOLDER_RE.find_iter(line) {
            tokens.insert(matched.as_str().to_string());
        }
    }
    tokens
}

pub(crate) fn mask_translation_controls(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    placeholder_build: &PlaceholderBuild,
) -> Result<Vec<String>, String> {
    let mut reverse_map = build_original_placeholder_queues(placeholder_build);
    item.translation_lines
        .iter()
        .map(|line| {
            let mut masked = replace_control_sequences(line, rules, |span| {
                reverse_map
                    .get_mut(&span.original)
                    .and_then(VecDeque::pop_front)
                    .unwrap_or_else(|| "[CUSTOM_UNEXPECTED_1]".to_string())
            })?;
            if placeholder_build
                .placeholder_map
                .get(REAL_LINE_BREAK_PLACEHOLDER)
                .is_some_and(|original| original == REAL_LINE_BREAK_MARKER)
            {
                masked = masked.replace(REAL_LINE_BREAK_MARKER, REAL_LINE_BREAK_PLACEHOLDER);
            }
            Ok(masked)
        })
        .collect()
}

fn build_original_placeholder_queues(
    placeholder_build: &PlaceholderBuild,
) -> HashMap<String, VecDeque<String>> {
    let mut queues: HashMap<String, VecDeque<String>> = HashMap::new();
    for line in &placeholder_build.original_lines_with_placeholders {
        for matched in PLACEHOLDER_RE.find_iter(line) {
            let placeholder = matched.as_str();
            if let Some(original) = placeholder_build.placeholder_map.get(placeholder) {
                queues
                    .entry(original.clone())
                    .or_default()
                    .push_back(placeholder.to_string());
            }
        }
    }
    if !queues.is_empty() {
        return queues;
    }

    for (placeholder, original) in &placeholder_build.placeholder_map {
        let repeat_count = placeholder_build
            .placeholder_counts
            .get(placeholder)
            .copied()
            .unwrap_or(1)
            .max(1);
        let queue = queues.entry(original.clone()).or_default();
        for _index in 0..repeat_count {
            queue.push_back(placeholder.clone());
        }
    }
    queues
}
