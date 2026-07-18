//! 源文残留检查。
//!
//! 本模块负责索引允许保留的源文规则，并在译文中识别不应出现的原文片段。

use regex::{Regex, RegexBuilder};
use serde_json::{Value, json};
use std::collections::HashMap;

use super::super::details::base_detail;
use super::super::models::{CompiledRules, NativeSourceResidualRule, NativeTranslationItem};
use super::super::rules::PLACEHOLDER_RE;
use super::PreparedControlState;

#[derive(Debug, Clone)]
pub(super) struct IndexedResidualRules {
    position_rules: HashMap<String, NativeSourceResidualRule>,
    structural_rules: Vec<CompiledStructuralResidualRule>,
}

#[derive(Debug, Clone)]
struct CompiledStructuralResidualRule {
    pattern: Regex,
    allowed_terms: Vec<String>,
    check_group: String,
}

/// 按规则类型索引源文残留例外规则，遇到损坏规则时立即报错。
pub(super) fn index_residual_rules(
    records: Vec<NativeSourceResidualRule>,
) -> Result<IndexedResidualRules, String> {
    let mut position_rules = HashMap::new();
    let mut structural_rules = Vec::new();
    for record in records {
        match record.rule_type.as_str() {
            "structural" => {
                if record.pattern_text.is_empty() || record.check_group.is_empty() {
                    return Err(format!(
                        "结构性源文保留规则缺少 pattern_text 或 check_group: {}",
                        record.rule_id
                    ));
                }
                let pattern = Regex::new(&record.pattern_text).map_err(|error| {
                    format!(
                        "结构性源文保留规则正则损坏: {}: {error}",
                        record.pattern_text
                    )
                })?;
                if !pattern
                    .capture_names()
                    .any(|name| name == Some(record.check_group.as_str()))
                {
                    return Err(format!(
                        "结构性源文保留规则缺少命名分组: {}",
                        record.check_group
                    ));
                }
                structural_rules.push(CompiledStructuralResidualRule {
                    pattern,
                    allowed_terms: record.allowed_terms,
                    check_group: record.check_group,
                });
            }
            "position" => {
                if record.location_path.is_empty() {
                    return Err(format!("位置源文保留规则缺少内部位置: {}", record.rule_id));
                }
                if record.allowed_terms.is_empty() {
                    return Err(format!(
                        "位置源文保留规则缺少允许保留的源文片段: {}",
                        record.rule_id
                    ));
                }
                position_rules.insert(record.location_path.clone(), record);
            }
            unknown_rule_type => {
                return Err(format!(
                    "源文保留规则类型无效: {}: {}",
                    record.rule_id, unknown_rule_type
                ));
            }
        }
    }
    Ok(IndexedResidualRules {
        position_rules,
        structural_rules,
    })
}

/// 收集单条译文的源文残留问题明细。
pub(super) fn collect_residual_detail(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    residual_rules: &IndexedResidualRules,
    prepared: &Result<PreparedControlState, String>,
) -> Option<Value> {
    match check_residual_item(item, rules, residual_rules, prepared) {
        Ok(()) => None,
        Err(reason) => {
            let mut detail = base_detail(item);
            detail.insert("reason".to_string(), json!(reason));
            if let Some(rule) = residual_rules.position_rules.get(&item.location_path)
                && !rule.allowed_terms.is_empty()
            {
                detail.insert("allowed_terms".to_string(), json!(rule.allowed_terms));
                detail.insert("exception_reason".to_string(), json!(rule.reason));
            }
            Some(Value::Object(detail))
        }
    }
}

/// 判断单条译文是否存在源文残留，不构造报告明细。
pub(super) fn has_residual_issue(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    residual_rules: &IndexedResidualRules,
    prepared: &Result<PreparedControlState, String>,
) -> bool {
    match build_checked_residual_lines(item, rules, residual_rules, prepared) {
        Ok(lines) => has_source_residual(&lines, rules),
        Err(_) => true,
    }
}

fn check_residual_item(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    residual_rules: &IndexedResidualRules,
    prepared: &Result<PreparedControlState, String>,
) -> Result<(), String> {
    let checked_lines = build_checked_residual_lines(item, rules, residual_rules, prepared)?;
    check_source_residual(&checked_lines, rules)
}

fn build_checked_residual_lines(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    residual_rules: &IndexedResidualRules,
    prepared: &Result<PreparedControlState, String>,
) -> Result<Vec<String>, String> {
    let allowed_terms = residual_rules
        .position_rules
        .get(&item.location_path)
        .map(|rule| rule.allowed_terms.as_slice())
        .unwrap_or(&[]);
    let control_masked_lines = match prepared {
        Ok(prepared) => &prepared.translation_lines_with_placeholders,
        Err(reason) => return Err(reason.clone()),
    };
    let checked_lines = mask_allowed_terms(
        control_masked_lines,
        allowed_terms,
        rules.source_residual_terms_ignore_case,
    );
    let checked_lines = mask_structural_terms(
        &checked_lines,
        &residual_rules.structural_rules,
        rules.source_residual_terms_ignore_case,
    );
    let checked_lines = mask_allowed_terms(
        &checked_lines,
        &rules.allowed_source_residual_terms,
        rules.source_residual_terms_ignore_case,
    );
    Ok(checked_lines)
}

fn mask_structural_terms(
    lines: &[String],
    structural_rules: &[CompiledStructuralResidualRule],
    ignore_case: bool,
) -> Vec<String> {
    if structural_rules.is_empty() {
        return lines.to_vec();
    }
    lines
        .iter()
        .map(|line| mask_structural_terms_in_line(line, structural_rules, ignore_case))
        .collect()
}

fn mask_structural_terms_in_line(
    line: &str,
    structural_rules: &[CompiledStructuralResidualRule],
    ignore_case: bool,
) -> String {
    let mut masked = line.to_string();
    for rule in structural_rules {
        masked = mask_one_structural_rule_in_line(&masked, rule, ignore_case);
    }
    masked
}

fn mask_one_structural_rule_in_line(
    line: &str,
    rule: &CompiledStructuralResidualRule,
    ignore_case: bool,
) -> String {
    let mut mask_ranges = Vec::new();
    for captures in rule.pattern.captures_iter(line) {
        let Some(full_match) = captures.get(0) else {
            continue;
        };
        let Some(group_match) = captures.name(&rule.check_group) else {
            continue;
        };
        if group_match.as_str().trim().is_empty() {
            continue;
        }
        let outside_ranges = [
            (full_match.start(), group_match.start()),
            (group_match.end(), full_match.end()),
        ];
        for term in &rule.allowed_terms {
            mask_ranges.extend(find_term_ranges_outside_group(
                line,
                term,
                &outside_ranges,
                ignore_case,
            ));
        }
    }
    replace_byte_ranges_with_spaces(line, &mask_ranges)
}

fn find_term_ranges_outside_group(
    line: &str,
    term: &str,
    outside_ranges: &[(usize, usize)],
    ignore_case: bool,
) -> Vec<(usize, usize)> {
    if term.is_empty() {
        return Vec::new();
    }
    let mut ranges = Vec::new();
    let Ok(pattern) = RegexBuilder::new(&regex::escape(term))
        .case_insensitive(ignore_case)
        .build()
    else {
        return ranges;
    };
    for (start, end) in outside_ranges {
        if *start >= *end || *end > line.len() {
            continue;
        }
        let segment = &line[*start..*end];
        for term_match in pattern.find_iter(segment) {
            ranges.push((*start + term_match.start(), *start + term_match.end()));
        }
    }
    ranges
}

fn replace_byte_ranges_with_spaces(line: &str, ranges: &[(usize, usize)]) -> String {
    if ranges.is_empty() {
        return line.to_string();
    }
    line.char_indices()
        .map(|(index, char_value)| {
            if ranges
                .iter()
                .any(|(start, end)| index >= *start && index < *end)
            {
                ' '
            } else {
                char_value
            }
        })
        .collect()
}

fn mask_allowed_terms(
    lines: &[String],
    allowed_terms: &[String],
    ignore_case: bool,
) -> Vec<String> {
    if allowed_terms.is_empty() {
        return lines.to_vec();
    }
    let mut sorted_terms = allowed_terms.to_vec();
    sorted_terms.sort_by_key(|term| usize::MAX - term.chars().count());
    lines
        .iter()
        .map(|line| {
            let mut masked = line.clone();
            for term in &sorted_terms {
                if ignore_case {
                    masked = mask_case_insensitive_term(&masked, term);
                } else {
                    masked = masked.replace(term, " ");
                }
            }
            masked
        })
        .collect()
}

fn mask_case_insensitive_term(text: &str, term: &str) -> String {
    let escaped_term = regex::escape(term);
    let pattern_text = format!(r"(?i)(^|[^A-Za-z0-9_]){escaped_term}($|[^A-Za-z0-9_])");
    let Ok(pattern) = Regex::new(&pattern_text) else {
        return text.to_string();
    };
    pattern
        .replace_all(text, |captures: &regex::Captures<'_>| {
            let left = captures.get(1).map_or("", |matched| matched.as_str());
            let right = captures.get(2).map_or("", |matched| matched.as_str());
            format!("{left} {right}")
        })
        .to_string()
}

fn check_source_residual(lines: &[String], rules: &CompiledRules) -> Result<(), String> {
    for (index, line) in lines.iter().enumerate() {
        let cleaned_line = strip_non_content_for_residual(line, rules);
        let segments: Vec<String> = rules
            .source_residual_segment_re
            .find_iter(&cleaned_line)
            .map(|matched| matched.as_str().to_string())
            .collect();
        if segments.is_empty() {
            continue;
        }

        let has_non_source_content = has_non_source_content(&cleaned_line, rules);
        let mut real_residual_segments = Vec::new();
        for segment in segments {
            let filtered: Vec<char> = segment
                .chars()
                .filter(|char_value| !rules.source_residual_allowed_chars.contains(char_value))
                .collect();
            if filtered.is_empty() {
                if !has_non_source_content {
                    real_residual_segments.push(segment);
                }
                continue;
            }
            if has_non_source_content
                && filtered.iter().all(|char_value| {
                    rules
                        .source_residual_allowed_tail_chars
                        .contains(char_value)
                })
            {
                continue;
            }
            real_residual_segments.push(segment);
        }

        if !real_residual_segments.is_empty() {
            return Err(format!(
                "发现{}残留(第 {} 行): {:?}",
                rules.source_residual_label,
                index + 1,
                real_residual_segments
            ));
        }
    }
    Ok(())
}

fn has_source_residual(lines: &[String], rules: &CompiledRules) -> bool {
    for line in lines {
        let cleaned_line = strip_non_content_for_residual(line, rules);
        let has_non_source_content = has_non_source_content(&cleaned_line, rules);
        for segment in rules.source_residual_segment_re.find_iter(&cleaned_line) {
            let filtered: Vec<char> = segment
                .as_str()
                .chars()
                .filter(|char_value| !rules.source_residual_allowed_chars.contains(char_value))
                .collect();
            if filtered.is_empty() {
                if !has_non_source_content {
                    return true;
                }
                continue;
            }
            if has_non_source_content
                && filtered.iter().all(|char_value| {
                    rules
                        .source_residual_allowed_tail_chars
                        .contains(char_value)
                })
            {
                continue;
            }
            return true;
        }
    }
    false
}

fn strip_non_content_for_residual(text: &str, rules: &CompiledRules) -> String {
    let stripped_placeholders = PLACEHOLDER_RE.replace_all(text, "");
    rules
        .residual_escape_sequence_re
        .replace_all(&stripped_placeholders, " ")
        .to_string()
}

fn has_non_source_content(text: &str, rules: &CompiledRules) -> bool {
    let text_without_source = rules.source_residual_segment_re.replace_all(text, "");
    text_without_source.chars().any(char::is_alphanumeric)
}
