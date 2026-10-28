//! Anthropic Messages 协议：历史 → 请求映射，SSE → 内部事件 transform。
//! 工具调用的参数以 input_json_delta 增量到达，content_block_stop 时拼好放出。
//! max_tokens 是该协议的必填字段，使用已校验的模型配置。

use super::{
    ContentPart, Finish, LlmConfig, LlmError, LlmEvent, Message, ProviderUsage, ToolCallReq,
    ToolSpec, UserContent,
};

const ANTHROPIC_VERSION: &str = "2023-06-01";

pub fn build_request(
    config: &LlmConfig,
    instructions: &str,
    history: &[Message],
    tools: &[ToolSpec],
) -> super::BuildRequestResult {
    build_request_inner(config, instructions, history, tools, true)
}

pub(super) fn build_estimate_body(
    config: &LlmConfig,
    instructions: &str,
    history: &[Message],
    tools: &[ToolSpec],
) -> Result<String, LlmError> {
    build_request_inner(config, instructions, history, tools, false).map(|(_, body, _)| body)
}

fn build_request_inner(
    config: &LlmConfig,
    instructions: &str,
    history: &[Message],
    tools: &[ToolSpec],
    include_image_bytes: bool,
) -> super::BuildRequestResult {
    let mut messages: Vec<serde_json::Value> = Vec::new();
    for message in history {
        match message {
            Message::User(content) => {
                let blocks = user_content(content, include_image_bytes)?;
                let fold_into = messages.last_mut().and_then(|last| {
                    if last["role"] == "user" && last["content"].is_array() {
                        last["content"].as_array_mut()
                    } else {
                        None
                    }
                });
                match fold_into {
                    Some(existing) => existing.extend(blocks),
                    None => messages.push(serde_json::json!({"role": "user", "content": blocks})),
                }
            }
            Message::Compaction(text) => {
                messages.push(serde_json::json!({
                    "role": "user",
                    "content": format!("此前历史的已验证压缩摘要：\n{text}"),
                }));
            }
            Message::Assistant { text, tool_calls } => {
                let mut content = Vec::new();
                if !text.is_empty() {
                    content.push(serde_json::json!({"type": "text", "text": text}));
                }
                for tc in tool_calls {
                    content.push(serde_json::json!({
                        "type": "tool_use",
                        "id": tc.call_id,
                        "name": tc.name,
                        // worker 在进入历史前已校验 arguments 为合法 JSON
                        "input": serde_json::from_str::<serde_json::Value>(&tc.arguments)
                            .expect("进入历史的工具参数必为合法 JSON"),
                    }));
                }
                messages.push(serde_json::json!({"role": "assistant", "content": content}));
            }
            // 这是 Responses 专属的无损历史；本协议使用紧邻它之前的 Assistant。
            Message::ResponseOutputItems(_) => {}
            Message::ToolResult { call_id, content } => {
                // 连续 ToolResult 折叠进同一条 user 消息（Anthropic 的交替约束）
                let block = serde_json::json!({
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                });
                let fold_into = messages.last_mut().and_then(|last| {
                    if last["role"] == "user" && last["content"].is_array() {
                        last["content"].as_array_mut()
                    } else {
                        None
                    }
                });
                match fold_into {
                    Some(blocks) => blocks.push(block),
                    None => messages.push(serde_json::json!({"role": "user", "content": [block]})),
                }
            }
        }
    }
    let mut body = serde_json::json!({
        "model": config.model,
        "max_tokens": config
            .anthropic_max_tokens
            .expect("Anthropic 配置边界必须提供 anthropic_max_tokens"),
        "stream": true,
        "system": instructions,
        "messages": messages,
    });
    if !tools.is_empty() {
        body["tools"] = tools
            .iter()
            .map(|t| {
                serde_json::json!({
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                })
            })
            .collect();
    }
    super::add_extra_body(&mut body, config);
    let mut headers = vec![(
        "anthropic-version".to_string(),
        ANTHROPIC_VERSION.to_string(),
    )];
    if let Some(key) = &config.api_key {
        headers.push(("x-api-key".to_string(), key.clone()));
    }
    Ok((config.endpoint.clone(), body.to_string(), headers))
}

fn user_content(
    content: &UserContent,
    include_image_bytes: bool,
) -> Result<Vec<serde_json::Value>, LlmError> {
    content
        .parts()
        .iter()
        .map(|part| match part {
            ContentPart::Text(text) => Ok(serde_json::json!({"type": "text", "text": text})),
            ContentPart::Image(image) => {
                let data = if include_image_bytes {
                    image
                        .data
                        .base64()
                        .map_err(|error| LlmError::RequestBuild {
                            reason: error.to_string(),
                        })?
                } else {
                    String::new()
                };
                Ok(serde_json::json!({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.data.media_type().mime(),
                        "data": data,
                    },
                }))
            }
        })
        .collect()
}

struct PendingToolUse {
    index: u64,
    call_id: String,
    name: String,
    arguments: String,
    arguments_seen: bool,
}

enum ActiveContentBlock {
    ToolUse(PendingToolUse),
    Text { index: u64 },
    Other { index: u64 },
}

impl ActiveContentBlock {
    fn index(&self) -> u64 {
        match self {
            Self::ToolUse(tool) => tool.index,
            Self::Text { index } | Self::Other { index } => *index,
        }
    }
}

/// Anthropic 的 content block 必须按稠密 index 逐块开始、增量、结束，不能嵌套。
#[derive(Default)]
pub struct Transform {
    active_block: Option<ActiveContentBlock>,
    next_block_index: u64,
    tool_call_ids: std::collections::HashSet<String>,
    pending_tool_calls: Vec<ToolCallReq>,
}

impl Transform {
    pub fn new() -> Self {
        Self::default()
    }
}

impl super::Transform for Transform {
    fn push(&mut self, payload: &str) -> Result<Vec<LlmEvent>, LlmError> {
        let v: serde_json::Value = serde_json::from_str(payload)
            .map_err(|e| LlmError::protocol(format!("JSON 解析失败：{e}"), payload))?;
        let ty = v["type"]
            .as_str()
            .ok_or_else(|| LlmError::protocol("缺少 type 字段", payload))?;
        match ty {
            "message_start" => Ok(usage_at(&v["message"]["usage"])
                .map(LlmEvent::Usage)
                .into_iter()
                .collect()),
            "content_block_start" => {
                if self.active_block.is_some() {
                    return Err(LlmError::protocol(
                        "前一个 content block 尚未结束，不能嵌套开始新块",
                        payload,
                    ));
                }
                let index = required_index(&v, payload)?;
                if index != self.next_block_index {
                    return Err(LlmError::protocol(
                        format!(
                            "content block index 必须稠密递增，期望 {}，实际为 {index}",
                            self.next_block_index
                        ),
                        payload,
                    ));
                }
                let content_block = v["content_block"].as_object().ok_or_else(|| {
                    LlmError::protocol("content_block_start 缺少 content_block object", payload)
                })?;
                let block_type = content_block
                    .get("type")
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| LlmError::protocol("content block 缺少字符串 type", payload))?;
                let active_block = match block_type {
                    "tool_use" => {
                        let call_id =
                            required_nonempty_string(content_block, "id", "tool_use", payload)?;
                        let name =
                            required_nonempty_string(content_block, "name", "tool_use", payload)?;
                        if self.tool_call_ids.contains(&call_id) {
                            return Err(LlmError::protocol("同一回合重复提供工具调用 id", payload));
                        }
                        self.tool_call_ids.insert(call_id.clone());
                        ActiveContentBlock::ToolUse(PendingToolUse {
                            index,
                            call_id,
                            name,
                            arguments: String::new(),
                            arguments_seen: false,
                        })
                    }
                    "text" => ActiveContentBlock::Text { index },
                    _ => ActiveContentBlock::Other { index },
                };
                self.active_block = Some(active_block);
                Ok(Vec::new())
            }
            "content_block_delta" => {
                let index = required_index(&v, payload)?;
                let active = self.active_block.as_mut().ok_or_else(|| {
                    LlmError::protocol("没有活动 content block 时收到 delta", payload)
                })?;
                if active.index() != index {
                    return Err(LlmError::protocol(
                        format!(
                            "content block delta index 与活动块不一致，期望 {}，实际为 {index}",
                            active.index()
                        ),
                        payload,
                    ));
                }
                let delta = v["delta"].as_object().ok_or_else(|| {
                    LlmError::protocol("content_block_delta 缺少 delta object", payload)
                })?;
                let delta_type = delta
                    .get("type")
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| {
                        LlmError::protocol("content block delta 缺少字符串 type", payload)
                    })?;
                match active {
                    ActiveContentBlock::ToolUse(tool) => {
                        if delta_type != "input_json_delta" {
                            return Err(LlmError::protocol(
                                "tool_use 块只能接收 input_json_delta",
                                payload,
                            ));
                        }
                        let partial_json = delta
                            .get("partial_json")
                            .and_then(serde_json::Value::as_str)
                            .ok_or_else(|| {
                                LlmError::protocol(
                                    "input_json_delta.partial_json 必须是字符串",
                                    payload,
                                )
                            })?;
                        tool.arguments.push_str(partial_json);
                        tool.arguments_seen = true;
                        Ok(Vec::new())
                    }
                    ActiveContentBlock::Text { .. } if delta_type == "text_delta" => {
                        let text = delta
                            .get("text")
                            .and_then(serde_json::Value::as_str)
                            .ok_or_else(|| {
                                LlmError::protocol("text_delta.text 必须是字符串", payload)
                            })?;
                        Ok((!text.is_empty())
                            .then(|| LlmEvent::TextDelta(text.to_string()))
                            .into_iter()
                            .collect())
                    }
                    ActiveContentBlock::Text { .. } | ActiveContentBlock::Other { .. } => {
                        if delta_type == "input_json_delta" {
                            Err(LlmError::protocol(
                                "input_json_delta 只能属于 tool_use 块",
                                payload,
                            ))
                        } else {
                            Ok(Vec::new())
                        }
                    }
                }
            }
            "content_block_stop" => {
                let index = required_index(&v, payload)?;
                let active = self.active_block.as_ref().ok_or_else(|| {
                    LlmError::protocol("没有活动 content block 时收到 stop", payload)
                })?;
                if active.index() != index {
                    return Err(LlmError::protocol(
                        format!(
                            "content block stop index 与活动块不一致，期望 {}，实际为 {index}",
                            active.index()
                        ),
                        payload,
                    ));
                }
                self.next_block_index = self
                    .next_block_index
                    .checked_add(1)
                    .ok_or_else(|| LlmError::protocol("content block index 已耗尽", payload))?;
                Ok(match self.active_block.take().expect("上面已确认活动块") {
                    ActiveContentBlock::ToolUse(tool) => {
                        if !tool.arguments_seen {
                            return Err(LlmError::protocol(
                                "tool_use 块结束前从未提供 input_json_delta.partial_json",
                                payload,
                            ));
                        }
                        self.pending_tool_calls.push(ToolCallReq {
                            call_id: tool.call_id,
                            name: tool.name,
                            arguments: tool.arguments,
                        });
                        Vec::new()
                    }
                    ActiveContentBlock::Text { .. } | ActiveContentBlock::Other { .. } => {
                        Vec::new()
                    }
                })
            }
            "message_delta" => {
                if self.active_block.is_some() {
                    return Err(LlmError::protocol(
                        "content block 尚未结束时收到 message_delta",
                        payload,
                    ));
                }
                let finish = match v["delta"]["stop_reason"].as_str() {
                    Some("end_turn") => Some(Finish::Stop),
                    Some("max_tokens") => Some(Finish::MaxTokens),
                    Some("model_context_window_exceeded") => Some(Finish::MaxTokens),
                    Some("tool_use") => Some(Finish::ToolUse),
                    Some("refusal") => Some(Finish::Refusal),
                    _ => None,
                };
                if finish == Some(Finish::ToolUse) && self.pending_tool_calls.is_empty() {
                    return Err(LlmError::protocol(
                        "stop_reason 声称 tool_use，但没有完成的 tool_use 块",
                        payload,
                    ));
                }
                if !self.pending_tool_calls.is_empty() && finish != Some(Finish::ToolUse) {
                    return Err(LlmError::protocol(
                        "已经完成 tool_use 块，但 stop_reason 不是 tool_use",
                        payload,
                    ));
                }
                let mut events: Vec<LlmEvent> = if finish == Some(Finish::ToolUse) {
                    self.pending_tool_calls
                        .drain(..)
                        .map(LlmEvent::ToolCall)
                        .collect()
                } else {
                    Vec::new()
                };
                events.extend(usage_at(&v["usage"]).map(LlmEvent::Usage));
                events.extend(finish.map(LlmEvent::Finished));
                Ok(events)
            }
            "error" => Err(LlmError::protocol("错误事件", payload)),
            _ => Ok(Vec::new()), // message_start / message_stop / ping 等
        }
    }
}

fn required_index(value: &serde_json::Value, payload: &str) -> Result<u64, LlmError> {
    value["index"]
        .as_u64()
        .ok_or_else(|| LlmError::protocol("content block 事件缺少非负整数 index", payload))
}

fn required_nonempty_string(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    object_name: &str,
    payload: &str,
) -> Result<String, LlmError> {
    let value = object
        .get(field)
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| {
            LlmError::protocol(format!("{object_name}.{field} 必须是字符串"), payload)
        })?;
    if value.is_empty() {
        return Err(LlmError::protocol(
            format!("{object_name}.{field} 不得为空"),
            payload,
        ));
    }
    Ok(value.to_string())
}

fn usage_at(usage: &serde_json::Value) -> Option<ProviderUsage> {
    usage.is_object().then(|| ProviderUsage {
        input_tokens: usage
            .get("input_tokens")
            .and_then(serde_json::Value::as_u64),
        output_tokens: usage
            .get("output_tokens")
            .and_then(serde_json::Value::as_u64),
        cache_read_tokens: usage
            .get("cache_read_input_tokens")
            .and_then(serde_json::Value::as_u64),
        cache_creation_tokens: usage
            .get("cache_creation_input_tokens")
            .and_then(serde_json::Value::as_u64),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::Transform as _;
    use crate::llm::events_of;

    const TEXT_SAMPLE: &str = concat!(
        "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg1\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"m\",\"stop_reason\":null,\"usage\":{\"input_tokens\":10,\"output_tokens\":1}}}\n\n",
        "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"你好\"}}\n\n",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"世界\"}}\n\n",
        "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
        "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\",\"stop_sequence\":null},\"usage\":{\"output_tokens\":5}}\n\n",
        "data: {\"type\":\"message_stop\"}\n\n",
    );

    const TOOL_SAMPLE: &str = concat!(
        "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg1\",\"type\":\"message\",\"role\":\"assistant\",\"content\":[],\"model\":\"m\",\"stop_reason\":null}}\n\n",
        "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"search\"}}\n\n",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"pattern\\\":\\\"苹果\\\"\"}}\n\n",
        "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\",\\\"scope\\\":\\\"input\\\"}\"}}\n\n",
        "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
        "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"}}\n\n",
        "data: {\"type\":\"message_stop\"}\n\n",
    );

    #[test]
    fn recorded_sample_yields_text_then_stop() {
        let events = events_of(Box::new(Transform::new()), TEXT_SAMPLE);
        assert_eq!(
            events,
            vec![
                LlmEvent::Usage(ProviderUsage {
                    input_tokens: Some(10),
                    output_tokens: Some(1),
                    cache_read_tokens: None,
                    cache_creation_tokens: None,
                }),
                LlmEvent::TextDelta("你好".into()),
                LlmEvent::TextDelta("世界".into()),
                LlmEvent::Usage(ProviderUsage {
                    input_tokens: None,
                    output_tokens: Some(5),
                    cache_read_tokens: None,
                    cache_creation_tokens: None,
                }),
                LlmEvent::Finished(Finish::Stop),
            ]
        );
    }

    #[test]
    fn tool_call_assembled_across_deltas() {
        let events = events_of(Box::new(Transform::new()), TOOL_SAMPLE);
        assert_eq!(
            events,
            vec![
                LlmEvent::ToolCall(ToolCallReq {
                    call_id: "toolu_1".into(),
                    name: "search".into(),
                    arguments: "{\"pattern\":\"苹果\",\"scope\":\"input\"}".into(),
                }),
                LlmEvent::Finished(Finish::ToolUse),
            ]
        );
    }

    #[test]
    fn content_block_indices_must_be_dense_and_match_delta_and_stop() {
        let mut wrong_start = Transform::new();
        assert!(matches!(
            wrong_start.push(
                r#"{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}"#
            ),
            Err(LlmError::Protocol { .. })
        ));

        let mut wrong_delta = Transform::new();
        wrong_delta
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
            )
            .unwrap();
        assert!(matches!(
            wrong_delta.push(
                r#"{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"x"}}"#
            ),
            Err(LlmError::Protocol { .. })
        ));

        let mut wrong_stop = Transform::new();
        wrong_stop
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
            )
            .unwrap();
        assert!(matches!(
            wrong_stop.push(r#"{"type":"content_block_stop","index":1}"#),
            Err(LlmError::Protocol { .. })
        ));

        let mut duplicate = Transform::new();
        duplicate
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
            )
            .unwrap();
        duplicate
            .push(r#"{"type":"content_block_stop","index":0}"#)
            .unwrap();
        assert!(matches!(
            duplicate.push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#
            ),
            Err(LlmError::Protocol { .. })
        ));
    }

    #[test]
    fn content_blocks_cannot_be_nested_or_receive_events_without_a_start() {
        let mut nested = Transform::new();
        nested
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}"#,
            )
            .unwrap();
        assert!(matches!(
            nested.push(
                r#"{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}"#
            ),
            Err(LlmError::Protocol { .. })
        ));

        for payload in [
            r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"x"}}"#,
            r#"{"type":"content_block_stop","index":0}"#,
        ] {
            let mut transform = Transform::new();
            assert!(matches!(
                transform.push(payload),
                Err(LlmError::Protocol { .. })
            ));
        }
    }

    #[test]
    fn tool_use_requires_identity_and_matching_argument_deltas() {
        for content_block in [
            r#"{"type":"tool_use","name":"search"}"#,
            r#"{"type":"tool_use","id":"","name":"search"}"#,
            r#"{"type":"tool_use","id":"tool_1"}"#,
            r#"{"type":"tool_use","id":"tool_1","name":""}"#,
        ] {
            let mut transform = Transform::new();
            let result = transform.push(&format!(
                r#"{{"type":"content_block_start","index":0,"content_block":{content_block}}}"#
            ));
            assert!(matches!(result, Err(LlmError::Protocol { .. })));
        }

        let mut no_arguments = Transform::new();
        no_arguments
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tool_1","name":"search"}}"#,
            )
            .unwrap();
        assert!(matches!(
            no_arguments.push(r#"{"type":"content_block_stop","index":0}"#),
            Err(LlmError::Protocol { .. })
        ));

        let mut wrong_delta = Transform::new();
        wrong_delta
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tool_1","name":"search"}}"#,
            )
            .unwrap();
        assert!(matches!(
            wrong_delta.push(
                r#"{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"{}"}}"#
            ),
            Err(LlmError::Protocol { .. })
        ));
    }

    #[test]
    fn tool_use_finish_requires_a_completed_tool_block() {
        let mut missing = Transform::new();
        assert!(matches!(
            missing.push(r#"{"type":"message_delta","delta":{"stop_reason":"tool_use"}}"#),
            Err(LlmError::Protocol { .. })
        ));

        let mut active = Transform::new();
        active
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tool_1","name":"search"}}"#,
            )
            .unwrap();
        active
            .push(
                r#"{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{}"}}"#,
            )
            .unwrap();
        assert!(matches!(
            active.push(r#"{"type":"message_delta","delta":{"stop_reason":"tool_use"}}"#),
            Err(LlmError::Protocol { .. })
        ));
    }

    #[test]
    fn duplicate_tool_call_ids_are_rejected_without_releasing_the_first_call() {
        let mut transform = Transform::new();
        transform
            .push(
                r#"{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"same","name":"first"}}"#,
            )
            .unwrap();
        transform
            .push(
                r#"{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{}"}}"#,
            )
            .unwrap();
        let events = transform
            .push(r#"{"type":"content_block_stop","index":0}"#)
            .unwrap();
        assert!(events.is_empty(), "终态确认前不得释放工具调用");
        assert!(matches!(
            transform.push(
                r#"{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"same","name":"second"}}"#
            ),
            Err(LlmError::Protocol { .. })
        ));
    }

    #[test]
    fn max_tokens_stop_detected() {
        let mut t = Transform::new();
        let events = t
            .push("{\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"max_tokens\"},\"usage\":{\"output_tokens\":9}}")
            .unwrap();
        assert_eq!(
            events,
            vec![
                LlmEvent::Usage(ProviderUsage {
                    input_tokens: None,
                    output_tokens: Some(9),
                    cache_read_tokens: None,
                    cache_creation_tokens: None,
                }),
                LlmEvent::Finished(Finish::MaxTokens)
            ]
        );
    }

    #[test]
    fn refusal_stop_is_not_reported_as_empty_output() {
        let mut transform = Transform::new();
        let events = transform
            .push("{\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"refusal\"}}")
            .unwrap();
        assert_eq!(events, vec![LlmEvent::Finished(Finish::Refusal)]);
    }

    #[test]
    fn history_folds_consecutive_tool_results() {
        let config = LlmConfig {
            protocol: super::super::Protocol::Anthropic,
            endpoint: "http://x/messages".parse().unwrap(),
            model: "m".into(),
            api_key: None,
            context_window_tokens: 131072,
            anthropic_max_tokens: Some(16384),
            ..LlmConfig::test_defaults()
        };
        let history = vec![
            Message::User("任务".into()),
            Message::Assistant {
                text: "查两个".into(),
                tool_calls: vec![
                    ToolCallReq {
                        call_id: "t1".into(),
                        name: "search".into(),
                        arguments: "{\"a\":1}".into(),
                    },
                    ToolCallReq {
                        call_id: "t2".into(),
                        name: "search".into(),
                        arguments: "{\"b\":2}".into(),
                    },
                ],
            },
            Message::ToolResult {
                call_id: "t1".into(),
                content: "甲".into(),
            },
            Message::ToolResult {
                call_id: "t2".into(),
                content: "乙".into(),
            },
        ];
        let (_url, body, _headers) = build_request(&config, "说明", &history, &[]).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["messages"][1]["role"], "assistant");
        assert_eq!(v["messages"][1]["content"][0]["type"], "text");
        assert_eq!(v["messages"][1]["content"][1]["type"], "tool_use");
        assert_eq!(v["messages"][1]["content"][1]["input"]["a"], 1);
        assert_eq!(v["messages"][2]["role"], "user");
        assert_eq!(
            v["messages"][2]["content"].as_array().unwrap().len(),
            2,
            "两个连续 ToolResult 折叠进一条 user 消息"
        );
        assert_eq!(v["messages"][2]["content"][0]["type"], "tool_result");
        assert_eq!(v["max_tokens"], 16384);
        let keys = v
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(
            keys,
            std::collections::BTreeSet::from([
                "max_tokens",
                "messages",
                "model",
                "stream",
                "system",
            ]),
            "未配置 extra_body 时只发送协议必填的 max_tokens"
        );
    }

    #[test]
    fn model_context_window_exceeded_is_truncation() {
        let mut transform = Transform::new();
        let events = transform
            .push(
                "{\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"model_context_window_exceeded\"}}",
            )
            .unwrap();
        assert_eq!(events, vec![LlmEvent::Finished(Finish::MaxTokens)]);
    }

    #[test]
    fn user_images_map_to_base64_blocks_and_follow_tool_results() {
        let mut config = LlmConfig::test_defaults();
        config.protocol = super::super::Protocol::Anthropic;
        config.anthropic_max_tokens = Some(1024);
        let mut media = UserContent::new();
        media.push_image(crate::image_input::test_image_input(
            crate::image_input::ImageSource::Archived("media/1/1.png".into()),
        ));
        let history = vec![
            Message::Assistant {
                text: String::new(),
                tool_calls: vec![ToolCallReq {
                    call_id: "call_1".into(),
                    name: "read_image".into(),
                    arguments: "{}".into(),
                }],
            },
            Message::ToolResult {
                call_id: "call_1".into(),
                content: "已读取图片".into(),
            },
            Message::User(media),
        ];
        let (_, body, _) = build_request(&config, "说明", &history, &[]).unwrap();
        let value: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(value["messages"][1]["content"][0]["type"], "tool_result");
        assert_eq!(value["messages"][1]["content"][1]["type"], "image");
        assert_eq!(
            value["messages"][1]["content"][1]["source"]["media_type"],
            "image/png"
        );
        assert!(
            value["messages"][1]["content"][1]["source"]["data"]
                .as_str()
                .is_some_and(|data| !data.is_empty())
        );
    }
}
