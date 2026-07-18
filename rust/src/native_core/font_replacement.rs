//! 写回计划内部的字体引用转换。

use serde_json::{Map, Value};

pub(crate) fn replace_font_names_in_text(
    text: &str,
    old_font_names: &[String],
    replacement_font_name: &str,
) -> Option<(String, usize)> {
    if !old_font_names
        .iter()
        .any(|old_font_name| text.contains(old_font_name))
    {
        return None;
    }
    if let Some(replaced_text) =
        replace_complete_font_reference_text(text, old_font_names, replacement_font_name)
    {
        return Some((replaced_text, 1));
    }
    replace_font_references_in_encoded_json_text(text, old_font_names, replacement_font_name)
}

pub(crate) fn replace_complete_font_reference_text(
    text: &str,
    old_font_names: &[String],
    replacement_font_name: &str,
) -> Option<String> {
    let stripped_text = text.trim();
    if stripped_text.is_empty() {
        return None;
    }

    let leading_len = text.len() - text.trim_start().len();
    let trailing_start = text.trim_end().len();
    let leading_text = &text[..leading_len];
    let trailing_text = &text[trailing_start..];
    for old_font_name in old_font_names {
        if stripped_text == old_font_name {
            return Some(format!(
                "{leading_text}{replacement_font_name}{trailing_text}"
            ));
        }
        let slash_index = stripped_text.rfind('/');
        let backslash_index = stripped_text.rfind('\\');
        let separator_index = match (slash_index, backslash_index) {
            (Some(left), Some(right)) => Some(left.max(right)),
            (Some(left), None) => Some(left),
            (None, Some(right)) => Some(right),
            (None, None) => None,
        };
        if let Some(index) = separator_index {
            let reference_name = &stripped_text[index + 1..];
            if reference_name == old_font_name {
                return Some(format!(
                    "{}{}{}{}",
                    leading_text,
                    &stripped_text[..index + 1],
                    replacement_font_name,
                    trailing_text
                ));
            }
        }
    }
    None
}

pub(crate) fn replace_font_references_in_encoded_json_text(
    text: &str,
    old_font_names: &[String],
    replacement_font_name: &str,
) -> Option<(String, usize)> {
    let parsed_value = serde_json::from_str::<Value>(text).ok()?;
    if !parsed_value.is_array() && !parsed_value.is_object() {
        return None;
    }
    let (replaced_value, count) =
        replace_font_names_in_json_value(parsed_value, old_font_names, replacement_font_name);
    if count == 0 {
        return None;
    }
    Some((serialize_python_style_json(&replaced_value), count))
}

pub(crate) fn replace_font_names_in_json_value(
    value: Value,
    old_font_names: &[String],
    replacement_font_name: &str,
) -> (Value, usize) {
    match value {
        Value::String(text) => {
            if let Some((replaced_text, count)) =
                replace_font_names_in_text(&text, old_font_names, replacement_font_name)
            {
                (Value::String(replaced_text), count)
            } else {
                (Value::String(text), 0)
            }
        }
        Value::Array(items) => {
            let mut replaced_items = Vec::with_capacity(items.len());
            let mut replaced_count = 0usize;
            for item in items {
                let (replaced_item, count) =
                    replace_font_names_in_json_value(item, old_font_names, replacement_font_name);
                replaced_items.push(replaced_item);
                replaced_count += count;
            }
            (Value::Array(replaced_items), replaced_count)
        }
        Value::Object(object) => {
            let mut replaced_object = Map::new();
            let mut replaced_count = 0usize;
            for (key, item) in object {
                let (replaced_item, count) =
                    replace_font_names_in_json_value(item, old_font_names, replacement_font_name);
                replaced_object.insert(key, replaced_item);
                replaced_count += count;
            }
            (Value::Object(replaced_object), replaced_count)
        }
        other => (other, 0),
    }
}

pub(crate) fn serialize_python_style_json(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let serialized_items: Vec<String> =
                items.iter().map(serialize_python_style_json).collect();
            format!("[{}]", serialized_items.join(", "))
        }
        Value::Object(object) => {
            let serialized_items: Vec<String> = object
                .iter()
                .map(|(key, item)| {
                    format!(
                        "{}: {}",
                        serde_json::to_string(key).unwrap_or_else(|_| "\"\"".to_string()),
                        serialize_python_style_json(item)
                    )
                })
                .collect();
            format!("{{{}}}", serialized_items.join(", "))
        }
        _ => serde_json::to_string(value).unwrap_or_else(|_| "null".to_string()),
    }
}

pub(crate) fn append_json_pointer_part(value_path: &str, part: &str) -> String {
    format!("{value_path}/{}", escape_json_pointer_part(part))
}

pub(crate) fn escape_json_pointer_part(part: &str) -> String {
    part.replace('~', "~0").replace('/', "~1")
}

#[cfg(test)]
mod tests {
    use super::{append_json_pointer_part, replace_font_names_in_text};

    #[test]
    fn write_plan_font_replacement_handles_direct_and_encoded_json_references() {
        let old_font_names = [
            "AnotherFont.woff".to_string(),
            "OldFont.woff".to_string(),
            "OldFont".to_string(),
        ];

        assert_eq!(
            replace_font_names_in_text("fonts/OldFont", &old_font_names, "NotoSansSC-Regular.ttf",),
            Some(("fonts/NotoSansSC-Regular.ttf".to_string(), 1)),
        );
        assert_eq!(
            replace_font_names_in_text(
                r#"{"font": "AnotherFont.woff", "text": "正文"}"#,
                &old_font_names,
                "NotoSansSC-Regular.ttf",
            ),
            Some((
                r#"{"font": "NotoSansSC-Regular.ttf", "text": "正文"}"#.to_string(),
                1,
            )),
        );
        assert_eq!(
            replace_font_names_in_text(
                "请选择 OldFont 字体",
                &old_font_names,
                "NotoSansSC-Regular.ttf",
            ),
            None,
        );
    }

    #[test]
    fn write_plan_font_replacement_escapes_json_pointer_parts() {
        assert_eq!(
            append_json_pointer_part("/parameters", "a~/b"),
            "/parameters/a~0~1b"
        );
    }
}
