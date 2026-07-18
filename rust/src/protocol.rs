//! Python/Rust 之间唯一的版本化 JSON 协议。

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

use crate::native_core;

pub(crate) const ABI_VERSION: u32 = 1;
pub(crate) const ENVELOPE_VERSION: u32 = 1;
pub(crate) const SCHEMA_VERSION: u32 = 1;

const OPERATIONS: &[&str] = &[
    "quality.scan",
    "quality.counts",
    "write_protocol.scan",
    "write_protocol.counts",
    "note_sources.collect",
    "hash.files",
    "placeholder_candidates.scan",
    "javascript.parse",
    "javascript.parse_batch",
    "plugins.parse",
    "runtime.thread_count",
    "write_back.plan",
];

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct NativeRequest {
    envelope_version: u32,
    abi_version: u32,
    schema_version: u32,
    operation: String,
    request_id: String,
    payload: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteBackRequest {
    game_path: String,
    db_path: String,
    setting: Value,
    mode: String,
    confirm_font_overwrite: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeThreadCountRequest {}

#[derive(Debug, Serialize)]
struct NativeErrorBody {
    code: &'static str,
    stage: &'static str,
    message: String,
    retryable: bool,
    details: BTreeMap<String, Value>,
}

#[derive(Debug)]
enum OperationError {
    Generic(String),
    InvalidRequest(String),
    FileHash(native_core::FileHashError),
}

fn response_base(request_id: Option<String>) -> Map<String, Value> {
    let mut response = Map::new();
    response.insert("envelope_version".to_owned(), json!(ENVELOPE_VERSION));
    response.insert("abi_version".to_owned(), json!(ABI_VERSION));
    response.insert("schema_version".to_owned(), json!(SCHEMA_VERSION));
    response.insert("request_id".to_owned(), json!(request_id));
    response
}

fn success_response(request_id: Option<String>, data: Value) -> Value {
    let mut response = response_base(request_id);
    response.insert("status".to_owned(), json!("ok"));
    response.insert("data".to_owned(), data);
    Value::Object(response)
}

fn error_response(
    request_id: Option<String>,
    code: &'static str,
    stage: &'static str,
    message: impl Into<String>,
) -> Value {
    error_response_with_details(request_id, code, stage, message, BTreeMap::new())
}

fn error_response_with_details(
    request_id: Option<String>,
    code: &'static str,
    stage: &'static str,
    message: impl Into<String>,
    details: BTreeMap<String, Value>,
) -> Value {
    let mut response = response_base(request_id);
    response.insert("status".to_owned(), json!("error"));
    response.insert(
        "error".to_owned(),
        serde_json::to_value(NativeErrorBody {
            code,
            stage,
            message: message.into(),
            retryable: false,
            details,
        })
        .expect("NativeErrorBody must be serializable"),
    );
    Value::Object(response)
}

fn parse_core_output(output: String) -> Result<Value, String> {
    serde_json::from_str(&output).map_err(|error| format!("原生核心返回了无效 JSON：{error}"))
}

fn invoke_operation(request: &NativeRequest) -> Result<Value, OperationError> {
    if request.operation == "runtime.thread_count" {
        let _payload = serde_json::from_value::<RuntimeThreadCountRequest>(request.payload.clone())
            .map_err(|error| {
                OperationError::InvalidRequest(format!("runtime.thread_count 请求无效：{error}"))
            })?;
        let thread_count = native_core::executor_thread_count().map_err(OperationError::Generic)?;
        return Ok(json!({"thread_count": thread_count}));
    }
    let payload_json = serde_json::to_string(&request.payload).map_err(|error| {
        OperationError::Generic(format!("原生请求 payload 无法序列化：{error}"))
    })?;
    if request.operation == "hash.files" {
        let output =
            native_core::hash_files_impl(&payload_json).map_err(OperationError::FileHash)?;
        return parse_core_output(output).map_err(OperationError::Generic);
    }
    let output = match request.operation.as_str() {
        "quality.scan" => native_core::scan_quality_impl(&payload_json),
        "quality.counts" => native_core::scan_quality_counts_impl(&payload_json),
        "write_protocol.scan" => native_core::scan_write_protocol_impl(&payload_json),
        "write_protocol.counts" => native_core::scan_write_protocol_count_impl(&payload_json),
        "note_sources.collect" => native_core::collect_note_tag_sources_impl(&payload_json),
        "placeholder_candidates.scan" => {
            native_core::scan_placeholder_candidates_impl(&payload_json)
        }
        "javascript.parse" => native_core::parse_javascript_string_spans_impl(&payload_json),
        "javascript.parse_batch" => {
            native_core::parse_javascript_string_spans_batch_impl(&payload_json)
        }
        "plugins.parse" => native_core::parse_plugins_array_impl(&payload_json),
        "write_back.plan" => {
            let payload: WriteBackRequest = serde_json::from_value(request.payload.clone())
                .map_err(|error| {
                    OperationError::Generic(format!("write_back.plan 请求无效：{error}"))
                })?;
            let setting = serde_json::to_string(&payload.setting).map_err(|error| {
                OperationError::Generic(format!("write_back.plan setting 无法序列化：{error}"))
            })?;
            native_core::build_write_back_plan_impl(
                &payload.game_path,
                &payload.db_path,
                &setting,
                &payload.mode,
                payload.confirm_font_overwrite,
            )
        }
        _ => {
            return Err(OperationError::Generic(format!(
                "未知原生操作：{}",
                request.operation
            )));
        }
    }
    .map_err(OperationError::Generic)?;
    parse_core_output(output).map_err(OperationError::Generic)
}

pub(crate) fn contract_json() -> Result<String, String> {
    let schemas: BTreeMap<&str, u32> = OPERATIONS
        .iter()
        .copied()
        .map(|operation| (operation, SCHEMA_VERSION))
        .collect();
    serde_json::to_string(&json!({
        "package_version": env!("CARGO_PKG_VERSION"),
        "abi_version": ABI_VERSION,
        "envelope_version": ENVELOPE_VERSION,
        "schemas": schemas,
    }))
    .map_err(|error| format!("原生 contract 无法序列化：{error}"))
}

pub(crate) fn invoke_json(request_json: &str) -> Result<String, String> {
    let request_value = match serde_json::from_str::<Value>(request_json) {
        Ok(value) => value,
        Err(error) => {
            return serde_json::to_string(&error_response(
                None,
                "invalid_json",
                "decode",
                format!("原生请求 JSON 无效：{error}"),
            ))
            .map_err(|serialize_error| serialize_error.to_string());
        }
    };
    let request = match serde_json::from_value::<NativeRequest>(request_value) {
        Ok(request) => request,
        Err(error) => {
            return serde_json::to_string(&error_response(
                None,
                "invalid_request",
                "decode",
                format!("原生请求结构无效：{error}"),
            ))
            .map_err(|serialize_error| serialize_error.to_string());
        }
    };
    let request_id = Some(request.request_id.clone());
    let response = if request.request_id.is_empty() {
        error_response(
            request_id,
            "invalid_request",
            "contract",
            "原生请求 request_id 不能为空",
        )
    } else if request.envelope_version != ENVELOPE_VERSION {
        error_response(
            request_id,
            "unsupported_envelope",
            "contract",
            format!(
                "不支持的 native envelope {}，当前要求 {}",
                request.envelope_version, ENVELOPE_VERSION
            ),
        )
    } else if request.abi_version != ABI_VERSION {
        error_response(
            request_id,
            "unsupported_abi",
            "contract",
            format!(
                "不支持的 native ABI {}，当前要求 {}",
                request.abi_version, ABI_VERSION
            ),
        )
    } else if request.schema_version != SCHEMA_VERSION {
        error_response(
            request_id,
            "unsupported_schema",
            "contract",
            format!(
                "不支持的操作 schema {}，当前要求 {}",
                request.schema_version, SCHEMA_VERSION
            ),
        )
    } else if !OPERATIONS.contains(&request.operation.as_str()) {
        error_response(
            request_id,
            "invalid_request",
            "dispatch",
            format!("未知原生操作：{}", request.operation),
        )
    } else {
        match invoke_operation(&request) {
            Ok(data) => success_response(request_id, data),
            Err(OperationError::Generic(error)) => {
                error_response(request_id, "native_operation_failed", "execute", error)
            }
            Err(OperationError::InvalidRequest(error)) => {
                error_response(request_id, "invalid_request", "validate", error)
            }
            Err(OperationError::FileHash(error)) => error_response_with_details(
                request_id,
                error.code,
                error.stage,
                error.message,
                error.details,
            ),
        }
    };
    serde_json::to_string(&response).map_err(|error| format!("原生响应无法序列化：{error}"))
}

#[cfg(test)]
mod tests {
    use super::{ABI_VERSION, ENVELOPE_VERSION, contract_json, invoke_json};
    use serde_json::{Value, json};

    #[test]
    fn contract_matches_package_and_operations() {
        let contract: Value =
            serde_json::from_str(&contract_json().expect("contract")).expect("json");
        assert_eq!(contract["package_version"], env!("CARGO_PKG_VERSION"));
        assert_eq!(contract["abi_version"], ABI_VERSION);
        assert_eq!(contract["envelope_version"], ENVELOPE_VERSION);
        assert_eq!(contract["schemas"]["quality.scan"], 1);
        assert_eq!(contract["schemas"]["hash.files"], 1);
        assert_eq!(contract["schemas"]["runtime.thread_count"], 1);
    }

    #[test]
    fn runtime_thread_count_uses_strict_versioned_operation() {
        let request = json!({
            "envelope_version": 1,
            "abi_version": 1,
            "schema_version": 1,
            "operation": "runtime.thread_count",
            "request_id": "thread-count",
            "payload": {},
        });
        let response: Value = serde_json::from_str(
            &invoke_json(&request.to_string()).expect("runtime.thread_count response"),
        )
        .expect("json");

        assert_eq!(response["status"], "ok");
        assert_eq!(response["request_id"], "thread-count");
        assert!(response["data"]["thread_count"].as_u64().unwrap_or(0) > 0);

        let request_with_unknown_field = json!({
            "envelope_version": 1,
            "abi_version": 1,
            "schema_version": 1,
            "operation": "runtime.thread_count",
            "request_id": "thread-count-invalid",
            "payload": {"legacy_count": true},
        });
        let response: Value = serde_json::from_str(
            &invoke_json(&request_with_unknown_field.to_string())
                .expect("runtime.thread_count error response"),
        )
        .expect("json");

        assert_eq!(response["status"], "error");
        assert_eq!(response["request_id"], "thread-count-invalid");
        assert_eq!(response["error"]["code"], "invalid_request");
        assert_eq!(response["error"]["stage"], "validate");
        assert_eq!(response["error"]["retryable"], false);
        assert_eq!(response["error"]["details"], json!({}));
    }

    #[test]
    fn hash_files_validation_error_preserves_stable_code_and_details() {
        let request = json!({
            "envelope_version": 1,
            "abi_version": 1,
            "schema_version": 1,
            "operation": "hash.files",
            "request_id": "hash-test",
            "payload": {
                "root": "unused",
                "files": [
                    {"id": "duplicate", "relative_path": "a.txt"},
                    {"id": "duplicate", "relative_path": "b.txt"},
                ],
            },
        });

        let response: Value =
            serde_json::from_str(&invoke_json(&request.to_string()).expect("hash.files response"))
                .expect("json");

        assert_eq!(response["status"], "error");
        assert_eq!(response["request_id"], "hash-test");
        assert_eq!(response["error"]["code"], "hash_files_duplicate_id");
        assert_eq!(response["error"]["stage"], "validate");
        assert_eq!(response["error"]["details"]["id"], "duplicate");
        assert_eq!(response["error"]["details"]["reason"], "duplicate_id");
    }

    #[test]
    fn invalid_and_unknown_requests_return_typed_errors() {
        let invalid: Value =
            serde_json::from_str(&invoke_json("not-json").expect("response")).expect("json");
        assert_eq!(invalid["error"]["code"], "invalid_json");

        let unknown = json!({
            "envelope_version": 1,
            "abi_version": 1,
            "schema_version": 1,
            "operation": "unknown",
            "request_id": "test",
            "payload": {},
        });
        let response: Value =
            serde_json::from_str(&invoke_json(&unknown.to_string()).expect("response"))
                .expect("json");
        assert_eq!(response["request_id"], "test");
        assert_eq!(response["error"]["code"], "invalid_request");

        let unsupported_envelope = json!({
            "envelope_version": 2,
            "abi_version": 1,
            "schema_version": 1,
            "operation": "quality.scan",
            "request_id": "envelope-test",
            "payload": {},
        });
        let response: Value = serde_json::from_str(
            &invoke_json(&unsupported_envelope.to_string()).expect("response"),
        )
        .expect("json");
        assert_eq!(response["request_id"], "envelope-test");
        assert_eq!(response["error"]["code"], "unsupported_envelope");

        let missing_field = json!({
            "envelope_version": 1,
            "abi_version": 1,
            "schema_version": 1,
            "operation": "quality.scan",
            "request_id": "missing-payload",
        });
        let response: Value =
            serde_json::from_str(&invoke_json(&missing_field.to_string()).expect("response"))
                .expect("json");
        assert_eq!(response["request_id"], Value::Null);
        assert_eq!(response["error"]["code"], "invalid_request");
    }

    #[test]
    fn plugins_parse_routes_json5_through_the_versioned_envelope() {
        let request = json!({
            "envelope_version": 1,
            "abi_version": 1,
            "schema_version": 1,
            "operation": "plugins.parse",
            "request_id": "plugins-test",
            "payload": {
                "array_text": "[{name: 'Example', status: true, parameters: {},},]"
            },
        });

        let response: Value = serde_json::from_str(
            &invoke_json(&request.to_string()).expect("plugins.parse response"),
        )
        .expect("json");
        assert_eq!(response["status"], "ok");
        assert_eq!(response["request_id"], "plugins-test");
        assert_eq!(response["data"][0]["name"], "Example");
    }
}
