//! 文本结构检查。
//!
//! 本模块负责校验译文行数量、真实换行、字面量换行和模型解释性输出。

use serde_json::{Value, json};

use super::super::controls::collect_unexpected_escape_fragments;
use super::super::details::base_detail;
use super::super::models::{CompiledRules, NativeTranslationItem};
use super::super::placeholders::{
    LITERAL_LINE_BREAK_MARKER, LITERAL_LINE_BREAK_PLACEHOLDER, REAL_LINE_BREAK_PLACEHOLDER,
};
use super::PreparedControlState;

/// 收集单条译文的文本结构问题明细。
pub(super) fn collect_text_structure_detail(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    prepared: &Result<PreparedControlState, String>,
) -> Option<Value> {
    match prepared {
        Ok(prepared) => match collect_text_structure_errors(
            item,
            rules,
            &item.translation_lines,
            &prepared.translation_lines_with_placeholders,
            &prepared.placeholder_build.original_lines_with_placeholders,
        ) {
            Ok(errors) if errors.is_empty() => None,
            Ok(errors) => {
                let mut detail = base_detail(item);
                detail.insert("reason".to_string(), json!(errors.join(";\n")));
                Some(Value::Object(detail))
            }
            Err(reason) => {
                let mut detail = base_detail(item);
                detail.insert("reason".to_string(), json!(reason));
                Some(Value::Object(detail))
            }
        },
        Err(reason) => {
            let mut detail = base_detail(item);
            detail.insert("reason".to_string(), json!(reason));
            Some(Value::Object(detail))
        }
    }
}

/// 判断单条译文是否存在结构问题，不构造报告明细。
pub(super) fn has_text_structure_issue(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    prepared: &Result<PreparedControlState, String>,
) -> bool {
    let Ok(prepared) = prepared else {
        return true;
    };
    if has_artifact_error(item, &item.translation_lines, rules) {
        return true;
    }
    if item.item_type != "short_text" {
        return false;
    }
    if item.translation_lines.len() != 1 {
        return true;
    }
    count_real_line_breaks(&prepared.placeholder_build.original_lines_with_placeholders)
        != count_real_line_breaks(&prepared.translation_lines_with_placeholders)
        || count_literal_line_breaks(&prepared.placeholder_build.original_lines_with_placeholders)
            != count_literal_line_breaks(&prepared.translation_lines_with_placeholders)
}

fn has_artifact_error(
    item: &NativeTranslationItem,
    translation_lines: &[String],
    rules: &CompiledRules,
) -> bool {
    let joined_text = translation_lines.join("\n");
    if !item.location_path.is_empty() && joined_text.contains(&item.location_path) {
        return true;
    }
    if translation_lines.iter().any(|line| {
        let stripped = line.trim();
        let lowered = stripped.to_lowercase();
        stripped.starts_with("译文：")
            || stripped.starts_with("译文:")
            || stripped.starts_with("翻译：")
            || stripped.starts_with("翻译:")
            || stripped.contains("以下是翻译")
            || lowered.starts_with("id:")
            || lowered.starts_with("id：")
            || lowered.starts_with("\"id\":")
            || lowered.starts_with("source_lines:")
            || lowered.starts_with("source_lines：")
            || lowered.starts_with("\"source_lines\":")
            || lowered.starts_with("translation_lines:")
            || lowered.starts_with("translation_lines：")
            || lowered.starts_with("\"translation_lines\":")
    }) {
        return true;
    }
    if item.item_type != "long_text" {
        return false;
    }
    let original_empty_count = item
        .original_lines
        .iter()
        .filter(|line| line.trim().is_empty())
        .count();
    let translation_empty_count = translation_lines
        .iter()
        .filter(|line| line.trim().is_empty())
        .count();
    if translation_empty_count > 0
        && (original_empty_count == 0 || translation_empty_count > original_empty_count)
    {
        return true;
    }
    if translation_lines
        .iter()
        .any(|line| has_suspicious_n_prefix(line))
    {
        return true;
    }
    match collect_unexpected_escape_fragments(translation_lines, rules) {
        Ok(fragments) => !fragments.is_empty(),
        Err(_) => true,
    }
}

fn collect_text_structure_errors(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    translation_lines: &[String],
    translation_lines_with_placeholders: &[String],
    original_lines_with_placeholders: &[String],
) -> Result<Vec<String>, String> {
    let mut errors = collect_artifact_errors(item, translation_lines, rules)?;
    if item.item_type != "short_text" {
        return Ok(errors);
    }
    if translation_lines.len() != 1 {
        errors.push(format!(
            "单字段文本必须只提供 1 条中文译文行，当前提供 {} 条",
            translation_lines.len()
        ));
        return Ok(errors);
    }

    let original_real_break_count = count_real_line_breaks(original_lines_with_placeholders);
    let translation_real_break_count = count_real_line_breaks(translation_lines_with_placeholders);
    if original_real_break_count != translation_real_break_count {
        errors.push(format!(
            "译文真实换行数量不一致（原文 {} 个，译文 {} 个）",
            original_real_break_count, translation_real_break_count
        ));
    }

    let original_literal_break_count = count_literal_line_breaks(original_lines_with_placeholders);
    let translation_literal_break_count =
        count_literal_line_breaks(translation_lines_with_placeholders);
    if original_literal_break_count != translation_literal_break_count {
        errors.push(format!(
            "译文字面量换行标记数量不一致（原文 {} 个，译文 {} 个）",
            original_literal_break_count, translation_literal_break_count
        ));
    }
    Ok(errors)
}

fn collect_artifact_errors(
    item: &NativeTranslationItem,
    translation_lines: &[String],
    rules: &CompiledRules,
) -> Result<Vec<String>, String> {
    let mut errors = Vec::new();
    let joined_text = translation_lines.join("\n");
    if !item.location_path.is_empty() && joined_text.contains(&item.location_path) {
        errors.push("译文包含文本在游戏里的内部位置，不能写进游戏文件".to_string());
    }

    for line in translation_lines {
        let stripped = line.trim();
        let lowered = stripped.to_lowercase();
        if stripped.starts_with("译文：")
            || stripped.starts_with("译文:")
            || stripped.starts_with("翻译：")
            || stripped.starts_with("翻译:")
        {
            errors.push("译文包含明显解释性前缀，不是可写入游戏的正文".to_string());
            break;
        }
        if stripped.contains("以下是翻译") {
            errors.push("译文包含明显解释性说明，不是可写入游戏的正文".to_string());
            break;
        }
        if lowered.starts_with("id:")
            || lowered.starts_with("id：")
            || lowered.starts_with("\"id\":")
            || lowered.starts_with("source_lines:")
            || lowered.starts_with("source_lines：")
            || lowered.starts_with("\"source_lines\":")
            || lowered.starts_with("translation_lines:")
            || lowered.starts_with("translation_lines：")
            || lowered.starts_with("\"translation_lines\":")
        {
            errors.push("译文包含模型输出协议字段，不是可写入游戏的正文".to_string());
            break;
        }
    }
    if item.item_type == "long_text" {
        errors.extend(collect_unexpected_empty_line_errors(
            item,
            translation_lines,
        ));
        errors.extend(collect_suspicious_n_prefix_errors(translation_lines));
        errors.extend(
            collect_unexpected_escape_fragments(translation_lines, rules)?
                .into_iter()
                .map(|fragment| {
                    if fragment.trailing {
                        format!(
                            "译文第 {} 行存在行尾裸反斜杠，疑似转义或换行标记被拆坏",
                            fragment.line_number
                        )
                    } else {
                        format!(
                            "译文第 {} 行存在异常反斜杠片段: {}",
                            fragment.line_number, fragment.fragment
                        )
                    }
                }),
        );
    }
    Ok(errors)
}

fn collect_unexpected_empty_line_errors(
    item: &NativeTranslationItem,
    translation_lines: &[String],
) -> Vec<String> {
    let original_empty_count = item
        .original_lines
        .iter()
        .filter(|line| line.trim().is_empty())
        .count();
    let translation_empty_line_numbers: Vec<usize> = translation_lines
        .iter()
        .enumerate()
        .filter_map(|(index, line)| {
            if line.trim().is_empty() {
                Some(index + 1)
            } else {
                None
            }
        })
        .collect();
    if translation_empty_line_numbers.is_empty() {
        return Vec::new();
    }
    if original_empty_count == 0 {
        return vec![format!(
            "原文没有空行，但译文第 {} 行是空行",
            translation_empty_line_numbers
                .iter()
                .map(usize::to_string)
                .collect::<Vec<String>>()
                .join("、")
        )];
    }
    if translation_empty_line_numbers.len() > original_empty_count {
        return vec![format!(
            "译文空行数量超过原文空行数量（原文 {} 行，译文 {} 行）",
            original_empty_count,
            translation_empty_line_numbers.len()
        )];
    }
    Vec::new()
}

fn collect_suspicious_n_prefix_errors(translation_lines: &[String]) -> Vec<String> {
    translation_lines
        .iter()
        .enumerate()
        .filter_map(|(index, line)| {
            if has_suspicious_n_prefix(line) {
                Some(format!(
                    "译文第 {} 行以异常 n 开头，疑似字面量换行标记 \\n 被拆坏",
                    index + 1
                ))
            } else {
                None
            }
        })
        .collect()
}

fn has_suspicious_n_prefix(line: &str) -> bool {
    let mut chars = line.trim_start().chars();
    matches!(chars.next(), Some('n')) && chars.next().is_some_and(is_cjk_or_fullwidth_char)
}

fn is_cjk_or_fullwidth_char(char_value: char) -> bool {
    matches!(
        char_value as u32,
        0x3000..=0x303f | 0x3400..=0x9fff | 0xff00..=0xffef
    )
}

fn count_real_line_breaks(lines: &[String]) -> usize {
    if lines.is_empty() {
        return 0;
    }
    lines.join("\n").matches('\n').count()
        + lines
            .iter()
            .map(|line| line.matches(REAL_LINE_BREAK_PLACEHOLDER).count())
            .sum::<usize>()
}

fn count_literal_line_breaks(lines: &[String]) -> usize {
    lines
        .iter()
        .map(|line| {
            line.matches(LITERAL_LINE_BREAK_MARKER).count()
                + line.matches(LITERAL_LINE_BREAK_PLACEHOLDER).count()
        })
        .sum()
}
