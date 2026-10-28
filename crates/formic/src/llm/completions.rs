//! OpenAI Chat Completions 协议：历史 → 请求映射，SSE → 内部事件 transform。
//! 兼容讲同一形状的供应商（OpenAI、DeepSeek、Moonshot、OpenRouter、vLLM 等）。
//! 工具调用的流式增量按 index 缓冲组装，finish_reason=tool_calls 时一次性放出。

use super::{
    ContentPart, Finish, LlmConfig, LlmError, LlmEvent, Message, ProviderUsage, ToolCallReq,
    ToolSpec, UserContent,
};

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
    let mut messages = vec![serde_json::json!({"role": "system", "content": instructions})];
    for message in history {
        match message {
            Message::User(content) => {
                messages.push(serde_json::json!({
                    "role": "user",
                    "content": user_content(content, include_image_bytes)?,
                }));
            }
            Message::Compaction(text) => {
                messages.push(serde_json::json!({
                    "role": "user",
                    "content": format!("此前历史的已验证压缩摘要：\n{text}"),
                }));
            }
            Message::Assistant { text, tool_calls } => {
                let mut msg = serde_json::json!({"role": "assistant"});
                msg["content"] = if text.is_empty() {
                    serde_json::Value::Null
                } else {
                    serde_json::json!(text)
                };
                if !tool_calls.is_empty() {
                    msg["tool_calls"] = tool_calls
                        .iter()
                        .map(|tc| {
                            serde_json::json!({
                                "id": tc.call_id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": tc.arguments},
                            })
                        })
                        .collect();
                }
                messages.push(msg);
            }
            // 这是 Responses 专属的无损历史；本协议使用紧邻它之前的 Assistant。
            Message::ResponseOutputItems(_) => {}
            Message::ToolResult { call_id, content } => {
                messages.push(serde_json::json!({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                }));
            }
        }
    }
    let mut body = serde_json::json!({
        "model": config.model,
        "stream": true,
        "messages": messages,
    });
    if !tools.is_empty() {
        body["tools"] = tools
            .iter()
            .map(|t| {
                serde_json::json!({
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                })
            })
            .collect();
    }
    super::add_extra_body(&mut body, config);
    let headers = config
        .api_key
        .as_ref()
        .map(|key| ("authorization".to_string(), format!("Bearer {key}")))
        .into_iter()
        .collect();
    Ok((config.endpoint.clone(), body.to_string(), headers))
}

fn user_content(
    content: &UserContent,
    include_image_bytes: bool,
) -> Result<serde_json::Value, LlmError> {
    if let [ContentPart::Text(text)] = content.parts() {
        return Ok(serde_json::json!(text));
    }
    let parts = content
        .parts()
        .iter()
        .map(|part| match part {
            ContentPart::Text(text) => Ok(serde_json::json!({"type": "text", "text": text})),
            ContentPart::Image(image) => {
                let url = if include_image_bytes {
                    image
                        .data
                        .data_url()
                        .map_err(|error| LlmError::RequestBuild {
                            reason: error.to_string(),
                        })?
                } else {
                    format!("data:{};base64,", image.data.media_type().mime())
                };
                Ok(serde_json::json!({
                    "type": "image_url",
                    "image_url": {"url": url, "detail": "auto"},
                }))
            }
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(serde_json::Value::Array(parts))
}

/// 每个 index 一条在组装的工具调用。`arguments_seen` 区分供应商实际发送了字符串
/// （包括空字符串），还是 arguments 缺失或为 null；参数 JSON 的语义校验仍由 worker 负责。
#[derive(Default)]
struct PartialToolCall {
    call_id: String,
    name: String,
    arguments: String,
    arguments_seen: bool,
}

#[derive(Default)]
pub struct Transform {
    partial: Vec<PartialToolCall>,
    saw_refusal: bool,
    pending_usage: Option<ProviderUsage>,
}

impl Transform {
    pub fn new() -> Self {
        Self::default()
    }

    fn finish_with_usage(&mut self, finish: Finish) -> Vec<LlmEvent> {
        let mut events = self
            .pending_usage
            .take()
            .map(LlmEvent::Usage)
            .into_iter()
            .collect::<Vec<_>>();
        events.push(LlmEvent::Finished(finish));
        events
    }
}

impl super::Transform for Transform {
    fn push(&mut self, payload: &str) -> Result<Vec<LlmEvent>, LlmError> {
        if payload.trim() == "[DONE]" {
            return Ok(Vec::new());
        }
        let v: serde_json::Value = serde_json::from_str(payload)
            .map_err(|e| LlmError::protocol(format!("JSON 解析失败：{e}"), payload))?;
        if let Some(usage) = v.get("usage").filter(|usage| usage.is_object()) {
            let event = LlmEvent::Usage(ProviderUsage {
                input_tokens: usage
                    .get("prompt_tokens")
                    .and_then(serde_json::Value::as_u64),
                output_tokens: usage
                    .get("completion_tokens")
                    .and_then(serde_json::Value::as_u64),
                cache_read_tokens: usage
                    .pointer("/prompt_tokens_details/cached_tokens")
                    .and_then(serde_json::Value::as_u64),
                cache_creation_tokens: usage
                    .pointer("/prompt_tokens_details/cache_creation_tokens")
                    .and_then(serde_json::Value::as_u64),
            });
            if v.get("choices")
                .and_then(serde_json::Value::as_array)
                .is_some_and(Vec::is_empty)
            {
                return Ok(vec![event]);
            }
            let LlmEvent::Usage(usage) = event else {
                unreachable!()
            };
            self.pending_usage = Some(usage);
        }
        let choices = v["choices"]
            .as_array()
            .ok_or_else(|| LlmError::protocol("缺少 choices 数组", payload))?;
        let Some(choice) = choices.first() else {
            return Ok(Vec::new()); // 纯用量等无 choices 的帧，与产出无关
        };

        let mut events = Vec::new();
        if let Some(delta) = choice.get("delta") {
            if let Some(tool_calls_value) = delta.get("tool_calls").filter(|value| !value.is_null())
            {
                let tool_calls = tool_calls_value.as_array().ok_or_else(|| {
                    LlmError::protocol("delta.tool_calls 必须是数组或 null", payload)
                })?;
                let mut frame_indices = std::collections::HashSet::with_capacity(tool_calls.len());
                for part in tool_calls {
                    let part = part.as_object().ok_or_else(|| {
                        LlmError::protocol("tool_calls 元素必须是 object", payload)
                    })?;
                    let index_u64 = part
                        .get("index")
                        .and_then(serde_json::Value::as_u64)
                        .ok_or_else(|| {
                            LlmError::protocol("tool_calls 元素缺少非负整数 index", payload)
                        })?;
                    let index = usize::try_from(index_u64).map_err(|_| {
                        LlmError::protocol("tool_calls index 超出本机可表示范围", payload)
                    })?;
                    if !frame_indices.insert(index) {
                        return Err(LlmError::protocol(
                            format!("同一增量帧重复提供 tool_calls index {index}"),
                            payload,
                        ));
                    }
                    if index > self.partial.len() {
                        return Err(LlmError::protocol(
                            format!(
                                "tool_calls index 必须稠密递增，期望不大于 {}，实际为 {index}",
                                self.partial.len()
                            ),
                            payload,
                        ));
                    }
                    if index == self.partial.len() {
                        self.partial.push(PartialToolCall::default());
                    }
                    if let Some(call_type) = part.get("type").filter(|value| !value.is_null())
                        && call_type.as_str() != Some("function")
                    {
                        return Err(LlmError::protocol(
                            "tool_calls.type 必须是 function 或 null",
                            payload,
                        ));
                    }
                    let slot = &mut self.partial[index];
                    if let Some(id) = part.get("id").filter(|value| !value.is_null()) {
                        slot.call_id.push_str(id.as_str().ok_or_else(|| {
                            LlmError::protocol("tool_calls.id 必须是字符串或 null", payload)
                        })?);
                    }
                    if let Some(function) = part.get("function").filter(|value| !value.is_null()) {
                        let function = function.as_object().ok_or_else(|| {
                            LlmError::protocol("tool_calls.function 必须是 object 或 null", payload)
                        })?;
                        if let Some(name) = function.get("name").filter(|value| !value.is_null()) {
                            slot.name.push_str(name.as_str().ok_or_else(|| {
                                LlmError::protocol(
                                    "tool_calls.function.name 必须是字符串或 null",
                                    payload,
                                )
                            })?);
                        }
                        if let Some(args) =
                            function.get("arguments").filter(|value| !value.is_null())
                        {
                            slot.arguments.push_str(args.as_str().ok_or_else(|| {
                                LlmError::protocol(
                                    "tool_calls.function.arguments 必须是字符串或 null",
                                    payload,
                                )
                            })?);
                            slot.arguments_seen = true;
                        }
                    }
                }
            }
            if let Some(content) = delta["content"].as_str()
                && !content.is_empty()
            {
                events.push(LlmEvent::TextDelta(content.to_string()));
            }
            if delta.get("refusal").is_some_and(|value| !value.is_null()) {
                self.saw_refusal = true;
            }
        }

        let terminal_events = match choice.get("finish_reason") {
            Some(serde_json::Value::String(reason)) => match reason.as_str() {
                "stop" if self.saw_refusal => Ok(self.finish_with_usage(Finish::Refusal)),
                "stop" => Ok(self.finish_with_usage(Finish::Stop)),
                "length" => Ok(self.finish_with_usage(Finish::MaxTokens)),
                "content_filter" | "refusal" => Ok(self.finish_with_usage(Finish::Refusal)),
                "tool_calls" | "function_call" => {
                    if self.partial.is_empty() {
                        return Err(LlmError::protocol(
                            "finish_reason 声称工具调用但没有工具调用内容",
                            payload,
                        ));
                    }
                    let mut call_ids = std::collections::HashSet::new();
                    for (index, call) in self.partial.iter().enumerate() {
                        if call.call_id.is_empty() {
                            return Err(LlmError::protocol(
                                format!("完成的工具调用 {index} 缺少非空 id"),
                                payload,
                            ));
                        }
                        if call.name.is_empty() {
                            return Err(LlmError::protocol(
                                format!("完成的工具调用 {index} 缺少非空 function.name"),
                                payload,
                            ));
                        }
                        if !call.arguments_seen {
                            return Err(LlmError::protocol(
                                format!("完成的工具调用 {index} 从未提供 function.arguments"),
                                payload,
                            ));
                        }
                        if !call_ids.insert(call.call_id.as_str()) {
                            return Err(LlmError::protocol("同一回合重复提供工具调用 id", payload));
                        }
                    }
                    let mut events: Vec<LlmEvent> = self
                        .partial
                        .drain(..)
                        .map(|call| {
                            LlmEvent::ToolCall(ToolCallReq {
                                call_id: call.call_id,
                                name: call.name,
                                arguments: call.arguments,
                            })
                        })
                        .collect();
                    if let Some(usage) = self.pending_usage.take() {
                        events.push(LlmEvent::Usage(usage));
                    }
                    events.push(LlmEvent::Finished(Finish::ToolUse));
                    Ok(events)
                }
                _ => Err(LlmError::protocol("未知 finish_reason", payload)),
            },
            _ => Ok(Vec::new()),
        }?;
        events.extend(terminal_events);
        Ok(events)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::Transform as _;
    use crate::llm::events_of;

    const TEXT_SAMPLE: &str = concat!(
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"\"},\"finish_reason\":null}]}\n\n",
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"你好\",\"tool_calls\":null},\"finish_reason\":null}]}\n\n",
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"世界\"},\"finish_reason\":null}]}\n\n",
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":null,\"tool_calls\":null},\"finish_reason\":\"stop\"}]}\n\n",
        "data: [DONE]\n\n",
    );

    const TOOL_SAMPLE: &str = concat!(
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":null,\"type\":null,\"function\":null}]},\"finish_reason\":null}]}\n\n",
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"search\",\"arguments\":\"\"}}]},\"finish_reason\":null}]}\n\n",
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":null,\"type\":null,\"function\":{\"name\":null,\"arguments\":\"{\\\"pattern\\\":\\\"苹果\\\"\"}}]},\"finish_reason\":null}]}\n\n",
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"name\":null,\"arguments\":null}}]},\"finish_reason\":null}]}\n\n",
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\",\\\"scope\\\":\\\"input\\\"}\"}}]},\"finish_reason\":null}]}\n\n",
        "data: {\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\"created\":1,\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":null},\"finish_reason\":\"tool_calls\"}]}\n\n",
        "data: [DONE]\n\n",
    );

    #[test]
    fn recorded_sample_yields_text_then_stop() {
        let events = events_of(Box::new(Transform::new()), TEXT_SAMPLE);
        assert_eq!(
            events,
            vec![
                LlmEvent::TextDelta("你好".into()),
                LlmEvent::TextDelta("世界".into()),
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
                    call_id: "call_1".into(),
                    name: "search".into(),
                    arguments: "{\"pattern\":\"苹果\",\"scope\":\"input\"}".into(),
                }),
                LlmEvent::Finished(Finish::ToolUse),
            ]
        );
    }

    #[test]
    fn tool_calls_finish_without_content_is_error() {
        let mut t = Transform::new();
        let result =
            t.push("{\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}");
        assert!(matches!(result, Err(LlmError::Protocol { .. })));
    }

    #[test]
    fn sparse_or_duplicate_tool_call_indices_are_protocol_errors() {
        let mut sparse = Transform::new();
        let result = sparse.push(
            "{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":1000000000,\"id\":\"call\"}]},\"finish_reason\":null}]}",
        );
        assert!(matches!(result, Err(LlmError::Protocol { .. })));
        assert!(sparse.partial.is_empty(), "跳号不得触发 resize 或留下空槽");

        let mut duplicate = Transform::new();
        let result = duplicate.push(
            "{\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"a\"},{\"index\":0,\"id\":\"b\"}]},\"finish_reason\":null}]}",
        );
        assert!(matches!(result, Err(LlmError::Protocol { .. })));
    }

    #[test]
    fn completed_tool_calls_require_nonempty_id_name_and_an_arguments_field() {
        for first_delta in [
            r#"{"index":0,"id":"","type":"function","function":{"name":"search","arguments":"{}"}}"#,
            r#"{"index":0,"id":"call_1","type":"function","function":{"name":"","arguments":"{}"}}"#,
            r#"{"index":0,"id":"call_1","type":"function","function":{"name":"search"}}"#,
            r#"{"index":0,"id":null,"type":null,"function":{"name":"search","arguments":"{}"}}"#,
            r#"{"index":0,"id":"call_1","function":{"name":null,"arguments":"{}"}}"#,
            r#"{"index":0,"id":"call_1","function":{"name":"search","arguments":null}}"#,
        ] {
            let mut transform = Transform::new();
            transform
                .push(&format!(
                    r#"{{"choices":[{{"delta":{{"tool_calls":[{first_delta}]}},"finish_reason":null}}]}}"#
                ))
                .unwrap();
            let result =
                transform.push(r#"{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}"#);
            assert!(matches!(result, Err(LlmError::Protocol { .. })));
        }
    }

    #[test]
    fn duplicate_tool_call_ids_are_rejected_before_any_call_is_released() {
        let mut transform = Transform::new();
        let events = transform
            .push(
                r#"{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"same","type":"function","function":{"name":"first","arguments":"{}"}},{"index":1,"id":"same","type":"function","function":{"name":"second","arguments":"{}"}}]},"finish_reason":null}]}"#,
            )
            .unwrap();
        assert!(events.is_empty());
        assert!(matches!(
            transform.push(r#"{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}"#),
            Err(LlmError::Protocol { .. })
        ));
    }

    #[test]
    fn final_text_delta_usage_and_finish_from_one_choice_are_all_preserved() {
        let mut transform = Transform::new();
        let events = transform
            .push(
                r#"{"usage":{"prompt_tokens":3,"completion_tokens":1},"choices":[{"delta":{"content":"最后一段"},"finish_reason":"stop"}]}"#,
            )
            .unwrap();
        assert_eq!(
            events,
            vec![
                LlmEvent::TextDelta("最后一段".into()),
                LlmEvent::Usage(ProviderUsage {
                    input_tokens: Some(3),
                    output_tokens: Some(1),
                    cache_read_tokens: None,
                    cache_creation_tokens: None,
                }),
                LlmEvent::Finished(Finish::Stop),
            ]
        );
    }

    #[test]
    fn final_arguments_delta_and_tool_finish_from_one_choice_are_both_processed() {
        let mut transform = Transform::new();
        transform
            .push(
                r#"{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"search","arguments":"{"}}]},"finish_reason":null}]}"#,
            )
            .unwrap();
        let events = transform
            .push(
                r#"{"usage":{"completion_tokens":2},"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":"tool_calls"}]}"#,
            )
            .unwrap();
        assert_eq!(
            events,
            vec![
                LlmEvent::ToolCall(ToolCallReq {
                    call_id: "call_1".into(),
                    name: "search".into(),
                    arguments: "{}".into(),
                }),
                LlmEvent::Usage(ProviderUsage {
                    input_tokens: None,
                    output_tokens: Some(2),
                    cache_read_tokens: None,
                    cache_creation_tokens: None,
                }),
                LlmEvent::Finished(Finish::ToolUse),
            ]
        );
    }

    #[test]
    fn tool_call_fields_with_wrong_json_types_are_protocol_errors() {
        for value in [r#"{}"#, r#""bad""#, "1", "true", "[null]"] {
            let mut transform = Transform::new();
            let result = transform.push(&format!(
                r#"{{"choices":[{{"delta":{{"tool_calls":{value}}},"finish_reason":null}}]}}"#
            ));
            assert!(matches!(result, Err(LlmError::Protocol { .. })), "{value}");
        }
        for part in [
            r#"{"index":null}"#,
            r#"{"index":"0"}"#,
            r#"{"index":0,"id":1}"#,
            r#"{"index":0,"function":[]}"#,
            r#"{"index":0,"function":{"name":1}}"#,
            r#"{"index":0,"function":{"arguments":{}}}"#,
            r#"{"index":0,"type":"custom"}"#,
            r#"{"index":0,"type":1}"#,
        ] {
            let mut transform = Transform::new();
            let result = transform.push(&format!(
                r#"{{"choices":[{{"delta":{{"tool_calls":[{part}]}},"finish_reason":null}}]}}"#
            ));
            assert!(matches!(result, Err(LlmError::Protocol { .. })), "{part}");
        }
    }

    #[test]
    fn length_finish_is_max_tokens() {
        let mut t = Transform::new();
        let events = t
            .push("{\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"length\"}]}")
            .unwrap();
        assert_eq!(events, vec![LlmEvent::Finished(Finish::MaxTokens)]);
    }

    #[test]
    fn usage_and_refusal_are_preserved_on_final_choice() {
        let mut usage = Transform::new();
        let events = usage
            .push("{\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":2,\"prompt_tokens_details\":{\"cached_tokens\":7,\"cache_creation_tokens\":1}},\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}")
            .unwrap();
        assert_eq!(
            events,
            vec![
                LlmEvent::Usage(ProviderUsage {
                    input_tokens: Some(10),
                    output_tokens: Some(2),
                    cache_read_tokens: Some(7),
                    cache_creation_tokens: Some(1),
                }),
                LlmEvent::Finished(Finish::Stop),
            ]
        );

        let mut refusal = Transform::new();
        let events = refusal
            .push("{\"choices\":[{\"delta\":{\"refusal\":\"no\"},\"finish_reason\":\"stop\"}]}")
            .unwrap();
        assert_eq!(events, vec![LlmEvent::Finished(Finish::Refusal)]);
    }

    #[test]
    fn history_maps_to_messages() {
        let config = LlmConfig {
            protocol: super::super::Protocol::Completions,
            endpoint: "http://x/v1/chat/completions".parse().unwrap(),
            model: "m".into(),
            api_key: None,
            context_window_tokens: 131072,
            anthropic_max_tokens: None,
            ..LlmConfig::test_defaults()
        };
        let history = vec![
            Message::User("任务".into()),
            Message::Assistant {
                text: String::new(),
                tool_calls: vec![ToolCallReq {
                    call_id: "call_1".into(),
                    name: "search".into(),
                    arguments: "{\"pattern\":\"x\"}".into(),
                }],
            },
            Message::ToolResult {
                call_id: "call_1".into(),
                content: "结果".into(),
            },
        ];
        let (_url, body, _headers) = build_request(&config, "说明", &history, &[]).unwrap();
        let v: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["messages"][0]["role"], "system");
        assert_eq!(v["messages"][1]["role"], "user");
        assert_eq!(v["messages"][2]["role"], "assistant");
        assert_eq!(v["messages"][2]["tool_calls"][0]["id"], "call_1");
        assert_eq!(v["messages"][3]["role"], "tool");
        assert_eq!(v["messages"][3]["tool_call_id"], "call_1");
        assert_eq!(v["messages"][3]["content"], "结果");
        assert!(v.get("tools").is_none(), "无工具时不发 tools 字段");
        let keys = v
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(
            keys,
            std::collections::BTreeSet::from(["messages", "model", "stream"]),
            "未配置 extra_body 时不得自行添加生成控制字段"
        );
    }

    #[test]
    fn user_images_use_data_urls_after_text_tool_results() {
        let mut config = LlmConfig::test_defaults();
        config.protocol = super::super::Protocol::Completions;
        let mut media = UserContent::new();
        media.push_text("附加图片");
        media.push_image(crate::image_input::test_image_input(
            crate::image_input::ImageSource::Input("sample.png".into()),
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
        assert_eq!(value["messages"][2]["role"], "tool");
        assert!(value["messages"][2]["content"].is_string());
        assert_eq!(value["messages"][3]["role"], "user");
        assert_eq!(value["messages"][3]["content"][1]["type"], "image_url");
        assert_eq!(
            value["messages"][3]["content"][1]["image_url"]["detail"],
            "auto"
        );
        assert!(
            value["messages"][3]["content"][1]["image_url"]["url"]
                .as_str()
                .unwrap()
                .starts_with("data:image/png;base64,")
        );
    }
}
