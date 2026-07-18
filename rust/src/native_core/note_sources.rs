//! Note 标签来源扫描。
//!
//! 本模块负责遍历 RPG Maker data JSON，收集可供规则判断的 note 字段文本来源。

use serde_json::Value;

use super::models::{NoteSourcesPayload, NoteTagMatchOutput, NoteTagSourceOutput};
use super::rules::{MAP_FILE_RE, NOTE_TAG_RE};

pub fn collect_note_tag_sources_impl(payload_json: &str) -> Result<String, String> {
    let payload: NoteSourcesPayload = serde_json::from_str(payload_json)
        .map_err(|error| format!("Rust Note 标签输入 JSON 解析失败: {error}"))?;
    let file_pattern = payload.file_pattern.as_deref();
    let mut file_names: Vec<String> = payload
        .data
        .iter()
        .filter_map(|(file_name, value)| {
            if file_name == "plugins.js" || !file_name.ends_with(".json") || value.is_string() {
                return None;
            }
            if let Some(pattern) = file_pattern
                && !file_pattern_matches(file_name, pattern)
            {
                return None;
            }
            Some(file_name.clone())
        })
        .collect();
    file_names.sort();

    let mut sources = Vec::new();
    for file_name in file_names {
        if let Some(value) = payload.data.get(&file_name) {
            collect_note_tag_sources_in_value(&file_name, value, &mut Vec::new(), &mut sources);
        }
    }
    serde_json::to_string(&sources)
        .map_err(|error| format!("Rust Note 标签输出 JSON 序列化失败: {error}"))
}

pub(crate) fn collect_note_tag_sources_in_value(
    file_name: &str,
    value: &Value,
    owner_path: &mut Vec<String>,
    sources: &mut Vec<NoteTagSourceOutput>,
) {
    if let Some(object) = value.as_object() {
        if let Some(note_value) = object.get("note").and_then(Value::as_str)
            && !note_value.is_empty()
        {
            sources.push(NoteTagSourceOutput {
                file_name: file_name.to_string(),
                owner_path: owner_path.clone(),
                note_text: note_value.to_string(),
                location_prefix: format_location_prefix(file_name, owner_path),
                matches: collect_note_tag_matches(note_value),
            });
        }
        for (key, child_value) in object {
            if key == "note" {
                continue;
            }
            owner_path.push(key.clone());
            collect_note_tag_sources_in_value(file_name, child_value, owner_path, sources);
            let _ = owner_path.pop();
        }
        return;
    }

    if let Some(array) = value.as_array() {
        for (index, child_value) in array.iter().enumerate() {
            if child_value.is_null() {
                continue;
            }
            owner_path.push(index.to_string());
            collect_note_tag_sources_in_value(file_name, child_value, owner_path, sources);
            let _ = owner_path.pop();
        }
    }
}

fn collect_note_tag_matches(note_text: &str) -> Vec<NoteTagMatchOutput> {
    NOTE_TAG_RE
        .captures_iter(note_text)
        .filter_map(|captures| {
            let tag_name = captures.name("tag")?.as_str().trim();
            if tag_name.is_empty() {
                return None;
            }
            let value = captures.name("value");
            Some(NoteTagMatchOutput {
                tag_name: tag_name.to_string(),
                value: value.map_or_else(String::new, |matched| matched.as_str().to_string()),
                has_value: value.is_some(),
            })
        })
        .collect()
}

pub(crate) fn format_location_prefix(file_name: &str, owner_path: &[String]) -> String {
    if owner_path.is_empty() {
        return file_name.to_string();
    }
    format!("{}/{}", file_name, owner_path.join("/"))
}

pub(crate) fn file_pattern_matches(file_name: &str, file_pattern: &str) -> bool {
    if file_pattern == "Map*.json" {
        return MAP_FILE_RE.is_match(file_name);
    }
    wildcard_match(file_name.as_bytes(), file_pattern.as_bytes())
}

pub(crate) fn wildcard_match(text: &[u8], pattern: &[u8]) -> bool {
    let mut previous = vec![false; text.len() + 1];
    previous[0] = true;
    for pattern_byte in pattern {
        let mut current = vec![false; text.len() + 1];
        if *pattern_byte == b'*' {
            current[0] = previous[0];
        }
        for text_index in 1..=text.len() {
            current[text_index] = match *pattern_byte {
                b'*' => current[text_index - 1] || previous[text_index],
                b'?' => previous[text_index - 1],
                _ => previous[text_index - 1] && *pattern_byte == text[text_index - 1],
            };
        }
        previous = current;
    }
    previous[text.len()]
}
