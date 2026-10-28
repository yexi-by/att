//! 端到端：真实生产入口（编译产物 formic）× 真实 HTTP/SSE（手写 mock server）。
//! mock 脚本化两轮：首轮返回 search 工具调用（参数分片传输），携带工具结果的
//! 请求返回最终文本。覆盖三协议主成功、HTTP 500 失败、停滞检测、计划校验错误。

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

use base64::Engine as _;

/// 三种协议罐装响应的共同最终消息，主成功路径断言产出与它一致。
const FINAL_TEXT: &str = "你好世界";

/// 请求体含此标记时 mock 返回 500，用于驱动失败路径。
const FAIL_MARKER: &str = "FAIL-UNIT";

/// 请求体含此标记时 mock 永远返回工具调用，用于驱动停滞检测路径。
const LOOP_MARKER: &str = "LOOP-MARKER";

/// 请求体含此标记时 mock 对首次请求返回 500、后续恢复正常，用于驱动重试成功路径。
const FLAKY_MARKER: &str = "FLAKY-UNIT";
const FAIL_TWICE_MARKER: &str = "FAIL-TWICE-UNIT";

/// 请求体含此标记时，completions mock 改为调用测试 MCP 工具。
const MCP_MARKER: &str = "MCP-UNIT";
const MCP_ERROR_MARKER: &str = "MCP-ERROR-UNIT";
const MCP_PARALLEL_MARKER: &str = "MCP-PARALLEL-UNIT";
const MCP_FALLBACK_FLOW_MARKER: &str = "MCP-FALLBACK-FLOW-UNIT";
const IMAGE_TOOL_MARKER: &str = "IMAGE-TOOL-UNIT";

/// 请求体含此标记时，mock 直接提交符合 schema 的结构化结果。
const STRUCTURED_MARKER: &str = "STRUCTURED-UNIT";
const INVALID_STRUCTURED_MARKER: &str = "BAD-SCHEMA-UNIT";
const MIXED_STRUCTURED_MARKER: &str = "MIXED-SUBMIT-UNIT";
const EXHAUST_STRUCTURED_MARKER: &str = "EXHAUST-SCHEMA-UNIT";
const REFUSAL_MARKER: &str = "REFUSAL-UNIT";
const TRUNCATION_MARKER: &str = "TRUNCATION-UNIT";
const COMPACTION_MARKER: &str = "COMPACTION-UNIT";
const SEGMENTED_COMPACTION_MARKER: &str = "SEGMENTED-HISTORY-UNIT";
const COMPACTION_PROOFS: [&str; 4] = [
    "HEAD_PROOF",
    "MIDDLE_PROOF",
    "TAIL_PROOF",
    "SECOND_RESULT_PROOF",
];
const MCP_TIMEOUT_MARKER: &str = "MCP-TIMEOUT-UNIT";
const AUTH_MARKER: &str = "AUTH-STATUS-UNIT";
const FORBIDDEN_MARKER: &str = "FORBIDDEN-STATUS-UNIT";
const QUOTA_MARKER: &str = "QUOTA-STATUS-UNIT";
const ACCOUNT_MARKER: &str = "ACCOUNT-STATUS-UNIT";
const RATE_LONG_MARKER: &str = "RATE-LONG-UNIT";
const SERVER_LONG_MARKER: &str = "SERVER-LONG-UNIT";
const RATE_NUMERIC_MARKER: &str = "RATE-NUMERIC-UNIT";
const RATE_DATE_MARKER: &str = "RATE-DATE-UNIT";
const BAD_PROTOCOL_MARKER: &str = "BAD-PROTOCOL-UNIT";
const EOF_WITHOUT_FINISH_MARKER: &str = "EOF-WITHOUT-FINISH-UNIT";
const DONE_WITHOUT_FINISH_MARKER: &str = "DONE-WITHOUT-FINISH-UNIT";
const SLOW_QUOTA_MARKER: &str = "SLOW-QUOTA-UNIT";
const SLOW_SERVER_MARKER: &str = "SLOW-SERVER-UNIT";
const NETWORK_CONTEXT_NETWORK_MARKER: &str = "NETWORK-CONTEXT-NETWORK-UNIT";
const SLOW_STREAM_MARKER: &str = "SLOW-STREAM-UNIT";
const RESPONSE_SECRET: &str = "SENSITIVE-RESPONSE-MARKER";
const LLM_API_KEY_SECRET: &str = "SENSITIVE-LLM-API-KEY";
const LLM_EXTRA_BODY_SECRET: &str = "SENSITIVE-LLM-EXTRA-BODY";
const MCP_BEARER_SECRET: &str = "SENSITIVE-MCP-BEARER";
const SELF_TEST_MARKER: &str = "Formic 连接自检";

// ---- 罐装 SSE：文本最终帧（三协议）----

const COMPLETIONS_FINAL: &str = concat!(
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"\",\"tool_calls\":null},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"你好\"},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"世界\"},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":null,\"tool_calls\":null},\"finish_reason\":\"stop\"}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":5,\"prompt_tokens_details\":{\"cached_tokens\":60,\"cache_creation_tokens\":20}}}\n\n",
    "data: [DONE]\n\n",
);

const RESPONSES_FINAL: &str = concat!(
    "data: {\"type\":\"response.created\",\"response\":{\"id\":\"r1\",\"status\":\"in_progress\"}}\n\n",
    "data: {\"type\":\"response.output_text.delta\",\"item_id\":\"m1\",\"output_index\":0,\"content_index\":0,\"delta\":\"你好\"}\n\n",
    "data: {\"type\":\"response.output_text.delta\",\"item_id\":\"m1\",\"output_index\":0,\"content_index\":0,\"delta\":\"世界\"}\n\n",
    "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"status\":\"completed\",\"usage\":{\"input_tokens\":100,\"output_tokens\":5,\"input_tokens_details\":{\"cached_tokens\":60,\"cache_creation_tokens\":20}}}}\n\n",
);

const ANTHROPIC_FINAL: &str = concat!(
    "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg1\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"m\",\"stop_reason\":null,\"usage\":{\"input_tokens\":100,\"output_tokens\":1,\"cache_read_input_tokens\":60,\"cache_creation_input_tokens\":20}}}\n\n",
    "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
    "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"你好\"}}\n\n",
    "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"世界\"}}\n\n",
    "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
    "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\",\"stop_sequence\":null},\"usage\":{\"output_tokens\":5}}\n\n",
    "data: {\"type\":\"message_stop\"}\n\n",
);

// ---- 罐装 SSE：search 工具调用（三协议，参数分片传输）----

const COMPLETIONS_TOOLCALL: &str = concat!(
    "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":null,\"type\":null,\"function\":null}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"search\",\"arguments\":\"\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":null,\"type\":null,\"function\":{\"name\":null,\"arguments\":\"{\\\"pattern\\\":\\\"苹果\\\"\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"name\":null,\"arguments\":null}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\",\\\"scope\\\":\\\"input\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":null},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_MCP_TOOLCALL: &str = concat!(
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call_mcp\",\"type\":\"function\",\"function\":{\"name\":\"demo__echo\",\"arguments\":\"{\\\"text\\\":\\\"hello\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_FAILED_MCP_TOOLCALL: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call_fail\",\"type\":\"function\",\"function\":{\"name\":\"demo__fail\",\"arguments\":\"{}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_PARALLEL_MCP_TOOLCALL: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[",
    "{\"index\":0,\"id\":\"call_left\",\"type\":\"function\",\"function\":{\"name\":\"left__slow\",\"arguments\":\"{}\"}},",
    "{\"index\":1,\"id\":\"call_right\",\"type\":\"function\",\"function\":{\"name\":\"right__slow\",\"arguments\":\"{}\"}}",
    "]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_TIMEOUT_AND_FAST_MCP_TOOLCALL: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[",
    "{\"index\":0,\"id\":\"call_slow\",\"type\":\"function\",\"function\":{\"name\":\"slow__slow\",\"arguments\":\"{}\"}},",
    "{\"index\":1,\"id\":\"call_fast\",\"type\":\"function\",\"function\":{\"name\":\"fast__echo\",\"arguments\":\"{\\\"text\\\":\\\"primary\\\"}\"}}",
    "]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_BACKUP_MCP_TOOLCALL: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call_backup\",\"type\":\"function\",\"function\":{\"name\":\"backup__echo\",\"arguments\":\"{\\\"text\\\":\\\"fallback\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_SLOW_MCP_TOOLCALL: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call_slow\",\"type\":\"function\",\"function\":{\"name\":\"demo__slow\",\"arguments\":\"{}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_READ_FOR_COMPACTION: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"read_1\",\"type\":\"function\",\"function\":{\"name\":\"read\",\"arguments\":\"{\\\"scope\\\":\\\"input\\\",\\\"path\\\":\\\"big.txt\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const COMPLETIONS_COMPACTION_SUBMIT: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"compact_1\",\"type\":\"function\",\"function\":{\"name\":\"formic_submit_compaction\",\"arguments\":\"{\\\"summary\\\":\\\"已读取大文件\\\",\\\"verified_facts\\\":[\\\"文件可读\\\"],\\\"evidence\\\":[\\\"read big.txt\\\"],\\\"remaining_work\\\":[\\\"形成答案\\\"]}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const RESPONSES_TOOLCALL: &str = concat!(
    "data: {\"type\":\"response.created\",\"response\":{\"id\":\"r1\",\"status\":\"in_progress\"}}\n\n",
    "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{\"type\":\"reasoning\",\"id\":\"rs_provider_1\",\"encrypted_content\":\"provider-only-state\",\"summary\":[]}}\n\n",
    "data: {\"type\":\"response.output_item.added\",\"output_index\":1,\"item\":{\"type\":\"function_call\",\"id\":\"fc1\",\"call_id\":\"call_1\",\"name\":\"search\",\"arguments\":\"\"}}\n\n",
    "data: {\"type\":\"response.function_call_arguments.delta\",\"item_id\":\"fc1\",\"output_index\":1,\"delta\":\"{\\\"pattern\\\":\\\"苹果\\\"\"}\n\n",
    "data: {\"type\":\"response.function_call_arguments.delta\",\"item_id\":\"fc1\",\"output_index\":1,\"delta\":\",\\\"scope\\\":\\\"input\\\"}\"}\n\n",
    "data: {\"type\":\"response.output_item.done\",\"output_index\":1,\"item\":{\"type\":\"function_call\",\"id\":\"fc1\",\"call_id\":\"call_1\",\"name\":\"search\",\"arguments\":\"{\\\"pattern\\\":\\\"苹果\\\",\\\"scope\\\":\\\"input\\\"}\"}}\n\n",
    "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"status\":\"completed\"}}\n\n",
);

const ANTHROPIC_TOOLCALL: &str = concat!(
    "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg1\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"m\",\"stop_reason\":null}}\n\n",
    "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"search\"}}\n\n",
    "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"pattern\\\":\\\"苹果\\\"\"}}\n\n",
    "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\",\\\"scope\\\":\\\"input\\\"}\"}}\n\n",
    "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
    "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"}}\n\n",
    "data: {\"type\":\"message_stop\"}\n\n",
);

const COMPLETIONS_IMAGE_TOOLCALL: &str = concat!(
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[",
    "{\"index\":0,\"id\":\"read_text\",\"type\":\"function\",\"function\":{\"name\":\"read\",\"arguments\":\"{\\\"scope\\\":\\\"input\\\",\\\"path\\\":\\\"note.txt\\\"}\"}},",
    "{\"index\":1,\"id\":\"read_image\",\"type\":\"function\",\"function\":{\"name\":\"read_image\",\"arguments\":\"{\\\"path\\\":\\\"sample.png\\\"}\"}}",
    "]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const RESPONSES_IMAGE_TOOLCALL: &str = concat!(
    "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc1\",\"call_id\":\"read_text\",\"name\":\"read\",\"arguments\":\"{\\\"scope\\\":\\\"input\\\",\\\"path\\\":\\\"note.txt\\\"}\"}}\n\n",
    "data: {\"type\":\"response.output_item.done\",\"output_index\":1,\"item\":{\"type\":\"function_call\",\"id\":\"fc2\",\"call_id\":\"read_image\",\"name\":\"read_image\",\"arguments\":\"{\\\"path\\\":\\\"sample.png\\\"}\"}}\n\n",
    "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\n\n",
);

const ANTHROPIC_IMAGE_TOOLCALL: &str = concat!(
    "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg1\",\"role\":\"assistant\",\"content\":[],\"model\":\"m\",\"stop_reason\":null}}\n\n",
    "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"read_text\",\"name\":\"read\"}}\n\n",
    "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"scope\\\":\\\"input\\\",\\\"path\\\":\\\"note.txt\\\"}\"}}\n\n",
    "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
    "data: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"tool_use\",\"id\":\"read_image\",\"name\":\"read_image\"}}\n\n",
    "data: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"path\\\":\\\"sample.png\\\"}\"}}\n\n",
    "data: {\"type\":\"content_block_stop\",\"index\":1}\n\n",
    "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"}}\n\n",
    "data: {\"type\":\"message_stop\"}\n\n",
);

fn image_tool_sse(path: &str) -> Option<&'static str> {
    if path.ends_with("/chat/completions") {
        Some(COMPLETIONS_IMAGE_TOOLCALL)
    } else if path.ends_with("/responses") {
        Some(RESPONSES_IMAGE_TOOLCALL)
    } else if path.ends_with("/messages") {
        Some(ANTHROPIC_IMAGE_TOOLCALL)
    } else {
        None
    }
}

const COMPLETIONS_STRUCTURED: &str = concat!(
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"submit_1\",\"type\":\"function\",\"function\":{\"name\":\"formic_submit_result\",\"arguments\":\"{\\\"answer\\\":\\\"ok\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
    "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
    "data: [DONE]\n\n",
);

const RESPONSES_STRUCTURED: &str = concat!(
    "data: {\"type\":\"response.created\",\"response\":{\"id\":\"r1\",\"status\":\"in_progress\"}}\n\n",
    "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc1\",\"call_id\":\"submit_1\",\"name\":\"formic_submit_result\",\"arguments\":\"{\\\"answer\\\":\\\"ok\\\"}\"}}\n\n",
    "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"status\":\"completed\"}}\n\n",
);

const ANTHROPIC_STRUCTURED: &str = concat!(
    "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg1\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"m\",\"stop_reason\":null}}\n\n",
    "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"submit_1\",\"name\":\"formic_submit_result\"}}\n\n",
    "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"answer\\\":\\\"ok\\\"}\"}}\n\n",
    "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
    "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"}}\n\n",
    "data: {\"type\":\"message_stop\"}\n\n",
);

fn structured_sse(path: &str) -> Option<&'static str> {
    if path.ends_with("/chat/completions") {
        Some(COMPLETIONS_STRUCTURED)
    } else if path.ends_with("/responses") {
        Some(RESPONSES_STRUCTURED)
    } else if path.ends_with("/messages") {
        Some(ANTHROPIC_STRUCTURED)
    } else {
        None
    }
}

fn structured_sse_with_validity(path: &str, valid: bool) -> Option<String> {
    let response = structured_sse(path)?;
    Some(if valid {
        response.to_string()
    } else {
        response.replace("{\\\"answer\\\":\\\"ok\\\"}", "{}")
    })
}

fn tool_calls_sse(path: &str, calls: &[(&str, &str, serde_json::Value)]) -> String {
    let mut events = Vec::new();
    if path.ends_with("/chat/completions") {
        let calls: Vec<_> = calls
            .iter()
            .enumerate()
            .map(|(index, (id, name, arguments))| {
                serde_json::json!({"index":index,"id":id,"type":"function",
                "function":{"name":name,"arguments":arguments.to_string()}})
            })
            .collect();
        events.push(serde_json::json!({"choices":[{"index":0,"delta":{"tool_calls":calls},"finish_reason":"tool_calls"}]}));
    } else if path.ends_with("/responses") {
        for (index, (id, name, arguments)) in calls.iter().enumerate() {
            events.push(
                serde_json::json!({"type":"response.output_item.done","output_index":index,
                "item":{"type":"function_call","id":format!("fc{index}"),"call_id":id,
                "name":name,"arguments":arguments.to_string()}}),
            );
        }
        events.push(
            serde_json::json!({"type":"response.completed","response":{"status":"completed"}}),
        );
    } else {
        assert!(path.ends_with("/messages"));
        for (index, (id, name, arguments)) in calls.iter().enumerate() {
            events.push(
                serde_json::json!({"type":"content_block_start","index":index,
                "content_block":{"type":"tool_use","id":id,"name":name}}),
            );
            events.push(
                serde_json::json!({"type":"content_block_delta","index":index,
                "delta":{"type":"input_json_delta","partial_json":arguments.to_string()}}),
            );
            events.push(serde_json::json!({"type":"content_block_stop","index":index}));
        }
        events.push(serde_json::json!({"type":"message_delta","delta":{"stop_reason":"tool_use"}}));
        events.push(serde_json::json!({"type":"message_stop"}));
    }
    let mut sse: String = events
        .iter()
        .map(|event| format!("data: {event}\n\n"))
        .collect();
    if path.ends_with("/chat/completions") {
        sse.push_str("data: [DONE]\n\n");
    }
    sse
}

fn terminal_sse(path: &str, refusal: bool) -> Option<String> {
    if path.ends_with("/chat/completions") {
        let reason = if refusal { "content_filter" } else { "length" };
        Some(format!(
            "data: {{\"id\":\"c1\",\"choices\":[{{\"index\":0,\"delta\":{{}},\"finish_reason\":\"{reason}\"}}]}}\n\ndata: [DONE]\n\n"
        ))
    } else if path.ends_with("/responses") {
        let reason = if refusal {
            "content_filter"
        } else {
            "max_output_tokens"
        };
        Some(format!(
            "data: {{\"type\":\"response.incomplete\",\"response\":{{\"status\":\"incomplete\",\"incomplete_details\":{{\"reason\":\"{reason}\"}}}}}}\n\n"
        ))
    } else if path.ends_with("/messages") {
        let reason = if refusal { "refusal" } else { "max_tokens" };
        Some(format!(
            "data: {{\"type\":\"message_delta\",\"delta\":{{\"stop_reason\":\"{reason}\"}}}}\n\ndata: {{\"type\":\"message_stop\"}}\n\n"
        ))
    } else {
        None
    }
}

fn mixed_structured_sse(path: &str) -> Option<String> {
    if path.ends_with("/chat/completions") {
        Some(concat!(
            "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[",
            "{\"index\":0,\"id\":\"search_1\",\"type\":\"function\",\"function\":{\"name\":\"search\",\"arguments\":\"{\\\"pattern\\\":\\\"苹果\\\",\\\"scope\\\":\\\"input\\\"}\"}},",
            "{\"index\":1,\"id\":\"submit_1\",\"type\":\"function\",\"function\":{\"name\":\"formic_submit_result\",\"arguments\":\"{\\\"answer\\\":\\\"ok\\\"}\"}}",
            "]},\"finish_reason\":null}]}\n\n",
            "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n",
            "data: [DONE]\n\n",
        )
        .to_string())
    } else if path.ends_with("/responses") {
        Some(concat!(
            "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc1\",\"call_id\":\"search_1\",\"name\":\"search\",\"arguments\":\"{\\\"pattern\\\":\\\"苹果\\\",\\\"scope\\\":\\\"input\\\"}\"}}\n\n",
            "data: {\"type\":\"response.output_item.done\",\"output_index\":1,\"item\":{\"type\":\"function_call\",\"id\":\"fc2\",\"call_id\":\"submit_1\",\"name\":\"formic_submit_result\",\"arguments\":\"{\\\"answer\\\":\\\"ok\\\"}\"}}\n\n",
            "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\n\n",
        )
        .to_string())
    } else if path.ends_with("/messages") {
        Some(concat!(
            "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"search_1\",\"name\":\"search\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"pattern\\\":\\\"苹果\\\",\\\"scope\\\":\\\"input\\\"}\"}}\n\n",
            "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
            "data: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"tool_use\",\"id\":\"submit_1\",\"name\":\"formic_submit_result\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"answer\\\":\\\"ok\\\"}\"}}\n\n",
            "data: {\"type\":\"content_block_stop\",\"index\":1}\n\n",
            "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"}}\n\n",
            "data: {\"type\":\"message_stop\"}\n\n",
        )
        .to_string())
    } else {
        None
    }
}

fn sse_for(path: &str, tool_call: bool) -> Option<&'static str> {
    match (path, tool_call) {
        (p, true) if p.ends_with("/chat/completions") => Some(COMPLETIONS_TOOLCALL),
        (p, false) if p.ends_with("/chat/completions") => Some(COMPLETIONS_FINAL),
        (p, true) if p.ends_with("/responses") => Some(RESPONSES_TOOLCALL),
        (p, false) if p.ends_with("/responses") => Some(RESPONSES_FINAL),
        (p, true) if p.ends_with("/messages") => Some(ANTHROPIC_TOOLCALL),
        (p, false) if p.ends_with("/messages") => Some(ANTHROPIC_FINAL),
        _ => None,
    }
}

/// 请求体里协议对应的「已携带工具结果」标记。
fn tool_result_marker(path: &str) -> &'static str {
    if path.ends_with("/chat/completions") {
        "\"role\":\"tool\""
    } else if path.ends_with("/responses") {
        "function_call_output"
    } else {
        "tool_result"
    }
}

fn tools_value(body: &str) -> serde_json::Value {
    serde_json::from_str::<serde_json::Value>(body).unwrap()["tools"].clone()
}

#[derive(Default)]
struct MixedRejectionHistory {
    sequence: Vec<String>,
    calls: Vec<(String, String)>,
    results: Vec<(String, String)>,
}

fn assert_mixed_rejection_history(protocol: &str, body: &str) {
    let value: serde_json::Value = serde_json::from_str(body).unwrap();
    let history = match protocol {
        "completions" => {
            let messages = value["messages"].as_array().unwrap();
            let mut history = MixedRejectionHistory::default();
            for message in messages {
                if let Some(tool_calls) = message["tool_calls"].as_array() {
                    history.sequence.push("assistant".to_string());
                    history.calls.extend(tool_calls.iter().map(|call| {
                        (
                            call["id"].as_str().unwrap().to_string(),
                            call["function"]["name"].as_str().unwrap().to_string(),
                        )
                    }));
                } else if message["role"] == "tool" {
                    history.sequence.push("tool_result".to_string());
                    history.results.push((
                        message["tool_call_id"].as_str().unwrap().to_string(),
                        message["content"].as_str().unwrap().to_string(),
                    ));
                }
            }
            history
        }
        "responses" => {
            let input = value["input"].as_array().unwrap();
            let mut history = MixedRejectionHistory::default();
            for item in input {
                match item["type"].as_str() {
                    Some("function_call") => {
                        history.sequence.push("function_call".to_string());
                        history.calls.push((
                            item["call_id"].as_str().unwrap().to_string(),
                            item["name"].as_str().unwrap().to_string(),
                        ));
                    }
                    Some("function_call_output") => {
                        history.sequence.push("function_call_output".to_string());
                        history.results.push((
                            item["call_id"].as_str().unwrap().to_string(),
                            item["output"].as_str().unwrap().to_string(),
                        ));
                    }
                    _ => {}
                }
            }
            history
        }
        "anthropic" => {
            let messages = value["messages"].as_array().unwrap();
            let mut history = MixedRejectionHistory::default();
            for part in messages
                .iter()
                .filter_map(|message| message["content"].as_array())
                .flatten()
            {
                match part["type"].as_str() {
                    Some("tool_use") => {
                        history.sequence.push("tool_use".to_string());
                        history.calls.push((
                            part["id"].as_str().unwrap().to_string(),
                            part["name"].as_str().unwrap().to_string(),
                        ));
                    }
                    Some("tool_result") => {
                        history.sequence.push("tool_result".to_string());
                        history.results.push((
                            part["tool_use_id"].as_str().unwrap().to_string(),
                            part["content"].as_str().unwrap().to_string(),
                        ));
                    }
                    _ => {}
                }
            }
            history
        }
        other => panic!("未知测试协议 {other}"),
    };

    let expected_sequence: &[&str] = match protocol {
        "completions" => &["assistant", "tool_result", "tool_result"],
        "responses" => &[
            "function_call",
            "function_call",
            "function_call_output",
            "function_call_output",
        ],
        "anthropic" => &["tool_use", "tool_use", "tool_result", "tool_result"],
        _ => unreachable!(),
    };
    assert_eq!(history.sequence, expected_sequence, "{protocol}: {body}");
    assert_eq!(
        history.calls,
        [
            ("search_1".to_string(), "search".to_string()),
            ("submit_1".to_string(), "formic_submit_result".to_string()),
        ],
        "{protocol}: {body}"
    );
    assert_eq!(
        history
            .results
            .iter()
            .map(|(id, _)| id.as_str())
            .collect::<Vec<_>>(),
        ["search_1", "submit_1"],
        "{protocol}: {body}"
    );
    assert!(
        history.results[0].1.contains("工具 search 未执行"),
        "{protocol}: {body}"
    );
    assert!(
        history.results[1]
            .1
            .contains("必须在不含文本和其他工具调用的回合中单独出现"),
        "{protocol}: {body}"
    );
    assert!(!body.contains("a.txt:1:"), "{protocol}: {body}");
}

fn assert_minimal_llm_body(protocol: &str, body: &str) {
    let value: serde_json::Value = serde_json::from_str(body).unwrap();
    let allowed: &[&str] = match protocol {
        "completions" => &["model", "stream", "messages", "tools"],
        "responses" => &["model", "stream", "instructions", "input", "tools"],
        "anthropic" => &[
            "model",
            "max_tokens",
            "stream",
            "system",
            "messages",
            "tools",
        ],
        other => panic!("未知测试协议 {other}"),
    };
    for key in value.as_object().unwrap().keys() {
        assert!(
            allowed.contains(&key.as_str()),
            "{protocol} 不得发送额外请求字段 {key}：{body}"
        );
    }
    if protocol == "anthropic" {
        assert_eq!(value["max_tokens"], 16384);
    }
}

struct Recorded {
    path: String,
    body: String,
    authorization: Option<String>,
    user_agent: Option<String>,
    opencode_session: Option<String>,
}

struct MockServer {
    port: u16,
    requests: Arc<Mutex<Vec<Recorded>>>,
    max_in_flight: Arc<AtomicUsize>,
}

#[derive(Debug)]
struct McpHttpRecorded {
    rpc_method: String,
    authorization: Option<String>,
    custom_header: Option<String>,
    session_id: Option<String>,
}

struct McpHttpMock {
    port: u16,
    requests: Arc<Mutex<Vec<McpHttpRecorded>>>,
}

fn start_mcp_http_mock() -> McpHttpMock {
    start_mcp_http_mock_with_image(None)
}

fn start_mcp_http_mock_with_image(image_base64: Option<String>) -> McpHttpMock {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let shared = Arc::clone(&requests);
    let image_base64 = image_base64.map(Arc::new);
    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { break };
            let shared = Arc::clone(&shared);
            let image_base64 = image_base64.clone();
            thread::spawn(move || handle_mcp_http(stream, shared, image_base64));
        }
    });
    McpHttpMock { port, requests }
}

fn handle_mcp_http(
    mut stream: TcpStream,
    requests: Arc<Mutex<Vec<McpHttpRecorded>>>,
    image_base64: Option<Arc<String>>,
) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() {
        return;
    }
    let http_method = request_line.split_whitespace().next().unwrap_or("");
    let mut content_length = 0usize;
    let mut authorization = None;
    let mut custom_header = None;
    let mut session_id = None;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).is_err() {
            return;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            break;
        }
        if let Some((name, value)) = trimmed.split_once(':') {
            match name.to_ascii_lowercase().as_str() {
                "content-length" => content_length = value.trim().parse().unwrap_or(0),
                "authorization" => authorization = Some(value.trim().to_string()),
                "x-formic-test" => custom_header = Some(value.trim().to_string()),
                "mcp-session-id" => session_id = Some(value.trim().to_string()),
                _ => {}
            }
        }
    }
    let mut body = vec![0; content_length];
    reader.read_exact(&mut body).ok();
    let value: serde_json::Value = serde_json::from_slice(&body).unwrap_or_default();
    let rpc_method = value["method"].as_str().unwrap_or(http_method).to_string();
    requests.lock().unwrap().push(McpHttpRecorded {
        rpc_method: rpc_method.clone(),
        authorization,
        custom_header,
        session_id,
    });

    if http_method == "DELETE" {
        write_http_response(&mut stream, "200 OK", None, "");
        return;
    }
    let Some(id) = value.get("id").cloned() else {
        write_http_response(&mut stream, "202 Accepted", None, "");
        return;
    };
    let result = match rpc_method.as_str() {
        "initialize" => serde_json::json!({
            "protocolVersion": value.pointer("/params/protocolVersion").cloned().unwrap(),
            "capabilities": {"tools": {"listChanged": true}},
            "serverInfo": {"name":"http-test-mcp","version":"1"}
        }),
        "tools/list" if value.pointer("/params/cursor").is_none() => serde_json::json!({
            "tools":[{
                "name":"echo",
                "description":"HTTP echo",
                "inputSchema":{
                    "type":"object",
                    "properties":{"text":{"type":"string"}},
                    "required":["text"],
                    "additionalProperties":false
                }
            }],
            "nextCursor":"page-2"
        }),
        "tools/list" => serde_json::json!({"tools":[]}),
        "tools/call" => match image_base64 {
            Some(image) => serde_json::json!({
                "content":[
                    {"type":"text","text":"http:image"},
                    {"type":"image","data":image.as_str(),"mimeType":"image/png"}
                ],
                "isError":false
            }),
            None => serde_json::json!({
                "content":[{"type":"text","text":"http:hello"}],
                "structuredContent":{"value":"hello"},
                "isError":false
            }),
        },
        _ => serde_json::json!({}),
    };
    let response = serde_json::json!({"jsonrpc":"2.0","id":id,"result":result}).to_string();
    let new_session = (rpc_method == "initialize").then_some("test-session");
    write_http_response(&mut stream, "200 OK", new_session, &response);
}

fn write_http_response(stream: &mut TcpStream, status: &str, session_id: Option<&str>, body: &str) {
    let session = session_id
        .map(|value| format!("mcp-session-id: {value}\r\n"))
        .unwrap_or_default();
    let response = format!(
        "HTTP/1.1 {status}\r\ncontent-type: application/json\r\n{session}content-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(response.as_bytes()).ok();
}

fn start_mock() -> MockServer {
    start_mock_with_delay(0)
}

fn start_mock_with_delay(delay_ms: u64) -> MockServer {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let in_flight = Arc::new(AtomicUsize::new(0));
    let max_in_flight = Arc::new(AtomicUsize::new(0));
    let flaky_seen = Arc::new(AtomicBool::new(false));
    let scenario_step = Arc::new(AtomicUsize::new(0));
    let shared = Arc::clone(&requests);
    let shared_in_flight = Arc::clone(&in_flight);
    let shared_max = Arc::clone(&max_in_flight);
    let shared_flaky = Arc::clone(&flaky_seen);
    let shared_scenario_step = Arc::clone(&scenario_step);
    thread::spawn(move || {
        for stream in listener.incoming() {
            match stream {
                Ok(stream) => {
                    let shared = Arc::clone(&shared);
                    let in_flight = Arc::clone(&shared_in_flight);
                    let max = Arc::clone(&shared_max);
                    let flaky = Arc::clone(&shared_flaky);
                    let scenario_step = Arc::clone(&shared_scenario_step);
                    thread::spawn(move || {
                        handle_conn(
                            stream,
                            shared,
                            in_flight,
                            max,
                            flaky,
                            scenario_step,
                            delay_ms,
                        )
                    });
                }
                Err(_) => break,
            }
        }
    });
    MockServer {
        port,
        requests,
        max_in_flight,
    }
}

fn handle_conn(
    mut stream: TcpStream,
    requests: Arc<Mutex<Vec<Recorded>>>,
    in_flight: Arc<AtomicUsize>,
    max_in_flight: Arc<AtomicUsize>,
    flaky_seen: Arc<AtomicBool>,
    scenario_step: Arc<AtomicUsize>,
    delay_ms: u64,
) {
    let current = in_flight.fetch_add(1, Ordering::SeqCst) + 1;
    max_in_flight.fetch_max(current, Ordering::SeqCst);
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() {
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }
    let path = request_line
        .split_whitespace()
        .nth(1)
        .unwrap_or("")
        .to_string();

    let mut content_length = 0usize;
    let mut authorization = None;
    let mut user_agent = None;
    let mut opencode_session = None;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).is_err() {
            return;
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            break;
        }
        if let Some((name, value)) = trimmed.split_once(':') {
            match name.to_ascii_lowercase().as_str() {
                "content-length" => content_length = value.trim().parse().unwrap_or(0),
                "authorization" => authorization = Some(value.trim().to_string()),
                "user-agent" => user_agent = Some(value.trim().to_string()),
                "x-opencode-session" => opencode_session = Some(value.trim().to_string()),
                _ => {}
            }
        }
    }
    let mut body = vec![0u8; content_length];
    if reader.read_exact(&mut body).is_err() {
        return;
    }
    let body_text = String::from_utf8_lossy(&body).to_string();
    requests.lock().unwrap().push(Recorded {
        path: path.clone(),
        body: body_text.clone(),
        authorization,
        user_agent,
        opencode_session,
    });

    if body_text.contains(SLOW_QUOTA_MARKER) && !flaky_seen.swap(true, Ordering::SeqCst) {
        write_mock_llm_response_with_slow_body(
            &mut stream,
            "400 Bad Request",
            "",
            &format!(
                r#"{{"error":{{"code":"insufficient_quota","message":"{RESPONSE_SECRET}"}}}}"#
            ),
            400,
        );
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }
    if body_text.contains(SLOW_SERVER_MARKER) && !flaky_seen.swap(true, Ordering::SeqCst) {
        write_mock_llm_response_with_slow_body(
            &mut stream,
            "500 Internal Server Error",
            "",
            &format!(r#"{{"error":{{"message":"{RESPONSE_SECRET}"}}}}"#),
            400,
        );
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }
    if body_text.contains(SLOW_STREAM_MARKER) {
        write_mock_llm_stalled_stream(&mut stream, RESPONSE_SECRET, 300);
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }
    if body_text.contains(FAIL_TWICE_MARKER) && scenario_step.fetch_add(1, Ordering::SeqCst) < 2 {
        write_mock_llm_response(&mut stream, "500 Internal Server Error", "", "transient");
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }
    if body_text.contains(NETWORK_CONTEXT_NETWORK_MARKER)
        && body_text.contains(tool_result_marker(&path))
        && !body_text.contains("formic_submit_compaction")
        && scenario_step
            .compare_exchange(0, 1, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    {
        write_mock_llm_response(
            &mut stream,
            "500 Internal Server Error",
            "",
            "transient-before-context",
        );
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }
    if body_text.contains(NETWORK_CONTEXT_NETWORK_MARKER)
        && body_text.contains(tool_result_marker(&path))
        && !body_text.contains("formic_submit_compaction")
        && scenario_step
            .compare_exchange(1, 2, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    {
        write_mock_llm_response(
            &mut stream,
            "400 Bad Request",
            "",
            r#"{"error":{"code":"context_length_exceeded"}}"#,
        );
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }

    let early_response = if body_text.contains(AUTH_MARKER) {
        Some((
            "401 Unauthorized".to_string(),
            String::new(),
            format!(r#"{{"error":{{"message":"{RESPONSE_SECRET}"}}}}"#),
        ))
    } else if body_text.contains(FORBIDDEN_MARKER) {
        Some((
            "403 Forbidden".to_string(),
            String::new(),
            format!(r#"{{"error":{{"message":"{RESPONSE_SECRET}"}}}}"#),
        ))
    } else if body_text.contains(QUOTA_MARKER) {
        Some((
            "429 Too Many Requests".to_string(),
            "retry-after: 600\r\n".to_string(),
            format!(r#"{{"error":{{"code":"insufficient_quota","message":"{RESPONSE_SECRET}"}}}}"#),
        ))
    } else if body_text.contains(ACCOUNT_MARKER) {
        Some((
            "400 Bad Request".to_string(),
            String::new(),
            format!(
                r#"{{"error":{{"code":"account_deactivated","message":"{RESPONSE_SECRET}"}}}}"#
            ),
        ))
    } else if body_text.contains(RATE_LONG_MARKER) {
        Some((
            "429 Too Many Requests".to_string(),
            "retry-after: 600\r\n".to_string(),
            format!(r#"{{"error":{{"message":"{RESPONSE_SECRET}"}}}}"#),
        ))
    } else if body_text.contains(SERVER_LONG_MARKER) {
        Some((
            "503 Service Unavailable".to_string(),
            "retry-after: 600\r\n".to_string(),
            format!(r#"{{"error":{{"message":"{RESPONSE_SECRET}"}}}}"#),
        ))
    } else if body_text.contains(RATE_NUMERIC_MARKER) && !flaky_seen.swap(true, Ordering::SeqCst) {
        Some((
            "429 Too Many Requests".to_string(),
            "retry-after: 1\r\n".to_string(),
            "{}".to_string(),
        ))
    } else if body_text.contains(RATE_DATE_MARKER) && !flaky_seen.swap(true, Ordering::SeqCst) {
        let deadline = chrono::Utc::now() + chrono::Duration::seconds(2);
        Some((
            "429 Too Many Requests".to_string(),
            format!(
                "retry-after: {}\r\n",
                deadline.format("%a, %d %b %Y %H:%M:%S GMT")
            ),
            "{}".to_string(),
        ))
    } else if body_text.contains(BAD_PROTOCOL_MARKER) {
        Some((
            "200 OK".to_string(),
            String::new(),
            format!("data: {{\"secret\":\"{RESPONSE_SECRET}\",\"choices\":\"bad\"}}\n\n"),
        ))
    } else if body_text.contains(EOF_WITHOUT_FINISH_MARKER)
        || body_text.contains(DONE_WITHOUT_FINISH_MARKER)
    {
        let done = if body_text.contains(DONE_WITHOUT_FINISH_MARKER) {
            "data: [DONE]\n\n"
        } else {
            ""
        };
        Some((
            "200 OK".to_string(),
            String::new(),
            format!(
                "data: {{\"id\":\"c1\",\"choices\":[{{\"index\":0,\"delta\":{{\"content\":\"partial\",\"tool_calls\":null}},\"finish_reason\":null}}]}}\n\n{done}"
            ),
        ))
    } else {
        None
    };
    if let Some((status, extra_headers, response_body)) = early_response {
        write_mock_llm_response(&mut stream, &status, &extra_headers, &response_body);
        in_flight.fetch_sub(1, Ordering::SeqCst);
        return;
    }

    let (status, response_body) = if body_text.contains(SELF_TEST_MARKER) {
        match sse_for(&path, false) {
            Some(sse) => ("200 OK", sse.to_string()),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(NETWORK_CONTEXT_NETWORK_MARKER)
        && body_text.contains("此前历史的已验证压缩摘要")
        && scenario_step
            .compare_exchange(2, 3, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    {
        ("500 Internal Server Error", "transient".to_string())
    } else if body_text.contains(FAIL_MARKER) {
        ("500 Internal Server Error", "boom".to_string())
    } else if body_text.contains(FLAKY_MARKER) && !flaky_seen.swap(true, Ordering::SeqCst) {
        ("500 Internal Server Error", "transient".to_string())
    } else if body_text.contains(SEGMENTED_COMPACTION_MARKER) {
        if body_text.contains("formic_submit_compaction") {
            if body_text.contains("FAIL-FRAGMENT-UNIT")
                && scenario_step.fetch_add(1, Ordering::SeqCst) > 0
            {
                ("404 Not Found", "{}".to_string())
            } else if body_text.contains("INVALID-ALL-FRAGMENTS")
                || (body_text.contains("CORRECT-FIRST-FRAGMENT")
                    && scenario_step.fetch_add(1, Ordering::SeqCst) == 0)
            {
                (
                    "200 OK",
                    tool_calls_sse(
                        &path,
                        &[("compact", "formic_submit_compaction", serde_json::json!({}))],
                    ),
                )
            } else {
                let facts: Vec<_> = COMPACTION_PROOFS
                    .iter()
                    .filter(|proof| body_text.contains(**proof))
                    .collect();
                (
                    "200 OK",
                    tool_calls_sse(
                        &path,
                        &[(
                            "compact",
                            "formic_submit_compaction",
                            serde_json::json!({"summary":"已核对收到的历史片段","verified_facts":facts,
                        "evidence":facts,"remaining_work":["完成当前任务"]}),
                        )],
                    ),
                )
            }
        } else if body_text.contains("此前历史的已验证压缩摘要") {
            if COMPACTION_PROOFS
                .iter()
                .all(|proof| body_text.contains(proof))
            {
                ("200 OK", sse_for(&path, false).unwrap().to_string())
            } else {
                ("404 Not Found", "{}".to_string())
            }
        } else if body_text.contains("PREVIOUS-TURN-UNIT") && !body_text.contains("prelude-read") {
            (
                "200 OK",
                tool_calls_sse(
                    &path,
                    &[(
                        "prelude-read",
                        "read",
                        serde_json::json!({"scope":"input","path":"a.txt"}),
                    )],
                ),
            )
        } else {
            (
                "200 OK",
                tool_calls_sse(
                    &path,
                    &[
                        (
                            "big-read",
                            "read",
                            serde_json::json!({"scope":"input","path":"big.txt"}),
                        ),
                        (
                            "tail-read",
                            "read",
                            serde_json::json!({"scope":"input","path":"tail.txt"}),
                        ),
                    ],
                ),
            )
        }
    } else if body_text.contains(COMPACTION_MARKER)
        || body_text.contains("formic_submit_compaction")
        || body_text.contains("此前历史的已验证压缩摘要")
    {
        if body_text.contains("formic_submit_compaction") {
            ("200 OK", COMPLETIONS_COMPACTION_SUBMIT.to_string())
        } else if body_text.contains("此前历史的已验证压缩摘要") {
            ("200 OK", COMPLETIONS_FINAL.to_string())
        } else {
            ("200 OK", COMPLETIONS_READ_FOR_COMPACTION.to_string())
        }
    } else if body_text.contains(INVALID_STRUCTURED_MARKER) {
        let corrected = body_text.contains(tool_result_marker(&path));
        match structured_sse_with_validity(&path, corrected) {
            Some(sse) => ("200 OK", sse),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(MIXED_STRUCTURED_MARKER) {
        let corrected = body_text.contains(tool_result_marker(&path));
        let response = if corrected {
            structured_sse_with_validity(&path, true)
        } else {
            mixed_structured_sse(&path)
        };
        match response {
            Some(sse) => ("200 OK", sse),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(EXHAUST_STRUCTURED_MARKER) {
        match structured_sse_with_validity(&path, false) {
            Some(sse) => ("200 OK", sse),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(REFUSAL_MARKER) {
        match terminal_sse(&path, true) {
            Some(sse) => ("200 OK", sse),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(TRUNCATION_MARKER) {
        match terminal_sse(&path, false) {
            Some(sse) => ("200 OK", sse),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(STRUCTURED_MARKER) {
        match structured_sse(&path) {
            Some(sse) => ("200 OK", sse.to_string()),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(IMAGE_TOOL_MARKER) {
        let is_second_turn = body_text.contains(tool_result_marker(&path));
        let response = if is_second_turn {
            sse_for(&path, false)
        } else {
            image_tool_sse(&path)
        };
        match response {
            Some(sse) => ("200 OK", sse.to_string()),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else if body_text.contains(MCP_FALLBACK_FLOW_MARKER) {
        if !path.ends_with("/chat/completions") {
            ("404 Not Found", "unknown path".to_string())
        } else if body_text.contains("\"tool_call_id\":\"call_backup\"") {
            ("200 OK", COMPLETIONS_FINAL.to_string())
        } else if body_text.contains("\"tool_call_id\":\"call_slow\"") {
            ("200 OK", COMPLETIONS_BACKUP_MCP_TOOLCALL.to_string())
        } else {
            (
                "200 OK",
                COMPLETIONS_TIMEOUT_AND_FAST_MCP_TOOLCALL.to_string(),
            )
        }
    } else if body_text.contains(MCP_ERROR_MARKER) {
        let is_second_turn = body_text.contains(tool_result_marker(&path));
        if path.ends_with("/chat/completions") && !is_second_turn {
            ("200 OK", COMPLETIONS_FAILED_MCP_TOOLCALL.to_string())
        } else {
            match sse_for(&path, false) {
                Some(sse) => ("200 OK", sse.to_string()),
                None => ("404 Not Found", "unknown path".to_string()),
            }
        }
    } else if body_text.contains(MCP_PARALLEL_MARKER) {
        let is_second_turn = body_text.contains(tool_result_marker(&path));
        if path.ends_with("/chat/completions") && !is_second_turn {
            ("200 OK", COMPLETIONS_PARALLEL_MCP_TOOLCALL.to_string())
        } else {
            match sse_for(&path, false) {
                Some(sse) => ("200 OK", sse.to_string()),
                None => ("404 Not Found", "unknown path".to_string()),
            }
        }
    } else if body_text.contains(MCP_TIMEOUT_MARKER) {
        let is_second_turn = body_text.contains(tool_result_marker(&path));
        if path.ends_with("/chat/completions") && !is_second_turn {
            ("200 OK", COMPLETIONS_SLOW_MCP_TOOLCALL.to_string())
        } else {
            match sse_for(&path, false) {
                Some(sse) => ("200 OK", sse.to_string()),
                None => ("404 Not Found", "unknown path".to_string()),
            }
        }
    } else if body_text.contains(MCP_MARKER) {
        let is_second_turn = body_text.contains(tool_result_marker(&path));
        if path.ends_with("/chat/completions") && !is_second_turn {
            ("200 OK", COMPLETIONS_MCP_TOOLCALL.to_string())
        } else {
            match sse_for(&path, false) {
                Some(sse) => ("200 OK", sse.to_string()),
                None => ("404 Not Found", "unknown path".to_string()),
            }
        }
    } else if body_text.contains(LOOP_MARKER) {
        match sse_for(&path, true) {
            Some(sse) => ("200 OK", sse.to_string()),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    } else {
        let is_second_turn = body_text.contains(tool_result_marker(&path));
        match sse_for(&path, !is_second_turn) {
            Some(sse) => ("200 OK", sse.to_string()),
            None => ("404 Not Found", "unknown path".to_string()),
        }
    };
    if delay_ms > 0 {
        thread::sleep(std::time::Duration::from_millis(delay_ms));
    }
    write_mock_llm_response(&mut stream, status, "", &response_body);
    in_flight.fetch_sub(1, Ordering::SeqCst);
}

fn write_mock_llm_response(stream: &mut TcpStream, status: &str, extra_headers: &str, body: &str) {
    let response = format!(
        "HTTP/1.1 {status}\r\ncontent-type: text/event-stream\r\n{extra_headers}content-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(response.as_bytes()).ok();
}

fn write_mock_llm_response_with_slow_body(
    stream: &mut TcpStream,
    status: &str,
    extra_headers: &str,
    body: &str,
    delay_ms: u64,
) {
    let headers = format!(
        "HTTP/1.1 {status}\r\ncontent-type: application/json\r\n{extra_headers}content-length: {}\r\nconnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(headers.as_bytes()).unwrap();
    stream.flush().unwrap();
    thread::sleep(std::time::Duration::from_millis(delay_ms));
    stream.write_all(body.as_bytes()).ok();
}

fn write_mock_llm_stalled_stream(stream: &mut TcpStream, secret: &str, delay_ms: u64) {
    let prefix = format!(r#"data: {{"secret":"{secret}""#);
    let suffix = ",\"choices\":[]}\n\n";
    let headers = format!(
        "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
        prefix.len() + suffix.len()
    );
    stream.write_all(headers.as_bytes()).unwrap();
    stream.write_all(prefix.as_bytes()).unwrap();
    stream.flush().unwrap();
    thread::sleep(std::time::Duration::from_millis(delay_ms));
    stream.write_all(suffix.as_bytes()).ok();
}

/// 搭一个两单元作业：单元 1 文件清单形状，单元 2 行区间形状。
/// marker 为 Some 时注入到单元 2 的分片内容里（FAIL-UNIT / LOOP-MARKER）。
fn write_job(dir: &Path, marker: Option<&str>) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let data = dir.join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "苹果是水果。\n香蕉也是。\n").unwrap();
    let big = match marker {
        Some(m) => format!("一\n{m}\n三\n"),
        None => "一\n二\n三\n".to_string(),
    };
    fs::write(data.join("big.txt"), big).unwrap();

    let plan = dir.join("plan.jsonl");
    fs::write(
        &plan,
        "{\"unit\":1,\"files\":[\"a.txt\"]}\n{\"unit\":2,\"file\":\"big.txt\",\"start\":2,\"end\":3}\n",
    )
    .unwrap();
    let task = dir.join("task.md");
    fs::write(&task, "判断分片内容，给出结论。\n").unwrap();
    let out = dir.join("out");
    (data, plan, task, out)
}

fn write_many_job(dir: &Path, count: u64, marker: &str) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let data = dir.join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "共享输入。\n").unwrap();
    let plan = dir.join("plan.jsonl");
    let plan_text: String = (1..=count)
        .map(|unit| format!("{{\"unit\":{unit},\"files\":[\"a.txt\"]}}\n"))
        .collect();
    fs::write(&plan, plan_text).unwrap();
    let task = dir.join("task.md");
    fs::write(&task, format!("测试供应商状态。{marker}\n")).unwrap();
    let out = dir.join("out");
    (data, plan, task, out)
}

fn write_image_job(
    dir: &Path,
    image_in_shard: bool,
    task_text: &str,
) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let data = dir.join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("note.txt"), "图片旁边的文字。\n").unwrap();
    fs::write(data.join("sample.png"), test_png_bytes()).unwrap();
    let plan = dir.join("plan.jsonl");
    let files = if image_in_shard {
        "[\"note.txt\",\"sample.png\"]"
    } else {
        "[\"note.txt\"]"
    };
    fs::write(&plan, format!("{{\"unit\":1,\"files\":{files}}}\n")).unwrap();
    let task = dir.join("task.md");
    fs::write(&task, task_text).unwrap();
    let out = dir.join("out");
    (data, plan, task, out)
}

fn test_png_bytes() -> Vec<u8> {
    let mut image_bytes = std::io::Cursor::new(Vec::new());
    image::DynamicImage::new_rgb8(3, 2)
        .write_to(&mut image_bytes, image::ImageFormat::Png)
        .unwrap();
    image_bytes.into_inner()
}

fn write_request_policy(dir: &Path, retry_delays_ms: &str, max_retry_after_ms: u64) {
    fs::write(
        dir.join("config.toml"),
        format!("retry_delays_ms = {retry_delays_ms}\nmax_retry_after_ms = {max_retry_after_ms}\n"),
    )
    .unwrap();
}

fn run_formic(
    protocol: &str,
    port: u16,
    concurrency: usize,
    data: &Path,
    plan: &Path,
    task: &Path,
    out: &Path,
) -> Output {
    run_formic_with_access(
        protocol,
        port,
        concurrency,
        data,
        plan,
        task,
        out,
        "published",
    )
}

#[allow(clippy::too_many_arguments)]
fn run_formic_with_access(
    protocol: &str,
    port: u16,
    concurrency: usize,
    data: &Path,
    plan: &Path,
    task: &Path,
    out: &Path,
    worker_output_access: &str,
) -> Output {
    run_formic_with_capabilities(
        protocol,
        port,
        concurrency,
        data,
        plan,
        task,
        out,
        worker_output_access,
        "text",
    )
}

#[allow(clippy::too_many_arguments)]
fn run_formic_with_capabilities(
    protocol: &str,
    port: u16,
    concurrency: usize,
    data: &Path,
    plan: &Path,
    task: &Path,
    out: &Path,
    worker_output_access: &str,
    input_modalities: &str,
) -> Output {
    let mut command =
        formic_command_with_access(concurrency, data, plan, task, out, worker_output_access);
    command.arg("--config").arg(write_mock_config(
        plan.parent().expect("测试计划文件有父目录"),
        protocol,
        port,
        131_072,
        input_modalities,
    ));
    command.output().unwrap()
}

fn run_formic_resume(
    protocol: &str,
    port: u16,
    concurrency: usize,
    data: &Path,
    plan: &Path,
    task: &Path,
    out: &Path,
) -> Output {
    let mut command = formic_command(concurrency, data, plan, task, out);
    command
        .arg("--resume")
        .arg("--config")
        .arg(write_mock_config(
            plan.parent().expect("测试计划文件有父目录"),
            protocol,
            port,
            131_072,
            "text",
        ));
    command.output().unwrap()
}

fn formic_command(
    concurrency: usize,
    data: &Path,
    plan: &Path,
    task: &Path,
    out: &Path,
) -> Command {
    formic_command_with_access(concurrency, data, plan, task, out, "published")
}

fn formic_command_with_access(
    concurrency: usize,
    data: &Path,
    plan: &Path,
    task: &Path,
    out: &Path,
    worker_output_access: &str,
) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_formic"));
    command
        .current_dir(plan.parent().expect("测试计划文件有父目录"))
        .arg("run")
        .arg("--data")
        .arg(data)
        .arg("--plan")
        .arg(plan)
        .arg("--task")
        .arg(task)
        .arg("--out")
        .arg(out)
        .arg("--worker-output-access")
        .arg(worker_output_access)
        .arg("--concurrency")
        .arg(concurrency.to_string());
    command
}

fn formic_test_command(config: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_formic"));
    command.arg("test").arg("--config").arg(config);
    command
}

fn write_mock_config(
    directory: &Path,
    protocol: &str,
    port: u16,
    context_window_tokens: u64,
    input_modalities: &str,
) -> PathBuf {
    let existing_path = directory.join("config.toml");
    let existing = fs::read_to_string(&existing_path).unwrap_or_default();
    if existing
        .lines()
        .any(|line| line.trim_start().starts_with("protocol"))
    {
        return existing_path;
    }
    let path = directory.join("formic-test.toml");
    let modalities = match input_modalities {
        "text" => "[\"text\"]",
        "text,image" => "[\"text\", \"image\"]",
        other => other,
    };
    let anthropic = if protocol == "anthropic" {
        "anthropic_max_tokens = 16384\n"
    } else {
        ""
    };
    fs::write(
        &path,
        format!(
            concat!(
                "protocol = \"{}\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\n",
                "context_window_tokens = {}\n",
                "model_input_modalities = {}\n",
                "{}",
                "{}",
            ),
            protocol, port, context_window_tokens, modalities, anthropic, existing,
        ),
    )
    .unwrap();
    path
}

#[cfg(windows)]
fn make_interruptible(command: &mut Command) {
    use std::os::windows::process::CommandExt;

    // 为子进程建立独立控制台进程组，测试只向该组发送 CTRL_BREAK_EVENT。
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    command.creation_flags(CREATE_NEW_PROCESS_GROUP);
}

#[cfg(unix)]
fn make_interruptible(_command: &mut Command) {}

#[cfg(windows)]
fn send_interrupt(child: &Child) {
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GenerateConsoleCtrlEvent(ctrl_event: u32, process_group_id: u32) -> i32;
    }
    const CTRL_BREAK_EVENT: u32 = 1;
    // SAFETY：child 以 CREATE_NEW_PROCESS_GROUP 启动，id 正是目标进程组；函数不持有指针。
    let sent = unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, child.id()) };
    assert_ne!(sent, 0, "应能向 Formic 子进程组发送 CTRL_BREAK_EVENT");
}

#[cfg(unix)]
fn send_interrupt(child: &Child) {
    unsafe extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }
    let pid = i32::try_from(child.id()).expect("子进程号应能用 pid_t 表示");
    // SAFETY：pid 来自仍存活的直接子进程，SIGINT 不涉及指针或共享内存。
    let sent = unsafe { kill(pid, 2) };
    assert_eq!(sent, 0, "应能向 Formic 子进程发送 SIGINT");
}

fn run_structured_formic(
    protocol: &str,
    port: u16,
    data: &Path,
    plan: &Path,
    task: &Path,
    out: &Path,
    schema: &Path,
) -> Output {
    let mut command = formic_command(2, data, plan, task, out);
    command
        .arg("--output-schema")
        .arg(schema)
        .arg("--config")
        .arg(write_mock_config(
            plan.parent().expect("测试计划文件有父目录"),
            protocol,
            port,
            131_072,
            "text",
        ));
    command.output().unwrap()
}

fn write_answer_schema(directory: &Path) -> PathBuf {
    let schema = directory.join("schema.json");
    fs::write(
        &schema,
        r#"{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}"#,
    )
    .unwrap();
    schema
}

fn stdout_of(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).to_string()
}

fn stderr_of(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).to_string()
}

fn compile_fake_mcp(directory: &Path) -> PathBuf {
    let source = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("fake_mcp_stdio.rs");
    let executable = directory.join(if cfg!(windows) {
        "fake-mcp-stdio.exe"
    } else {
        "fake-mcp-stdio"
    });
    let compiled = Command::new("rustc")
        .arg("--edition=2024")
        .arg(&source)
        .arg("-o")
        .arg(&executable)
        .output()
        .unwrap();
    assert!(
        compiled.status.success(),
        "fake MCP 编译失败：{}",
        String::from_utf8_lossy(&compiled.stderr)
    );
    executable
}

fn toml_string(path: &Path) -> String {
    serde_json::to_string(&path.to_string_lossy()).unwrap()
}

/// 读取当前 run 的 stats.jsonl，返回按单元号索引的 JSON 行。
fn stats_lines(out: &Path) -> Vec<serde_json::Value> {
    fs::read_to_string(run_dir(out).join("stats.jsonl"))
        .unwrap_or_default()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn stats_of(lines: &[serde_json::Value], unit: u64) -> &serde_json::Value {
    lines
        .iter()
        .find(|l| l["unit"] == unit)
        .unwrap_or_else(|| panic!("stats 缺单元 {unit} 的行"))
}

fn run_dir(out: &Path) -> PathBuf {
    let directories = run_dirs(out);
    assert_eq!(directories.len(), 1, "一次测试作业应只有一个运行目录");
    directories.into_iter().next().unwrap()
}

fn run_dirs(out: &Path) -> Vec<PathBuf> {
    let mut directories: Vec<PathBuf> = fs::read_dir(out.join("runs"))
        .expect("缺少 runs 观测目录")
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.is_dir())
        .collect();
    directories.sort();
    directories
}

fn latest_run_dir(out: &Path) -> PathBuf {
    run_dirs(out).pop().expect("缺少运行目录")
}

fn latest_run_summary(out: &Path) -> serde_json::Value {
    run_summary(&latest_run_dir(out))
}

fn run_summary(run: &Path) -> serde_json::Value {
    serde_json::from_slice(&fs::read(run.join("summary.json")).expect("缺少运行汇总")).unwrap()
}

fn read_output_tree(path: &Path) -> String {
    let mut text = String::new();
    for entry in fs::read_dir(path).unwrap() {
        let entry = entry.unwrap();
        if entry.path().is_dir() {
            text.push_str(&read_output_tree(&entry.path()));
        } else {
            text.push_str(&String::from_utf8_lossy(&fs::read(entry.path()).unwrap()));
        }
    }
    text
}

fn assert_summary_identities(summary: &serde_json::Value) {
    let value = |name: &str| summary[name].as_u64().unwrap();
    assert_eq!(
        value("planned"),
        value("already_completed") + value("started") + value("not_started"),
        "计划恒等式不成立：{summary}"
    );
    assert_eq!(
        value("started"),
        value("published") + value("failed") + value("stopped"),
        "本轮状态恒等式不成立：{summary}"
    );
    assert_eq!(
        value("llm_calls"),
        value("llm_calls_with_provider_usage") + value("llm_calls_without_provider_usage"),
        "调用 usage 恒等式不成立：{summary}"
    );
}

fn results_dir(out: &Path) -> PathBuf {
    out.join("results")
}

fn output_tree_snapshot(root: &Path) -> Vec<(PathBuf, Option<Vec<u8>>)> {
    fn collect(root: &Path, current: &Path, entries: &mut Vec<(PathBuf, Option<Vec<u8>>)>) {
        let mut children: Vec<_> = fs::read_dir(current)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect();
        children.sort();
        for child in children {
            let relative = child.strip_prefix(root).unwrap().to_path_buf();
            if child.is_dir() {
                entries.push((relative, None));
                collect(root, &child, entries);
            } else {
                entries.push((relative, Some(fs::read(&child).unwrap())));
            }
        }
    }

    let mut entries = Vec::new();
    collect(root, root, &mut entries);
    entries
}

fn worker_run_dir(out: &Path) -> PathBuf {
    run_dir(out).join("workers")
}

fn worker_report(out: &Path, unit: u64) -> String {
    let directory = worker_run_dir(out);
    assert!(
        fs::read_dir(&directory).unwrap().all(|entry| entry
            .unwrap()
            .path()
            .extension()
            .is_none_or(|ext| ext == "md")),
        "成功渲染后不得残留重复 JSONL 或临时文件：{}",
        directory.display()
    );
    fs::read_to_string(directory.join(format!("{unit}.md"))).unwrap()
}

/// 主成功路径：三协议各跑一次，断言产出、审计、多轮请求、汇总与退出码。
fn assert_success(protocol: &str, expected_path: &str) {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    let mock = start_mock();
    let output = run_formic(protocol, mock.port, 2, &data, &plan, &task, &out);

    assert_eq!(
        output.status.code(),
        Some(0),
        "{protocol} 退出码应为 0：{}",
        stderr_of(&output)
    );
    let stdout = stdout_of(&output);
    assert!(
        stdout.contains("发布 2"),
        "{protocol} 汇总应含完成数：{stdout}"
    );
    assert!(
        stdout.contains("失败 0"),
        "{protocol} 汇总应含失败数：{stdout}"
    );
    let progress = stderr_of(&output);
    assert!(!progress.contains(['\r', '\u{1b}']), "{progress:?}");
    assert!(progress.contains("0%（0/2）"), "{progress}");
    assert!(progress.contains("100%（2/2）"), "{progress}");
    assert!(progress.lines().count() <= 101, "{progress}");

    for unit in [1, 2] {
        assert_eq!(
            fs::read_to_string(results_dir(&out).join(format!("{unit}.md"))).unwrap(),
            FINAL_TEXT,
            "{protocol} 单元 {unit} 产出应为最终回合文本"
        );
        let report = worker_report(&out, unit);
        for section in [
            "状态：准备输入",
            "上下文预算判断",
            "LLM 请求",
            "模型响应",
            "模型请求工具",
            "工具执行事实",
            "工具结果",
        ] {
            assert!(
                report.contains(section),
                "{protocol} 单元 {unit} 运行档案缺 {section}：{report}"
            );
        }
        assert!(
            report.contains("\"pattern\":\"苹果\""),
            "{protocol} 运行档案应含工具参数原文：{report}"
        );
        assert_eq!(
            report.matches("完整模型输入（基准）").count(),
            1,
            "{protocol} 运行档案只应保存首轮完整模型输入：{report}"
        );
        assert!(
            report.contains("本轮模型输入变化（逐字保留）"),
            "{protocol} 后续模型输入应保存可逆变化量：{report}"
        );
        assert!(
            report.contains(&format!("[{unit}.md](../../../results/{unit}.md)")),
            "{protocol} worker 档案应正确链接到 results：{report}"
        );
        assert_eq!(
            report.matches("· 模型响应").count(),
            2,
            "{protocol} 两次模型调用应各有一个解析响应标题：{report}"
        );
        assert!(
            !report.contains("choices")
                && !report.contains("response.output_text.delta")
                && !report.contains("encrypted_content")
                && !report.contains("provider-only-state"),
            "{protocol} HTTP/SSE envelope 与 opaque replay payload 不得进入 worker 报告：{report}"
        );
        assert!(report.contains("结局：`published`"), "{report}");
    }

    // 单元统计：轮数、调用数、工具计数、token 估算
    let stats = stats_lines(&out);
    assert_eq!(stats.len(), 2, "{protocol} stats 应有两个单元行");
    for unit in [1, 2] {
        let s = stats_of(&stats, unit);
        assert_eq!(s["outcome"], "published", "{protocol} 单元 {unit}：{s}");
        assert_eq!(s["turns"], 2, "{protocol} 单元 {unit} 应为 2 轮：{s}");
        assert_eq!(
            s["llm_calls"], 2,
            "{protocol} 单元 {unit} 应为 2 次调用：{s}"
        );
        assert_eq!(
            s["tool_calls"]["search"], 1,
            "{protocol} 单元 {unit} 应调用 search 一次：{s}"
        );
        assert!(
            s["input_tokens_est"].as_u64().unwrap() > 0,
            "{protocol} 单元 {unit} input：{s}"
        );
        assert!(
            s["output_tokens_est"].as_u64().unwrap() > 0,
            "{protocol} 单元 {unit} output：{s}"
        );
        assert_eq!(s["llm_calls_with_provider_usage"], 1, "{protocol}：{s}");
        assert_eq!(s["llm_calls_without_provider_usage"], 1, "{protocol}：{s}");
        assert_eq!(s["provider_input_tokens"], 100, "{protocol}：{s}");
        assert_eq!(s["provider_output_tokens"], 5, "{protocol}：{s}");
        assert_eq!(s["provider_cache_read_tokens"], 60, "{protocol}：{s}");
        assert_eq!(s["provider_cache_creation_tokens"], 20, "{protocol}：{s}");
    }

    let requests = mock.requests.lock().unwrap();
    assert_eq!(requests.len(), 4, "{protocol} 每单元两轮共 4 次请求");
    assert!(
        requests.iter().all(|r| r.path.ends_with(expected_path)),
        "{protocol} 请求路径应为 {expected_path}"
    );
    for request in requests.iter() {
        assert_minimal_llm_body(protocol, &request.body);
    }
    // 并发下请求不按单元顺序到达，按多重集断言：两个首轮（携带 tools schema），
    // 两个次轮（携带协议工具结果消息与 search 命中行）
    let marker = tool_result_marker(expected_path);
    let first_turns: Vec<_> = requests
        .iter()
        .filter(|r| !r.body.contains(marker))
        .collect();
    let second_turns: Vec<_> = requests
        .iter()
        .filter(|r| r.body.contains(marker))
        .collect();
    assert_eq!(first_turns.len(), 2, "{protocol} 应有两个首轮请求");
    assert_eq!(second_turns.len(), 2, "{protocol} 应有两个次轮请求");
    assert!(
        first_turns.iter().all(|r| r.body.contains("\"search\"")),
        "{protocol} 首轮请求应携带 tools schema"
    );
    assert!(
        second_turns.iter().all(|r| r.body.contains("苹果是水果")),
        "{protocol} 次轮请求应回注 search 结果"
    );
    if protocol == "responses" {
        assert!(
            second_turns
                .iter()
                .all(|request| request.body.contains("provider-only-state")),
            "Responses 次轮必须原样重放供应商 reasoning item"
        );
        assert!(
            second_turns.iter().all(|request| request
                .body
                .matches("\"type\":\"function_call\"")
                .count()
                == 1),
            "Responses 原始 function_call 不得再合成一份重复项"
        );
    }
    let frozen_tools = tools_value(&requests[0].body);
    assert!(
        requests
            .iter()
            .all(|request| tools_value(&request.body) == frozen_tools),
        "{protocol} 的工具名称、顺序和 schema 必须在全部单元与回合中保持一致"
    );
}

#[test]
fn completions_success() {
    assert_success("completions", "/chat/completions");
}

#[test]
fn responses_success() {
    assert_success("responses", "/responses");
}

#[test]
fn anthropic_success() {
    assert_success("anthropic", "/messages");
}

fn request_contains_image(protocol: &str, body: &serde_json::Value) -> bool {
    match protocol {
        "completions" => body["messages"].as_array().unwrap().iter().any(|message| {
            message["content"]
                .as_array()
                .is_some_and(|parts| parts.iter().any(|part| part["type"] == "image_url"))
        }),
        "responses" => body["input"].as_array().unwrap().iter().any(|item| {
            item["content"]
                .as_array()
                .is_some_and(|parts| parts.iter().any(|part| part["type"] == "input_image"))
        }),
        "anthropic" => body["messages"].as_array().unwrap().iter().any(|message| {
            message["content"]
                .as_array()
                .is_some_and(|parts| parts.iter().any(|part| part["type"] == "image"))
        }),
        _ => false,
    }
}

#[test]
fn initial_mixed_image_shards_reach_all_protocols_without_public_base64() {
    for protocol in ["completions", "responses", "anthropic"] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_image_job(dir.path(), true, "解释当前文字和图片。\n");
        let mock = start_mock();
        let output = run_formic_with_capabilities(
            protocol,
            mock.port,
            1,
            &data,
            &plan,
            &task,
            &out,
            "none",
            "text,image",
        );
        assert_eq!(
            output.status.code(),
            Some(0),
            "{protocol}: {}",
            stderr_of(&output)
        );
        let requests = mock.requests.lock().unwrap();
        assert_eq!(requests.len(), 2, "{protocol}");
        for request in requests.iter() {
            let body: serde_json::Value = serde_json::from_str(&request.body).unwrap();
            assert!(
                request_contains_image(protocol, &body),
                "{protocol}: {body}"
            );
            assert!(request.body.contains("read_image"), "{protocol}");
        }
        drop(requests);

        let report = worker_report(&out, 1);
        assert!(report.contains("sample.png"), "{protocol}: {report}");
        assert!(report.contains("image/png"), "{protocol}: {report}");
        assert!(
            report.contains("模型输入模态：`text`, `image`"),
            "{protocol}"
        );
        let public = read_output_tree(&out);
        assert!(!public.contains("data:image/png;base64,"), "{protocol}");
        assert!(
            !public.contains("\"source\":{\"type\":\"base64\""),
            "{protocol}"
        );
        let manifest: serde_json::Value =
            serde_json::from_slice(&fs::read(out.join(".formic-job.json")).unwrap()).unwrap();
        assert_eq!(
            manifest["model_input_modalities"],
            serde_json::json!(["text", "image"])
        );

        let mut resume = formic_command_with_access(1, &data, &plan, &task, &out, "none");
        resume
            .arg("--resume")
            .arg("--config")
            .arg(write_mock_config(
                dir.path(),
                protocol,
                mock.port,
                131_072,
                "text",
            ));
        let resumed = resume.output().unwrap();
        assert_eq!(resumed.status.code(), Some(2), "{protocol}");
        assert!(stderr_of(&resumed).contains("模型输入模态"), "{protocol}");
        assert_eq!(mock.requests.lock().unwrap().len(), 2, "{protocol}");
    }
}

#[test]
fn multi_tool_image_results_follow_all_tool_results_in_all_protocols() {
    for protocol in ["completions", "responses", "anthropic"] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_image_job(
            dir.path(),
            false,
            &format!("调用文字和图片工具。{IMAGE_TOOL_MARKER}\n"),
        );
        let mock = start_mock();
        let output = run_formic_with_capabilities(
            protocol,
            mock.port,
            1,
            &data,
            &plan,
            &task,
            &out,
            "none",
            "text,image",
        );
        assert_eq!(
            output.status.code(),
            Some(0),
            "{protocol}: {}",
            stderr_of(&output)
        );
        let requests = mock.requests.lock().unwrap();
        assert_eq!(requests.len(), 2, "{protocol}");
        let first: serde_json::Value = serde_json::from_str(&requests[0].body).unwrap();
        let second: serde_json::Value = serde_json::from_str(&requests[1].body).unwrap();
        assert!(!request_contains_image(protocol, &first), "{protocol}");
        assert!(
            request_contains_image(protocol, &second),
            "{protocol}: {second}"
        );
        match protocol {
            "completions" => {
                let messages = second["messages"].as_array().unwrap();
                let tool_positions: Vec<_> = messages
                    .iter()
                    .enumerate()
                    .filter_map(|(index, message)| (message["role"] == "tool").then_some(index))
                    .collect();
                let image_position = messages
                    .iter()
                    .position(|message| {
                        message["content"].as_array().is_some_and(|parts| {
                            parts.iter().any(|part| part["type"] == "image_url")
                        })
                    })
                    .unwrap();
                assert_eq!(tool_positions.len(), 2);
                assert!(
                    tool_positions
                        .into_iter()
                        .all(|index| index < image_position)
                );
            }
            "responses" => {
                let input = second["input"].as_array().unwrap();
                let outputs: Vec<_> = input
                    .iter()
                    .enumerate()
                    .filter_map(|(index, item)| {
                        (item["type"] == "function_call_output").then_some(index)
                    })
                    .collect();
                let image_position = input
                    .iter()
                    .position(|item| {
                        item["content"].as_array().is_some_and(|parts| {
                            parts.iter().any(|part| part["type"] == "input_image")
                        })
                    })
                    .unwrap();
                assert_eq!(outputs.len(), 2);
                assert!(outputs.into_iter().all(|index| index < image_position));
            }
            "anthropic" => {
                let media_user = second["messages"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|message| {
                        message["content"]
                            .as_array()
                            .is_some_and(|parts| parts.iter().any(|part| part["type"] == "image"))
                    })
                    .unwrap();
                let types: Vec<_> = media_user["content"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|part| part["type"].as_str().unwrap())
                    .collect();
                assert_eq!(&types[..2], ["tool_result", "tool_result"]);
                assert!(types[2..].contains(&"image"));
            }
            _ => unreachable!(),
        }
        drop(requests);
        let report = worker_report(&out, 1);
        assert!(report.contains("工具图片"), "{protocol}: {report}");
        assert!(
            report.contains("input 相对路径：`sample.png`"),
            "{protocol}: {report}"
        );
        assert!(!out.join("runs/run-000001/media").exists());
        assert!(!read_output_tree(&out).contains("data:image/png;base64,"));
    }
}

#[test]
fn mcp_images_are_archived_once_and_only_references_enter_public_audit() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_image_job(
        dir.path(),
        false,
        &format!("调用 MCP 获取图片。{MCP_MARKER}\n"),
    );
    let llm = start_mock();
    let image_bytes = test_png_bytes();
    let encoded = base64::engine::general_purpose::STANDARD.encode(&image_bytes);
    let mcp = start_mcp_http_mock_with_image(Some(encoded.clone()));
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol='completions'\n",
                "url='http://127.0.0.1:{}/v1'\n",
                "model='test-model'\n",
                "context_window_tokens=131072\n",
                "model_input_modalities=['text','image']\n",
                "[mcp_servers.demo]\nenabled=true\nurl='http://127.0.0.1:{}/mcp'\n",
            ),
            llm.port, mcp.port,
        ),
    )
    .unwrap();

    let output = run_formic_with_capabilities(
        "completions",
        llm.port,
        1,
        &data,
        &plan,
        &task,
        &out,
        "none",
        "text,image",
    );
    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    let requests = llm.requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    let second: serde_json::Value = serde_json::from_str(&requests[1].body).unwrap();
    assert!(request_contains_image("completions", &second));
    drop(requests);

    let media = out.join("runs/run-000001/media/1/1.png");
    assert_eq!(fs::read(&media).unwrap(), image_bytes);
    assert_eq!(
        fs::read_dir(media.parent().unwrap()).unwrap().count(),
        1,
        "一次 MCP 图片只应留下一个自然编号文件"
    );
    let report = worker_report(&out, 1);
    assert!(
        report.contains("[media/1/1.png](../media/1/1.png)"),
        "{report}"
    );
    assert!(!report.contains(&encoded));
    let public = read_output_tree(&out);
    assert!(!public.contains("data:image/png;base64,"));
    assert!(!public.contains(&encoded));
    assert_eq!(
        mcp.requests
            .lock()
            .unwrap()
            .iter()
            .filter(|request| request.rpc_method == "tools/call")
            .count(),
        1,
        "已完成的 MCP 调用不能因本地处理再次发送"
    );
}

#[test]
fn mcp_media_archive_failure_fails_worker_without_replaying_remote_call() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_image_job(
        dir.path(),
        false,
        &format!("调用 MCP 获取图片。{MCP_MARKER}\n"),
    );
    let llm = start_mock_with_delay(300);
    let encoded = base64::engine::general_purpose::STANDARD.encode(test_png_bytes());
    let mcp = start_mcp_http_mock_with_image(Some(encoded.clone()));
    fs::write(
        dir.path().join("config.toml"),
        format!(
            "[mcp_servers.demo]\nenabled=true\nurl='http://127.0.0.1:{}/mcp'\n",
            mcp.port
        ),
    )
    .unwrap();
    let mut command = formic_command_with_access(1, &data, &plan, &task, &out, "none");
    command
        .arg("--config")
        .arg(write_mock_config(
            dir.path(),
            "completions",
            llm.port,
            131_072,
            "text,image",
        ))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = command.spawn().unwrap();
    let run = out.join("runs/run-000001");
    for _ in 0..200 {
        if run.join("workers").is_dir() {
            break;
        }
        thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(
        run.join("workers").is_dir(),
        "worker run 应在模型响应前建立"
    );
    fs::write(run.join("media"), "阻止创建媒体目录").unwrap();
    let output = child.wait_with_output().unwrap();

    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert_eq!(llm.requests.lock().unwrap().len(), 1, "不得再次请求模型");
    assert_eq!(
        mcp.requests
            .lock()
            .unwrap()
            .iter()
            .filter(|request| request.rpc_method == "tools/call")
            .count(),
        1,
        "留档失败发生在远端调用完成后，不得重放 tools/call"
    );
    let report = worker_report(&out, 1);
    assert!(report.contains("写入输出区失败"), "{report}");
    assert!(!report.contains(&encoded));
}

#[test]
fn worker_output_access_is_required_before_startup() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    let mock = start_mock();
    let config = write_mock_config(dir.path(), "completions", mock.port, 131_072, "text");
    let output = Command::new(env!("CARGO_BIN_EXE_formic"))
        .current_dir(dir.path())
        .arg("run")
        .arg("--data")
        .arg(&data)
        .arg("--plan")
        .arg(&plan)
        .arg("--task")
        .arg(&task)
        .arg("--out")
        .arg(&out)
        .arg("--config")
        .arg(config)
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(2));
    assert!(stderr_of(&output).contains("--worker-output-access"));
    assert!(mock.requests.lock().unwrap().is_empty());
    assert!(!out.exists());
}

#[test]
fn model_input_modalities_are_required_before_startup() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    let mock = start_mock();
    let config = dir.path().join("missing-modalities.toml");
    fs::write(
        &config,
        format!(
            "protocol='completions'\nurl='http://127.0.0.1:{}/v1'\nmodel='test-model'\ncontext_window_tokens=131072\n",
            mock.port
        ),
    )
    .unwrap();
    let mut command = formic_command(1, &data, &plan, &task, &out);
    let output = command.arg("--config").arg(config).output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(stderr_of(&output).contains("model_input_modalities"));
    assert!(mock.requests.lock().unwrap().is_empty());
    assert!(!out.exists());
}

#[test]
fn text_only_models_reject_planned_images_before_mcp_or_llm_requests() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_image_job(dir.path(), true, "不应开始任何远端请求。\n");
    let llm = start_mock();
    let mcp = start_mcp_http_mock();
    fs::write(
        dir.path().join("config.toml"),
        format!(
            "[mcp_servers.demo]\nenabled=true\nurl='http://127.0.0.1:{}/mcp'\n",
            mcp.port
        ),
    )
    .unwrap();
    let output = run_formic_with_capabilities(
        "completions",
        llm.port,
        1,
        &data,
        &plan,
        &task,
        &out,
        "none",
        "text",
    );
    assert_eq!(output.status.code(), Some(2));
    assert!(stderr_of(&output).contains("model_input_modalities"));
    assert!(llm.requests.lock().unwrap().is_empty());
    assert!(mcp.requests.lock().unwrap().is_empty());
}

#[test]
fn isolated_workers_receive_only_input_scope_under_concurrency() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 4, "独立生成元数据");
    let mock = start_mock_with_delay(20);
    let output = run_formic_with_access(
        "completions",
        mock.port,
        4,
        &data,
        &plan,
        &task,
        &out,
        "none",
    );
    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));

    let requests = mock.requests.lock().unwrap();
    assert_eq!(requests.len(), 8);
    for request in requests.iter() {
        let body: serde_json::Value = serde_json::from_str(&request.body).unwrap();
        assert!(
            body["messages"][0]["content"]
                .as_str()
                .unwrap()
                .contains("不能读取其他 worker 的结果")
        );
        for tool in body["tools"].as_array().unwrap() {
            if matches!(tool["function"]["name"].as_str(), Some("read" | "search")) {
                assert_eq!(
                    tool["function"]["parameters"]["properties"]["scope"]["enum"],
                    serde_json::json!(["input"])
                );
            }
        }
    }
    drop(requests);

    for unit in 1..=4 {
        assert!(results_dir(&out).join(format!("{unit}.md")).exists());
    }
    let manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(out.join(".formic-job.json")).unwrap()).unwrap();
    assert_eq!(manifest["worker_output_access"], "none");
    assert!(worker_report(&out, 1).contains("Worker 输出读取：`none`"));
}

#[test]
fn resume_rejects_changed_worker_output_access_before_request() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    let mock = start_mock();
    let first = run_formic("completions", mock.port, 2, &data, &plan, &task, &out);
    assert_eq!(first.status.code(), Some(0), "{}", stderr_of(&first));
    let requests_before = mock.requests.lock().unwrap().len();

    let mut command = formic_command_with_access(2, &data, &plan, &task, &out, "none");
    let resumed = command
        .arg("--resume")
        .arg("--config")
        .arg(write_mock_config(
            dir.path(),
            "completions",
            mock.port,
            131_072,
            "text",
        ))
        .output()
        .unwrap();

    assert_eq!(resumed.status.code(), Some(2));
    assert!(stderr_of(&resumed).contains("worker 输出权限"));
    assert_eq!(mock.requests.lock().unwrap().len(), requests_before);
    assert_eq!(run_dirs(&out).len(), 1, "权限不一致不得创建新的运行档案");
}

#[test]
fn structured_output_succeeds_for_all_protocols() {
    for (protocol, expected_path) in [
        ("completions", "/chat/completions"),
        ("responses", "/responses"),
        ("anthropic", "/messages"),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_job(dir.path(), None);
        fs::write(
            &task,
            format!("按结构化契约提交结果。{STRUCTURED_MARKER}\n"),
        )
        .unwrap();
        let schema = dir.path().join("schema.json");
        fs::write(
            &schema,
            r#"{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}"#,
        )
        .unwrap();
        let mock = start_mock();
        let output = run_structured_formic(protocol, mock.port, &data, &plan, &task, &out, &schema);
        assert_eq!(
            output.status.code(),
            Some(0),
            "{protocol} 结构化模式失败：{}",
            stderr_of(&output)
        );
        for unit in [1, 2] {
            assert_eq!(
                fs::read_to_string(results_dir(&out).join(format!("{unit}.json"))).unwrap(),
                "{\n  \"answer\": \"ok\"\n}\n"
            );
            assert!(!results_dir(&out).join(format!("{unit}.md")).exists());
            let report = worker_report(&out, unit);
            assert!(report.contains("结构化结果校验"), "{report}");
            assert!(report.contains("校验通过：`true`"), "{report}");
        }
        assert!(results_dir(&out).join("output-schema.json").exists());
        let requests = mock.requests.lock().unwrap();
        assert_eq!(requests.len(), 2, "{protocol} 每单元只需一次提交");
        assert!(requests.iter().all(|request| {
            request.path.ends_with(expected_path)
                && request.body.contains("formic_submit_result")
                && request.body.contains("additionalProperties")
        }));
        assert_eq!(
            tools_value(&requests[0].body),
            tools_value(&requests[1].body),
            "{protocol} 的结构化 schema 与工具顺序必须在全部单元中一致"
        );
    }
}

#[test]
fn structured_invalid_and_mixed_turns_are_corrected_for_all_protocols() {
    for protocol in ["completions", "responses", "anthropic"] {
        for (marker, mixed) in [
            (INVALID_STRUCTURED_MARKER, false),
            (MIXED_STRUCTURED_MARKER, true),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let (data, plan, task, out) = write_job(dir.path(), None);
            fs::write(&task, format!("提交结构化结果。{marker}\n")).unwrap();
            let schema = write_answer_schema(dir.path());
            let mock = start_mock();
            let output =
                run_structured_formic(protocol, mock.port, &data, &plan, &task, &out, &schema);
            assert_eq!(
                output.status.code(),
                Some(0),
                "{protocol} marker={marker}：{}",
                stderr_of(&output)
            );
            for unit in [1, 2] {
                assert!(results_dir(&out).join(format!("{unit}.json")).exists());
                let report = worker_report(&out, unit);
                assert!(report.contains("校验通过：`false`"), "{report}");
                assert!(report.contains("校验通过：`true`"), "{report}");
                if mixed {
                    assert!(report.contains("工具 search 未执行"), "{report}");
                    assert!(
                        report.contains(
                            "formic_submit_result 必须在不含文本和其他工具调用的回合中单独出现"
                        ),
                        "{report}"
                    );
                    assert!(
                        report.contains("工具：`search`；来源：`builtin`"),
                        "{report}"
                    );
                    assert!(
                        report.contains("工具：`formic_submit_result`；来源：`internal`"),
                        "{report}"
                    );
                    assert!(!report.contains("工具执行事实"), "{report}");
                }
            }
            let stats = stats_lines(&out);
            for unit in [1, 2] {
                let value = stats_of(&stats, unit);
                assert_eq!(value["structured_corrections"], 1, "{value}");
                if mixed {
                    assert!(
                        value["tool_calls"].as_object().unwrap().is_empty(),
                        "{value}"
                    );
                }
            }
            let requests = mock.requests.lock().unwrap();
            assert_eq!(requests.len(), 4);
            if mixed {
                let corrected: Vec<_> = requests
                    .iter()
                    .filter(|request| request.body.contains(tool_result_marker(&request.path)))
                    .collect();
                assert_eq!(corrected.len(), 2, "{protocol}");
                for request in corrected {
                    assert_mixed_rejection_history(protocol, &request.body);
                }
            }
        }
    }
}

#[test]
fn refusal_truncation_and_structured_exhaustion_publish_nothing() {
    for protocol in ["completions", "responses", "anthropic"] {
        for (marker, expected) in [(REFUSAL_MARKER, "拒绝"), (TRUNCATION_MARKER, "截断")] {
            let dir = tempfile::tempdir().unwrap();
            let (data, plan, task, out) = write_job(dir.path(), None);
            fs::write(&task, format!("终态测试。{marker}\n")).unwrap();
            let mock = start_mock();
            let output = run_formic(protocol, mock.port, 2, &data, &plan, &task, &out);
            assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
            assert!(worker_report(&out, 1).contains(expected));
            assert!(!stderr_of(&output).contains("100%"));
            assert!(!results_dir(&out).join("1.md").exists());
            assert!(!results_dir(&out).join("2.md").exists());
            assert_eq!(mock.requests.lock().unwrap().len(), 2);
        }

        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_job(dir.path(), None);
        fs::write(
            &task,
            format!("持续提交无效结构。{EXHAUST_STRUCTURED_MARKER}\n"),
        )
        .unwrap();
        let schema = write_answer_schema(dir.path());
        let mock = start_mock();
        let output = run_structured_formic(protocol, mock.port, &data, &plan, &task, &out, &schema);
        assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
        assert!(worker_report(&out, 1).contains("连续 5 次无效"));
        assert!(!stderr_of(&output).contains("100%"));
        assert!(!results_dir(&out).join("1.json").exists());
        assert!(!results_dir(&out).join("2.json").exists());
        assert_eq!(mock.requests.lock().unwrap().len(), 10);
    }
}

#[test]
fn context_budget_compacts_complete_tool_group_then_continues() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "small shard\n").unwrap();
    let mut large = String::new();
    for line in 1..=260 {
        large.push_str(&format!(
            "line-{line:04} alpha beta gamma delta epsilon value-{line:04}\n"
        ));
    }
    fs::write(data.join("big.txt"), large).unwrap();
    let plan = dir.path().join("plan.jsonl");
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    let task = dir.path().join("task.md");
    fs::write(
        &task,
        format!("先读取 big.txt，再完成任务。{COMPACTION_MARKER}\n"),
    )
    .unwrap();
    let out = dir.path().join("out");
    let mock = start_mock();
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol='completions'\n",
                "url='http://127.0.0.1:{}/v1'\n",
                "model='test-model'\n",
                "context_window_tokens=5700\n",
                "model_input_modalities=['text']\n",
                "[execution]\ncontext_safety_tokens=500\n",
                "[tools.read]\nmax_result_bytes=30000\n",
            ),
            mock.port,
        ),
    )
    .unwrap();
    let mut command = formic_command(1, &data, &plan, &task, &out);
    let output = command.output().unwrap();
    assert_eq!(
        output.status.code(),
        Some(0),
        "压缩后应继续完成：{}",
        stderr_of(&output)
    );
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    let report = worker_report(&out, 1);
    assert!(report.contains("上下文压缩请求"), "{report}");
    assert!(report.contains("上下文压缩结果"), "{report}");
    assert!(report.contains("校验通过：`true`"), "{report}");
    let stats = stats_lines(&out);
    let value = stats_of(&stats, 1);
    assert_eq!(value["compactions"], 1, "{value}");
    assert!(
        value["compaction_before_tokens"].as_u64().unwrap()
            > value["compaction_after_tokens"].as_u64().unwrap(),
        "{value}"
    );
    let requests = mock.requests.lock().unwrap();
    let kinds: Vec<_> = requests
        .iter()
        .map(|request| {
            if request.body.contains("formic_submit_compaction") {
                "compaction"
            } else if request.body.contains("此前历史的已验证压缩摘要") {
                "after"
            } else {
                "normal"
            }
        })
        .collect();
    assert_eq!(
        requests.len(),
        4,
        "两轮完整工具组后压缩，再继续正常调用：{kinds:?}"
    );
    assert_eq!(kinds, ["normal", "normal", "compaction", "after"]);
}

fn segmented_evidence_text(lines: usize) -> String {
    let mut large = String::from("HEAD_PROOF\n");
    for line in 0..lines {
        if line == lines / 2 {
            large.push_str("MIDDLE_PROOF\n");
        }
        large.push_str(&format!(
            "record-{line:04} 中文😀 quote=\"value-{line:04}\" backslash=\\ 保留全部证据\n"
        ));
    }
    large.push_str("TAIL_PROOF\n");
    large
}

fn write_segmented_compaction_job(
    dir: &Path,
    protocol: &str,
    port: u16,
    extra: &str,
) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    let data = dir.join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "小分片\n").unwrap();
    fs::write(data.join("big.txt"), segmented_evidence_text(1400)).unwrap();
    fs::write(data.join("tail.txt"), "SECOND_RESULT_PROOF\n").unwrap();
    let plan = dir.join("plan.jsonl");
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    let task = dir.join("task.md");
    fs::write(
        &task,
        format!("读取工具证据后完成任务。{SEGMENTED_COMPACTION_MARKER} {extra}\n"),
    )
    .unwrap();
    let anthropic = if protocol == "anthropic" {
        "anthropic_max_tokens=1000\n"
    } else {
        ""
    };
    fs::write(dir.join("config.toml"), format!(
        "protocol='{protocol}'\nurl='http://127.0.0.1:{port}/v1'\nmodel='test-model'\ncontext_window_tokens=7500\nmodel_input_modalities=['text']\n{anthropic}[execution]\ncontext_safety_tokens=500\n" ,
    )).unwrap();
    (data, plan, task, dir.join("out"))
}

#[test]
fn oversized_tool_groups_compact_on_all_protocols_without_losing_tail_evidence() {
    let bpe = tiktoken_rs::o200k_base().unwrap();
    for protocol in ["completions", "responses", "anthropic"] {
        for extra in ["", "PREVIOUS-TURN-UNIT", "CORRECT-FIRST-FRAGMENT"] {
            let dir = tempfile::tempdir().unwrap();
            let mock = start_mock();
            let (data, plan, task, out) =
                write_segmented_compaction_job(dir.path(), protocol, mock.port, extra);
            let output = run_formic(protocol, mock.port, 1, &data, &plan, &task, &out);
            assert_eq!(
                output.status.code(),
                Some(0),
                "{protocol} {extra}: {}\n{}",
                stderr_of(&output),
                worker_report(&out, 1)
            );
            assert_eq!(
                fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
                FINAL_TEXT
            );
            let stats = stats_lines(&out);
            let stats = stats_of(&stats, 1);
            assert_eq!(stats["compactions"], 1);
            assert_eq!(
                stats["tool_calls"]["read"],
                if extra == "PREVIOUS-TURN-UNIT" { 3 } else { 2 }
            );
            assert_eq!(stats["retries"], 0);
            let requests = mock.requests.lock().unwrap();
            let compactions: Vec<_> = requests
                .iter()
                .filter(|request| request.body.contains("formic_submit_compaction"))
                .collect();
            assert!(compactions.len() > 1, "{protocol}: 大工具回合必须分段");
            let budget = if protocol == "anthropic" { 6000 } else { 7000 };
            assert!(
                requests
                    .iter()
                    .all(|request| bpe.encode_ordinary(&request.body).len() <= budget)
            );
            assert_eq!(stats["llm_calls"], requests.len() as u64);
        }
    }
}

#[test]
fn later_compaction_fragment_failure_does_not_publish_partial_progress() {
    let dir = tempfile::tempdir().unwrap();
    let mock = start_mock();
    let (data, plan, task, out) =
        write_segmented_compaction_job(dir.path(), "completions", mock.port, "FAIL-FRAGMENT-UNIT");
    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert!(!results_dir(&out).join("1.md").exists());
    let stats = stats_lines(&out);
    let stats = stats_of(&stats, 1);
    assert_eq!(stats["compactions"], 0, "片段成功不能冒充已替换完整历史");
    assert_eq!(stats["tool_calls"]["read"], 2);
    assert_eq!(
        mock.requests.lock().unwrap().len(),
        3,
        "读取一次，压缩首段成功，次段失败"
    );
}

#[test]
fn segmented_compaction_handles_the_recorded_peak_with_default_tool_limits() {
    let dir = tempfile::tempdir().unwrap();
    let mock = start_mock();
    let (data, plan, task, out) =
        write_segmented_compaction_job(dir.path(), "completions", mock.port, "");
    fs::write(data.join("big.txt"), segmented_evidence_text(9500)).unwrap();
    fs::write(
        data.join("tail.txt"),
        format!("SECOND_RESULT_PROOF\n{}", segmented_evidence_text(9500)),
    )
    .unwrap();
    let config = fs::read_to_string(dir.path().join("config.toml"))
        .unwrap()
        .replace("context_window_tokens=7500", "context_window_tokens=131072")
        .replace("context_safety_tokens=500", "context_safety_tokens=2048");
    fs::write(dir.path().join("config.toml"), config).unwrap();
    let started = std::time::Instant::now();
    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}\n{}",
        stderr_of(&output),
        worker_report(&out, 1)
    );
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    let stats = stats_lines(&out);
    let stats = stats_of(&stats, 1);
    assert!(
        stats["compaction_before_tokens"].as_u64().unwrap() >= 464_525,
        "{stats}"
    );
    assert_eq!(stats["compactions"], 1);
    assert_eq!(stats["tool_calls"]["read"], 2);
    let bpe = tiktoken_rs::o200k_base().unwrap();
    let requests = mock.requests.lock().unwrap();
    assert!(
        requests
            .iter()
            .all(|request| bpe.encode_ordinary(&request.body).len() <= 129024)
    );
    println!(
        "peak_before_tokens={} after_tokens={} llm_calls={} elapsed_ms={}",
        stats["compaction_before_tokens"],
        stats["compaction_after_tokens"],
        stats["llm_calls"],
        started.elapsed().as_millis()
    );
}

#[test]
fn shrinking_a_fragment_preserves_the_invalid_submission_limit() {
    let dir = tempfile::tempdir().unwrap();
    let mock = start_mock();
    let (data, plan, task, out) = write_segmented_compaction_job(
        dir.path(),
        "completions",
        mock.port,
        "INVALID-ALL-FRAGMENTS",
    );
    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(output.status.code(), Some(1));
    assert!(worker_report(&out, 1).contains("连续 5 次无效"));
    assert!(!results_dir(&out).join("1.md").exists());
    let stats = stats_lines(&out);
    let stats = stats_of(&stats, 1);
    assert_eq!(stats["compactions"], 0);
    assert_eq!(stats["tool_calls"]["read"], 2);
    assert_eq!(
        mock.requests.lock().unwrap().len(),
        6,
        "首次读取和默认五次无效提交，不能因重新分段重置"
    );
}

#[test]
fn context_recovery_resets_the_consecutive_network_retry_schedule() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "small shard\n").unwrap();
    let mut large = String::new();
    for line in 1..=260 {
        large.push_str(&format!(
            "line-{line:04} alpha beta gamma delta epsilon value-{line:04}\n"
        ));
    }
    fs::write(data.join("big.txt"), large).unwrap();
    let plan = dir.path().join("plan.jsonl");
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    let task = dir.path().join("task.md");
    fs::write(
        &task,
        format!(
            "先读取 big.txt，再完成任务。{COMPACTION_MARKER} {NETWORK_CONTEXT_NETWORK_MARKER}\n"
        ),
    )
    .unwrap();
    let out = dir.path().join("out");
    fs::write(
        dir.path().join("config.toml"),
        "retry_delays_ms = [1]\nmax_retry_after_ms = 1000\n[tools.read]\nmax_result_bytes = 30000\n",
    )
    .unwrap();
    let mock = start_mock();

    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);

    assert_eq!(
        output.status.code(),
        Some(0),
        "stdout:\n{}\nstderr:\n{}\nartifacts:\n{}",
        stdout_of(&output),
        stderr_of(&output),
        read_output_tree(&out)
    );
    assert_eq!(mock.requests.lock().unwrap().len(), 6);
    let stats = stats_lines(&out);
    let value = stats_of(&stats, 1);
    assert_eq!(value["llm_calls"], 6, "{value}");
    assert_eq!(
        value["retries"], 2,
        "上下文响应应中断连续网络失败，压缩后的网络失败重新使用第一档：{value}"
    );
    assert_eq!(value["compactions"], 1, "{value}");
    let summary = latest_run_summary(&out);
    assert_summary_identities(&summary);
    assert_eq!(summary["published"], 1);
    assert_eq!(summary["llm_calls"], 6);
}

#[test]
fn config_file_supplies_http_settings() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    let mock = start_mock();
    let config_directory = dir.path().join("settings");
    fs::create_dir(&config_directory).unwrap();
    let config_path = config_directory.join("formic.toml");
    let extra_body = serde_json::json!({
        "temperature": 0.25,
        "reasoning": {"effort": "high"},
        "vendor_flag": true,
        "vendor_secret": LLM_EXTRA_BODY_SECRET,
    })
    .to_string();
    fs::write(
        &config_path,
        format!(
            "protocol = \"completions\"\nurl = \"http://127.0.0.1:{}/v1\"\napi_key = \"{}\"\nmodel = \"config-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\nextra_body_json = '''{extra_body}'''\n",
            mock.port, LLM_API_KEY_SECRET,
        ),
    )
    .unwrap();

    let mut command = formic_command(2, &data, &plan, &task, &out);
    let output = command
        .arg("--config")
        .arg(&config_path)
        .current_dir(dir.path())
        .env("FORMIC_LLM_PROTOCOL", "anthropic")
        .env("FORMIC_LLM_BASE_URL", "http://127.0.0.1:1/wrong")
        .env("FORMIC_LLM_API_KEY", "wrong-environment-key")
        .env("FORMIC_LLM_MODEL", "wrong-environment-model")
        .env("FORMIC_LLM_CONTEXT_WINDOW_TOKENS", "1")
        .env("FORMIC_LLM_INPUT_MODALITIES", "image")
        .env("FORMIC_ANTHROPIC_MAX_TOKENS", "1")
        .env("FORMIC_METRICS", "1")
        .env("HTTP_PROXY", "http://127.0.0.1:1")
        .env("HTTPS_PROXY", "http://127.0.0.1:1")
        .env("ALL_PROXY", "http://127.0.0.1:1")
        .env("NO_PROXY", "")
        .output()
        .unwrap();

    assert_eq!(
        output.status.code(),
        Some(0),
        "config.toml 应能完成真实调用：{}",
        stderr_of(&output)
    );
    let requests = mock.requests.lock().unwrap();
    assert!(!requests.is_empty());
    assert!(
        requests
            .iter()
            .all(|request| request.path.ends_with("/v1/chat/completions")),
        "请求应使用 config.toml 的 url"
    );
    assert!(
        requests
            .iter()
            .all(|request| request.body.contains("\"model\":\"config-model\"")),
        "请求应使用 config.toml 的 model"
    );
    assert!(
        requests.iter().all(|request| {
            let body: serde_json::Value = serde_json::from_str(&request.body).unwrap();
            body["temperature"] == 0.25
                && body["reasoning"]["effort"] == "high"
                && body["vendor_flag"] == true
                && body["vendor_secret"] == LLM_EXTRA_BODY_SECRET
        }),
        "每次模型请求都应带上 extra_body_json"
    );
    let expected_authorization = format!("Bearer {LLM_API_KEY_SECRET}");
    assert!(
        requests.iter().all(
            |request| request.authorization.as_deref() == Some(expected_authorization.as_str())
        ),
        "请求应使用 config.toml 的明文 api_key"
    );
    drop(requests);
    let public = format!(
        "{}{}{}",
        stdout_of(&output),
        stderr_of(&output),
        read_output_tree(&out)
    );
    for secret in [LLM_API_KEY_SECRET, LLM_EXTRA_BODY_SECRET] {
        assert!(!public.contains(secret), "LLM 密钥不得进入任何作业产物");
    }
    assert!(
        !stderr_of(&output)
            .lines()
            .any(|line| line.starts_with("metrics ")),
        "环境变量不能开启 metrics"
    );
}

#[test]
fn explicitly_selected_missing_config_fails_before_any_request() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    let mock = start_mock();
    let missing = dir.path().join("missing-config.toml");

    let output = formic_command(1, &data, &plan, &task, &out)
        .arg("--config")
        .arg(&missing)
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(2));
    let stderr = stderr_of(&output);
    assert!(stderr.contains("指定的配置文件"), "{stderr}");
    assert!(stderr.contains("missing-config.toml"), "{stderr}");
    assert!(stderr.contains("不存在"), "{stderr}");
    assert!(mock.requests.lock().unwrap().is_empty());

    let test_output = formic_test_command(&missing)
        .current_dir(dir.path())
        .output()
        .unwrap();
    assert_eq!(test_output.status.code(), Some(2));
    let stdout = stdout_of(&test_output);
    assert!(stdout.contains("配置：失败（指定的配置文件"), "{stdout}");
    assert!(stdout.contains("汇总：0/1 项通过"), "{stdout}");
    assert!(mock.requests.lock().unwrap().is_empty());
}

#[test]
fn invalid_llm_url_is_a_configuration_error_before_job_creation() {
    for url in [
        "not-a-url",
        "https://",
        "ftp://example.com/private-url-sentinel",
        "https://example.com:invalid/private-url-sentinel",
    ] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_job(dir.path(), None);
        let config = dir.path().join("config.toml");
        fs::write(
            &config,
            format!(
                "protocol='completions'\nurl='{url}'\nmodel='m'\ncontext_window_tokens=8192\nmodel_input_modalities=['text']\n"
            ),
        )
        .unwrap();
        let run = formic_command(1, &data, &plan, &task, &out)
            .arg("--config")
            .arg(&config)
            .output()
            .unwrap();
        assert_eq!(run.status.code(), Some(2), "{}", stderr_of(&run));
        assert!(stderr_of(&run).contains("url 必须是"));
        assert!(!out.exists());

        let check = formic_test_command(&config).output().unwrap();
        assert_eq!(check.status.code(), Some(2));
        let stdout = stdout_of(&check);
        assert!(stdout.contains("配置：失败"), "{stdout}");
        assert!(!stdout.contains("LLM："), "{stdout}");
        for output in [&run, &check] {
            assert!(!stdout_of(output).contains("private-url-sentinel"));
            assert!(!stderr_of(output).contains("private-url-sentinel"));
        }
    }
}

#[test]
fn configured_llm_headers_reach_test_and_worker_on_all_protocols() {
    for protocol in ["completions", "responses", "anthropic"] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_job(dir.path(), None);
        let mock = start_mock();
        let config = write_mock_config(dir.path(), protocol, mock.port, 131072, "text");
        let original = fs::read_to_string(&config).unwrap();
        fs::write(
            &config,
            format!(
                "headers = {{ user-agent = 'Formic/0.3.0', x-opencode-session = 'session-sentinel' }}\n{original}"
            ),
        )
        .unwrap();
        let check = formic_test_command(&config).output().unwrap();
        assert_eq!(check.status.code(), Some(0), "{}", stderr_of(&check));
        let run = formic_command(2, &data, &plan, &task, &out)
            .arg("--config")
            .arg(&config)
            .output()
            .unwrap();
        assert_eq!(run.status.code(), Some(0), "{}", stderr_of(&run));
        let requests = mock.requests.lock().unwrap();
        assert!(requests.len() >= 5, "必须覆盖自检和 worker 多轮调用");
        for request in requests.iter() {
            assert_eq!(request.user_agent.as_deref(), Some("Formic/0.3.0"));
            assert_eq!(
                request.opencode_session.as_deref(),
                Some("session-sentinel")
            );
        }
        for output in [&check, &run] {
            assert!(!stdout_of(output).contains("session-sentinel"));
            assert!(!stderr_of(output).contains("session-sentinel"));
        }
        for unit in [1, 2] {
            assert!(!worker_report(&out, unit).contains("session-sentinel"));
        }
    }
}

#[test]
fn invalid_llm_headers_stop_before_http_send() {
    let dir = tempfile::tempdir().unwrap();
    let mock = start_mock();
    let config = write_mock_config(dir.path(), "completions", mock.port, 131072, "text");
    let original = fs::read_to_string(&config).unwrap();
    fs::write(
        &config,
        format!("headers = {{ Authorization = 'header-secret' }}\n{original}"),
    )
    .unwrap();
    let output = formic_test_command(&config).output().unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(mock.requests.lock().unwrap().is_empty());
    assert!(!stdout_of(&output).contains("header-secret"));
    assert!(!stderr_of(&output).contains("header-secret"));
}

#[test]
fn test_command_checks_llm_and_each_mcp_without_job_outputs() {
    let dir = tempfile::tempdir().unwrap();
    let llm = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let alpha_start = dir.path().join("alpha-start.log");
    let alpha_calls = dir.path().join("alpha-calls.log");
    let zeta_start = dir.path().join("zeta-start.log");
    let zeta_calls = dir.path().join("zeta-calls.log");
    let config = dir.path().join("self-test.toml");
    fs::write(
        &config,
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\n",
                "context_window_tokens = 131072\n",
                "model_input_modalities = [\"text\"]\n",
                "[mcp_servers.zeta]\n",
                "enabled = true\n",
                "command = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {}, FAKE_MCP_CALL_LOG = {} }}\n",
                "[mcp_servers.alpha]\n",
                "enabled = true\n",
                "command = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {}, FAKE_MCP_CALL_LOG = {} }}\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&zeta_start),
            toml_string(&zeta_calls),
            toml_string(&fake_mcp),
            toml_string(&alpha_start),
            toml_string(&alpha_calls),
        ),
    )
    .unwrap();

    let output = formic_test_command(&config)
        .current_dir(dir.path())
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    assert!(stderr_of(&output).is_empty(), "自检只输出类型化项目结果");
    let stdout = stdout_of(&output);
    let config_at = stdout.find("配置：通过").unwrap();
    let llm_at = stdout.find("LLM：通过（completions，流式）").unwrap();
    let alpha_at = stdout.find("MCP alpha：通过（可用 3 个工具）").unwrap();
    let zeta_at = stdout.find("MCP zeta：通过（可用 3 个工具）").unwrap();
    assert!(config_at < llm_at && llm_at < alpha_at && alpha_at < zeta_at);
    assert!(stdout.contains("汇总：4/4 项通过"), "{stdout}");
    let requests = llm.requests.lock().unwrap();
    assert_eq!(requests.len(), 1, "LLM 自检只发送一次请求");
    assert!(requests[0].body.contains(SELF_TEST_MARKER));
    drop(requests);
    assert_eq!(fs::read_to_string(&alpha_start).unwrap().lines().count(), 1);
    assert_eq!(fs::read_to_string(&zeta_start).unwrap().lines().count(), 1);
    assert!(
        fs::read_to_string(&alpha_calls)
            .unwrap_or_default()
            .is_empty()
    );
    assert!(
        fs::read_to_string(&zeta_calls)
            .unwrap_or_default()
            .is_empty()
    );
    for name in [
        "data",
        "plan.jsonl",
        "task.md",
        "out",
        "projects",
        "runs",
        "workers",
    ] {
        assert!(!dir.path().join(name).exists(), "test 命令不应创建 {name}");
    }
}

#[test]
fn test_command_rejects_cross_server_model_name_collisions() {
    let dir = tempfile::tempdir().unwrap();
    let llm = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let call_log = dir.path().join("mcp-calls.log");
    let config = dir.path().join("self-test.toml");
    fs::write(
        &config,
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\n",
                "context_window_tokens = 131072\n",
                "model_input_modalities = [\"text\"]\n",
                "[mcp_servers.alpha]\n",
                "enabled = true\n",
                "command = {}\n",
                "enabled_tools = [\"echo\"]\n",
                "tool_aliases = {{ echo = \"beta__echo\" }}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "[mcp_servers.alpha__beta]\n",
                "enabled = true\n",
                "command = {}\n",
                "enabled_tools = [\"echo\"]\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&call_log),
            toml_string(&fake_mcp),
            toml_string(&call_log),
        ),
    )
    .unwrap();

    let output = formic_test_command(&config)
        .current_dir(dir.path())
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert!(stderr_of(&output).is_empty());
    let stdout = stdout_of(&output);
    assert!(
        stdout.contains("MCP alpha：失败（模型可见工具名发生碰撞）"),
        "{stdout}"
    );
    assert!(
        stdout.contains("MCP alpha__beta：失败（模型可见工具名发生碰撞）"),
        "{stdout}"
    );
    assert!(stdout.contains("汇总：2/4 项通过"), "{stdout}");
    assert_eq!(llm.requests.lock().unwrap().len(), 1);
    assert!(fs::read_to_string(&call_log).unwrap_or_default().is_empty());
}

#[test]
fn test_command_continues_after_llm_and_mcp_failures() {
    let dir = tempfile::tempdir().unwrap();
    let unavailable = TcpListener::bind("127.0.0.1:0").unwrap();
    let unavailable_port = unavailable.local_addr().unwrap().port();
    drop(unavailable);
    let fake_mcp = compile_fake_mcp(dir.path());
    let good_start = dir.path().join("good-start.log");
    let good_calls = dir.path().join("good-calls.log");
    let config = dir.path().join("self-test.toml");
    fs::write(
        &config,
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\n",
                "context_window_tokens = 131072\n",
                "model_input_modalities = [\"text\"]\n",
                "connect_timeout_ms = 100\n",
                "request_timeout_ms = 500\n",
                "retry_delays_ms = [1, 2, 3]\n",
                "[mcp_servers.alpha]\n",
                "enabled = true\n",
                "command = \"formic-missing-mcp-command\"\n",
                "[mcp_servers.zeta]\n",
                "enabled = true\n",
                "command = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {}, FAKE_MCP_CALL_LOG = {} }}\n",
            ),
            unavailable_port,
            toml_string(&fake_mcp),
            toml_string(&good_start),
            toml_string(&good_calls),
        ),
    )
    .unwrap();

    let output = formic_test_command(&config)
        .current_dir(dir.path())
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert!(
        stderr_of(&output).is_empty(),
        "自检失败原因也由受控报告输出"
    );
    let stdout = stdout_of(&output);
    let llm_at = stdout
        .find("LLM：失败（LLM ")
        .unwrap_or_else(|| panic!("{stdout}"));
    let alpha_at = stdout.find("MCP alpha：失败（stdio 启动失败）").unwrap();
    let zeta_at = stdout.find("MCP zeta：通过（可用 3 个工具）").unwrap();
    assert!(llm_at < alpha_at && alpha_at < zeta_at, "{stdout}");
    assert!(stdout.contains("汇总：2/4 项通过"), "{stdout}");
    assert_eq!(fs::read_to_string(&good_start).unwrap().lines().count(), 1);
    assert!(
        fs::read_to_string(&good_calls)
            .unwrap_or_default()
            .is_empty()
    );
}

#[test]
fn test_command_ctrl_c_stops_before_mcp_and_returns_three() {
    let dir = tempfile::tempdir().unwrap();
    let llm = start_mock_with_delay(1_000);
    let fake_mcp = compile_fake_mcp(dir.path());
    let start_log = dir.path().join("mcp-start.log");
    let config = dir.path().join("self-test.toml");
    fs::write(
        &config,
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\n",
                "context_window_tokens = 131072\n",
                "model_input_modalities = [\"text\"]\n",
                "[mcp_servers.demo]\n",
                "enabled = true\n",
                "command = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {} }}\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&start_log),
        ),
    )
    .unwrap();
    let mut command = formic_test_command(&config);
    command
        .current_dir(dir.path())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    make_interruptible(&mut command);
    let mut child = command.spawn().unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while llm.requests.lock().unwrap().is_empty() && std::time::Instant::now() < deadline {
        assert!(child.try_wait().unwrap().is_none());
        thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(!llm.requests.lock().unwrap().is_empty());
    send_interrupt(&child);
    let output = child.wait_with_output().unwrap();

    assert_eq!(output.status.code(), Some(3), "{}", stderr_of(&output));
    let stdout = stdout_of(&output);
    assert!(stdout.contains("LLM：停止（收到终止信号）"), "{stdout}");
    assert!(stdout.contains("汇总：1/3 项通过，已停止后续项"));
    assert!(!start_log.exists(), "LLM 中断后不再启动 MCP");
}

#[test]
fn custom_stdio_mcp_auto_discovers_all_tools_and_is_audited() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), Some(MCP_MARKER));
    fs::write(&task, format!("调用配置的外部工具完成任务。{MCP_MARKER}\n")).unwrap();
    let mock = start_mock_with_delay(40);
    let fake_mcp = compile_fake_mcp(dir.path());
    let start_log = dir.path().join("mcp-start.log");
    let call_log = dir.path().join("mcp-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\n",
                "context_window_tokens = 131072\n",
                "model_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n",
                "[tools.read]\nenabled = false\n",
                "[mcp_servers.demo]\n",
                "enabled = true\n",
                "command = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {}, FAKE_MCP_CALL_LOG = {} }}\n",
                "session_scope = \"job\"\n",
                "max_in_flight = 1\n",
                "startup_timeout_sec = 10\n",
                "tool_timeout_sec = 10\n",
            ),
            mock.port,
            toml_string(&fake_mcp),
            toml_string(&start_log),
            toml_string(&call_log),
        ),
    )
    .unwrap();

    let mut command = formic_command(2, &data, &plan, &task, &out);
    let output = command.current_dir(dir.path()).output().unwrap();

    assert_eq!(
        output.status.code(),
        Some(0),
        "自定义 MCP 作业应成功：{}",
        stderr_of(&output)
    );
    assert_eq!(
        fs::read_to_string(&start_log).unwrap().lines().count(),
        1,
        "job scope 应只建立一个 stdio 会话"
    );
    assert_eq!(
        fs::read_to_string(&call_log).unwrap().lines().count(),
        2,
        "两个单元各调用一次，不能自动重放"
    );
    for unit in [1, 2] {
        assert_eq!(
            fs::read_to_string(results_dir(&out).join(format!("{unit}.md"))).unwrap(),
            FINAL_TEXT
        );
        let report = worker_report(&out, unit);
        assert!(
            report.contains("mcp:demo/echo"),
            "运行档案应记录 MCP 来源：{report}"
        );
        assert!(
            report.contains("structuredContent"),
            "文本与结构结果应使用固定包装：{report}"
        );
    }
    let stats = stats_lines(&out);
    for unit in [1, 2] {
        let value = stats_of(&stats, unit);
        assert_eq!(value["mcp_peak_in_flight"]["demo"], 1, "{value}");
    }
    let requests = mock.requests.lock().unwrap();
    assert!(
        requests
            .iter()
            .filter(|request| !request.body.contains("\"role\":\"tool\""))
            .all(|request| request.body.contains("demo__echo")),
        "冻结工具目录必须出现在每个首轮请求中"
    );
    assert!(
        requests
            .iter()
            .filter(|request| !request.body.contains("\"role\":\"tool\""))
            .all(|request| !request.body.contains("完整 input 根")),
        "关闭全部 input 工具后，首轮提示不得继续宣称完整 input 可检索"
    );
}

#[test]
fn job_scoped_stdio_transport_close_is_returned_to_all_waiters_without_replay() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(
        &task,
        format!("共享 MCP 会话中断后仍须继续当前单元。{MCP_MARKER}\n"),
    )
    .unwrap();
    let llm = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let start_log = dir.path().join("closed-session-start.log");
    let call_log = dir.path().join("closed-session-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n[tools.read]\nenabled = false\n",
                "[mcp_servers.demo]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {}, FAKE_MCP_CALL_LOG = {}, FAKE_MCP_EXIT_AFTER_CALLS = \"2\" }}\n",
                "enabled_tools = [\"echo\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 2\ntool_timeout_sec = 10\nreconnect = true\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&start_log),
            toml_string(&call_log),
        ),
    )
    .unwrap();

    let mut command = formic_command(2, &data, &plan, &task, &out);
    let output = command.output().unwrap();

    assert_eq!(
        output.status.code(),
        Some(0),
        "共享 MCP 会话中断应作为模型可见工具结果，而不是终止 worker：{}",
        stderr_of(&output)
    );
    assert_eq!(
        fs::read_to_string(&start_log).unwrap().lines().count(),
        1,
        "没有新的模型工具调用时不得仅因断线自动重放或重连"
    );
    assert_eq!(
        fs::read_to_string(&call_log).unwrap().lines().count(),
        2,
        "两个已发送的原调用必须各执行一次，断线后不得自动重放"
    );
    for unit in [1, 2] {
        assert_eq!(
            fs::read_to_string(results_dir(&out).join(format!("{unit}.md"))).unwrap(),
            FINAL_TEXT
        );
        let report = worker_report(&out, unit);
        assert!(report.contains("会话已中断"), "{report}");
        assert!(report.contains("原工具调用的结局未知"), "{report}");
        assert!(report.contains("未自动重放"), "{report}");
    }
    let requests = llm.requests.lock().unwrap();
    let tool_result_requests: Vec<_> = requests
        .iter()
        .filter(|request| request.body.contains("\"role\":\"tool\""))
        .collect();
    assert_eq!(
        tool_result_requests.len(),
        2,
        "每个 worker 都应把断线结果回注模型"
    );
    for request in tool_result_requests {
        assert!(request.body.contains("会话已中断"), "{}", request.body);
        assert!(
            request.body.contains("原工具调用的结局未知"),
            "{}",
            request.body
        );
        assert!(request.body.contains("未自动重放"), "{}", request.body);
    }
    let summary = latest_run_summary(&out);
    assert_eq!(summary["published"], 2);
    assert_eq!(summary["failed"], 0);
}

#[test]
fn stdio_message_limit_can_exceed_result_limit_and_the_session_is_reused() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(
        &task,
        format!("读取较大的 MCP 响应并继续当前单元。{MCP_MARKER}\n"),
    )
    .unwrap();
    let llm = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let start_log = dir.path().join("large-result-start.log");
    let call_log = dir.path().join("large-result-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n[tools.read]\nenabled = false\n",
                "[mcp_servers.demo]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {}, FAKE_MCP_CALL_LOG = {}, FAKE_MCP_RESULT_TEXT_BYTES = \"102400\" }}\n",
                "enabled_tools = [\"echo\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 1\ntool_timeout_sec = 10\n",
                "max_result_bytes = 1024\nmax_message_bytes = 262144\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&start_log),
            toml_string(&call_log),
        ),
    )
    .unwrap();

    let mut command = formic_command(1, &data, &plan, &task, &out);
    let output = command.output().unwrap();

    assert_eq!(
        output.status.code(),
        Some(0),
        "显式传输上限内的大响应应先解析、再按结果上限截断：{}",
        stderr_of(&output)
    );
    assert_eq!(
        fs::read_to_string(&start_log).unwrap().lines().count(),
        1,
        "两个串行单元应复用同一个健康的 job-scope 会话"
    );
    assert_eq!(
        fs::read_to_string(&call_log).unwrap().lines().count(),
        2,
        "两个单元应各调用一次 MCP"
    );
    for unit in [1, 2] {
        assert_eq!(
            fs::read_to_string(results_dir(&out).join(format!("{unit}.md"))).unwrap(),
            FINAL_TEXT
        );
        let report = worker_report(&out, unit);
        assert!(
            report.contains("[已截断：MCP 文本结果达到 1024 字节上限]"),
            "{report}"
        );
        assert!(
            !report.contains(&"x".repeat(2048)),
            "公开档案不得保留未截断的大响应"
        );
    }
    let requests = llm.requests.lock().unwrap();
    let tool_result_requests: Vec<_> = requests
        .iter()
        .filter(|request| request.body.contains("\"role\":\"tool\""))
        .collect();
    assert_eq!(tool_result_requests.len(), 2);
    assert!(tool_result_requests.iter().all(|request| {
        request
            .body
            .contains("[已截断：MCP 文本结果达到 1024 字节上限]")
    }));
    let summary = latest_run_summary(&out);
    assert_eq!(summary["published"], 2);
    assert_eq!(summary["failed"], 0);
}

#[test]
fn explicit_mcp_tool_error_is_returned_to_the_model() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    fs::write(
        &task,
        format!("调用会返回明确错误的 MCP 工具。{MCP_ERROR_MARKER}\n"),
    )
    .unwrap();
    let mock = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let call_log = dir.path().join("error-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n[tools.read]\nenabled = false\n",
                "[mcp_servers.demo]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"fail\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 1\ntool_timeout_sec = 10\n",
            ),
            mock.port,
            toml_string(&fake_mcp),
            toml_string(&call_log),
        ),
    )
    .unwrap();

    let mut command = formic_command(1, &data, &plan, &task, &out);
    let output = command.output().unwrap();

    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    assert_eq!(fs::read_to_string(call_log).unwrap().lines().count(), 1);
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    let report = worker_report(&out, 1);
    assert!(report.contains("MCP 工具报告失败"), "{report}");
    assert!(report.contains("expected tool failure"), "{report}");
}

#[test]
fn one_worker_runs_same_turn_mcp_calls_concurrently() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    fs::write(
        &task,
        format!("在同一回合调用两个独立慢工具。{MCP_PARALLEL_MARKER}\n"),
    )
    .unwrap();
    let mock = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let left_log = dir.path().join("left-call.log");
    let right_log = dir.path().join("right-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n[tools.read]\nenabled = false\n",
                "[mcp_servers.left]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"slow\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 1\ntool_timeout_sec = 10\n",
                "[mcp_servers.right]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"slow\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 1\ntool_timeout_sec = 10\n",
            ),
            mock.port,
            toml_string(&fake_mcp),
            toml_string(&left_log),
            toml_string(&fake_mcp),
            toml_string(&right_log),
        ),
    )
    .unwrap();

    let mut command = formic_command(1, &data, &plan, &task, &out);
    let output = command.output().unwrap();

    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    let call_started_ms = |path: &Path| {
        fs::read_to_string(path)
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap()
            .parse::<u128>()
            .unwrap()
    };
    let left_started = call_started_ms(&left_log);
    let right_started = call_started_ms(&right_log);
    assert!(
        left_started.abs_diff(right_started) < 2_000,
        "两个各耗时 5 秒的独立工具应同时启动：left={left_started}, right={right_started}"
    );
    assert_eq!(fs::read_to_string(left_log).unwrap().lines().count(), 1);
    assert_eq!(fs::read_to_string(right_log).unwrap().lines().count(), 1);
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    let report = worker_report(&out, 1);
    assert!(report.contains("mcp:left/slow"), "{report}");
    assert!(report.contains("mcp:right/slow"), "{report}");
}

#[test]
fn parallel_mcp_timeout_keeps_call_order_then_uses_backup_and_publishes() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    fs::write(
        &task,
        format!("并发调用两个外部工具，主工具超时时改用备用工具。{MCP_FALLBACK_FLOW_MARKER}\n"),
    )
    .unwrap();
    let llm = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let slow_log = dir.path().join("fallback-slow-call.log");
    let fast_log = dir.path().join("fallback-fast-call.log");
    let backup_log = dir.path().join("fallback-backup-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n[tools.read]\nenabled = false\n",
                "[mcp_servers.slow]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"slow\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 1\ntool_timeout_sec = 3\nreconnect = true\n",
                "[mcp_servers.fast]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"echo\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 1\ntool_timeout_sec = 10\n",
                "[mcp_servers.backup]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"echo\"]\nsession_scope = \"job\"\n",
                "max_in_flight = 1\ntool_timeout_sec = 10\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&slow_log),
            toml_string(&fake_mcp),
            toml_string(&fast_log),
            toml_string(&fake_mcp),
            toml_string(&backup_log),
        ),
    )
    .unwrap();

    let mut command = formic_command(1, &data, &plan, &task, &out);
    let output = command.output().unwrap();

    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    let call_started_ms = |path: &Path| {
        fs::read_to_string(path)
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap()
            .parse::<u128>()
            .unwrap()
    };
    let slow_started = call_started_ms(&slow_log);
    let fast_started = call_started_ms(&fast_log);
    assert!(
        slow_started.abs_diff(fast_started) < 2_000,
        "slow 与 fast 必须在 slow 的 3 秒超时前并发启动：slow={slow_started}, fast={fast_started}"
    );
    assert_eq!(
        fs::read_to_string(&slow_log).unwrap().lines().count(),
        1,
        "已发送后超时的主调用不得自动重放"
    );
    assert_eq!(fs::read_to_string(&fast_log).unwrap().lines().count(), 1);
    assert_eq!(
        fs::read_to_string(&backup_log).unwrap().lines().count(),
        1,
        "模型应只调用一次备用工具"
    );

    let requests = llm.requests.lock().unwrap();
    assert_eq!(
        requests.len(),
        3,
        "应恰好经历主调用、备用调用和最终发布三轮"
    );
    let second: serde_json::Value = serde_json::from_str(&requests[1].body).unwrap();
    let second_tool_results: Vec<_> = second["messages"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|message| message["role"] == "tool")
        .collect();
    assert_eq!(second_tool_results.len(), 2);
    assert_eq!(second_tool_results[0]["tool_call_id"], "call_slow");
    assert!(
        second_tool_results[0]["content"]
            .as_str()
            .unwrap()
            .contains("工具调用已超时")
    );
    assert_eq!(second_tool_results[1]["tool_call_id"], "call_fast");
    assert!(
        second_tool_results[1]["content"]
            .as_str()
            .unwrap()
            .contains("echo:hello")
    );

    let third: serde_json::Value = serde_json::from_str(&requests[2].body).unwrap();
    let third_tool_results: Vec<_> = third["messages"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|message| message["role"] == "tool")
        .collect();
    assert_eq!(third_tool_results.len(), 3);
    assert_eq!(third_tool_results[2]["tool_call_id"], "call_backup");
    assert!(
        third_tool_results[2]["content"]
            .as_str()
            .unwrap()
            .contains("echo:hello")
    );
    drop(requests);

    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    let report = worker_report(&out, 1);
    assert!(report.contains("mcp:slow/slow"), "{report}");
    assert!(report.contains("工具调用已超时"), "{report}");
    assert!(report.contains("mcp:fast/echo"), "{report}");
    assert!(report.contains("mcp:backup/echo"), "{report}");
    let summary = latest_run_summary(&out);
    assert_eq!(summary["published"], 1);
    assert_eq!(summary["failed"], 0);
}

#[test]
fn stdio_mcp_unit_scope_creates_and_reclaims_one_session_per_unit() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(&task, format!("调用 unit scope MCP。{MCP_MARKER}\n")).unwrap();
    let llm = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let start_log = dir.path().join("unit-start.log");
    let call_log = dir.path().join("unit-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n[tools.read]\nenabled = false\n",
                "[mcp_servers.demo]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_START_LOG = {}, FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"echo\"]\nsession_scope = \"unit\"\nmax_in_flight = 2\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&start_log),
            toml_string(&call_log),
        ),
    )
    .unwrap();
    let mut command = formic_command(2, &data, &plan, &task, &out);
    let output = command
        .env("HTTP_PROXY", "http://127.0.0.1:1")
        .env("HTTPS_PROXY", "http://127.0.0.1:1")
        .env("ALL_PROXY", "http://127.0.0.1:1")
        .env("NO_PROXY", "")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    assert_eq!(
        fs::read_to_string(start_log).unwrap().lines().count(),
        3,
        "启动发现使用一个临时会话，两个活动单元各使用一个独立会话"
    );
    assert_eq!(fs::read_to_string(call_log).unwrap().lines().count(), 2);
}

#[test]
fn timed_out_mcp_call_is_returned_to_model_without_replay() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    fs::write(&task, format!("调用慢 MCP。{MCP_TIMEOUT_MARKER}\n")).unwrap();
    let llm = start_mock();
    let fake_mcp = compile_fake_mcp(dir.path());
    let call_log = dir.path().join("timeout-call.log");
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\ncontext_window_tokens = 131072\nmodel_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n[tools.read]\nenabled = false\n",
                "[mcp_servers.demo]\nenabled = true\ncommand = {}\n",
                "env = {{ FAKE_MCP_CALL_LOG = {} }}\n",
                "enabled_tools = [\"slow\"]\ntool_timeout_sec = 1\nreconnect = true\n",
            ),
            llm.port,
            toml_string(&fake_mcp),
            toml_string(&call_log),
        ),
    )
    .unwrap();
    let mut command = formic_command(1, &data, &plan, &task, &out);
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let observation_deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    while fs::metadata(&call_log).map_or(true, |metadata| metadata.len() == 0)
        && std::time::Instant::now() < observation_deadline
    {
        if child.try_wait().unwrap().is_some() {
            break;
        }
        thread::sleep(std::time::Duration::from_millis(10));
    }
    let call_seen = fs::metadata(&call_log).is_ok_and(|metadata| metadata.len() > 0);
    let call_started = std::time::Instant::now();
    let output = child.wait_with_output().unwrap();
    assert!(
        call_seen,
        "fake MCP 未收到预期的 tools/call：{}",
        stderr_of(&output)
    );
    assert!(
        call_started.elapsed() < std::time::Duration::from_millis(3500),
        "tools/call 发出后，1 秒工具超时不应再等待旧 stdio 调用；实际耗时 {:?}",
        call_started.elapsed()
    );
    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    let report = worker_report(&out, 1);
    assert!(report.contains("工具调用已超时"), "{report}");
    assert!(report.contains("原工具调用的结局未知"), "{report}");
    assert!(report.contains("未自动重放"), "{report}");
    assert_eq!(
        fs::read_to_string(call_log).unwrap().lines().count(),
        1,
        "超时的原调用不得自动重放；reconnect 只影响后续新调用"
    );
    let requests = llm.requests.lock().unwrap();
    let tool_result = requests
        .iter()
        .find(|request| request.body.contains("\"role\":\"tool\""))
        .expect("超时工具结果必须回注模型");
    assert!(tool_result.body.contains("工具调用已超时"));
    assert!(tool_result.body.contains("原工具调用的结局未知"));
    assert!(tool_result.body.contains("未自动重放"));
}

#[test]
fn custom_streamable_http_mcp_uses_session_auth_and_frozen_catalog() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(&task, format!("调用 HTTP MCP。{MCP_MARKER}\n")).unwrap();
    let llm = start_mock();
    let mcp = start_mcp_http_mock();
    fs::write(
        dir.path().join("config.toml"),
        format!(
            concat!(
                "protocol = \"completions\"\n",
                "url = \"http://127.0.0.1:{}/v1\"\n",
                "model = \"test-model\"\n",
                "context_window_tokens = 131072\n",
                "model_input_modalities = [\"text\"]\n",
                "[tools.search]\nenabled = false\n",
                "[tools.read]\nenabled = false\n",
                "[mcp_servers.demo]\n",
                "enabled = true\n",
                "url = \"http://127.0.0.1:{}/mcp\"\n",
                "bearer_token = \"{}\"\n",
                "headers = {{ \"x-formic-test\" = \"yes\" }}\n",
                "enabled_tools = [\"echo\"]\n",
                "session_scope = \"job\"\n",
                "max_in_flight = 2\n",
            ),
            llm.port, mcp.port, MCP_BEARER_SECRET,
        ),
    )
    .unwrap();
    let mut command = formic_command(2, &data, &plan, &task, &out);
    let output = command
        .env("HTTP_PROXY", "http://127.0.0.1:1")
        .env("HTTPS_PROXY", "http://127.0.0.1:1")
        .env("ALL_PROXY", "http://127.0.0.1:1")
        .env("NO_PROXY", "")
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(0),
        "Streamable HTTP MCP 作业应成功：{}",
        stderr_of(&output)
    );
    let requests = mcp.requests.lock().unwrap();
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.rpc_method == "initialize")
            .count(),
        1
    );
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.rpc_method == "tools/list")
            .count(),
        2,
        "tools/list 应遍历分页"
    );
    assert_eq!(
        requests
            .iter()
            .filter(|request| request.rpc_method == "tools/call")
            .count(),
        2
    );
    let expected_authorization = format!("Bearer {MCP_BEARER_SECRET}");
    assert!(requests.iter().all(|request| {
        request.authorization.as_deref() == Some(expected_authorization.as_str())
            && request.custom_header.as_deref() == Some("yes")
    }));
    assert!(
        requests
            .iter()
            .filter(|request| request.rpc_method != "initialize")
            .any(|request| request.session_id.as_deref() == Some("test-session")),
        "初始化后的请求应复用 MCP session id"
    );
    drop(requests);
    let public = format!(
        "{}{}{}",
        stdout_of(&output),
        stderr_of(&output),
        read_output_tree(&out)
    );
    assert!(
        !public.contains(MCP_BEARER_SECRET),
        "MCP bearer 不得进入任何作业产物"
    );
}

#[test]
fn failed_unit_leaves_no_record() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), Some(FAIL_MARKER));
    let mock = start_mock();
    let output = run_formic("completions", mock.port, 2, &data, &plan, &task, &out);

    assert_eq!(
        output.status.code(),
        Some(1),
        "存在失败单元时退出码应为 1：{}",
        stderr_of(&output)
    );
    let stdout = stdout_of(&output);
    assert!(stdout.contains("发布 1"), "{stdout}");
    assert!(stdout.contains("失败 1"), "{stdout}");
    assert!(stdout.contains("首个未完成单元 2"), "{stdout}");
    assert!(
        !stdout.contains("全部成功"),
        "失败路径不得出现成功文案：{stdout}"
    );

    assert!(
        results_dir(&out).join("1.md").exists(),
        "成功单元的记录应在"
    );
    assert!(
        !results_dir(&out).join("2.md").exists(),
        "失败单元不得留下记录"
    );
    assert!(
        !results_dir(&out).join(".tmp-unit-2").exists(),
        "失败单元不得留下临时文件"
    );

    let report = worker_report(&out, 2);
    assert!(report.contains("500"), "失败档案应含直接原因：{report}");
    assert!(
        report.contains("重试 3 次"),
        "瞬时故障应按网络重试配置耗尽：{report}"
    );

    // 单元 1 两轮 2 次请求 + 单元 2 四次尝试均 500
    let requests = mock.requests.lock().unwrap();
    assert_eq!(requests.len(), 6, "重试预算应耗尽：{:?}", requests.len());
    drop(requests);

    let stats = stats_lines(&out);
    assert_eq!(stats_of(&stats, 1)["outcome"], "published");
    let failed = stats_of(&stats, 2);
    assert_eq!(failed["outcome"], "failed", "{failed}");
    assert_eq!(failed["llm_calls"], 4, "失败单元应尝试 4 次：{failed}");
    assert_eq!(failed["retries"], 3, "失败单元应重试 3 次：{failed}");
}

#[test]
fn permanent_service_errors_stop_within_the_initial_window_and_never_leak_body() {
    for (marker, stop_reason, visible_fact) in [
        (AUTH_MARKER, "鉴权失败", "HTTP 401"),
        (FORBIDDEN_MARKER, "权限不足", "HTTP 403"),
        (QUOTA_MARKER, "额度不可用", "insufficient\\_quota"),
        (ACCOUNT_MARKER, "账户不可用", "account\\_deactivated"),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_many_job(dir.path(), 20, marker);
        write_request_policy(dir.path(), "[1]", 50);
        let mock = start_mock();
        let output = run_formic("completions", mock.port, 4, &data, &plan, &task, &out);

        assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
        let requests = mock.requests.lock().unwrap().len() as u64;
        assert!(
            (1..=4).contains(&requests),
            "永久错误的实际调用数不得超过首次活动窗口：{requests}"
        );
        let summary = latest_run_summary(&out);
        assert_summary_identities(&summary);
        assert_eq!(summary["llm_calls"], requests);
        assert_eq!(summary["llm_calls_with_provider_usage"], 0);
        assert_eq!(summary["llm_calls_without_provider_usage"], requests);
        assert_eq!(summary["stop_reason"], stop_reason);
        assert!(summary["not_started"].as_u64().unwrap() >= 16, "{summary}");
        let all_public = format!(
            "{}\n{}\n{}",
            stdout_of(&output),
            stderr_of(&output),
            read_output_tree(&out)
        );
        assert!(!all_public.contains(RESPONSE_SECRET), "泄漏了错误响应正文");
        assert!(
            all_public.contains(visible_fact),
            "缺少安全诊断事实：{all_public}"
        );
        assert!(!stderr_of(&output).contains("100%"), "失败不得补写 100%");
    }
}

#[test]
fn slow_quota_classification_blocks_new_calls_beyond_the_initial_window() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 20, SLOW_QUOTA_MARKER);
    write_request_policy(dir.path(), "[1]", 1_000);
    let mock = start_mock();

    let output = run_formic("completions", mock.port, 2, &data, &plan, &task, &out);

    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    let requests = mock.requests.lock().unwrap().len() as u64;
    assert!(
        (1..=2).contains(&requests),
        "慢速 quota 正文分类期间不得超过首次活动窗口：{requests}"
    );
    let summary = latest_run_summary(&out);
    assert_summary_identities(&summary);
    assert_eq!(summary["llm_calls"], requests);
    assert_eq!(summary["stop_reason"], "额度不可用");
    assert!(summary["not_started"].as_u64().unwrap() >= 18, "{summary}");
    assert!(!read_output_tree(&out).contains(RESPONSE_SECRET));
}

#[test]
fn slow_server_error_releases_classification_gate_for_later_calls() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 2, SLOW_SERVER_MARKER);
    write_request_policy(dir.path(), "[1]", 1_000);
    let mock = start_mock();

    let output = run_formic("completions", mock.port, 2, &data, &plan, &task, &out);

    assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
    assert_eq!(
        mock.requests.lock().unwrap().len(),
        5,
        "一次 500 后两个单元都应继续完成各自回合"
    );
    let summary = latest_run_summary(&out);
    assert_summary_identities(&summary);
    assert_eq!(summary["published"], 2);
    assert_eq!(summary["llm_calls"], 5);
    assert!(summary["stop_reason"].is_null(), "{summary}");
    assert!(!read_output_tree(&out).contains(RESPONSE_SECRET));
}

#[test]
fn oversized_retry_after_on_429_or_503_stops_without_waiting() {
    for marker in [RATE_LONG_MARKER, SERVER_LONG_MARKER] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_many_job(dir.path(), 20, marker);
        write_request_policy(dir.path(), "[1]", 20);
        let mock = start_mock();
        let started = std::time::Instant::now();
        let output = run_formic("completions", mock.port, 4, &data, &plan, &task, &out);
        assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
        assert!(
            started.elapsed() < std::time::Duration::from_secs(5),
            "不得等待 600 秒 Retry-After：{:?}",
            started.elapsed()
        );
        assert!(mock.requests.lock().unwrap().len() <= 4);
        let summary = latest_run_summary(&out);
        assert_summary_identities(&summary);
        assert_eq!(summary["stop_reason"], "供应商要求的等待时间超过配置上限");
        let public = format!(
            "{}{}{}",
            stdout_of(&output),
            stderr_of(&output),
            read_output_tree(&out)
        );
        assert!(!public.contains(RESPONSE_SECRET));
    }
}

#[test]
fn numeric_and_http_date_retry_after_gate_the_retry_then_succeed() {
    for (marker, minimum_wait) in [
        (RATE_NUMERIC_MARKER, std::time::Duration::from_millis(800)),
        (RATE_DATE_MARKER, std::time::Duration::from_millis(800)),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let (data, plan, task, out) = write_many_job(dir.path(), 1, marker);
        write_request_policy(dir.path(), "[1]", 5_000);
        let mock = start_mock();
        let started = std::time::Instant::now();
        let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
        assert_eq!(output.status.code(), Some(0), "{}", stderr_of(&output));
        assert!(started.elapsed() >= minimum_wait, "Retry-After 未生效");
        assert_eq!(mock.requests.lock().unwrap().len(), 3);
        let summary = latest_run_summary(&out);
        assert_summary_identities(&summary);
        assert_eq!(summary["published"], 1);
        assert_eq!(summary["llm_calls"], 3);
    }
}

#[test]
fn malformed_protocol_payload_is_discarded_from_every_public_artifact() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 2, BAD_PROTOCOL_MARKER);
    write_request_policy(dir.path(), "[]", 100);
    let mock = start_mock();
    let output = run_formic("completions", mock.port, 2, &data, &plan, &task, &out);
    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert_eq!(mock.requests.lock().unwrap().len(), 2);
    let public = format!(
        "{}{}{}",
        stdout_of(&output),
        stderr_of(&output),
        read_output_tree(&out)
    );
    assert!(!public.contains(RESPONSE_SECRET));
    assert!(public.contains("协议响应无效"), "{public}");
    assert!(!stderr_of(&output).contains("100%"));
}

#[test]
fn eof_without_completion_is_fatal_protocol_but_does_not_stop_later_units() {
    for marker in [EOF_WITHOUT_FINISH_MARKER, DONE_WITHOUT_FINISH_MARKER] {
        assert_unfinished_stream_does_not_stop_later_units(marker);
    }
}

fn assert_unfinished_stream_does_not_stop_later_units(marker: &str) {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("bad.txt"), format!("{marker}\n")).unwrap();
    fs::write(data.join("good.txt"), "普通输入\n").unwrap();
    let plan = dir.path().join("plan.jsonl");
    fs::write(
        &plan,
        "{\"unit\":1,\"files\":[\"bad.txt\"]}\n{\"unit\":2,\"files\":[\"good.txt\"]}\n",
    )
    .unwrap();
    let task = dir.path().join("task.md");
    fs::write(&task, "给出结论。\n").unwrap();
    let out = dir.path().join("out");
    write_request_policy(dir.path(), "[]", 100);
    let mock = start_mock();

    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);

    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert_eq!(mock.requests.lock().unwrap().len(), 3);
    let summary = latest_run_summary(&out);
    assert_summary_identities(&summary);
    assert_eq!(summary["started"], 2);
    assert_eq!(summary["failed"], 1);
    assert_eq!(summary["published"], 1);
    assert_eq!(summary["not_started"], 0);
    assert_eq!(summary["failure_reasons"]["协议响应无效"], 1);
    assert!(summary["stop_reason"].is_null(), "{summary}");
    let stats = stats_lines(&out);
    assert_eq!(stats_of(&stats, 1)["llm_calls"], 1);
    assert_eq!(stats_of(&stats, 1)["retries"], 0);
    assert!(!results_dir(&out).join("1.md").exists());
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("2.md")).unwrap(),
        FINAL_TEXT
    );
    assert!(worker_report(&out, 1).contains("流结束但没有收到完成事件"));
    assert!(!read_output_tree(&out).contains("partial"));
}

#[test]
fn stalled_llm_stream_times_out_without_persisting_partial_payload() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 1, SLOW_STREAM_MARKER);
    fs::write(
        dir.path().join("config.toml"),
        "read_timeout_ms = 50\nrequest_timeout_ms = 1000\nretry_delays_ms = []\nmax_retry_after_ms = 100\n",
    )
    .unwrap();
    let mock = start_mock();

    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);

    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert_eq!(mock.requests.lock().unwrap().len(), 1);
    let summary = latest_run_summary(&out);
    assert_summary_identities(&summary);
    assert_eq!(summary["llm_calls"], 1);
    assert_eq!(summary["failure_reasons"]["请求重试耗尽"], 1);
    let public = format!(
        "{}{}{}",
        stdout_of(&output),
        stderr_of(&output),
        read_output_tree(&out)
    );
    assert!(public.contains("等待 LLM 流数据超过 50ms"), "{public}");
    assert!(
        !public.contains(RESPONSE_SECRET),
        "部分 SSE payload 不得落盘"
    );
    assert!(!stderr_of(&output).contains("100%"));
}

#[test]
fn empty_retry_policy_makes_one_request_and_stops_admission() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 1, FAIL_MARKER);
    write_request_policy(dir.path(), "[]", 100);
    let mock = start_mock();
    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(output.status.code(), Some(1), "{}", stderr_of(&output));
    assert_eq!(mock.requests.lock().unwrap().len(), 1);
    let summary = latest_run_summary(&out);
    assert_summary_identities(&summary);
    assert_eq!(summary["llm_calls"], 1);
    assert_eq!(summary["stop_reason"], "网络请求重试已经耗尽");
    let stats = stats_lines(&out);
    assert_eq!(stats_of(&stats, 1)["retries"], 0);
}

#[test]
fn resume_retries_only_unpublished_units_and_all_complete_is_zero_request() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 2, FLAKY_MARKER);
    write_request_policy(dir.path(), "[]", 100);
    let mock = start_mock();

    let first = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(first.status.code(), Some(1), "{}", stderr_of(&first));
    assert_eq!(mock.requests.lock().unwrap().len(), 1);
    let first_summary = latest_run_summary(&out);
    assert_summary_identities(&first_summary);
    assert_eq!(first_summary["failed"], 1);
    assert_eq!(first_summary["not_started"], 1);

    let second = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(second.status.code(), Some(0), "{}", stderr_of(&second));
    assert_eq!(mock.requests.lock().unwrap().len(), 5);
    assert!(results_dir(&out).join("1.md").exists());
    assert!(results_dir(&out).join("2.md").exists());
    let second_summary = latest_run_summary(&out);
    assert_summary_identities(&second_summary);
    assert_eq!(second_summary["published"], 2);

    let before = mock.requests.lock().unwrap().len();
    let third = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(third.status.code(), Some(0), "{}", stderr_of(&third));
    assert!(stdout_of(&third).contains("无需处理"));
    assert_eq!(mock.requests.lock().unwrap().len(), before);
    assert_eq!(run_dirs(&out).len(), 3);
    let third_summary = latest_run_summary(&out);
    assert_summary_identities(&third_summary);
    assert_eq!(third_summary["already_completed"], 2);
    assert_eq!(third_summary["started"], 0);
}

#[test]
fn cancelled_run_resumes_in_the_same_output_directory() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 1, "CANCEL-THEN-RESUME");
    let mock = start_mock_with_delay(1000);
    let mut command = formic_command(1, &data, &plan, &task, &out);
    command
        .arg("--config")
        .arg(write_mock_config(
            dir.path(),
            "completions",
            mock.port,
            131_072,
            "text",
        ))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    make_interruptible(&mut command);
    let mut child = command.spawn().unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while mock.requests.lock().unwrap().is_empty() && std::time::Instant::now() < deadline {
        assert!(
            child.try_wait().unwrap().is_none(),
            "Formic 不应在收到信号前退出"
        );
        thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(
        !mock.requests.lock().unwrap().is_empty(),
        "首轮模型调用应已开始"
    );
    send_interrupt(&child);
    let cancelled = child.wait_with_output().unwrap();

    assert_eq!(
        cancelled.status.code(),
        Some(3),
        "{}",
        stderr_of(&cancelled)
    );
    assert!(!results_dir(&out).join("1.md").exists());
    let first_runs = run_dirs(&out);
    assert_eq!(
        first_runs[0].file_name().unwrap().to_string_lossy(),
        "run-000001"
    );
    let first_summary = run_summary(&first_runs[0]);
    assert_summary_identities(&first_summary);
    assert_eq!(first_summary["stopped"], 1);
    assert_eq!(first_summary["not_started"], 0);
    let stopped_report = fs::read_to_string(first_runs[0].join("workers/1.md")).unwrap();
    assert!(
        stopped_report.contains("结局：`stopped`（已停止）"),
        "{stopped_report}"
    );

    let resumed = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(resumed.status.code(), Some(0), "{}", stderr_of(&resumed));
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    let runs = run_dirs(&out);
    assert_eq!(runs.len(), 2);
    assert_eq!(runs[1].file_name().unwrap().to_string_lossy(), "run-000002");
    let second_summary = run_summary(&runs[1]);
    assert_summary_identities(&second_summary);
    assert_eq!(second_summary["published"], 1);
}

#[test]
fn failed_unit_can_fail_again_then_succeed_on_a_third_run() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 1, FAIL_TWICE_MARKER);
    write_request_policy(dir.path(), "[]", 100);
    let mock = start_mock();

    let first = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(first.status.code(), Some(1), "{}", stderr_of(&first));
    assert!(!results_dir(&out).join("1.md").exists());
    assert_eq!(mock.requests.lock().unwrap().len(), 1);

    let second = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(second.status.code(), Some(1), "{}", stderr_of(&second));
    assert!(!results_dir(&out).join("1.md").exists());
    assert_eq!(mock.requests.lock().unwrap().len(), 2);

    let third = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(third.status.code(), Some(0), "{}", stderr_of(&third));
    assert_eq!(mock.requests.lock().unwrap().len(), 4);
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );

    let runs = run_dirs(&out);
    assert_eq!(runs.len(), 3);
    for (index, run) in runs.iter().enumerate() {
        assert_eq!(
            run.file_name().unwrap().to_string_lossy(),
            format!("run-{:06}", index + 1)
        );
        assert_summary_identities(&run_summary(run));
    }
    assert_eq!(run_summary(&runs[0])["failed"], 1);
    assert_eq!(run_summary(&runs[1])["failed"], 1);
    assert_eq!(run_summary(&runs[2])["published"], 1);
}

#[test]
fn wrong_structured_resume_is_read_only_and_correct_text_resume_still_succeeds() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 1, FLAKY_MARKER);
    write_request_policy(dir.path(), "[]", 100);
    let schema = write_answer_schema(dir.path());
    let mock = start_mock();

    let first = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(first.status.code(), Some(1), "{}", stderr_of(&first));
    assert_eq!(mock.requests.lock().unwrap().len(), 1);
    assert!(!results_dir(&out).join("1.md").exists());
    let before = output_tree_snapshot(&out);

    let mut wrong = formic_command(1, &data, &plan, &task, &out);
    let wrong = wrong
        .arg("--resume")
        .arg("--output-schema")
        .arg(&schema)
        .arg("--config")
        .arg(write_mock_config(
            dir.path(),
            "completions",
            mock.port,
            131_072,
            "text",
        ))
        .output()
        .unwrap();

    assert_eq!(wrong.status.code(), Some(2), "{}", stderr_of(&wrong));
    assert_eq!(mock.requests.lock().unwrap().len(), 1);
    assert_eq!(
        output_tree_snapshot(&out),
        before,
        "错误的结构化 resume 不得写 schema、状态或运行档案"
    );
    assert!(!results_dir(&out).join("output-schema.json").exists());

    let resumed = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(resumed.status.code(), Some(0), "{}", stderr_of(&resumed));
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT
    );
    assert_eq!(run_dirs(&out).len(), 2);
}

#[test]
fn resume_rejects_published_result_without_terminal_state_before_any_request() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 1, "NORMAL-UNIT");
    let mock = start_mock();
    let first = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(first.status.code(), Some(0), "{}", stderr_of(&first));
    let result_before = fs::read(results_dir(&out).join("1.md")).unwrap();
    let state_path = out.join(".formic-job-state.jsonl");
    let state = fs::read_to_string(&state_path).unwrap();
    let first_line = state.lines().next().unwrap();
    fs::write(&state_path, format!("{first_line}\n")).unwrap();
    let request_count = mock.requests.lock().unwrap().len();

    let resumed = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(resumed.status.code(), Some(2), "{}", stderr_of(&resumed));
    assert_eq!(mock.requests.lock().unwrap().len(), request_count);
    assert_eq!(
        fs::read(results_dir(&out).join("1.md")).unwrap(),
        result_before
    );
    assert!(stderr_of(&resumed).contains("状态不是 published"));
    assert_eq!(run_dirs(&out).len(), 1, "请求前失败不建立新 run");
}

#[test]
fn resume_rejects_a_damaged_published_result_before_any_request() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_many_job(dir.path(), 1, "NORMAL-UNIT");
    let mock = start_mock();
    let first = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(first.status.code(), Some(0), "{}", stderr_of(&first));
    let result_path = results_dir(&out).join("1.md");
    fs::write(&result_path, []).unwrap();
    let damaged = fs::read(&result_path).unwrap();
    let request_count = mock.requests.lock().unwrap().len();

    let resumed = run_formic_resume("completions", mock.port, 1, &data, &plan, &task, &out);

    assert_eq!(resumed.status.code(), Some(2), "{}", stderr_of(&resumed));
    assert_eq!(mock.requests.lock().unwrap().len(), request_count);
    assert_eq!(
        fs::read(&result_path).unwrap(),
        damaged,
        "损坏结果不得被覆盖"
    );
    assert!(stderr_of(&resumed).contains("已发布结果"));
    assert!(stderr_of(&resumed).contains("完成记录为空"));
    assert_eq!(run_dirs(&out).len(), 1, "请求前失败不建立新 run");
}

/// 瞬时故障重试成功：首次 500 → 重发 → 正常完成两轮。
#[test]
fn retry_succeeds_after_transient_failure() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "苹果是水果。\nFLAKY-UNIT\n").unwrap();
    let plan = dir.path().join("plan.jsonl");
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    let task = dir.path().join("task.md");
    fs::write(&task, "任务。\n").unwrap();
    let out = dir.path().join("out");

    let mock = start_mock();
    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    assert_eq!(
        output.status.code(),
        Some(0),
        "重试后应成功：{}",
        stderr_of(&output)
    );
    assert_eq!(
        fs::read_to_string(results_dir(&out).join("1.md")).unwrap(),
        FINAL_TEXT,
        "产出应为最终回合文本"
    );

    // 500 一次 + 重试的工具调用 + 最终回合，共 3 次请求
    let requests = mock.requests.lock().unwrap();
    assert_eq!(requests.len(), 3, "{:?}", requests.len());
    drop(requests);

    let report = worker_report(&out, 1);
    assert!(report.contains("模型调用重试"), "{report}");
    assert!(report.contains("第 `1` 次尝试失败"), "{report}");
    assert!(report.contains("第 `2` 次尝试"), "{report}");

    let stats = stats_lines(&out);
    let s = stats_of(&stats, 1);
    assert_eq!(s["outcome"], "published", "{s}");
    assert_eq!(s["retries"], 1, "应恰好重试 1 次：{s}");
    assert_eq!(s["llm_calls"], 3, "{s}");
    assert_eq!(s["tool_calls"]["search"], 1, "{s}");
}

#[test]
fn stalled_unit_is_terminated() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), Some(LOOP_MARKER));
    let mock = start_mock();
    let output = run_formic("completions", mock.port, 2, &data, &plan, &task, &out);

    assert_eq!(
        output.status.code(),
        Some(1),
        "停滞单元应判失败：{}",
        stderr_of(&output)
    );
    let stdout = stdout_of(&output);
    assert!(stdout.contains("首个未完成单元 2"), "{stdout}");
    assert!(
        !results_dir(&out).join("2.md").exists(),
        "停滞单元不得留下记录"
    );

    assert!(
        stdout.contains("重复工具调用停滞 1"),
        "汇总应按原因计数：{stdout}"
    );

    let report = worker_report(&out, 2);
    assert!(
        report.contains("模型请求工具"),
        "停滞单元的工具调用应留痕：{report}"
    );
    assert!(report.contains("结局：`failed`"), "{report}");
    assert!(report.contains("停滞"), "{report}");

    let stats = stats_lines(&out);
    let s = stats_of(&stats, 2);
    assert_eq!(s["outcome"], "failed", "{s}");
    assert_eq!(
        s["tool_calls"]["search"], 16,
        "停滞前应执行 16 次相同调用：{s}"
    );
}

#[test]
fn invalid_plan_reports_unit_and_exits_2() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "内容\n").unwrap();
    // 真实存在于数据根之外的文件，canonicalize 成功后触发逃逸判定
    fs::write(dir.path().join("escape.txt"), "根外文件\n").unwrap();
    let plan = dir.path().join("plan.jsonl");
    fs::write(&plan, "{\"unit\":7,\"files\":[\"../escape.txt\"]}\n").unwrap();
    let task = dir.path().join("task.md");
    fs::write(&task, "任务。\n").unwrap();
    let mock = start_mock();

    let output = run_formic(
        "completions",
        mock.port,
        1,
        &data,
        &plan,
        &task,
        &dir.path().join("out"),
    );
    assert_eq!(output.status.code(), Some(2), "启动失败退出码应为 2");
    let stderr = stderr_of(&output);
    assert!(stderr.contains("单元 7"), "错误应含单元号：{stderr}");
    assert!(
        stderr.contains("不含 . 或 .. 的数据根内相对路径"),
        "错误应说明原因：{stderr}"
    );
}

#[test]
fn output_directory_cannot_overlap_input_data() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, _out) = write_job(dir.path(), None);
    let mock = start_mock();
    let output = run_formic("completions", mock.port, 1, &data, &plan, &task, &data);

    assert_eq!(output.status.code(), Some(2), "启动失败退出码应为 2");
    let stderr = stderr_of(&output);
    assert!(
        stderr.contains("不能相同或互相包含"),
        "错误应明确说明目录边界：{stderr}"
    );
    assert!(data.join("a.txt").exists(), "输入文件不得被覆盖");
    assert!(mock.requests.lock().unwrap().is_empty());
}

#[test]
fn rejected_nested_output_directory_is_not_created() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, _out) = write_job(dir.path(), None);
    let nested_out = data.join("generated").join("out");
    let mock = start_mock();

    let output = run_formic(
        "completions",
        mock.port,
        1,
        &data,
        &plan,
        &task,
        &nested_out,
    );

    assert_eq!(output.status.code(), Some(2), "启动失败退出码应为 2");
    assert!(
        stderr_of(&output).contains("不能相同或互相包含"),
        "错误应明确说明目录边界：{}",
        stderr_of(&output)
    );
    assert!(!nested_out.exists(), "拒绝参数时不得污染输入目录");
    assert!(!data.join("generated").exists());
    assert!(mock.requests.lock().unwrap().is_empty());
}

#[test]
fn concurrent_jobs_cannot_share_an_output_directory() {
    let dir = tempfile::tempdir().unwrap();
    let (data, plan, task, out) = write_job(dir.path(), None);
    fs::write(&plan, "{\"unit\":1,\"files\":[\"a.txt\"]}\n").unwrap();
    let mock = start_mock_with_delay(1000);

    let mut first_command = formic_command(1, &data, &plan, &task, &out);
    first_command
        .arg("--config")
        .arg(write_mock_config(
            dir.path(),
            "completions",
            mock.port,
            131_072,
            "text",
        ))
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut first = first_command.spawn().unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while mock.requests.lock().unwrap().is_empty() && std::time::Instant::now() < deadline {
        thread::sleep(std::time::Duration::from_millis(10));
    }
    assert!(
        !mock.requests.lock().unwrap().is_empty(),
        "首个作业应已取得输出目录锁并开始调用模型"
    );
    assert!(first.try_wait().unwrap().is_none(), "首个作业应仍在运行");

    let second = run_formic("completions", mock.port, 1, &data, &plan, &task, &out);
    let first_output = first.wait_with_output().unwrap();
    assert_eq!(
        first_output.status.code(),
        Some(0),
        "首个作业应正常完成：{}",
        stderr_of(&first_output)
    );
    assert_eq!(second.status.code(), Some(2));
    assert!(
        stderr_of(&second).contains("正在被另一个 Formic 作业使用"),
        "第二个作业应报告输出目录冲突：{}",
        stderr_of(&second)
    );
}

/// 窗口证据：mock 端在途请求峰值 == 并发窗口（真实入口验证，不是单元推断）。
#[test]
fn concurrency_window_bounds_in_flight() {
    let run = |concurrency: usize| {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path().join("data");
        fs::create_dir_all(&data).unwrap();
        fs::write(data.join("a.txt"), "苹果是水果。\n").unwrap();
        let plan = dir.path().join("plan.jsonl");
        fs::write(
            &plan,
            "{\"unit\":1,\"files\":[\"a.txt\"]}\n{\"unit\":2,\"files\":[\"a.txt\"]}\n{\"unit\":3,\"files\":[\"a.txt\"]}\n{\"unit\":4,\"files\":[\"a.txt\"]}\n",
        )
        .unwrap();
        let task = dir.path().join("task.md");
        fs::write(&task, "判断分片内容。\n").unwrap();
        let out = dir.path().join("out");

        let mock = start_mock_with_delay(100);
        let output = run_formic(
            "completions",
            mock.port,
            concurrency,
            &data,
            &plan,
            &task,
            &out,
        );
        assert_eq!(
            output.status.code(),
            Some(0),
            "concurrency={concurrency} 应全部成功：{}",
            stderr_of(&output)
        );
        let max = mock.max_in_flight.load(Ordering::SeqCst);
        drop(dir);
        max
    };

    assert_eq!(run(2), 2, "窗口 2 + 慢响应时，在途峰值应恰好达到 2");
    assert_eq!(run(1), 1, "窗口 1 时请求必须严格串行");
}

/// 汇总确定性：并发改变完成时间，不改变自然顺序——失败单元按计划文件顺序呈现。
#[test]
fn summary_follows_plan_order() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    fs::create_dir_all(&data).unwrap();
    fs::write(data.join("a.txt"), "FAIL-UNIT 甲\n").unwrap();
    fs::write(data.join("b.txt"), "FAIL-UNIT 乙\n").unwrap();
    // 计划行乱序：单元 2 在前、单元 1 在后；两者都触发 500
    let plan = dir.path().join("plan.jsonl");
    fs::write(
        &plan,
        "{\"unit\":2,\"files\":[\"a.txt\"]}\n{\"unit\":1,\"files\":[\"b.txt\"]}\n",
    )
    .unwrap();
    let task = dir.path().join("task.md");
    fs::write(&task, "任务。\n").unwrap();
    let out = dir.path().join("out");

    let mock = start_mock_with_delay(50);
    let output = run_formic("completions", mock.port, 2, &data, &plan, &task, &out);
    assert_eq!(output.status.code(), Some(1));
    let stdout = stdout_of(&output);
    assert!(
        stdout.contains("首个未完成单元 2，样例 2, 1"),
        "失败单元必须按计划文件顺序呈现，与完成时间无关：{stdout}"
    );
}
