//! LLM 调用层：三种 API 协议（Chat Completions / Responses / Anthropic）各自负责
//! 双向翻译——请求侧把内部对话历史映射为协议格式，响应侧把 SSE 流组装成统一
//! 内部事件；worker 主循环只消费内部事件，不感知后端差异。供应商响应先在内存中
//! 转成类型化事件；原始 HTTP 正文、SSE envelope、URL 和传输错误原文都不进入档案。

pub mod anthropic;
pub mod completions;
pub mod responses;

use std::collections::VecDeque;
use std::fmt;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bytes::{Bytes, BytesMut};
use futures_util::{Stream, StreamExt};

use crate::image_input::ImageInput;

pub(crate) type BuiltRequest = (reqwest::Url, String, Vec<(String, String)>);
pub(crate) type BuildRequestResult = Result<BuiltRequest, LlmError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputModalities {
    Text,
    TextAndImage,
}

impl InputModalities {
    pub fn parse(values: &[String]) -> Result<Self, String> {
        match values {
            [text] if text == "text" => Ok(Self::Text),
            [text, image] if text == "text" && image == "image" => Ok(Self::TextAndImage),
            _ => Err("model_input_modalities 只接受 [\"text\"] 或 [\"text\", \"image\"]".into()),
        }
    }

    pub const fn supports_image(self) -> bool {
        matches!(self, Self::TextAndImage)
    }

    pub const fn names(self) -> &'static [&'static str] {
        match self {
            Self::Text => &["text"],
            Self::TextAndImage => &["text", "image"],
        }
    }
}

/// 协议形状，由 TOML 的 `protocol` 选择。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Protocol {
    Completions,
    Responses,
    Anthropic,
}

impl Protocol {
    pub fn parse(s: &str) -> Result<Self, String> {
        match s {
            "completions" => Ok(Self::Completions),
            "responses" => Ok(Self::Responses),
            "anthropic" => Ok(Self::Anthropic),
            other => Err(format!(
                "未知 protocol {other:?}，可选值：completions / responses / anthropic"
            )),
        }
    }

    /// 请求结构由 Formic 填写，扩展 JSON 不得覆盖这些字段。
    pub(crate) fn managed_request_fields(self) -> &'static [&'static str] {
        match self {
            Self::Completions => &["model", "stream", "messages", "tools"],
            Self::Responses => &["model", "stream", "instructions", "input", "tools"],
            Self::Anthropic => &[
                "model",
                "max_tokens",
                "stream",
                "system",
                "messages",
                "tools",
            ],
        }
    }

    pub fn estimate_image_tokens(self, width: u32, height: u32) -> u64 {
        match self {
            Self::Completions | Self::Responses => openai_image_tokens(width, height),
            Self::Anthropic => u64::from(width)
                .checked_mul(u64::from(height))
                .and_then(|pixels| pixels.checked_add(749))
                .map(|pixels| pixels / 750)
                .unwrap_or(u64::MAX),
        }
    }
}

fn openai_image_tokens(width: u32, height: u32) -> u64 {
    let mut width = u64::from(width);
    let mut height = u64::from(height);
    let longest = width.max(height);
    if longest > 2048 {
        width = ceil_mul_div(width, 2048, longest).unwrap_or(u64::MAX);
        height = ceil_mul_div(height, 2048, longest).unwrap_or(u64::MAX);
    }
    let shortest = width.min(height);
    if shortest > 768 {
        width = ceil_mul_div(width, 768, shortest).unwrap_or(u64::MAX);
        height = ceil_mul_div(height, 768, shortest).unwrap_or(u64::MAX);
    }
    let columns = ceil_div(width, 512).unwrap_or(u64::MAX);
    let rows = ceil_div(height, 512).unwrap_or(u64::MAX);
    columns
        .checked_mul(rows)
        .and_then(|tiles| tiles.checked_mul(170))
        .and_then(|tokens| tokens.checked_add(85))
        .unwrap_or(u64::MAX)
}

fn ceil_mul_div(value: u64, multiplier: u64, divisor: u64) -> Option<u64> {
    value
        .checked_mul(multiplier)?
        .checked_add(divisor.checked_sub(1)?)?
        .checked_div(divisor)
}

fn ceil_div(value: u64, divisor: u64) -> Option<u64> {
    value
        .checked_add(divisor.checked_sub(1)?)?
        .checked_div(divisor)
}

/// 一条完整组装的工具调用请求。arguments 是模型给出的原始 JSON 文本；
/// JSON 合法性由 worker 在进入历史前校验（唯一一次，在 LLM 边界上）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolCallReq {
    pub call_id: String,
    pub name: String,
    pub arguments: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ContentPart {
    Text(String),
    Image(ImageInput),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserContent {
    parts: Vec<ContentPart>,
}

impl UserContent {
    pub fn new() -> Self {
        Self { parts: Vec::new() }
    }

    pub fn parts(&self) -> &[ContentPart] {
        &self.parts
    }

    #[cfg(test)]
    pub fn as_text(&self) -> Option<&str> {
        match self.parts.as_slice() {
            [ContentPart::Text(text)] => Some(text),
            _ => None,
        }
    }

    pub fn push_text(&mut self, text: impl Into<String>) {
        let text = text.into();
        if text.is_empty() {
            return;
        }
        match self.parts.last_mut() {
            Some(ContentPart::Text(existing)) => existing.push_str(&text),
            _ => self.parts.push(ContentPart::Text(text)),
        }
    }

    pub fn push_image(&mut self, image: ImageInput) {
        self.parts.push(ContentPart::Image(image));
    }

    pub fn extend(&mut self, other: &Self) {
        for part in &other.parts {
            match part {
                ContentPart::Text(text) => self.push_text(text.clone()),
                ContentPart::Image(image) => self.push_image(image.clone()),
            }
        }
    }

    pub fn has_images(&self) -> bool {
        self.parts
            .iter()
            .any(|part| matches!(part, ContentPart::Image(_)))
    }

    pub fn estimated_image_tokens(&self) -> u64 {
        self.parts
            .iter()
            .filter_map(|part| match part {
                ContentPart::Image(image) => Some(image.estimated_tokens),
                ContentPart::Text(_) => None,
            })
            .try_fold(0u64, u64::checked_add)
            .unwrap_or(u64::MAX)
    }

    pub fn byte_len(&self) -> usize {
        self.parts
            .iter()
            .map(|part| match part {
                ContentPart::Text(text) => text.len(),
                ContentPart::Image(image) => image.data.byte_len(),
            })
            .try_fold(0usize, usize::checked_add)
            .unwrap_or(usize::MAX)
    }

    pub fn audit_parts(&self) -> Vec<serde_json::Value> {
        self.parts
            .iter()
            .map(|part| match part {
                ContentPart::Text(text) => serde_json::json!({"type": "text", "text": text}),
                ContentPart::Image(image) => image.audit_value(),
            })
            .collect()
    }
}

impl Default for UserContent {
    fn default() -> Self {
        Self::new()
    }
}

impl From<String> for UserContent {
    fn from(text: String) -> Self {
        let mut content = Self::new();
        content.push_text(text);
        content
    }
}

impl From<&str> for UserContent {
    fn from(text: &str) -> Self {
        text.to_string().into()
    }
}

/// 内部对话历史：协议无关，三个协议各自翻译成自己的请求格式。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Message {
    User(UserContent),
    /// 经内部压缩工具验证的历史摘要；不是模型可调用的普通工具结果。
    Compaction(String),
    Assistant {
        text: String,
        tool_calls: Vec<ToolCallReq>,
    },
    /// Responses 返回的完整 output item，按供应商顺序原样保存。该项紧跟对应的
    /// Assistant；Responses 重放它，其他协议继续使用协议无关的 Assistant。
    ResponseOutputItems(Vec<serde_json::Value>),
    ToolResult {
        call_id: String,
        content: String,
    },
}

/// 工具规格：走请求的 tools 字段，不进提示词文字。实例的唯一来源是调度器。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub parameters: serde_json::Value,
}

/// 统一内部事件：worker 只消费它。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LlmEvent {
    /// 产出一个文本增量。
    TextDelta(String),
    /// 一条完整组装的工具调用（流式增量已拼好）。
    ToolCall(ToolCallReq),
    /// 供应商明确报告的用量；缺失字段保持 None，不用估算值冒充。
    Usage(ProviderUsage),
    /// 流正常收尾。
    Finished(Finish),
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ProviderUsage {
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub cache_read_tokens: Option<u64>,
    pub cache_creation_tokens: Option<u64>,
}

impl ProviderUsage {
    pub fn merge(&mut self, newer: Self) {
        if newer.input_tokens.is_some() {
            self.input_tokens = newer.input_tokens;
        }
        if newer.output_tokens.is_some() {
            self.output_tokens = newer.output_tokens;
        }
        if newer.cache_read_tokens.is_some() {
            self.cache_read_tokens = newer.cache_read_tokens;
        }
        if newer.cache_creation_tokens.is_some() {
            self.cache_creation_tokens = newer.cache_creation_tokens;
        }
    }

    pub fn is_empty(&self) -> bool {
        self.input_tokens.is_none()
            && self.output_tokens.is_none()
            && self.cache_read_tokens.is_none()
            && self.cache_creation_tokens.is_none()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Finish {
    Stop,
    MaxTokens,
    Refusal,
    /// 模型以工具调用结束本回合；transform 保证此时确有 ToolCall 事件发出。
    ToolUse,
}

/// 供应商服务状态的公开分类。它只保存调度和恢复真正需要的事实。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceCategory {
    Authentication,
    Authorization,
    Quota,
    Account,
    RateLimit,
    Server,
    Request,
}

impl fmt::Display for ServiceCategory {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Authentication => "鉴权失败",
            Self::Authorization => "权限不足",
            Self::Quota => "额度不可用",
            Self::Account => "账户不可用",
            Self::RateLimit => "请求频率受限",
            Self::Server => "供应商服务故障",
            Self::Request => "请求被供应商拒绝",
        })
    }
}

/// 全局停止接纳后续模型调用的原因。原始响应和供应商请求标识不进入该类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopReason {
    Authentication,
    Authorization,
    Quota,
    Account,
    RetryAfterTooLong,
    RetriesExhausted,
}

impl fmt::Display for StopReason {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Authentication => "鉴权失败",
            Self::Authorization => "权限不足",
            Self::Quota => "额度不可用",
            Self::Account => "账户不可用",
            Self::RetryAfterTooLong => "供应商要求的等待时间超过配置上限",
            Self::RetriesExhausted => "网络请求重试已经耗尽",
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransportCategory {
    Connect,
    Read,
    Timeout,
    Request,
}

impl fmt::Display for TransportCategory {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Connect => "连接失败",
            Self::Read => "读取响应失败",
            Self::Timeout => "请求超时",
            Self::Request => "发送请求失败",
        })
    }
}

/// 调用层错误：只保留控制流和公开诊断需要的类型化事实。
#[derive(Debug)]
pub enum LlmError {
    Transport {
        category: TransportCategory,
    },
    Http {
        status: u16,
        category: ServiceCategory,
        provider_code: Option<String>,
        retry_after: Option<Duration>,
    },
    ContextLimit {
        status: u16,
        provider_code: Option<String>,
    },
    Protocol {
        reason: String,
    },
    Timeout {
        phase: &'static str,
        timeout: Duration,
    },
    StreamLimit {
        limit: usize,
    },
    RequestBuild {
        reason: String,
    },
    AdmissionStopped {
        reason: StopReason,
    },
}

impl LlmError {
    pub(crate) fn protocol(reason: impl Into<String>, _payload: &str) -> Self {
        LlmError::Protocol {
            reason: reason.into(),
        }
    }

    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::Transport { .. }
                | Self::Timeout { .. }
                | Self::Http {
                    category: ServiceCategory::RateLimit | ServiceCategory::Server,
                    ..
                }
        )
    }

    pub fn retry_after(&self) -> Option<Duration> {
        match self {
            Self::Http { retry_after, .. } => *retry_after,
            _ => None,
        }
    }

    pub fn is_admission_stopped(&self) -> bool {
        matches!(self, Self::AdmissionStopped { .. })
    }
}

impl fmt::Display for LlmError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Transport { category } => write!(formatter, "LLM {category}"),
            Self::Http {
                status,
                category,
                provider_code,
                retry_after,
            } => {
                write!(formatter, "LLM 返回 HTTP {status}（{category}）")?;
                if let Some(code) = provider_code {
                    write!(formatter, "，provider code：{code}")?;
                }
                if let Some(delay) = retry_after {
                    write!(formatter, "，Retry-After：{} ms", delay.as_millis())?;
                }
                Ok(())
            }
            Self::ContextLimit {
                status,
                provider_code,
            } => {
                write!(formatter, "LLM 明确报告上下文超过限制（HTTP {status}")?;
                if let Some(code) = provider_code {
                    write!(formatter, "，provider code：{code}")?;
                }
                formatter.write_str("）")
            }
            Self::Protocol { reason } => write!(formatter, "LLM 协议响应无效：{reason}"),
            Self::Timeout { phase, timeout } => write!(formatter, "{phase}超过 {timeout:?}"),
            Self::StreamLimit { limit } => {
                write!(formatter, "LLM 流超过本地安全上限 {limit} 字节")
            }
            Self::RequestBuild { reason } => write!(formatter, "LLM 请求构造失败：{reason}"),
            Self::AdmissionStopped { reason } => {
                write!(formatter, "作业已因{reason}停止接纳后续模型调用")
            }
        }
    }
}

impl std::error::Error for LlmError {}

/// HTTP 错误响应体的内存解析上限；正文只用于提取类型化事实，绝不写入诊断。
const HTTP_ERROR_BODY_LIMIT: usize = 1024;
/// 单次供应商响应的本地硬边界，同时约束 SSE 缓冲和文本增量总量。
const MAX_STREAM_BYTES: usize = 64 * 1024 * 1024;

pub struct LlmClient {
    http: reqwest::Client,
    config: LlmConfig,
    gate: Arc<RequestGate>,
    requests_started: AtomicU64,
    calls_with_provider_usage: Arc<AtomicU64>,
}

/// 已按当前协议完整构造、但尚未发送的请求。worker 只保存协议无关的 `audit_body`；
/// 实际 HTTP body、URL 和 headers 保持私有，不进入档案。
#[derive(Clone)]
pub struct PreparedLlmCall {
    url: reqwest::Url,
    body: String,
    audit_body: String,
    headers: Vec<(String, String)>,
}

/// 单次调用是否已经越过共享门控并开始 HTTP 发送。worker 用它区分本地档案失败、
/// send 前取消和真正的供应商调用；该事实不会暴露供应商请求身份。
#[derive(Default)]
pub struct RequestObservation {
    started: AtomicBool,
}

impl RequestObservation {
    pub fn started(&self) -> bool {
        self.started.load(Ordering::Acquire)
    }

    fn mark_started(&self) {
        self.started.store(true, Ordering::Release);
    }
}

impl PreparedLlmCall {
    pub fn audit_body(&self) -> &str {
        &self.audit_body
    }
}

/// LLM 调用配置，由启动边界解析唯一 TOML，缺失必填项时明确失败。
#[derive(Clone)]
pub struct LlmConfig {
    pub protocol: Protocol,
    pub endpoint: reqwest::Url,
    pub model: String,
    pub api_key: Option<String>,
    /// 配置边界已校验的自定义请求头，统一用于全部模型协议与调用。
    pub headers: reqwest::header::HeaderMap,
    /// 模型声明的完整上下文窗口，用于调用前预算。
    pub context_window_tokens: u64,
    /// Anthropic Messages 协议要求的 max_tokens；其他协议必须为 None。
    pub anthropic_max_tokens: Option<u64>,
    pub input_modalities: InputModalities,
    /// 加入每次模型请求根对象的供应商扩展字段。
    pub extra_body: serde_json::Map<String, serde_json::Value>,
    pub connect_timeout: Duration,
    pub read_timeout: Duration,
    pub request_timeout: Duration,
    pub retry_delays: Vec<Duration>,
    pub max_retry_after: Duration,
    pub requests_per_minute: Option<u32>,
}

#[cfg(test)]
impl LlmConfig {
    pub(crate) fn test_defaults() -> Self {
        Self {
            protocol: Protocol::Completions,
            endpoint: "http://127.0.0.1/chat/completions".parse().unwrap(),
            model: "test-model".into(),
            api_key: None,
            headers: reqwest::header::HeaderMap::new(),
            context_window_tokens: 100_000,
            anthropic_max_tokens: None,
            input_modalities: InputModalities::Text,
            extra_body: serde_json::Map::new(),
            connect_timeout: Duration::from_secs(1),
            read_timeout: Duration::from_secs(1),
            request_timeout: Duration::from_secs(5),
            retry_delays: vec![Duration::from_millis(1)],
            max_retry_after: Duration::from_secs(1),
            requests_per_minute: None,
        }
    }
}

/// 配置边界已排除协议字段冲突，这里只完成请求组装。
pub(crate) fn add_extra_body(body: &mut serde_json::Value, config: &LlmConfig) {
    let object = body.as_object_mut().expect("模型请求根值必须是 JSON 对象");
    for (key, value) in &config.extra_body {
        assert!(
            !object.contains_key(key),
            "扩展请求字段必须在配置边界排除冲突"
        );
        object.insert(key.clone(), value.clone());
    }
}

#[derive(Debug)]
struct GateState {
    stopped: Option<StopReason>,
    not_before: tokio::time::Instant,
    next_rate_slot: tokio::time::Instant,
    classifications_pending: usize,
}

struct RequestGate {
    state: Mutex<GateState>,
    changed: tokio::sync::Notify,
    rate_interval: Option<Duration>,
}

impl RequestGate {
    fn new(requests_per_minute: Option<u32>) -> Self {
        let now = tokio::time::Instant::now();
        Self {
            state: Mutex::new(GateState {
                stopped: None,
                not_before: now,
                next_rate_slot: now,
                classifications_pending: 0,
            }),
            changed: tokio::sync::Notify::new(),
            rate_interval: requests_per_minute
                .map(|limit| Duration::from_secs_f64(60.0 / f64::from(limit))),
        }
    }

    async fn wait(&self) -> Result<(), StopReason> {
        loop {
            let notified = self.changed.notified();
            let deadline = {
                let mut state = self.state.lock().expect("请求门控互斥锁不能中毒");
                if let Some(reason) = state.stopped {
                    return Err(reason);
                }
                if state.classifications_pending > 0 {
                    None
                } else {
                    let now = tokio::time::Instant::now();
                    let deadline = state.not_before.max(state.next_rate_slot);
                    if deadline <= now {
                        if let Some(interval) = self.rate_interval {
                            state.next_rate_slot = now + interval;
                        }
                        return Ok(());
                    }
                    Some(deadline)
                }
            };
            match deadline {
                Some(deadline) => tokio::select! {
                    _ = tokio::time::sleep_until(deadline) => {}
                    _ = notified => {}
                },
                None => notified.await,
            }
        }
    }

    fn begin_classification(&self) -> ClassificationGuard<'_> {
        let mut state = self.state.lock().expect("请求门控互斥锁不能中毒");
        state.classifications_pending = state
            .classifications_pending
            .checked_add(1)
            .expect("活动 HTTP 分类数量不可能耗尽 usize");
        ClassificationGuard { gate: self }
    }

    fn defer(&self, delay: Duration) {
        let deadline = tokio::time::Instant::now() + delay;
        let mut state = self.state.lock().expect("请求门控互斥锁不能中毒");
        if deadline > state.not_before {
            state.not_before = deadline;
        }
        drop(state);
        self.changed.notify_waiters();
    }

    fn stop(&self, reason: StopReason) {
        let mut state = self.state.lock().expect("请求门控互斥锁不能中毒");
        if state
            .stopped
            .is_none_or(|current| stop_priority(reason) > stop_priority(current))
        {
            state.stopped = Some(reason);
        }
        drop(state);
        self.changed.notify_waiters();
    }

    fn stop_reason(&self) -> Option<StopReason> {
        self.state.lock().expect("请求门控互斥锁不能中毒").stopped
    }

    async fn stopped(&self) -> StopReason {
        loop {
            let notified = self.changed.notified();
            if let Some(reason) = self.stop_reason() {
                return reason;
            }
            notified.await;
        }
    }
}

/// 非成功响应从收到 status 到完成结构化分类之间暂停新请求。已经在途的初始窗口仍可
/// 收敛；普通瞬时错误分类完成后自动恢复，永久错误会先写入 stopped 再释放等待者。
struct ClassificationGuard<'a> {
    gate: &'a RequestGate,
}

impl Drop for ClassificationGuard<'_> {
    fn drop(&mut self) {
        let mut state = self.gate.state.lock().expect("请求门控互斥锁不能中毒");
        state.classifications_pending = state
            .classifications_pending
            .checked_sub(1)
            .expect("分类守卫必须与开始次数一一对应");
        drop(state);
        self.gate.changed.notify_waiters();
    }
}

fn stop_priority(reason: StopReason) -> u8 {
    match reason {
        StopReason::Authentication
        | StopReason::Authorization
        | StopReason::Quota
        | StopReason::Account => 2,
        StopReason::RetryAfterTooLong | StopReason::RetriesExhausted => 1,
    }
}

impl LlmClient {
    pub fn new(config: LlmConfig) -> Self {
        let gate = Arc::new(RequestGate::new(config.requests_per_minute));
        Self {
            http: reqwest::Client::builder()
                .no_proxy()
                .connect_timeout(config.connect_timeout)
                .timeout(config.request_timeout)
                .build()
                .expect("固定的 LLM HTTP client 配置必须有效"),
            config,
            gate,
            requests_started: AtomicU64::new(0),
            calls_with_provider_usage: Arc::new(AtomicU64::new(0)),
        }
    }

    pub fn retry_delay(&self, failed_attempt: usize) -> Option<Duration> {
        self.config
            .retry_delays
            .get(failed_attempt.saturating_sub(1))
            .copied()
    }

    pub fn stop_retries_exhausted(&self) {
        self.gate.stop(StopReason::RetriesExhausted);
    }

    pub fn stop_reason(&self) -> Option<StopReason> {
        self.gate.stop_reason()
    }

    pub fn retry_after_too_long(&self, delay: Option<Duration>) -> bool {
        delay.is_some_and(|delay| delay > self.config.max_retry_after)
    }

    pub async fn stopped(&self) -> StopReason {
        self.gate.stopped().await
    }

    pub fn requests_started(&self) -> u64 {
        self.requests_started.load(Ordering::Relaxed)
    }

    pub fn calls_with_provider_usage(&self) -> u64 {
        self.calls_with_provider_usage.load(Ordering::Relaxed)
    }

    pub fn prepare_call(
        &self,
        instructions: &str,
        history: &[Message],
        tools: &[ToolSpec],
    ) -> Result<PreparedLlmCall, LlmError> {
        if !self.config.input_modalities.supports_image()
            && history
                .iter()
                .any(|message| matches!(message, Message::User(content) if content.has_images()))
        {
            return Err(LlmError::RequestBuild {
                reason: "模型配置只声明 text 输入，不能发送图片".into(),
            });
        }
        let (url, body, headers) = self.build_request(instructions, history, tools)?;
        Ok(PreparedLlmCall {
            url,
            body,
            audit_body: build_audit_request(instructions, history, tools),
            headers,
        })
    }

    /// 发送一个已经留痕的流式调用。SSE 解析在调用方（worker）循环内驱动，
    /// 不另起任务。
    pub async fn send(
        &self,
        prepared: PreparedLlmCall,
        observation: &RequestObservation,
    ) -> Result<Call, LlmError> {
        self.gate
            .wait()
            .await
            .map_err(|reason| LlmError::AdmissionStopped { reason })?;
        observation.mark_started();
        self.requests_started.fetch_add(1, Ordering::Relaxed);
        // 在途计数覆盖「请求发送 → 响应头 → 流式正文」全程（规模观测）
        let in_flight = LlmInFlight::new();

        let mut req = self
            .http
            .post(prepared.url)
            .headers(self.config.headers.clone())
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(prepared.body);
        for (name, value) in prepared.headers {
            req = req.header(name, value);
        }
        let resp = req.send().await.map_err(transport_error)?;
        let status = resp.status();
        if !status.is_success() {
            // 除 401/403 外，最终类别可能只能从限长结构化正文确定。分类期间暂停新
            // 请求，避免另一槽先完成后越过仍未判定的永久额度/账户错误。
            let _classification =
                (!matches!(status.as_u16(), 401 | 403)).then(|| self.gate.begin_classification());
            let retry_after = parse_retry_after(resp.headers());
            // 401/403 单凭状态码就足以停止后续请求，不等待可能迟迟不结束的错误正文。
            // 429 的共享等待也先由 header 生效，正文随后只用于识别额度/账户短码。
            apply_service_gate(
                &self.gate,
                classify_service(status.as_u16(), None),
                retry_after,
                self.config.max_retry_after,
            );
            let snippet = if matches!(status.as_u16(), 401 | 403) {
                String::new()
            } else {
                read_error_body(resp, self.config.read_timeout).await
            };
            let provider_code = public_provider_code(&snippet);
            if is_structured_context_limit(&snippet) {
                return Err(LlmError::ContextLimit {
                    status: status.as_u16(),
                    provider_code,
                });
            }
            let category = classify_service(status.as_u16(), provider_code.as_deref());
            apply_service_gate(
                &self.gate,
                category,
                retry_after,
                self.config.max_retry_after,
            );
            return Err(LlmError::Http {
                status: status.as_u16(),
                category,
                provider_code,
                retry_after,
            });
        }
        let transform: Box<dyn Transform + Send> = match self.config.protocol {
            Protocol::Completions => Box::new(completions::Transform::new()),
            Protocol::Responses => Box::new(responses::Transform::new()),
            Protocol::Anthropic => Box::new(anthropic::Transform::new()),
        };
        Ok(Call {
            stream: EventStream::new(
                Box::pin(resp.bytes_stream()),
                transform,
                self.config.read_timeout,
            ),
            calls_with_provider_usage: Arc::clone(&self.calls_with_provider_usage),
            provider_usage_observed: false,
            _in_flight: in_flight,
        })
    }

    pub fn build_request(
        &self,
        instructions: &str,
        history: &[Message],
        tools: &[ToolSpec],
    ) -> BuildRequestResult {
        match self.config.protocol {
            Protocol::Completions => {
                completions::build_request(&self.config, instructions, history, tools)
            }
            Protocol::Responses => {
                responses::build_request(&self.config, instructions, history, tools)
            }
            Protocol::Anthropic => {
                anthropic::build_request(&self.config, instructions, history, tools)
            }
        }
    }

    pub fn estimate_request_tokens(
        &self,
        instructions: &str,
        history: &[Message],
        tools: &[ToolSpec],
    ) -> u64 {
        let estimate_body = match self.config.protocol {
            Protocol::Completions => {
                completions::build_estimate_body(&self.config, instructions, history, tools)
            }
            Protocol::Responses => {
                responses::build_estimate_body(&self.config, instructions, history, tools)
            }
            Protocol::Anthropic => {
                anthropic::build_estimate_body(&self.config, instructions, history, tools)
            }
        };
        let Ok(estimate_body) = estimate_body else {
            return u64::MAX;
        };
        let text = crate::tokenize::count(&estimate_body);
        history
            .iter()
            .filter_map(|message| match message {
                Message::User(content) => Some(content.estimated_image_tokens()),
                _ => None,
            })
            .try_fold(text, u64::checked_add)
            .unwrap_or(u64::MAX)
    }

    pub fn input_budget(&self, safety_tokens: u64) -> u64 {
        self.config
            .context_window_tokens
            .saturating_sub(self.config.anthropic_max_tokens.unwrap_or(0))
            .saturating_sub(safety_tokens)
    }

    pub const fn input_modalities(&self) -> InputModalities {
        self.config.input_modalities
    }

    pub const fn protocol(&self) -> Protocol {
        self.config.protocol
    }
}

/// 构造允许进入 worker 档案的模型输入语义。Responses 的 replay item 可能包含
/// opaque 或 encrypted payload，只记录数量；实际值只留在当前 HTTP 请求内。
fn build_audit_request(instructions: &str, history: &[Message], tools: &[ToolSpec]) -> String {
    let history: Vec<serde_json::Value> = history
        .iter()
        .map(|message| match message {
            Message::User(content) => serde_json::json!({
                "role": "user",
                "content": content.audit_parts(),
            }),
            Message::Compaction(text) => {
                serde_json::json!({"role": "compaction", "text": text})
            }
            Message::Assistant { text, tool_calls } => serde_json::json!({
                "role": "assistant",
                "text": text,
                "tool_calls": tool_calls.iter().map(|call| serde_json::json!({
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                })).collect::<Vec<_>>(),
            }),
            Message::ResponseOutputItems(items) => serde_json::json!({
                "role": "provider_replay",
                "item_count": items.len(),
                "payload": "omitted",
            }),
            Message::ToolResult { call_id, content } => serde_json::json!({
                "role": "tool_result",
                "call_id": call_id,
                "content": content,
            }),
        })
        .collect();
    let tools: Vec<serde_json::Value> = tools
        .iter()
        .map(|tool| {
            serde_json::json!({
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            })
        })
        .collect();
    serde_json::json!({
        "instructions": instructions,
        "history": history,
        "tools": tools,
    })
    .to_string()
}

async fn read_error_body(resp: reqwest::Response, read_timeout: Duration) -> String {
    let mut bytes = resp.bytes_stream();
    let mut body = Vec::new();
    let mut idle_deadline = tokio::time::Instant::now() + read_timeout;
    while body.len() < HTTP_ERROR_BODY_LIMIT {
        let Ok(next) = tokio::time::timeout_at(idle_deadline, bytes.next()).await else {
            break;
        };
        match next {
            Some(Ok(chunk)) if chunk.is_empty() => continue,
            Some(Ok(chunk)) => {
                idle_deadline = tokio::time::Instant::now() + read_timeout;
                let remaining = HTTP_ERROR_BODY_LIMIT - body.len();
                body.extend_from_slice(&chunk[..chunk.len().min(remaining)]);
            }
            Some(Err(_)) => break,
            None => break,
        }
    }
    String::from_utf8_lossy(&body).into_owned()
}

fn apply_service_gate(
    gate: &RequestGate,
    category: ServiceCategory,
    retry_after: Option<Duration>,
    max_retry_after: Duration,
) {
    match category {
        ServiceCategory::Authentication => {
            gate.stop(StopReason::Authentication);
            return;
        }
        ServiceCategory::Authorization => {
            gate.stop(StopReason::Authorization);
            return;
        }
        ServiceCategory::Quota => {
            gate.stop(StopReason::Quota);
            return;
        }
        ServiceCategory::Account => {
            gate.stop(StopReason::Account);
            return;
        }
        ServiceCategory::RateLimit | ServiceCategory::Server | ServiceCategory::Request => {}
    }
    if retry_after.is_some_and(|delay| delay > max_retry_after) {
        gate.stop(StopReason::RetryAfterTooLong);
        return;
    }
    match category {
        ServiceCategory::RateLimit => {
            if let Some(delay) = retry_after {
                gate.defer(delay);
            }
        }
        ServiceCategory::Server | ServiceCategory::Request => {}
        ServiceCategory::Authentication
        | ServiceCategory::Authorization
        | ServiceCategory::Quota
        | ServiceCategory::Account => unreachable!("永久类别已在上方返回"),
    }
}

fn transport_error(error: reqwest::Error) -> LlmError {
    let category = if error.is_timeout() {
        TransportCategory::Timeout
    } else if error.is_connect() {
        TransportCategory::Connect
    } else if error.is_body() || error.is_decode() {
        TransportCategory::Read
    } else {
        TransportCategory::Request
    };
    LlmError::Transport { category }
}

const PUBLIC_PROVIDER_CODES: &[&str] = &[
    "account_deactivated",
    "account_suspended",
    "authentication_error",
    "billing_hard_limit_reached",
    "billing_not_active",
    "context_length_exceeded",
    "context_window_exceeded",
    "credit_balance_too_low",
    "insufficient_quota",
    "invalid_api_key",
    "permission_error",
    "prompt_too_long",
    "quota_exceeded",
    "rate_limit_error",
    "rate_limit_exceeded",
    "request_too_large",
    "unauthorized",
];

fn provider_codes(body: &str) -> Vec<String> {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(body) else {
        return Vec::new();
    };
    [
        value
            .pointer("/error/code")
            .and_then(serde_json::Value::as_str),
        value
            .pointer("/error/type")
            .and_then(serde_json::Value::as_str),
        value.get("code").and_then(serde_json::Value::as_str),
        value.get("type").and_then(serde_json::Value::as_str),
    ]
    .into_iter()
    .flatten()
    .map(str::to_owned)
    .collect()
}

fn public_provider_code(body: &str) -> Option<String> {
    provider_codes(body)
        .into_iter()
        .find(|code| PUBLIC_PROVIDER_CODES.contains(&code.as_str()))
}

fn classify_service(status: u16, provider_code: Option<&str>) -> ServiceCategory {
    match provider_code {
        Some(
            "insufficient_quota"
            | "quota_exceeded"
            | "billing_hard_limit_reached"
            | "credit_balance_too_low",
        ) => {
            return ServiceCategory::Quota;
        }
        Some("billing_not_active" | "account_deactivated" | "account_suspended") => {
            return ServiceCategory::Account;
        }
        Some("invalid_api_key" | "authentication_error" | "unauthorized") => {
            return ServiceCategory::Authentication;
        }
        Some("permission_error") => return ServiceCategory::Authorization,
        _ => {}
    }
    match status {
        401 => ServiceCategory::Authentication,
        403 => ServiceCategory::Authorization,
        429 => ServiceCategory::RateLimit,
        500..=599 => ServiceCategory::Server,
        _ => ServiceCategory::Request,
    }
}

fn parse_retry_after(headers: &reqwest::header::HeaderMap) -> Option<Duration> {
    let value = headers
        .get(reqwest::header::RETRY_AFTER)?
        .to_str()
        .ok()?
        .trim();
    if let Ok(seconds) = value.parse::<u64>() {
        return Some(Duration::from_secs(seconds));
    }
    let deadline = chrono::DateTime::parse_from_rfc2822(value)
        .ok()?
        .with_timezone(&chrono::Utc);
    let now = chrono::Utc::now();
    if deadline <= now {
        Some(Duration::ZERO)
    } else {
        (deadline - now).to_std().ok()
    }
}

fn is_structured_context_limit(body: &str) -> bool {
    let known = [
        "context_length_exceeded",
        "context_window_exceeded",
        "prompt_too_long",
        "request_too_large",
    ];
    provider_codes(body)
        .iter()
        .any(|code| known.contains(&code.as_str()))
}

/// LLM 在途调用的计数守卫：创建即 +1，销毁即 -1（规模观测）。
struct LlmInFlight;

impl LlmInFlight {
    fn new() -> Self {
        crate::metrics::gauge_add(&crate::metrics::LLM_IN_FLIGHT, 1);
        Self
    }
}

impl Drop for LlmInFlight {
    fn drop(&mut self) {
        crate::metrics::gauge_add(&crate::metrics::LLM_IN_FLIGHT, -1);
    }
}

/// 一次进行中的调用。供应商 payload 只在 EventStream 内完成协议转换，不对外暴露。
/// 在途计数随请求开始与调用结束更新（规模观测）。
pub struct Call {
    stream: EventStream,
    calls_with_provider_usage: Arc<AtomicU64>,
    provider_usage_observed: bool,
    _in_flight: LlmInFlight,
}

impl Call {
    /// 取下一个内部事件；流结束返回 Ok(None)。
    pub async fn next_event(&mut self) -> Result<Option<LlmEvent>, LlmError> {
        let event = self.stream.next_event().await?;
        if !self.provider_usage_observed
            && matches!(&event, Some(LlmEvent::Usage(usage)) if !usage.is_empty())
        {
            self.provider_usage_observed = true;
            self.calls_with_provider_usage
                .fetch_add(1, Ordering::Relaxed);
        }
        Ok(event)
    }

    /// Responses 本回合已经完成的全部 output item。只有收到供应商完成事件并接受
    /// 本回合后，worker 才把它们写入历史。
    pub fn response_output_items(&self) -> &[serde_json::Value] {
        self.stream.transform.response_output_items()
    }
}

/// 协议 transform：把 SSE data 负载翻译成 0..n 个内部事件。
/// 有状态——流式工具调用的增量在同一调用的连续帧中组装。
pub(crate) trait Transform {
    fn push(&mut self, payload: &str) -> Result<Vec<LlmEvent>, LlmError>;

    fn response_output_items(&self) -> &[serde_json::Value] {
        &[]
    }
}

type ByteStream = Pin<Box<dyn Stream<Item = Result<Bytes, reqwest::Error>> + Send>>;

/// SSE 字节缓冲。`scan_from` 记录上次已经确认不可能出现分隔符的位置，避免一个
/// 大帧被拆成许多小网络块时反复扫描已有字节。
#[derive(Default)]
pub(crate) struct SseBuffer {
    bytes: BytesMut,
    scan_from: usize,
}

impl SseBuffer {
    fn extend_from_slice(&mut self, bytes: &[u8]) {
        self.bytes.extend_from_slice(bytes);
    }

    fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }

    #[cfg(test)]
    fn remaining(&self) -> &[u8] {
        &self.bytes
    }
}

struct EventStream {
    bytes: ByteStream,
    transform: Box<dyn Transform + Send>,
    pending: VecDeque<LlmEvent>,
    buffer: SseBuffer,
    received_bytes: usize,
    max_stream_bytes: usize,
    idle_timeout: Duration,
    eof: bool,
    finished: bool,
}

impl EventStream {
    fn new(
        bytes: ByteStream,
        transform: Box<dyn Transform + Send>,
        read_timeout: Duration,
    ) -> Self {
        Self::with_policy(bytes, transform, MAX_STREAM_BYTES, read_timeout)
    }

    fn with_policy(
        bytes: ByteStream,
        transform: Box<dyn Transform + Send>,
        max_stream_bytes: usize,
        idle_timeout: Duration,
    ) -> Self {
        Self {
            bytes,
            transform,
            pending: VecDeque::new(),
            buffer: SseBuffer::default(),
            received_bytes: 0,
            max_stream_bytes,
            idle_timeout,
            eof: false,
            finished: false,
        }
    }

    fn accept_event(&mut self, event: LlmEvent) -> Result<LlmEvent, LlmError> {
        if self.finished && !matches!(event, LlmEvent::Usage(_)) {
            return Err(LlmError::protocol("首个完成事件之后又收到协议事件", ""));
        }
        if matches!(event, LlmEvent::Finished(_)) {
            self.finished = true;
        }
        Ok(event)
    }

    async fn next_event(&mut self) -> Result<Option<LlmEvent>, LlmError> {
        let mut idle_deadline = tokio::time::Instant::now() + self.idle_timeout;
        loop {
            if let Some(event) = self.pending.pop_front() {
                return self.accept_event(event).map(Some);
            }
            while let Some(frame) = take_frame(&mut self.buffer) {
                let payloads = match frame_payloads(&frame) {
                    Ok(payloads) => payloads,
                    Err(_) => return Err(LlmError::protocol("完整 SSE 帧不是合法 UTF-8", "")),
                };
                for payload in payloads {
                    self.pending.extend(self.transform.push(&payload)?);
                }
                if let Some(event) = self.pending.pop_front() {
                    return self.accept_event(event).map(Some);
                }
            }
            if self.eof {
                return Ok(None);
            }
            let next = tokio::time::timeout_at(idle_deadline, self.bytes.next())
                .await
                .map_err(|_| LlmError::Timeout {
                    phase: "等待 LLM 流数据",
                    timeout: self.idle_timeout,
                })?;
            match next {
                Some(Ok(chunk)) if chunk.is_empty() => continue,
                Some(Ok(chunk)) => {
                    idle_deadline = tokio::time::Instant::now() + self.idle_timeout;
                    let remaining = self.max_stream_bytes.saturating_sub(self.received_bytes);
                    if chunk.len() > remaining {
                        return Err(LlmError::StreamLimit {
                            limit: self.max_stream_bytes,
                        });
                    }
                    self.received_bytes += chunk.len();
                    self.buffer.extend_from_slice(&chunk);
                }
                Some(Err(e)) => return Err(transport_error(e)),
                None => {
                    self.eof = true;
                    // 流末尾可能有没有空行收尾的残帧，补一个分帧符再收一遍。
                    if !self.buffer.is_empty() {
                        self.buffer.extend_from_slice(b"\n\n");
                    }
                }
            }
        }
    }
}

/// 从字节缓冲取出下一个完整 SSE 帧（空行分隔，兼容 LF 与 CRLF）。已消费的
/// 前缀通过 `BytesMut::split_to` 分离，不随剩余流长度搬移；每个输入字节只会被
/// 扫描常数次。
pub(crate) fn take_frame(buffer: &mut SseBuffer) -> Option<BytesMut> {
    let bytes = &buffer.bytes;
    let mut index = buffer.scan_from.min(bytes.len());
    let separator = loop {
        if bytes.get(index..index.saturating_add(2)) == Some(b"\n\n") {
            break Some((index, 2));
        }
        if bytes.get(index..index.saturating_add(4)) == Some(b"\r\n\r\n") {
            break Some((index, 4));
        }
        if index == bytes.len() {
            break None;
        }
        index += 1;
    };

    let Some((index, separator_len)) = separator else {
        // 最长分隔符有四字节。只保留末尾三个候选起点供下一批字节到达后复查。
        buffer.scan_from = buffer.bytes.len().saturating_sub(3);
        return None;
    };

    let mut frame = buffer.bytes.split_to(index + separator_len);
    frame.truncate(index);
    buffer.scan_from = 0;
    Some(frame)
}

/// 从一帧中提取 data 负载：多行 data 以 \n 拼接；忽略 event:/注释等其他行。
pub(crate) fn frame_payloads(frame: &[u8]) -> Result<Vec<String>, std::str::Utf8Error> {
    let text = std::str::from_utf8(frame)?;
    let mut data_lines: Vec<&str> = Vec::new();
    let mut out = Vec::new();
    for line in text.split('\n') {
        let line = line.strip_suffix('\r').unwrap_or(line);
        if line.is_empty() {
            continue;
        }
        if let Some(data) = line.strip_prefix("data:") {
            data_lines.push(data.strip_prefix(' ').unwrap_or(data));
        }
    }
    if !data_lines.is_empty() {
        out.push(data_lines.join("\n"));
    }
    Ok(out)
}

#[cfg(test)]
pub(crate) fn events_of(mut t: Box<dyn Transform + Send>, sample: &str) -> Vec<LlmEvent> {
    let mut buffer = SseBuffer::default();
    buffer.extend_from_slice(sample.as_bytes());
    let mut events = Vec::new();
    while let Some(frame) = take_frame(&mut buffer) {
        for payload in frame_payloads(&frame).expect("测试样本必须是合法 UTF-8") {
            events.extend(t.push(&payload).unwrap());
        }
    }
    events
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retry_after_accepts_seconds_and_http_date() {
        let mut seconds = reqwest::header::HeaderMap::new();
        seconds.insert(reqwest::header::RETRY_AFTER, "12".parse().unwrap());
        assert_eq!(parse_retry_after(&seconds), Some(Duration::from_secs(12)));

        let deadline = chrono::Utc::now() + chrono::Duration::seconds(60);
        let value = deadline.format("%a, %d %b %Y %H:%M:%S GMT").to_string();
        let mut date = reqwest::header::HeaderMap::new();
        date.insert(reqwest::header::RETRY_AFTER, value.parse().unwrap());
        let parsed = parse_retry_after(&date).unwrap();
        assert!(
            (Duration::from_secs(58)..=Duration::from_secs(60)).contains(&parsed),
            "HTTP-date 应换算成剩余等待时间：{parsed:?}"
        );
    }

    #[test]
    fn only_allowlisted_provider_code_survives_http_body_classification() {
        const SECRET: &str = "NEVER-REPORT-THIS-MESSAGE";
        let body = format!(r#"{{"error":{{"code":"insufficient_quota","message":"{SECRET}"}}}}"#);
        let code = public_provider_code(&body);
        assert_eq!(code.as_deref(), Some("insufficient_quota"));
        let error = LlmError::Http {
            status: 429,
            category: classify_service(429, code.as_deref()),
            provider_code: code,
            retry_after: None,
        };
        assert!(!error.to_string().contains(SECRET));

        let unknown = r#"{"error":{"code":"PRIVATE-CODE","message":"PRIVATE-BODY"}}"#;
        assert_eq!(public_provider_code(unknown), None);
    }

    #[test]
    fn oversized_retry_after_stops_every_service_category() {
        for category in [ServiceCategory::RateLimit, ServiceCategory::Server] {
            let gate = RequestGate::new(None);
            apply_service_gate(
                &gate,
                category,
                Some(Duration::from_secs(2)),
                Duration::from_secs(1),
            );
            assert_eq!(gate.stop_reason(), Some(StopReason::RetryAfterTooLong));
        }
    }

    #[test]
    fn explicit_quota_overrides_preliminary_long_retry_after() {
        let gate = RequestGate::new(None);
        apply_service_gate(
            &gate,
            ServiceCategory::RateLimit,
            Some(Duration::from_secs(600)),
            Duration::from_secs(1),
        );
        assert_eq!(gate.stop_reason(), Some(StopReason::RetryAfterTooLong));
        apply_service_gate(
            &gate,
            ServiceCategory::Quota,
            Some(Duration::from_secs(600)),
            Duration::from_secs(1),
        );
        assert_eq!(gate.stop_reason(), Some(StopReason::Quota));
    }

    #[test]
    fn frames_split_across_chunks() {
        let mut buffer = SseBuffer::default();
        buffer.extend_from_slice("data: 你".as_bytes());
        assert!(take_frame(&mut buffer).is_none(), "半个帧不产生输出");
        buffer.extend_from_slice("好\n\n".as_bytes());
        assert_eq!(take_frame(&mut buffer).unwrap(), "data: 你好".as_bytes());
    }

    #[test]
    fn crlf_frames_split() {
        let mut buffer = SseBuffer::default();
        buffer.extend_from_slice(b"data: a\r\n\r\ndata: b\r\n\r\n");
        assert_eq!(take_frame(&mut buffer).unwrap().as_ref(), b"data: a");
        assert_eq!(take_frame(&mut buffer).unwrap().as_ref(), b"data: b");
        assert!(take_frame(&mut buffer).is_none());
    }

    #[test]
    fn many_small_frames_are_consumed_in_order_and_leave_residual_unchanged() {
        const FRAME_COUNT: usize = 25_000;
        const RESIDUAL: &[u8] = b"data: unfinished";

        let mut source = Vec::with_capacity(FRAME_COUNT * 10 + RESIDUAL.len());
        for index in 0..FRAME_COUNT {
            source.extend_from_slice(format!("data: {index}\n\n").as_bytes());
        }
        source.extend_from_slice(RESIDUAL);

        let mut buffer = SseBuffer::default();
        buffer.extend_from_slice(&source);
        for index in 0..FRAME_COUNT {
            let frame = take_frame(&mut buffer).expect("每个完整帧都应被取出");
            assert_eq!(frame, format!("data: {index}").as_bytes());
        }
        assert!(take_frame(&mut buffer).is_none());
        assert_eq!(buffer.remaining(), RESIDUAL);
    }

    #[test]
    fn long_frame_split_into_single_bytes_is_scanned_incrementally() {
        const PAYLOAD_LEN: usize = 32 * 1024;

        let mut expected = b"data: ".to_vec();
        expected.extend(std::iter::repeat_n(b'x', PAYLOAD_LEN));
        let mut buffer = SseBuffer::default();
        for byte in &expected {
            buffer.extend_from_slice(std::slice::from_ref(byte));
            assert!(take_frame(&mut buffer).is_none());
        }
        buffer.extend_from_slice(b"\r");
        assert!(take_frame(&mut buffer).is_none());
        buffer.extend_from_slice(b"\n");
        assert!(take_frame(&mut buffer).is_none());
        buffer.extend_from_slice(b"\r");
        assert!(take_frame(&mut buffer).is_none());
        buffer.extend_from_slice(b"\n");

        assert_eq!(take_frame(&mut buffer).unwrap(), expected);
        assert!(buffer.is_empty());
    }

    #[test]
    fn multiline_data_joined_and_comments_ignored() {
        let frame = ": comment\nevent: message\ndata: 第一\ndata: 第二\n".as_bytes();
        assert_eq!(
            frame_payloads(frame).unwrap(),
            vec!["第一\n第二".to_string()]
        );
    }

    #[test]
    fn non_data_frame_yields_nothing() {
        assert!(
            frame_payloads(b"event: message_start\n")
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn input_budget_only_reserves_anthropic_required_output() {
        let common = LlmConfig {
            protocol: Protocol::Responses,
            endpoint: "http://localhost/responses".parse().unwrap(),
            model: "m".into(),
            api_key: None,
            context_window_tokens: 100_000,
            anthropic_max_tokens: None,
            ..LlmConfig::test_defaults()
        };
        assert_eq!(LlmClient::new(common.clone()).input_budget(2_000), 98_000);

        let anthropic = LlmConfig {
            protocol: Protocol::Anthropic,
            anthropic_max_tokens: Some(8_000),
            ..common
        };
        assert_eq!(LlmClient::new(anthropic).input_budget(2_000), 90_000);
    }

    #[test]
    fn extra_body_is_added_to_every_protocol_request() {
        for protocol in [
            Protocol::Completions,
            Protocol::Responses,
            Protocol::Anthropic,
        ] {
            let mut config = LlmConfig::test_defaults();
            config.protocol = protocol;
            config.anthropic_max_tokens = (protocol == Protocol::Anthropic).then_some(16_384);
            config.extra_body = serde_json::from_value(serde_json::json!({
                "temperature": 0.2,
                "reasoning": {"effort": "high"},
                "nullable": null,
            }))
            .unwrap();
            let client = LlmClient::new(config);
            let (_, body, _) = client.build_request("说明", &[], &[]).unwrap();
            let body: serde_json::Value = serde_json::from_str(&body).unwrap();
            assert_eq!(body["temperature"], 0.2, "{protocol:?}");
            assert_eq!(body["reasoning"]["effort"], "high", "{protocol:?}");
            assert!(body["nullable"].is_null(), "{protocol:?}");
        }
    }

    #[test]
    fn managed_field_lists_match_protocol_requests() {
        let tools = [ToolSpec {
            name: "search".into(),
            description: "搜索".into(),
            parameters: serde_json::json!({"type": "object"}),
        }];
        for protocol in [
            Protocol::Completions,
            Protocol::Responses,
            Protocol::Anthropic,
        ] {
            let mut config = LlmConfig::test_defaults();
            config.protocol = protocol;
            config.anthropic_max_tokens = (protocol == Protocol::Anthropic).then_some(16_384);
            let client = LlmClient::new(config);
            let (_, body, _) = client.build_request("说明", &[], &tools).unwrap();
            let body: serde_json::Value = serde_json::from_str(&body).unwrap();
            let actual: std::collections::BTreeSet<_> = body
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect();
            let managed = protocol.managed_request_fields().iter().copied().collect();
            assert_eq!(actual, managed, "{protocol:?}");
        }
    }

    #[test]
    fn image_requests_keep_base64_out_of_audit_and_text_bpe_budget() {
        for protocol in [
            Protocol::Completions,
            Protocol::Responses,
            Protocol::Anthropic,
        ] {
            let mut config = LlmConfig::test_defaults();
            config.protocol = protocol;
            config.input_modalities = InputModalities::TextAndImage;
            config.anthropic_max_tokens = (protocol == Protocol::Anthropic).then_some(1024);
            let client = LlmClient::new(config);
            let mut content = UserContent::from("看图");
            let image = crate::image_input::test_image_input(
                crate::image_input::ImageSource::Input("sample.png".into()),
            );
            let encoded = image.data.base64().unwrap();
            content.push_image(image);
            let history = [Message::User(content)];
            let prepared = client.prepare_call("说明", &history, &[]).unwrap();
            assert!(prepared.body.contains(&encoded), "{protocol:?}");
            assert!(!prepared.audit_body().contains("base64"), "{protocol:?}");
            assert!(
                !prepared.audit_body().contains("data:image"),
                "{protocol:?}"
            );
            assert!(prepared.audit_body().contains("sample.png"), "{protocol:?}");
            let estimate_body = match protocol {
                Protocol::Completions => {
                    completions::build_estimate_body(&client.config, "说明", &history, &[])
                }
                Protocol::Responses => {
                    responses::build_estimate_body(&client.config, "说明", &history, &[])
                }
                Protocol::Anthropic => {
                    anthropic::build_estimate_body(&client.config, "说明", &history, &[])
                }
            }
            .unwrap();
            assert!(!estimate_body.contains(&encoded), "{protocol:?}");
            assert_eq!(
                client.estimate_request_tokens("说明", &history, &[]),
                crate::tokenize::count(&estimate_body) + 255,
                "{protocol:?}"
            );
        }
    }

    #[test]
    fn token_estimate_keeps_extra_body_and_responses_replay_payloads() {
        for protocol in [
            Protocol::Completions,
            Protocol::Responses,
            Protocol::Anthropic,
        ] {
            let mut base = LlmConfig::test_defaults();
            base.protocol = protocol;
            base.anthropic_max_tokens = (protocol == Protocol::Anthropic).then_some(1024);
            let baseline = LlmClient::new(base.clone()).estimate_request_tokens("说明", &[], &[]);
            base.extra_body.insert(
                "provider_context".into(),
                serde_json::json!("扩展载荷 ".repeat(512)),
            );
            let expanded = LlmClient::new(base).estimate_request_tokens("说明", &[], &[]);
            assert!(expanded > baseline + 100, "{protocol:?}");
        }

        let mut config = LlmConfig::test_defaults();
        config.protocol = Protocol::Responses;
        let client = LlmClient::new(config);
        let baseline = client.estimate_request_tokens("说明", &[], &[]);
        let history = [Message::ResponseOutputItems(vec![serde_json::json!({
            "type": "reasoning",
            "encrypted_content": "opaque replay payload ".repeat(512),
        })])];
        assert!(client.estimate_request_tokens("说明", &history, &[]) > baseline + 100);
    }

    #[test]
    fn text_only_clients_reject_images_before_request_construction() {
        let client = LlmClient::new(LlmConfig::test_defaults());
        let mut content = UserContent::from("看图");
        content.push_image(crate::image_input::test_image_input(
            crate::image_input::ImageSource::Input("sample.png".into()),
        ));
        let error = client
            .prepare_call("说明", &[Message::User(content)], &[])
            .err()
            .unwrap();
        assert!(matches!(error, LlmError::RequestBuild { .. }));
        assert_eq!(client.requests_started(), 0);
    }

    #[test]
    fn visual_token_estimates_are_dimension_based_and_checked() {
        assert_eq!(Protocol::Anthropic.estimate_image_tokens(1000, 750), 1000);
        assert_eq!(Protocol::Completions.estimate_image_tokens(512, 512), 255);
        assert!(Protocol::Responses.estimate_image_tokens(4096, 2048) > 255);
    }

    #[tokio::test]
    async fn stream_rejects_received_bytes_over_local_limit() {
        const CHUNK: &[u8] = b"data: 12345\n\n";
        let bytes =
            futures_util::stream::iter(vec![Ok::<_, reqwest::Error>(Bytes::from_static(CHUNK))]);
        let mut stream = EventStream::with_policy(
            Box::pin(bytes),
            Box::new(completions::Transform::new()),
            4,
            Duration::from_secs(1),
        );

        let error = stream.next_event().await.unwrap_err();
        assert!(matches!(error, LlmError::StreamLimit { limit: 4 }));
    }

    #[tokio::test]
    async fn stream_idle_timeout_is_per_read() {
        let bytes = futures_util::stream::pending::<Result<Bytes, reqwest::Error>>();
        let mut stream = EventStream::with_policy(
            Box::pin(bytes),
            Box::new(completions::Transform::new()),
            1024,
            Duration::from_millis(1),
        );

        let error = stream.next_event().await.unwrap_err();
        assert!(matches!(
            error,
            LlmError::Timeout {
                phase: "等待 LLM 流数据",
                timeout
            } if timeout == Duration::from_millis(1)
        ));
    }

    #[tokio::test]
    async fn usage_is_allowed_after_finish_but_a_second_terminal_event_is_not() {
        let bytes = futures_util::stream::iter(vec![Ok::<_, reqwest::Error>(
            Bytes::from_static(concat!(
                "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                "data: {\"usage\":{\"prompt_tokens\":1,\"completion_tokens\":1},\"choices\":[]}\n\n",
                "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n"
            ).as_bytes()),
        )]);
        let mut stream = EventStream::with_policy(
            Box::pin(bytes),
            Box::new(completions::Transform::new()),
            1024,
            Duration::from_secs(1),
        );

        assert_eq!(
            stream.next_event().await.unwrap(),
            Some(LlmEvent::Finished(Finish::Stop))
        );
        assert_eq!(
            stream.next_event().await.unwrap(),
            Some(LlmEvent::Usage(ProviderUsage {
                input_tokens: Some(1),
                output_tokens: Some(1),
                cache_read_tokens: None,
                cache_creation_tokens: None,
            }))
        );
        assert!(matches!(
            stream.next_event().await,
            Err(LlmError::Protocol { reason, .. })
                if reason.contains("首个完成事件之后")
        ));
    }

    #[tokio::test]
    async fn incomplete_sse_frame_is_rejected_without_exposing_its_bytes() {
        const RESIDUAL: &str = "data: {\"choices\":[{\"delta\":{\"content\":\"半帧";
        let body = format!(
            "data: {{\"choices\":[{{\"delta\":{{\"content\":\"完整\"}},\"finish_reason\":null}}]}}\n\n{RESIDUAL}"
        );
        let bytes = futures_util::stream::iter(vec![Ok::<_, reqwest::Error>(Bytes::from(body))]);
        let mut stream = EventStream::with_policy(
            Box::pin(bytes),
            Box::new(completions::Transform::new()),
            4096,
            Duration::from_secs(1),
        );

        assert_eq!(
            stream.next_event().await.unwrap(),
            Some(LlmEvent::TextDelta("完整".into()))
        );
        assert!(matches!(
            stream.next_event().await,
            Err(LlmError::Protocol { reason, .. }) if reason.contains("JSON 解析失败")
        ));
        assert!(stream.buffer.remaining().is_empty());
    }

    #[tokio::test]
    async fn invalid_utf8_frame_is_rejected_without_entering_transform() {
        use std::sync::Arc;
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct CountingTransform(Arc<AtomicUsize>);

        impl Transform for CountingTransform {
            fn push(&mut self, _payload: &str) -> Result<Vec<LlmEvent>, LlmError> {
                self.0.fetch_add(1, Ordering::Relaxed);
                Ok(Vec::new())
            }
        }

        const INVALID_FRAME: &[u8] = b"data: invalid-\xff";
        let mut body = b"data: valid\n\n".to_vec();
        body.extend_from_slice(INVALID_FRAME);
        body.extend_from_slice(b"\n\n");
        let bytes = futures_util::stream::iter(vec![Ok::<_, reqwest::Error>(Bytes::from(body))]);
        let transform_calls = Arc::new(AtomicUsize::new(0));
        let mut stream = EventStream::with_policy(
            Box::pin(bytes),
            Box::new(CountingTransform(Arc::clone(&transform_calls))),
            4096,
            Duration::from_secs(1),
        );

        assert!(matches!(
            stream.next_event().await,
            Err(LlmError::Protocol { reason, .. }) if reason.contains("不是合法 UTF-8")
        ));
        assert_eq!(
            transform_calls.load(Ordering::Relaxed),
            1,
            "非法帧不得进入协议 transform"
        );
    }
}
