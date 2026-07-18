//! JavaScript AST 字符串节点扫描。
//!
//! 本模块只解析源码并返回稳定范围，不判断游戏私有语义。

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tree_sitter::{Node, Parser};

use super::pool::run_with_optional_pool;
use rayon::prelude::*;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JavaScriptAstPayload {
    source: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JavaScriptAstBatchPayload {
    files: Vec<JavaScriptAstBatchInput>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JavaScriptAstBatchInput {
    file_name: String,
    source: String,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct JavaScriptStringSpan {
    pub(crate) kind: String,
    pub(crate) quote: String,
    pub(crate) start_index: usize,
    pub(crate) end_index: usize,
    pub(crate) content_start_index: usize,
    pub(crate) content_end_index: usize,
    pub(crate) content_start_byte_index: usize,
    #[serde(skip_serializing)]
    pub(crate) content_end_byte_index: usize,
    pub(crate) ast_context: JavaScriptStringAstContext,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct JavaScriptStringAstContext {
    pub(crate) node_kind: String,
    pub(crate) property_key: String,
    pub(crate) property_path: Vec<String>,
    pub(crate) call_name: String,
    pub(crate) call_argument_index: Option<usize>,
    pub(crate) return_function_name: String,
    pub(crate) assignment_name: String,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct JavaScriptAstOutput {
    pub(crate) source_sha256: String,
    pub(crate) has_error: bool,
    pub(crate) spans: Vec<JavaScriptStringSpan>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct JavaScriptAstFileOutput {
    pub(crate) file_name: String,
    pub(crate) source_sha256: String,
    pub(crate) has_error: bool,
    pub(crate) spans: Vec<JavaScriptStringSpan>,
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct JavaScriptAstBatchOutput {
    pub(crate) files: Vec<JavaScriptAstFileOutput>,
}

pub(crate) fn parse_javascript_string_spans_impl(payload_json: &str) -> Result<String, String> {
    let payload: JavaScriptAstPayload = serde_json::from_str(payload_json)
        .map_err(|error| format!("JS AST 输入不是有效 JSON: {error}"))?;
    let output = parse_javascript_string_spans(&payload.source)?;
    serde_json::to_string(&output).map_err(|error| format!("JS AST 输出 JSON 失败: {error}"))
}

pub(crate) fn parse_javascript_string_spans_batch_impl(
    payload_json: &str,
) -> Result<String, String> {
    let payload: JavaScriptAstBatchPayload = serde_json::from_str(payload_json)
        .map_err(|error| format!("批量 JS AST 输入不是有效 JSON: {error}"))?;
    let files = run_with_optional_pool(|| {
        payload
            .files
            .par_iter()
            .map(parse_javascript_file_spans)
            .collect::<Result<Vec<_>, String>>()
    })??;
    let output = JavaScriptAstBatchOutput { files };
    serde_json::to_string(&output).map_err(|error| format!("批量 JS AST 输出 JSON 失败: {error}"))
}

fn parse_javascript_file_spans(
    input: &JavaScriptAstBatchInput,
) -> Result<JavaScriptAstFileOutput, String> {
    let output = parse_javascript_string_spans(&input.source)
        .map_err(|error| format!("{} JS AST 解析失败: {error}", input.file_name))?;
    Ok(JavaScriptAstFileOutput {
        file_name: input.file_name.clone(),
        source_sha256: output.source_sha256,
        has_error: output.has_error,
        spans: output.spans,
    })
}

pub(crate) fn parse_javascript_string_spans(source: &str) -> Result<JavaScriptAstOutput, String> {
    let source_sha256 = format!("{:x}", Sha256::digest(source.as_bytes()));
    let mut parser = Parser::new();
    let language = tree_sitter_javascript::LANGUAGE;
    parser
        .set_language(&language.into())
        .map_err(|error| format!("JS AST 语言初始化失败: {error}"))?;
    let tree = parser
        .parse(source, None)
        .ok_or_else(|| "JS AST 解析失败".to_string())?;
    let root = tree.root_node();
    let mut spans: Vec<JavaScriptStringSpan> = Vec::new();
    let char_indices = CharIndexLookup::new(source);
    collect_string_nodes(root, source, &char_indices, &mut spans);
    Ok(JavaScriptAstOutput {
        source_sha256,
        has_error: root.has_error(),
        spans,
    })
}

fn collect_string_nodes(
    node: Node<'_>,
    source: &str,
    char_indices: &CharIndexLookup,
    spans: &mut Vec<JavaScriptStringSpan>,
) {
    if node.kind() == "string"
        && let Some(span) = build_string_span(node, source, char_indices)
    {
        spans.push(span);
    }
    if node.kind() == "string_fragment"
        && node
            .parent()
            .is_some_and(|parent| parent.kind() == "template_string")
        && let Some(span) = build_template_fragment_span(node, source, char_indices)
    {
        spans.push(span);
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_string_nodes(child, source, char_indices, spans);
    }
}

fn build_string_span(
    node: Node<'_>,
    source: &str,
    char_indices: &CharIndexLookup,
) -> Option<JavaScriptStringSpan> {
    let start_byte = node.start_byte();
    let end_byte = node.end_byte();
    let raw = source.get(start_byte..end_byte)?;
    let quote = raw.chars().next()?;
    if quote != '\'' && quote != '"' {
        return None;
    }
    let start_index = char_indices.char_index(start_byte)?;
    let end_index = char_indices.char_index(end_byte)?;
    if end_index <= start_index + 1 {
        return None;
    }
    let content_start_byte = start_byte + quote.len_utf8();
    let content_end_byte = end_byte.checked_sub(quote.len_utf8())?;
    Some(JavaScriptStringSpan {
        kind: node.kind().to_string(),
        quote: quote.to_string(),
        start_index,
        end_index,
        content_start_index: start_index + 1,
        content_end_index: end_index - 1,
        content_start_byte_index: content_start_byte,
        content_end_byte_index: content_end_byte,
        ast_context: build_ast_context(node, source),
    })
}

fn build_template_fragment_span(
    node: Node<'_>,
    source: &str,
    char_indices: &CharIndexLookup,
) -> Option<JavaScriptStringSpan> {
    let start_byte = node.start_byte();
    let end_byte = node.end_byte();
    let start_index = char_indices.char_index(start_byte)?;
    let end_index = char_indices.char_index(end_byte)?;
    if end_index <= start_index {
        return None;
    }
    Some(JavaScriptStringSpan {
        kind: "template_fragment".to_string(),
        quote: "`".to_string(),
        start_index,
        end_index,
        content_start_index: start_index,
        content_end_index: end_index,
        content_start_byte_index: start_byte,
        content_end_byte_index: end_byte,
        ast_context: build_ast_context(node, source),
    })
}

fn build_ast_context(node: Node<'_>, source: &str) -> JavaScriptStringAstContext {
    let (call_name, call_argument_index) =
        collect_call_context(node, source).unwrap_or_else(|| (String::new(), None));
    let property_path = collect_property_path(node, source);
    let property_key = property_path.last().cloned().unwrap_or_default();
    JavaScriptStringAstContext {
        node_kind: node.kind().to_string(),
        property_key,
        property_path,
        call_name,
        call_argument_index,
        return_function_name: collect_return_function_name(node, source).unwrap_or_default(),
        assignment_name: collect_assignment_name(node, source).unwrap_or_default(),
    }
}

fn collect_property_path(node: Node<'_>, source: &str) -> Vec<String> {
    let mut keys: Vec<String> = Vec::new();
    let mut current = node.parent();
    while let Some(parent) = current {
        if parent.kind() == "pair"
            && parent
                .child_by_field_name("value")
                .is_some_and(|value| node_contains(value, node))
            && let Some(key_node) = parent.child_by_field_name("key")
            && let Some(key) = property_key_text(key_node, source)
        {
            keys.push(key);
        }
        current = parent.parent();
    }
    keys.reverse();
    keys
}

fn collect_call_context(node: Node<'_>, source: &str) -> Option<(String, Option<usize>)> {
    let mut current = node.parent();
    while let Some(parent) = current {
        if parent.kind() == "arguments"
            && let Some(call_expression) = parent.parent()
            && call_expression.kind() == "call_expression"
            && let Some(function_node) = call_expression.child_by_field_name("function")
        {
            let call_name = node_text(function_node, source)?.trim().to_string();
            return Some((call_name, argument_index(parent, node)));
        }
        current = parent.parent();
    }
    None
}

fn argument_index(arguments_node: Node<'_>, target_node: Node<'_>) -> Option<usize> {
    let mut cursor = arguments_node.walk();
    for (index, child) in arguments_node.named_children(&mut cursor).enumerate() {
        if node_contains(child, target_node) {
            return Some(index);
        }
    }
    None
}

fn collect_return_function_name(node: Node<'_>, source: &str) -> Option<String> {
    let mut current = node.parent();
    while let Some(parent) = current {
        if parent.kind() == "return_statement" && node_contains(parent, node) {
            return enclosing_function_name(parent, source);
        }
        current = parent.parent();
    }
    None
}

fn enclosing_function_name(node: Node<'_>, source: &str) -> Option<String> {
    let mut current = node.parent();
    while let Some(parent) = current {
        if is_function_like(parent.kind()) {
            return function_name(parent, source);
        }
        current = parent.parent();
    }
    None
}

fn is_function_like(kind: &str) -> bool {
    matches!(
        kind,
        "function_declaration"
            | "function_expression"
            | "generator_function_declaration"
            | "generator_function"
            | "arrow_function"
            | "method_definition"
    )
}

fn function_name(node: Node<'_>, source: &str) -> Option<String> {
    if let Some(name_node) = node.child_by_field_name("name")
        && let Some(name) = node_text(name_node, source)
    {
        return Some(name.trim().to_string());
    }
    if let Some(parent) = node.parent() {
        if parent.kind() == "pair"
            && parent
                .child_by_field_name("value")
                .is_some_and(|value| node_contains(value, node))
            && let Some(key_node) = parent.child_by_field_name("key")
        {
            return property_key_text(key_node, source);
        }
        if parent.kind() == "variable_declarator"
            && parent
                .child_by_field_name("value")
                .is_some_and(|value| node_contains(value, node))
            && let Some(name_node) = parent.child_by_field_name("name")
        {
            return node_text(name_node, source).map(|text| text.trim().to_string());
        }
        if parent.kind() == "assignment_expression"
            && parent
                .child_by_field_name("right")
                .is_some_and(|right| node_contains(right, node))
            && let Some(left_node) = parent.child_by_field_name("left")
        {
            return node_text(left_node, source).map(|text| text.trim().to_string());
        }
    }
    None
}

fn collect_assignment_name(node: Node<'_>, source: &str) -> Option<String> {
    let mut current = node.parent();
    while let Some(parent) = current {
        if parent.kind() == "assignment_expression"
            && parent
                .child_by_field_name("right")
                .is_some_and(|right| node_contains(right, node))
            && let Some(left_node) = parent.child_by_field_name("left")
        {
            return node_text(left_node, source).map(|text| text.trim().to_string());
        }
        if parent.kind() == "variable_declarator"
            && parent
                .child_by_field_name("value")
                .is_some_and(|value| node_contains(value, node))
            && let Some(name_node) = parent.child_by_field_name("name")
        {
            return node_text(name_node, source).map(|text| text.trim().to_string());
        }
        current = parent.parent();
    }
    None
}

fn property_key_text(node: Node<'_>, source: &str) -> Option<String> {
    let raw_text = node_text(node, source)?.trim().to_string();
    if raw_text.len() >= 2
        && ((raw_text.starts_with('\'') && raw_text.ends_with('\''))
            || (raw_text.starts_with('"') && raw_text.ends_with('"')))
    {
        return Some(raw_text[1..raw_text.len() - 1].to_string());
    }
    Some(raw_text)
}

fn node_contains(parent: Node<'_>, child: Node<'_>) -> bool {
    parent.start_byte() <= child.start_byte() && parent.end_byte() >= child.end_byte()
}

fn node_text<'a>(node: Node<'_>, source: &'a str) -> Option<&'a str> {
    source.get(node.start_byte()..node.end_byte())
}

struct CharIndexLookup {
    byte_offsets: Vec<usize>,
}

impl CharIndexLookup {
    fn new(source: &str) -> Self {
        let mut byte_offsets: Vec<usize> =
            source.char_indices().map(|(index, _char)| index).collect();
        byte_offsets.push(source.len());
        Self { byte_offsets }
    }

    fn char_index(&self, byte_index: usize) -> Option<usize> {
        self.byte_offsets.binary_search(&byte_index).ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    #[test]
    fn parses_string_nodes_and_skips_comments() {
        let payload = json!({
            "source": "// drawText('コメント')\nconst a = '日文'; const b = \"English\";"
        });
        let output =
            parse_javascript_string_spans_impl(&payload.to_string()).expect("JS AST 扫描应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value["has_error"], json!(false));
        assert_eq!(value["spans"].as_array().map(Vec::len), Some(2));
        assert_eq!(value["spans"][0]["quote"], json!("'"));
    }

    #[test]
    fn returns_ast_context_for_arrays_returns_and_calls() {
        let payload = json!({
            "source": "const x = { param2: ['プフクスッ'] };\nfunction termSecondPerson() { return 'キミ'; }\nWindow_Base.prototype.drawText('短い', 0, 0);"
        });
        let output =
            parse_javascript_string_spans_impl(&payload.to_string()).expect("JS AST 扫描应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        let spans = value["spans"].as_array().expect("spans 应为数组");
        assert_eq!(spans[0]["ast_context"]["property_key"], json!("param2"));
        assert_eq!(spans[0]["ast_context"]["property_path"], json!(["param2"]));
        assert_eq!(
            spans[1]["ast_context"]["return_function_name"],
            json!("termSecondPerson")
        );
        assert_eq!(
            spans[2]["ast_context"]["call_name"],
            json!("Window_Base.prototype.drawText")
        );
        assert_eq!(spans[2]["ast_context"]["call_argument_index"], json!(0));
    }

    #[test]
    fn parses_template_static_fragments() {
        let payload = json!({
            "source": "const text = `名前: ${actor.name} さん`;"
        });
        let output =
            parse_javascript_string_spans_impl(&payload.to_string()).expect("JS AST 扫描应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value["has_error"], json!(false));
        assert_eq!(value["spans"].as_array().map(Vec::len), Some(2));
        assert_eq!(value["spans"][0]["quote"], json!("`"));
    }

    #[test]
    fn parses_string_nodes_for_batch_files() {
        let payload = json!({
            "files": [
                {"file_name": "A.js", "source": "const a = '日文';"},
                {"file_name": "B.js", "source": "Window_Base.prototype.drawText('短い', 0, 0);"}
            ]
        });
        let output = parse_javascript_string_spans_batch_impl(&payload.to_string())
            .expect("批量 JS AST 扫描应成功");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");
        assert_eq!(value["files"].as_array().map(Vec::len), Some(2));
        assert_eq!(value["files"][0]["file_name"], json!("A.js"));
        assert_eq!(
            value["files"][0]["source_sha256"],
            parse_javascript_string_spans("const a = '日文';")
                .expect("单文件 JS AST 扫描应成功")
                .source_sha256
        );
        assert_eq!(value["files"][0]["spans"].as_array().map(Vec::len), Some(1));
        assert_eq!(value["files"][1]["file_name"], json!("B.js"));
        assert_eq!(
            value["files"][1]["spans"][0]["ast_context"]["call_name"],
            json!("Window_Base.prototype.drawText")
        );
    }

    #[test]
    fn returns_sha256_for_exact_unicode_utf8_source() {
        let source = "const 文本 = 'こんにちは😀';";

        let output = parse_javascript_string_spans(source).expect("Unicode JS AST 扫描应成功");

        assert_eq!(
            output.source_sha256,
            "960f4efaac990fa56cf2650aa5773734aad3b386309b044cde7b6813a14fd939"
        );
    }

    #[test]
    fn versioned_single_operation_rejects_unknown_payload_field() {
        let request = versioned_request(
            "javascript.parse",
            json!({"source": "const text = '日文';", "unexpected": true}),
        );

        let response = invoke_versioned_request(request);

        assert_eq!(response["status"], json!("error"));
        assert_eq!(response["error"]["code"], json!("native_operation_failed"));
        assert!(
            response["error"]["message"]
                .as_str()
                .is_some_and(|message| message.contains("unknown field `unexpected`"))
        );
    }

    #[test]
    fn versioned_batch_operation_rejects_unknown_nested_input_field() {
        let request = versioned_request(
            "javascript.parse_batch",
            json!({
                "files": [{
                    "file_name": "Plugin.js",
                    "source": "const text = '日文';",
                    "unexpected": true
                }]
            }),
        );

        let response = invoke_versioned_request(request);

        assert_eq!(response["status"], json!("error"));
        assert_eq!(response["error"]["code"], json!("native_operation_failed"));
        assert!(
            response["error"]["message"]
                .as_str()
                .is_some_and(|message| message.contains("unknown field `unexpected`"))
        );
    }

    fn versioned_request(operation: &str, payload: Value) -> Value {
        json!({
            "envelope_version": crate::protocol::ENVELOPE_VERSION,
            "abi_version": crate::protocol::ABI_VERSION,
            "schema_version": crate::protocol::SCHEMA_VERSION,
            "operation": operation,
            "request_id": "javascript-ast-test",
            "payload": payload,
        })
    }

    fn invoke_versioned_request(request: Value) -> Value {
        let output = crate::protocol::invoke_json(&request.to_string())
            .expect("版本化 native 调用应返回 JSON");
        serde_json::from_str(&output).expect("native 响应应是 JSON")
    }

    #[test]
    fn batch_ast_accepts_thread_pool_env_on_hot_path() {
        let payload = json!({
            "files": [
                {"file_name": "A.js", "source": "const a = '日文';"},
                {"file_name": "B.js", "source": "const b = '短い';"}
            ]
        });

        let result =
            crate::native_core::pool::with_thread_count_override_for_test(Some("1"), || {
                parse_javascript_string_spans_batch_impl(&payload.to_string())
            });
        let output = result.expect("批量 JS AST 热路径应接受 ATT_MZ_RUST_THREADS");
        let value: Value = serde_json::from_str(&output).expect("输出应是 JSON");

        assert_eq!(value["files"].as_array().map(Vec::len), Some(2));
    }

    #[test]
    fn batch_ast_rejects_invalid_thread_pool_env_on_hot_path() {
        let payload = json!({
            "files": [
                {"file_name": "A.js", "source": "const a = '日文';"}
            ]
        });

        let error =
            crate::native_core::pool::with_thread_count_override_for_test(Some("invalid"), || {
                parse_javascript_string_spans_batch_impl(&payload.to_string())
            })
            .expect_err("批量 JS AST 热路径必须读取并校验 ATT_MZ_RUST_THREADS");

        assert!(
            error.contains("ATT_MZ_RUST_THREADS 必须是非负整数"),
            "错误文案应说明线程配置非法，实际为 {error}",
        );
    }
}
