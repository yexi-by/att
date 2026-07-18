//! 行宽检查。
//!
//! 本模块负责按 RMMZ 文本显示规则统计有效字符宽度，并报告游戏窗口放不下的译文行。

use serde_json::{Value, json};

use super::super::controls::iter_control_sequence_spans_lossy;
use super::super::details::base_detail;
use super::super::models::{CompiledRules, NativeTranslationItem};
use super::super::placeholders::LITERAL_LINE_BREAK_MARKER;
use super::super::rules::PLACEHOLDER_RE;

/// 收集单条译文中所有行宽超限问题。
pub(super) fn collect_overwide_details(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
) -> Vec<Value> {
    let mut details = Vec::new();
    visit_overwide_lines(item, rules, |finding| {
        let mut detail = base_detail(item);
        detail.insert("line_index".to_string(), json!(finding.line_index));
        detail.insert("line".to_string(), json!(finding.line));
        detail.insert("line_width".to_string(), json!(finding.line_width));
        detail.insert(
            "line_width_limit".to_string(),
            json!(finding.effective_limit),
        );
        if let Some(width) = finding.original_width {
            detail.insert("original_line_width".to_string(), json!(width));
            detail.insert(
                "configured_line_width_limit".to_string(),
                json!(rules.long_text_line_width_limit),
            );
        }
        if let Some(width_limit) = finding.original_text_width_limit {
            detail.insert("original_text_width_limit".to_string(), json!(width_limit));
        }
        details.push(Value::Object(detail));
    });
    details
}

/// 统计单条译文的超宽行，不构造报告明细。
pub(super) fn count_overwide_lines(item: &NativeTranslationItem, rules: &CompiledRules) -> usize {
    let mut count = 0;
    visit_overwide_lines(item, rules, |_| count += 1);
    count
}

struct OverwideLineFinding {
    line_index: usize,
    line: String,
    line_width: usize,
    effective_limit: usize,
    original_width: Option<usize>,
    original_text_width_limit: Option<usize>,
}

fn visit_overwide_lines(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    mut visit: impl FnMut(OverwideLineFinding),
) {
    let original_text_width_limit = original_short_text_width_limit(item, rules);
    for (line_index, line, original_line) in iter_line_width_check_lines(item) {
        if line.is_empty() {
            continue;
        }
        let mut effective_limit = rules.long_text_line_width_limit;
        let mut original_width = None;
        if let Some(original_line_text) = original_line {
            let width = count_line_width_chars(&original_line_text, rules);
            effective_limit = effective_limit.max(width);
            original_width = Some(width);
        }
        if let Some(width_limit) = original_text_width_limit {
            effective_limit = effective_limit.max(width_limit);
        }
        let line_width = count_line_width_chars(&line, rules);
        if line_width <= effective_limit {
            continue;
        }
        visit(OverwideLineFinding {
            line_index,
            line,
            line_width,
            effective_limit,
            original_width,
            original_text_width_limit,
        });
    }
}

fn original_short_text_width_limit(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
) -> Option<usize> {
    if item.item_type != "short_text" || item.original_lines.is_empty() {
        return None;
    }
    let original_lines = split_display_line_breaks(&item.original_lines[0]);
    if original_lines.is_empty() {
        return None;
    }
    original_lines
        .iter()
        .map(|line| count_line_width_chars(line, rules))
        .max()
}

fn iter_line_width_check_lines(
    item: &NativeTranslationItem,
) -> Vec<(usize, String, Option<String>)> {
    if item.item_type == "long_text" {
        return item
            .translation_lines
            .iter()
            .enumerate()
            .map(|(index, line)| (index, line.clone(), None))
            .collect();
    }
    if item.item_type != "short_text" || item.translation_lines.is_empty() {
        return Vec::new();
    }
    let original_has_line_break = has_display_line_break(&item.original_lines);
    let translated_text = &item.translation_lines[0];
    if !has_display_line_break(std::slice::from_ref(translated_text)) && !original_has_line_break {
        return Vec::new();
    }
    let translated_lines = split_display_line_breaks(translated_text);
    let original_text = item
        .original_lines
        .first()
        .map(String::as_str)
        .unwrap_or("");
    let original_lines = split_display_line_breaks(original_text);
    translated_lines
        .into_iter()
        .enumerate()
        .map(|(index, line)| {
            let original_line = original_lines.get(index).cloned();
            (index, line, original_line)
        })
        .collect()
}

fn has_display_line_break(lines: &[String]) -> bool {
    lines
        .iter()
        .any(|line| line.contains('\n') || line.contains(LITERAL_LINE_BREAK_MARKER))
}

fn split_display_line_breaks(text: &str) -> Vec<String> {
    text.replace(LITERAL_LINE_BREAK_MARKER, "\n")
        .split('\n')
        .map(str::to_string)
        .collect()
}

fn count_line_width_chars(text: &str, rules: &CompiledRules) -> usize {
    let mut protected_spans: Vec<(usize, usize)> = PLACEHOLDER_RE
        .find_iter(text)
        .map(|matched| (matched.start(), matched.end()))
        .collect();
    protected_spans.extend(
        iter_control_sequence_spans_lossy(text, rules)
            .into_iter()
            .map(|span| (span.start, span.end)),
    );
    text.char_indices()
        .filter(|(byte_index, char_value)| {
            !protected_spans
                .iter()
                .any(|(start, end)| *start <= *byte_index && *byte_index < *end)
                && rules.line_width_count_re.is_match(&char_value.to_string())
        })
        .count()
}
