//! RPG Maker 插件配置解析。

use serde::Deserialize;
use serde_json::Value;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PluginsParsePayload {
    array_text: String,
}

/// 使用 JSON5 解析 `plugins.js` 中已捕获的 `$plugins` 数组文本。
pub(super) fn parse_plugins_array_impl(payload_json: &str) -> Result<String, String> {
    let payload: PluginsParsePayload = serde_json::from_str(payload_json)
        .map_err(|error| format!("插件配置解析输入无效: {error}"))?;
    let parsed: Value = json5::from_str(&payload.array_text)
        .map_err(|error| format!("plugins.js 中的 $plugins 数组解析失败: {error}"))?;
    if !parsed.is_array() {
        return Err("plugins.js 中的 $plugins 必须是数组".to_string());
    }
    serde_json::to_string(&parsed).map_err(|error| format!("插件配置输出序列化失败: {error}"))
}

#[cfg(test)]
mod tests {
    use super::parse_plugins_array_impl;
    use serde_json::{Value, json};

    #[test]
    fn parses_standard_json_and_json5_plugin_arrays() {
        let payload = json!({
            "array_text": "[{name: 'Example', status: true, parameters: {Message: '文本',},},]"
        });
        let output =
            parse_plugins_array_impl(&payload.to_string()).expect("JSON5 插件数组应可解析");
        let parsed: Value = serde_json::from_str(&output).expect("输出应为 JSON");
        assert_eq!(parsed[0]["name"], "Example");
        assert_eq!(parsed[0]["parameters"]["Message"], "文本");
    }

    #[test]
    fn rejects_non_array_and_unknown_payload_fields() {
        let non_array = json!({"array_text": "{name: 'Example'}"});
        let error = parse_plugins_array_impl(&non_array.to_string()).expect_err("对象顶层必须失败");
        assert!(error.contains("必须是数组"));

        let unknown_field = json!({"array_text": "[]", "fallback": true});
        let error =
            parse_plugins_array_impl(&unknown_field.to_string()).expect_err("未知字段必须失败");
        assert!(error.contains("unknown field"));
    }
}
