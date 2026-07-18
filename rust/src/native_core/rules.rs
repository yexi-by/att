//! 文本规则编译。
//!
//! 本模块负责内置正则、用户配置正则和自定义占位符规则的编译与共享。

use fancy_regex::Regex as FancyRegex;
use regex::Regex;
use std::collections::HashSet;
use std::sync::LazyLock;

use super::models::{CompiledCustomRule, CompiledRules, CompiledStructuredRule, NativeTextRules};

pub(crate) static INDEXED_STANDARD_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\\(?P<code>V|N|P|C|I|PX|PY|FS)\[(?P<param>\d+)\]")
        .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static SYMBOL_STANDARD_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\\(?P<symbol>[{}\\$.\|!><^])")
        .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static TERMS_PERCENT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"%(?P<param>\d+)").unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static LITERAL_DYNAMIC_UNICODE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\\(?:u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})")
        .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static LITERAL_DYNAMIC_HEX_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\\x[0-9A-Fa-f]{2}").unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static LITERAL_DYNAMIC_OCTAL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\\[0-7]{1,3}").unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static RAW_BRACKETED_CONTROL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\\[A-Za-z]+\d*\[[A-Za-z0-9_./:-]{1,32}[^\]\w\s\[\]\\]|\\[A-Za-z]+\d*\[[^\]\r\n]{0,64}\]",
    )
    .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static RAW_BARE_CONTROL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\\(?P<code>[A-Za-z]+)\d*")
        .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static RAW_SYMBOL_CONTROL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\\[{}\\$.\|!><^]").unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static PLACEHOLDER_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)\[RMMZ_(?:(?:VARIABLE|ACTOR_NAME|PARTY_MEMBER_NAME|TEXT_COLOR|ICON|TEXT_X_POSITION|TEXT_Y_POSITION|FONT_SIZE|MESSAGE_ARGUMENT)_\d+|LITERAL_(?:UNICODE|HEX|OCTAL)_ESCAPE_[0-9A-F]+)\]|\[RMMZ_REAL_LINE_BREAK\]|\[RMMZ_LITERAL_(?:DOUBLE_QUOTE|SINGLE_QUOTE|SLASH|QUESTION_MARK|BELL|BACKSPACE|FORM_FEED|LINE_BREAK|CARRIAGE_RETURN|TAB|VERTICAL_TAB)\]|\[RMMZ_(?:CURRENCY_UNIT|FONT_LARGER|FONT_SMALLER|BACKSLASH|SHOW_GOLD_WINDOW|WAIT_SHORT|WAIT_LONG|WAIT_INPUT|INSTANT_TEXT_ON|INSTANT_TEXT_OFF|NO_WAIT)\]|\[CUSTOM_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_\d+\]",
    )
    .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static DOUBLED_CONTROL_LITERAL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\\\\(?:[A-Za-z]+\d*(?:\[[^\]\r\n]{0,64}\])?|[{}\\$.\|!><^]|[nrt])")
        .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static NOTE_TAG_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?s)<(?P<tag>[^<>:\r\n]+)(?::(?P<value>[^<>]*))?>")
        .unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) static MAP_FILE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^Map\d+\.json$").unwrap_or_else(|error| panic!("内置正则编译失败: {error}"))
});

pub(crate) fn compile_rules(rules: NativeTextRules) -> Result<CompiledRules, String> {
    let mut custom_placeholder_rules = Vec::new();
    for rule in rules.custom_placeholder_rules {
        let pattern = FancyRegex::new(&rule.pattern_text).map_err(|error| {
            format!(
                "Rust 自定义游戏控制符正则无效: {}: {error}",
                rule.pattern_text
            )
        })?;
        custom_placeholder_rules.push(CompiledCustomRule {
            pattern,
            pattern_text: rule.pattern_text,
            placeholder_template: rule.placeholder_template,
        });
    }

    let mut structured_placeholder_rules = Vec::new();
    for rule in rules.structured_placeholder_rules {
        if rule.rule_type != "paired_shell" {
            return Err(format!(
                "Rust 结构化游戏控制符规则类型当前只支持 paired_shell: {}",
                rule.rule_type
            ));
        }
        let pattern = FancyRegex::new(&rule.pattern_text).map_err(|error| {
            format!(
                "Rust 结构化游戏控制符正则无效: {}: {error}",
                rule.pattern_text
            )
        })?;
        let group_names = pattern
            .capture_names()
            .flatten()
            .map(str::to_string)
            .collect::<HashSet<String>>();
        if !group_names.contains(&rule.translatable_group) {
            return Err(format!(
                "Rust 结构化游戏控制符正则缺少可翻译命名分组: {}",
                rule.translatable_group
            ));
        }
        for group_name in rule.protected_groups.keys() {
            if !group_names.contains(group_name) {
                return Err(format!(
                    "Rust 结构化游戏控制符正则缺少保护命名分组: {}",
                    group_name
                ));
            }
        }
        structured_placeholder_rules.push(CompiledStructuredRule {
            rule_name: rule.rule_name,
            pattern,
            translatable_group: rule.translatable_group,
            protected_groups: rule.protected_groups,
        });
    }

    Ok(CompiledRules {
        custom_placeholder_rules,
        structured_placeholder_rules,
        source_residual_allowed_chars: collect_chars(rules.source_residual_allowed_chars),
        source_residual_allowed_tail_chars: collect_chars(rules.source_residual_allowed_tail_chars),
        allowed_source_residual_terms: rules.allowed_source_residual_terms,
        source_residual_terms_ignore_case: rules.source_residual_terms_ignore_case,
        source_residual_label: rules.source_residual_label,
        source_residual_segment_re: Regex::new(&rules.source_residual_segment_pattern)
            .map_err(|error| format!("Rust 源文残留正则无效: {error}"))?,
        line_width_count_re: Regex::new(&rules.line_width_count_pattern)
            .map_err(|error| format!("Rust 行宽正则无效: {error}"))?,
        residual_escape_sequence_re: Regex::new(&rules.residual_escape_sequence_pattern)
            .map_err(|error| format!("Rust 残留转义正则无效: {error}"))?,
        long_text_line_width_limit: rules.long_text_line_width_limit,
    })
}

pub(crate) fn collect_chars(values: Vec<String>) -> HashSet<char> {
    values
        .into_iter()
        .flat_map(|value| value.chars().collect::<Vec<char>>())
        .collect()
}
