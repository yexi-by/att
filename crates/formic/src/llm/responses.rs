//! OpenAI Responses 协议：历史 → 请求映射，SSE → 内部事件 transform。
//! 工具调用的完整参数在 response.output_item.done 事件上取，无需增量组装。
//! 事件类型是开放集合，未识别的一律忽略；失败类事件显式报错。

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
    let mut input = Vec::new();
    for (index, message) in history.iter().enumerate() {
        match message {
            Message::User(content) => {
                input.push(serde_json::json!({
                    "type": "message",
                    "role": "user",
                    "content": user_content(content, include_image_bytes)?,
                }));
            }
            Message::Compaction(text) => {
                input.push(serde_json::json!({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": format!("此前历史的已验证压缩摘要：\n{text}")}],
                }));
            }
            Message::Assistant { text, tool_calls } => {
                // 有完整供应商 output item 时只重放原始项，避免再合成一份重复历史。
                if matches!(
                    history.get(index + 1),
                    Some(Message::ResponseOutputItems(items)) if !items.is_empty()
                ) {
                    continue;
                }
                if !text.is_empty() {
                    input.push(serde_json::json!({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }));
                }
                for tc in tool_calls {
                    input.push(serde_json::json!({
                        "type": "function_call",
                        "call_id": tc.call_id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }));
                }
            }
            Message::ResponseOutputItems(items) => input.extend(items.iter().cloned()),
            Message::ToolResult { call_id, content } => {
                input.push(serde_json::json!({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": content,
                }));
            }
        }
    }
    let mut body = serde_json::json!({
        "model": config.model,
        "stream": true,
        "instructions": instructions,
        "input": input,
    });
    if !tools.is_empty() {
        body["tools"] = tools
            .iter()
            .map(|t| {
                serde_json::json!({
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
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
) -> Result<Vec<serde_json::Value>, LlmError> {
    content
        .parts()
        .iter()
        .map(|part| match part {
            ContentPart::Text(text) => Ok(serde_json::json!({"type": "input_text", "text": text})),
            ContentPart::Image(image) => {
                let image_url = if include_image_bytes {
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
                    "type": "input_image",
                    "image_url": image_url,
                    "detail": "auto",
                }))
            }
        })
        .collect()
}

#[derive(Default)]
pub struct Transform {
    saw_tool_call: bool,
    saw_refusal: bool,
    terminal: bool,
    output_items: Vec<serde_json::Value>,
    next_output_index: u64,
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
        if self.terminal {
            return Err(LlmError::protocol(
                "Responses 终态之后又收到协议事件",
                payload,
            ));
        }
        match ty {
            "response.output_text.delta" => {
                let delta = v["delta"].as_str().unwrap_or_default();
                Ok((!delta.is_empty())
                    .then(|| LlmEvent::TextDelta(delta.to_string()))
                    .into_iter()
                    .collect())
            }
            "response.refusal.delta" => {
                self.saw_refusal = true;
                Ok(Vec::new())
            }
            "response.output_item.done" => {
                let output_index = v["output_index"].as_u64().ok_or_else(|| {
                    LlmError::protocol("完成事件缺少非负整数 output_index", payload)
                })?;
                if output_index != self.next_output_index {
                    return Err(LlmError::protocol(
                        format!(
                            "完成项 output_index 必须稠密递增，期望 {}，实际为 {output_index}",
                            self.next_output_index
                        ),
                        payload,
                    ));
                }
                let item = v
                    .get("item")
                    .filter(|item| item.is_object())
                    .ok_or_else(|| LlmError::protocol("完成事件缺少 item object", payload))?;
                let item_type = item["type"]
                    .as_str()
                    .ok_or_else(|| LlmError::protocol("完成的 item 缺少字符串 type", payload))?;
                let tool_call = if item_type == "function_call" {
                    let tool_call = ToolCallReq {
                        call_id: required_nonempty_string(item, "call_id", payload)?,
                        name: required_nonempty_string(item, "name", payload)?,
                        arguments: required_nonempty_string(item, "arguments", payload)?,
                    };
                    if self.tool_call_ids.contains(&tool_call.call_id) {
                        return Err(LlmError::protocol("同一回合重复提供工具调用 id", payload));
                    }
                    Some(tool_call)
                } else {
                    None
                };
                self.next_output_index = self
                    .next_output_index
                    .checked_add(1)
                    .ok_or_else(|| LlmError::protocol("output_index 已耗尽", payload))?;
                self.output_items.push(item.clone());
                if let Some(tool_call) = tool_call {
                    self.tool_call_ids.insert(tool_call.call_id.clone());
                    self.saw_tool_call = true;
                    self.pending_tool_calls.push(tool_call);
                    Ok(Vec::new())
                } else {
                    if item_type == "refusal" {
                        self.saw_refusal = true;
                    }
                    Ok(Vec::new())
                }
            }
            "response.completed" => {
                self.terminal = true;
                let finish = if self.saw_refusal {
                    Finish::Refusal
                } else if self.saw_tool_call {
                    Finish::ToolUse
                } else {
                    Finish::Stop
                };
                let mut events: Vec<LlmEvent> = if finish == Finish::ToolUse {
                    self.pending_tool_calls
                        .drain(..)
                        .map(LlmEvent::ToolCall)
                        .collect()
                } else {
                    Vec::new()
                };
                events.extend(response_usage(&v).into_iter().map(LlmEvent::Usage));
                events.push(LlmEvent::Finished(finish));
                Ok(events)
            }
            "response.incomplete" => {
                let finish = match v["response"]["incomplete_details"]["reason"].as_str() {
                    Some("max_output_tokens") => Finish::MaxTokens,
                    Some("content_filter") => Finish::Refusal,
                    _ => return Err(LlmError::protocol("响应未完成且原因未知", payload)),
                };
                self.terminal = true;
                Ok(vec![LlmEvent::Finished(finish)])
            }
            "response.failed" | "error" => Err(LlmError::protocol("响应失败事件", payload)),
            _ => Ok(Vec::new()),
        }
    }

    fn response_output_items(&self) -> &[serde_json::Value] {
        &self.output_items
    }
}

fn required_nonempty_string(
    item: &serde_json::Value,
    field: &str,
    payload: &str,
) -> Result<String, LlmError> {
    let value = item
        .get(field)
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| {
            LlmError::protocol(
                format!("完成的 function_call.{field} 必须是字符串"),
                payload,
            )
        })?;
    if value.is_empty() {
        return Err(LlmError::protocol(
            format!("完成的 function_call.{field} 不得为空"),
            payload,
        ));
    }
    Ok(value.to_string())
}

fn response_usage(value: &serde_json::Value) -> Option<ProviderUsage> {
    let usage = value.pointer("/response/usage")?;
    Some(ProviderUsage {
        input_tokens: usage
            .get("input_tokens")
            .and_then(serde_json::Value::as_u64),
        output_tokens: usage
            .get("output_tokens")
            .and_then(serde_json::Value::as_u64),
        cache_read_tokens: usage
            .pointer("/input_tokens_details/cached_tokens")
            .and_then(serde_json::Value::as_u64),
        cache_creation_tokens: usage
            .pointer("/input_tokens_details/cache_creation_tokens")
            .and_then(serde_json::Value::as_u64),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::Transform as _;
    use crate::llm::events_of;

    const TEXT_SAMPLE: &str = concat!(
        "data: {\"type\":\"response.created\",\"response\":{\"id\":\"r1\",\"status\":\"in_progress\"}}\n\n",
        "data: {\"type\":\"response.output_item.added\",\"output_index\":0,\"item\":{\"type\":\"message\",\"id\":\"m1\",\"status\":\"in_progress\"}}\n\n",
        "data: {\"type\":\"response.output_text.delta\",\"item_id\":\"m1\",\"output_index\":0,\"content_index\":0,\"delta\":\"你好\"}\n\n",
        "data: {\"type\":\"response.output_text.delta\",\"item_id\":\"m1\",\"output_index\":0,\"content_index\":0,\"delta\":\"世界\"}\n\n",
        "data: {\"type\":\"response.output_text.done\",\"item_id\":\"m1\",\"output_index\":0,\"content_index\":0,\"text\":\"你好世界\"}\n\n",
        "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{\"type\":\"message\",\"id\":\"m1\",\"status\":\"completed\"}}\n\n",
        "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"status\":\"completed\"}}\n\n",
    );

    const TOOL_SAMPLE: &str = concat!(
        "data: {\"type\":\"response.created\",\"response\":{\"id\":\"r1\",\"status\":\"in_progress\"}}\n\n",
        "data: {\"type\":\"response.output_item.added\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc1\",\"call_id\":\"call_1\",\"name\":\"search\",\"arguments\":\"\"}}\n\n",
        "data: {\"type\":\"response.function_call_arguments.delta\",\"item_id\":\"fc1\",\"output_index\":0,\"delta\":\"{\\\"pattern\\\":\\\"苹果\\\"\"}\n\n",
        "data: {\"type\":\"response.function_call_arguments.delta\",\"item_id\":\"fc1\",\"output_index\":0,\"delta\":\",\\\"scope\\\":\\\"input\\\"}\"}\n\n",
        "data: {\"type\":\"response.output_item.done\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc1\",\"call_id\":\"call_1\",\"name\":\"search\",\"arguments\":\"{\\\"pattern\\\":\\\"苹果\\\",\\\"scope\\\":\\\"input\\\"}\"}}\n\n",
        "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"status\":\"completed\"}}\n\n",
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
    fn tool_call_taken_from_done_item() {
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
    fn completed_function_call_requires_nonempty_string_fields() {
        for item in [
            serde_json::json!({"type":"function_call","name":"search","arguments":"{}"}),
            serde_json::json!({"type":"function_call","call_id":"","name":"search","arguments":"{}"}),
            serde_json::json!({"type":"function_call","call_id":1,"name":"search","arguments":"{}"}),
            serde_json::json!({"type":"function_call","call_id":"call_1","arguments":"{}"}),
            serde_json::json!({"type":"function_call","call_id":"call_1","name":"","arguments":"{}"}),
            serde_json::json!({"type":"function_call","call_id":"call_1","name":"search"}),
            serde_json::json!({"type":"function_call","call_id":"call_1","name":"search","arguments":""}),
            serde_json::json!({"type":"function_call","call_id":"call_1","name":"search","arguments":{}}),
        ] {
            let mut transform = Transform::new();
            let result = transform.push(
                &serde_json::json!({
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": item,
                })
                .to_string(),
            );
            assert!(matches!(result, Err(LlmError::Protocol { .. })));
            assert!(
                transform.response_output_items().is_empty(),
                "非法完成项不得进入可重放历史"
            );
        }
    }

    #[test]
    fn function_arguments_json_is_left_for_the_worker_to_parse_once() {
        let mut transform = Transform::new();
        let events = transform
            .push(
                r#"{"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","call_id":"call_1","name":"search","arguments":"{"}}"#,
            )
            .unwrap();
        assert!(events.is_empty(), "终态确认前不得释放工具调用");
        let events = transform
            .push(r#"{"type":"response.completed","response":{}}"#)
            .unwrap();
        assert_eq!(
            events,
            vec![
                LlmEvent::ToolCall(ToolCallReq {
                    call_id: "call_1".into(),
                    name: "search".into(),
                    arguments: "{".into(),
                }),
                LlmEvent::Finished(Finish::ToolUse),
            ]
        );
    }

    #[test]
    fn completed_output_indices_must_be_dense_and_cannot_repeat() {
        for invalid_index in [0, 2] {
            let mut transform = Transform::new();
            transform
                .push(
                    r#"{"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"r1"}}"#,
                )
                .unwrap();
            let result = transform.push(
                &serde_json::json!({
                    "type": "response.output_item.done",
                    "output_index": invalid_index,
                    "item": {"type":"message","id":"m1"},
                })
                .to_string(),
            );
            assert!(matches!(result, Err(LlmError::Protocol { .. })));
        }

        let mut missing = Transform::new();
        assert!(matches!(
            missing.push(
                r#"{"type":"response.output_item.done","item":{"type":"message","id":"m1"}}"#
            ),
            Err(LlmError::Protocol { .. })
        ));
    }

    #[test]
    fn output_item_after_terminal_is_rejected_without_mutating_replay_state() {
        let reasoning = serde_json::json!({
            "type": "reasoning",
            "id": "reasoning-1",
            "encrypted_content": "opaque-state",
        });
        let mut transform = Transform::new();
        transform
            .push(
                &serde_json::json!({
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": reasoning,
                })
                .to_string(),
            )
            .unwrap();
        assert_eq!(
            transform
                .push(r#"{"type":"response.completed","response":{}}"#)
                .unwrap(),
            vec![LlmEvent::Finished(Finish::Stop)]
        );

        let result = transform.push(
            r#"{"type":"response.output_item.done","output_index":1,"item":{"type":"function_call","call_id":"late-call","name":"search","arguments":"{}"}}"#,
        );

        assert!(matches!(result, Err(LlmError::Protocol { .. })));
        assert_eq!(transform.response_output_items(), &[reasoning]);
        assert_eq!(transform.next_output_index, 1);
        assert!(transform.pending_tool_calls.is_empty());
        assert!(!transform.tool_call_ids.contains("late-call"));
    }

    #[test]
    fn duplicate_tool_call_ids_are_rejected_without_releasing_the_first_call() {
        let mut transform = Transform::new();
        let first = transform
            .push(
                r#"{"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","call_id":"same","name":"first","arguments":"{}"}}"#,
            )
            .unwrap();
        assert!(first.is_empty());
        let result = transform.push(
            r#"{"type":"response.output_item.done","output_index":1,"item":{"type":"function_call","call_id":"same","name":"second","arguments":"{}"}}"#,
        );
        assert!(matches!(result, Err(LlmError::Protocol { .. })));
    }

    #[test]
    fn incomplete_max_output_tokens() {
        let mut t = Transform::new();
        let events = t
            .push("{\"type\":\"response.incomplete\",\"response\":{\"status\":\"incomplete\",\"incomplete_details\":{\"reason\":\"max_output_tokens\"}}}")
            .unwrap();
        assert_eq!(events, vec![LlmEvent::Finished(Finish::MaxTokens)]);
    }

    #[test]
    fn completed_usage_and_refusal_are_distinct() {
        let mut usage = Transform::new();
        let events = usage
            .push("{\"type\":\"response.completed\",\"response\":{\"usage\":{\"input_tokens\":10,\"output_tokens\":2,\"input_tokens_details\":{\"cached_tokens\":7,\"cache_creation_tokens\":1}}}}")
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
        refusal
            .push("{\"type\":\"response.refusal.delta\",\"delta\":\"no\"}")
            .unwrap();
        let events = refusal
            .push("{\"type\":\"response.completed\",\"response\":{}}")
            .unwrap();
        assert_eq!(events, vec![LlmEvent::Finished(Finish::Refusal)]);
    }

    #[test]
    fn history_maps_to_input_items() {
        let config = LlmConfig {
            protocol: super::super::Protocol::Responses,
            endpoint: "http://x/v1/responses".parse().unwrap(),
            model: "m".into(),
            api_key: None,
            context_window_tokens: 131072,
            anthropic_max_tokens: None,
            ..LlmConfig::test_defaults()
        };
        let history = vec![
            Message::User("任务".into()),
            Message::Assistant {
                text: "看一下".into(),
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
        assert_eq!(v["instructions"], "说明");
        assert_eq!(v["input"][0]["role"], "user");
        assert_eq!(v["input"][1]["role"], "assistant");
        assert_eq!(v["input"][2]["type"], "function_call");
        assert_eq!(v["input"][2]["call_id"], "call_1");
        assert_eq!(v["input"][3]["type"], "function_call_output");
        assert_eq!(v["input"][3]["output"], "结果");
        let keys = v
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(
            keys,
            std::collections::BTreeSet::from(["input", "instructions", "model", "stream"]),
            "未配置 extra_body 时不得自行添加生成控制字段"
        );
    }

    #[test]
    fn completed_output_items_are_preserved_and_replayed_verbatim() {
        let reasoning = serde_json::json!({
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "opaque-provider-state",
            "summary": [],
        });
        let function_call = serde_json::json!({
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "search",
            "arguments": "{\"pattern\":\"x\"}",
            "status": "completed",
        });
        let mut transform = Transform::new();
        transform
            .push(
                &serde_json::json!({
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": reasoning,
                })
                .to_string(),
            )
            .unwrap();
        transform
            .push(
                &serde_json::json!({
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": function_call,
                })
                .to_string(),
            )
            .unwrap();
        let preserved = transform.response_output_items().to_vec();
        assert_eq!(preserved, vec![reasoning.clone(), function_call.clone()]);

        let config = LlmConfig {
            protocol: super::super::Protocol::Responses,
            endpoint: "http://x/v1/responses".parse().unwrap(),
            model: "m".into(),
            api_key: None,
            context_window_tokens: 131072,
            anthropic_max_tokens: None,
            ..LlmConfig::test_defaults()
        };
        let history = vec![
            Message::User("任务".into()),
            Message::Assistant {
                text: "不会重复发送".into(),
                tool_calls: vec![ToolCallReq {
                    call_id: "call_1".into(),
                    name: "search".into(),
                    arguments: "{\"pattern\":\"x\"}".into(),
                }],
            },
            Message::ResponseOutputItems(preserved),
            Message::ToolResult {
                call_id: "call_1".into(),
                content: "结果".into(),
            },
        ];
        let (_url, body, _headers) = build_request(&config, "说明", &history, &[]).unwrap();
        let value: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(value["input"][1], reasoning);
        assert_eq!(value["input"][2], function_call);
        assert_eq!(value["input"][3]["type"], "function_call_output");
        assert_eq!(value["input"].as_array().unwrap().len(), 4);
    }

    #[test]
    fn user_images_map_to_input_image_blocks() {
        let mut config = LlmConfig::test_defaults();
        config.protocol = super::super::Protocol::Responses;
        let mut content = UserContent::new();
        content.push_text("看图");
        content.push_image(crate::image_input::test_image_input(
            crate::image_input::ImageSource::Input("sample.png".into()),
        ));
        let (_, body, _) = build_request(&config, "说明", &[Message::User(content)], &[]).unwrap();
        let value: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(value["input"][0]["content"][0]["type"], "input_text");
        assert_eq!(value["input"][0]["content"][1]["type"], "input_image");
        assert_eq!(value["input"][0]["content"][1]["detail"], "auto");
        assert!(
            value["input"][0]["content"][1]["image_url"]
                .as_str()
                .unwrap()
                .starts_with("data:image/png;base64,")
        );
    }
}
