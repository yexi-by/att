//! 占位符风险检查。
//!
//! 本模块负责发现泄漏的项目内部占位符，并校验译文是否完整保留游戏控制符。

use serde_json::{Value, json};

use super::super::details::base_detail;
use super::super::models::{CompiledRules, NativeTranslationItem};
use super::super::placeholders::{
    collect_placeholder_tokens, placeholders_match, verify_placeholders,
};
use super::PreparedControlState;

/// 收集单条译文的占位符风险明细。
pub(super) fn collect_placeholder_detail(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    prepared: &Result<PreparedControlState, String>,
) -> Option<Value> {
    let leaked_tokens = collect_placeholder_tokens(&item.translation_lines);
    if !leaked_tokens.is_empty() {
        let mut sorted_tokens: Vec<String> = leaked_tokens.into_iter().collect();
        sorted_tokens.sort();
        let mut detail = base_detail(item);
        detail.insert(
            "reason".to_string(),
            json!(format!(
                "译文残留项目内部占位符，不能写进游戏文件: {}",
                sorted_tokens.join("、")
            )),
        );
        return Some(Value::Object(detail));
    }

    match prepared {
        Ok(prepared) => match verify_placeholders(
            item,
            rules,
            &prepared.placeholder_build,
            &prepared.translation_lines_with_placeholders,
        ) {
            Ok(()) => None,
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

/// 判断单条译文是否存在占位符风险，不构造报告明细。
pub(super) fn has_placeholder_issue(
    item: &NativeTranslationItem,
    rules: &CompiledRules,
    prepared: &Result<PreparedControlState, String>,
) -> bool {
    if !collect_placeholder_tokens(&item.translation_lines).is_empty() {
        return true;
    }
    match prepared {
        Ok(prepared) => match placeholders_match(
            item,
            rules,
            &prepared.placeholder_build,
            &prepared.translation_lines_with_placeholders,
        ) {
            Ok(matches) => !matches,
            Err(_) => true,
        },
        Err(_) => true,
    }
}
