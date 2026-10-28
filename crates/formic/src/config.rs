//! 作业启动配置：只读取调用方明确指定的 TOML，并在边界上完成默认值、
//! 外部服务参数解析和全部资源参数校验。

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use serde::Deserialize;

use crate::llm::{InputModalities, LlmConfig, Protocol};

const CONFIG_FILE: &str = "config.toml";
const DEFAULT_LLM_ATTEMPTS: u32 = 5;
const DEFAULT_MAX_CONCURRENT_UNITS: usize = 64;
const DEFAULT_IDENTICAL_TOOL_CALL_LIMIT: u32 = 16;
const DEFAULT_CONTEXT_SAFETY_TOKENS: u64 = 2048;
const DEFAULT_CONNECT_TIMEOUT_MS: u64 = 30_000;
const DEFAULT_READ_TIMEOUT_MS: u64 = 600_000;
const DEFAULT_REQUEST_TIMEOUT_MS: u64 = 1_800_000;
const DEFAULT_RETRY_DELAYS_MS: &[u64] = &[1_000, 2_000, 5_000];
const DEFAULT_MAX_RETRY_AFTER_MS: u64 = 60_000;
const DEFAULT_MAX_RESULT_BYTES: usize = 1024 * 1024;
const DEFAULT_MAX_IN_FLIGHT: usize = 64;
const DEFAULT_MAX_MATCHES: usize = 1000;
const DEFAULT_MAX_CONTEXT_LINES: usize = 100;
const DEFAULT_CACHE_BYTES: usize = 1024 * 1024 * 1024;
const DEFAULT_MCP_MAX_IN_FLIGHT: usize = 64;
const DEFAULT_MCP_STARTUP_TIMEOUT_SEC: u64 = 60;
const DEFAULT_MCP_TOOL_TIMEOUT_SEC: u64 = 600;

#[derive(Clone)]
pub struct AppConfig {
    pub llm: LlmConfig,
    pub metrics_enabled: bool,
    pub execution: ExecutionConfig,
    pub tools: ToolsConfig,
    pub cache: CacheConfig,
    pub mcp_servers: BTreeMap<String, McpServerConfig>,
}

#[derive(Debug, Clone)]
pub struct ExecutionConfig {
    pub llm_attempts: u32,
    pub max_concurrent_units: usize,
    pub identical_tool_call_limit: u32,
    pub context_safety_tokens: u64,
}

#[derive(Debug, Clone)]
pub struct ToolsConfig {
    pub max_in_flight: usize,
    pub search: SearchToolConfig,
    pub read: ReadToolConfig,
}

#[derive(Debug, Clone)]
pub struct SearchToolConfig {
    pub enabled: bool,
    pub max_result_bytes: usize,
    pub max_in_flight: usize,
    pub max_matches: usize,
    pub max_context_lines: usize,
}

#[derive(Debug, Clone)]
pub struct ReadToolConfig {
    pub enabled: bool,
    pub max_result_bytes: usize,
    pub max_in_flight: usize,
}

#[derive(Debug, Clone)]
pub struct CacheConfig {
    pub enabled: bool,
    pub max_bytes: usize,
}

#[derive(Clone)]
pub struct McpServerConfig {
    pub enabled_tools: Option<Vec<String>>,
    pub tool_aliases: BTreeMap<String, String>,
    pub session_scope: SessionScope,
    pub max_in_flight: usize,
    pub startup_timeout: Duration,
    pub tool_timeout: Duration,
    pub max_result_bytes: usize,
    pub max_message_bytes: Option<usize>,
    pub reconnect: bool,
    pub tool_limits: BTreeMap<String, McpToolLimit>,
    pub transport: McpTransportConfig,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionScope {
    Job,
    Unit,
}

#[derive(Debug, Clone)]
pub struct McpToolLimit {
    pub max_in_flight: usize,
}

#[derive(Clone)]
pub enum McpTransportConfig {
    Stdio {
        command: String,
        args: Vec<String>,
        env: BTreeMap<String, String>,
    },
    Http {
        url: String,
        bearer_token: Option<String>,
        headers: BTreeMap<String, String>,
    },
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileConfig {
    protocol: Option<String>,
    url: Option<String>,
    api_key: Option<String>,
    headers: BTreeMap<String, String>,
    model: Option<String>,
    context_window_tokens: Option<u64>,
    model_input_modalities: Option<Vec<String>>,
    /// Anthropic Messages 协议要求的必填参数；其他协议不得配置。
    anthropic_max_tokens: Option<u64>,
    extra_body_json: Option<String>,
    connect_timeout_ms: Option<u64>,
    read_timeout_ms: Option<u64>,
    request_timeout_ms: Option<u64>,
    retry_delays_ms: Option<Vec<u64>>,
    max_retry_after_ms: Option<u64>,
    requests_per_minute: Option<u32>,
    metrics: bool,
    execution: FileExecutionConfig,
    tools: FileToolsConfig,
    cache: FileCacheConfig,
    mcp_servers: BTreeMap<String, FileMcpServerConfig>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileExecutionConfig {
    llm_attempts: Option<u32>,
    max_concurrent_units: Option<usize>,
    identical_tool_call_limit: Option<u32>,
    context_safety_tokens: Option<u64>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileToolsConfig {
    max_result_bytes: Option<usize>,
    max_in_flight: Option<usize>,
    search: FileSearchToolConfig,
    read: FileReadToolConfig,
}

#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileSearchToolConfig {
    enabled: bool,
    max_result_bytes: Option<usize>,
    max_in_flight: Option<usize>,
    max_matches: Option<usize>,
    max_context_lines: Option<usize>,
}

impl Default for FileSearchToolConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            max_result_bytes: None,
            max_in_flight: None,
            max_matches: None,
            max_context_lines: None,
        }
    }
}

#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileReadToolConfig {
    enabled: bool,
    max_result_bytes: Option<usize>,
    max_in_flight: Option<usize>,
}

impl Default for FileReadToolConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            max_result_bytes: None,
            max_in_flight: None,
        }
    }
}

#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileCacheConfig {
    enabled: bool,
    max_bytes: Option<usize>,
}

impl Default for FileCacheConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            max_bytes: None,
        }
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileMcpServerConfig {
    enabled: bool,
    command: Option<String>,
    args: Vec<String>,
    env: BTreeMap<String, String>,
    url: Option<String>,
    bearer_token: Option<String>,
    headers: BTreeMap<String, String>,
    enabled_tools: Option<Vec<String>>,
    tool_aliases: BTreeMap<String, String>,
    session_scope: Option<String>,
    max_in_flight: Option<usize>,
    startup_timeout_sec: Option<u64>,
    tool_timeout_sec: Option<u64>,
    max_result_bytes: Option<usize>,
    max_message_bytes: Option<usize>,
    reconnect: Option<bool>,
    tool_limits: BTreeMap<String, FileMcpToolLimit>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FileMcpToolLimit {
    max_in_flight: Option<usize>,
    max_result_bytes: Option<usize>,
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("无法读取配置文件 {path}：{source}")]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("指定的配置文件 {0} 不存在")]
    MissingFile(PathBuf),
    #[error("配置文件 {path} 不是有效配置：请检查 TOML 语法、字段名和字段类型")]
    Parse { path: PathBuf },
    #[error("配置缺少 protocol：填写 completions、responses 或 anthropic")]
    MissingProtocol,
    #[error("{0}")]
    InvalidProtocol(String),
    #[error("配置缺少 url：填写模型服务地址")]
    MissingUrl,
    #[error("配置缺少 model：填写模型名")]
    MissingModel,
    #[error("配置缺少 context_window_tokens：填写模型上下文大小")]
    MissingContextWindow,
    #[error("配置缺少 model_input_modalities：填写 [\"text\"] 或 [\"text\", \"image\"]")]
    MissingInputModalities,
    #[error("anthropic 协议缺少 anthropic_max_tokens")]
    MissingAnthropicMaxTokens,
    #[error("配置文件必须使用 .toml 扩展名：{0}")]
    InvalidExtension(PathBuf),
    #[error("配置无效：{0}")]
    Invalid(String),
}

/// 显式配置路径必须存在；省略路径时读取当前目录的 `config.toml`。
/// 所有部署与外部服务配置只来自该 TOML。
pub fn load(path: Option<&Path>) -> Result<AppConfig, ConfigError> {
    load_from(path.unwrap_or_else(|| Path::new(CONFIG_FILE)))
}

fn load_from(path: &Path) -> Result<AppConfig, ConfigError> {
    if !path
        .extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| extension.eq_ignore_ascii_case("toml"))
    {
        return Err(ConfigError::InvalidExtension(path.to_path_buf()));
    }
    let file = match fs::read_to_string(path) {
        Ok(contents) => toml::from_str(&contents).map_err(|_| ConfigError::Parse {
            path: path.to_path_buf(),
        })?,
        Err(source) if source.kind() == std::io::ErrorKind::NotFound => {
            return Err(ConfigError::MissingFile(path.to_path_buf()));
        }
        Err(source) => {
            return Err(ConfigError::Read {
                path: path.to_path_buf(),
                source,
            });
        }
    };

    resolve(file)
}

fn resolve(file: FileConfig) -> Result<AppConfig, ConfigError> {
    let protocol_name = non_empty(file.protocol).ok_or(ConfigError::MissingProtocol)?;
    let protocol = Protocol::parse(&protocol_name).map_err(ConfigError::InvalidProtocol)?;
    let extra_body = parse_extra_body_json(file.extra_body_json.as_deref(), protocol)?;
    let headers = parse_llm_headers(file.headers)?;
    let context_window_tokens = file
        .context_window_tokens
        .ok_or(ConfigError::MissingContextWindow)?;
    require_positive(context_window_tokens, "context_window_tokens")?;
    let input_modalities = parse_input_modalities(file.model_input_modalities)?;

    let configured_anthropic_max_tokens = file.anthropic_max_tokens;
    let anthropic_max_tokens = match (protocol, configured_anthropic_max_tokens) {
        (Protocol::Anthropic, Some(value)) => {
            require_positive(value, "anthropic_max_tokens")?;
            Some(value)
        }
        (Protocol::Anthropic, None) => return Err(ConfigError::MissingAnthropicMaxTokens),
        (_, Some(_)) => {
            return Err(ConfigError::Invalid(
                "anthropic_max_tokens 只允许用于 anthropic 协议".into(),
            ));
        }
        (_, None) => None,
    };

    let execution = ExecutionConfig {
        llm_attempts: positive_or(
            file.execution.llm_attempts,
            DEFAULT_LLM_ATTEMPTS,
            "execution.llm_attempts",
        )?,
        max_concurrent_units: positive_or(
            file.execution.max_concurrent_units,
            DEFAULT_MAX_CONCURRENT_UNITS,
            "execution.max_concurrent_units",
        )?,
        identical_tool_call_limit: positive_or(
            file.execution.identical_tool_call_limit,
            DEFAULT_IDENTICAL_TOOL_CALL_LIMIT,
            "execution.identical_tool_call_limit",
        )?,
        context_safety_tokens: positive_or(
            file.execution.context_safety_tokens,
            DEFAULT_CONTEXT_SAFETY_TOKENS,
            "execution.context_safety_tokens",
        )?,
    };
    let reserved = anthropic_max_tokens
        .unwrap_or(0)
        .checked_add(execution.context_safety_tokens)
        .ok_or_else(|| ConfigError::Invalid("上下文保留配置发生整数溢出".into()))?;
    if reserved >= context_window_tokens {
        let components = if anthropic_max_tokens.is_some() {
            "anthropic_max_tokens 与 execution.context_safety_tokens 之和"
        } else {
            "execution.context_safety_tokens"
        };
        return Err(ConfigError::Invalid(format!(
            "context_window_tokens 必须大于 {components}（当前保留 {reserved}）"
        )));
    }

    let global_result = positive_or(
        file.tools.max_result_bytes,
        DEFAULT_MAX_RESULT_BYTES,
        "tools.max_result_bytes",
    )?;
    let global_in_flight = positive_or(
        file.tools.max_in_flight,
        DEFAULT_MAX_IN_FLIGHT,
        "tools.max_in_flight",
    )?;
    let tools = ToolsConfig {
        max_in_flight: global_in_flight,
        search: SearchToolConfig {
            enabled: file.tools.search.enabled,
            max_result_bytes: positive_or(
                file.tools.search.max_result_bytes,
                global_result,
                "tools.search.max_result_bytes",
            )?,
            max_in_flight: positive_or(
                file.tools.search.max_in_flight,
                global_in_flight,
                "tools.search.max_in_flight",
            )?,
            max_matches: positive_or(
                file.tools.search.max_matches,
                DEFAULT_MAX_MATCHES,
                "tools.search.max_matches",
            )?,
            max_context_lines: positive_or(
                file.tools.search.max_context_lines,
                DEFAULT_MAX_CONTEXT_LINES,
                "tools.search.max_context_lines",
            )?,
        },
        read: ReadToolConfig {
            enabled: file.tools.read.enabled,
            max_result_bytes: positive_or(
                file.tools.read.max_result_bytes,
                global_result,
                "tools.read.max_result_bytes",
            )?,
            max_in_flight: positive_or(
                file.tools.read.max_in_flight,
                global_in_flight,
                "tools.read.max_in_flight",
            )?,
        },
    };
    let cache = CacheConfig {
        enabled: file.cache.enabled,
        max_bytes: positive_or(file.cache.max_bytes, DEFAULT_CACHE_BYTES, "cache.max_bytes")?,
    };

    let mut mcp_servers = BTreeMap::new();
    for (name, server) in file.mcp_servers {
        if !server.enabled {
            continue;
        }
        let resolved = resolve_mcp_server(&name, server, global_result)?;
        mcp_servers.insert(name, resolved);
    }

    Ok(AppConfig {
        llm: LlmConfig {
            protocol,
            endpoint: parse_llm_endpoint(file.url, protocol)?,
            model: non_empty(file.model).ok_or(ConfigError::MissingModel)?,
            api_key: non_empty(file.api_key),
            headers,
            context_window_tokens,
            anthropic_max_tokens,
            input_modalities,
            extra_body,
            connect_timeout: Duration::from_millis(positive_or(
                file.connect_timeout_ms,
                DEFAULT_CONNECT_TIMEOUT_MS,
                "connect_timeout_ms",
            )?),
            read_timeout: Duration::from_millis(positive_or(
                file.read_timeout_ms,
                DEFAULT_READ_TIMEOUT_MS,
                "read_timeout_ms",
            )?),
            request_timeout: Duration::from_millis(positive_or(
                file.request_timeout_ms,
                DEFAULT_REQUEST_TIMEOUT_MS,
                "request_timeout_ms",
            )?),
            retry_delays: retry_delays(file.retry_delays_ms)?,
            max_retry_after: Duration::from_millis(positive_or(
                file.max_retry_after_ms,
                DEFAULT_MAX_RETRY_AFTER_MS,
                "max_retry_after_ms",
            )?),
            requests_per_minute: optional_positive(
                file.requests_per_minute,
                "requests_per_minute",
            )?,
        },
        metrics_enabled: file.metrics,
        execution,
        tools,
        cache,
        mcp_servers,
    })
}

fn parse_llm_endpoint(
    raw: Option<String>,
    protocol: Protocol,
) -> Result<reqwest::Url, ConfigError> {
    let raw = non_empty(raw).ok_or(ConfigError::MissingUrl)?;
    let invalid = || ConfigError::Invalid("url 必须是带主机名的 HTTP 或 HTTPS 地址".into());
    let mut url = reqwest::Url::parse(&raw).map_err(|_| invalid())?;
    if !matches!(url.scheme(), "http" | "https") || !url.has_host() {
        return Err(invalid());
    }
    let endpoint_path = match protocol {
        Protocol::Completions => "chat/completions",
        Protocol::Responses => "responses",
        Protocol::Anthropic => "messages",
    };
    url.set_path(&format!(
        "{}/{endpoint_path}",
        url.path().trim_end_matches('/')
    ));
    Ok(url)
}

fn parse_llm_headers(raw: BTreeMap<String, String>) -> Result<HeaderMap, ConfigError> {
    let invalid = || {
        ConfigError::Invalid(
            "headers 必须包含合法且不重复的 HTTP 请求头，不能覆盖认证、正文类型、分帧或协议固定头"
                .into(),
        )
    };
    let mut headers = HeaderMap::new();
    for (name, value) in raw {
        let name = HeaderName::from_bytes(name.as_bytes()).map_err(|_| invalid())?;
        if matches!(
            name.as_str(),
            "authorization"
                | "proxy-authorization"
                | "content-type"
                | "content-length"
                | "host"
                | "transfer-encoding"
                | "connection"
                | "trailer"
                | "upgrade"
                | "x-api-key"
                | "anthropic-version"
        ) || headers.contains_key(&name)
        {
            return Err(invalid());
        }
        let mut value = HeaderValue::from_bytes(value.as_bytes()).map_err(|_| invalid())?;
        value.set_sensitive(true);
        headers.insert(name, value);
    }
    Ok(headers)
}

fn parse_input_modalities(file: Option<Vec<String>>) -> Result<InputModalities, ConfigError> {
    let values = file.ok_or(ConfigError::MissingInputModalities)?;
    InputModalities::parse(&values).map_err(ConfigError::Invalid)
}

fn parse_extra_body_json(
    raw: Option<&str>,
    protocol: Protocol,
) -> Result<serde_json::Map<String, serde_json::Value>, ConfigError> {
    let Some(raw) = raw else {
        return Ok(serde_json::Map::new());
    };
    let value: serde_json::Value = serde_json::from_str(raw)
        .map_err(|error| ConfigError::Invalid(format!("extra_body_json 不是有效 JSON：{error}")))?;
    let serde_json::Value::Object(object) = value else {
        return Err(ConfigError::Invalid(
            "extra_body_json 必须是用 { } 包住的 JSON 对象".into(),
        ));
    };
    let conflicts: Vec<_> = protocol
        .managed_request_fields()
        .iter()
        .copied()
        .filter(|field| object.contains_key(*field))
        .collect();
    if !conflicts.is_empty() {
        return Err(ConfigError::Invalid(format!(
            "extra_body_json 不能修改 Formic 管理的字段：{}",
            conflicts.join(", ")
        )));
    }
    Ok(object)
}

fn resolve_mcp_server(
    name: &str,
    file: FileMcpServerConfig,
    global_result: usize,
) -> Result<McpServerConfig, ConfigError> {
    let enabled_tools = file.enabled_tools;
    let mut unique = BTreeSet::new();
    if let Some(enabled_tools) = &enabled_tools {
        if enabled_tools.is_empty() {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name}.enabled_tools 若配置则不能为空"
            )));
        }
        for tool in enabled_tools {
            if tool.is_empty() || !unique.insert(tool.clone()) {
                return Err(ConfigError::Invalid(format!(
                    "mcp_servers.{name}.enabled_tools 含空名称或重复名称 {tool:?}"
                )));
            }
        }
    }
    let mut aliases = BTreeSet::new();
    for (remote, alias) in &file.tool_aliases {
        if enabled_tools.is_some() && !unique.contains(remote) {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name}.tool_aliases.{remote} 不在 enabled_tools 中"
            )));
        }
        if alias.is_empty() || !aliases.insert(alias) {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name}.tool_aliases 的别名必须非空且互不重复"
            )));
        }
    }
    for remote in file.tool_limits.keys() {
        if enabled_tools.is_some() && !unique.contains(remote) {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name}.tool_limits.{remote} 不在 enabled_tools 中"
            )));
        }
    }

    let server_in_flight = positive_or(
        file.max_in_flight,
        DEFAULT_MCP_MAX_IN_FLIGHT,
        &format!("mcp_servers.{name}.max_in_flight"),
    )?;
    let server_result = positive_or(
        file.max_result_bytes,
        global_result,
        &format!("mcp_servers.{name}.max_result_bytes"),
    )?;
    let mut tool_limits = BTreeMap::new();
    for (tool, limit) in file.tool_limits {
        if limit.max_result_bytes.is_some() {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name}.tool_limits.{tool}.max_result_bytes 无效；结果字节上限必须统一配置在 mcp_servers.{name}.max_result_bytes"
            )));
        }
        tool_limits.insert(
            tool.clone(),
            McpToolLimit {
                max_in_flight: positive_or(
                    limit.max_in_flight,
                    server_in_flight,
                    &format!("mcp_servers.{name}.tool_limits.{tool}.max_in_flight"),
                )?,
            },
        );
    }

    let has_stdio_extras = !file.args.is_empty() || !file.env.is_empty();
    let has_http_extras = file.bearer_token.is_some() || !file.headers.is_empty();
    let transport = match (non_empty(file.command), non_empty(file.url)) {
        (Some(command), None) if !has_http_extras => McpTransportConfig::Stdio {
            command,
            args: file.args,
            env: file.env,
        },
        (None, Some(url)) if !has_stdio_extras => {
            let headers = file.headers;
            validate_headers(name, &headers)?;
            let bearer_token = non_empty(file.bearer_token);
            McpTransportConfig::Http {
                url,
                bearer_token,
                headers,
            }
        }
        (Some(_), Some(_)) => {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name} 的 command 与 url 互斥"
            )));
        }
        (Some(_), None) => {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name} 使用 stdio 时不能配置 HTTP 字段"
            )));
        }
        (None, Some(_)) => {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name} 使用 HTTP 时不能配置 stdio 字段"
            )));
        }
        (None, None) => {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name} 必须且只能配置 command 或 url"
            )));
        }
    };

    let session_scope = match file.session_scope.as_deref().unwrap_or("job") {
        "job" => SessionScope::Job,
        "unit" => SessionScope::Unit,
        other => {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{name}.session_scope 必须是 job 或 unit，当前为 {other:?}"
            )));
        }
    };

    Ok(McpServerConfig {
        enabled_tools,
        tool_aliases: file.tool_aliases,
        session_scope,
        max_in_flight: server_in_flight,
        startup_timeout: Duration::from_secs(positive_or(
            file.startup_timeout_sec,
            DEFAULT_MCP_STARTUP_TIMEOUT_SEC,
            &format!("mcp_servers.{name}.startup_timeout_sec"),
        )?),
        tool_timeout: Duration::from_secs(positive_or(
            file.tool_timeout_sec,
            DEFAULT_MCP_TOOL_TIMEOUT_SEC,
            &format!("mcp_servers.{name}.tool_timeout_sec"),
        )?),
        max_result_bytes: server_result,
        max_message_bytes: optional_positive(
            file.max_message_bytes,
            &format!("mcp_servers.{name}.max_message_bytes"),
        )?,
        reconnect: file.reconnect.unwrap_or(true),
        tool_limits,
        transport,
    })
}

fn validate_headers(server: &str, headers: &BTreeMap<String, String>) -> Result<(), ConfigError> {
    for (name, value) in headers {
        if name.parse::<HeaderName>().is_err() || HeaderValue::from_str(value).is_err() {
            return Err(ConfigError::Invalid(format!(
                "mcp_servers.{server}.headers 含无效 HTTP header {name:?}"
            )));
        }
    }
    Ok(())
}

fn positive_or<T>(value: Option<T>, default: T, name: &str) -> Result<T, ConfigError>
where
    T: Copy + PartialEq + From<u8>,
{
    let value = value.unwrap_or(default);
    if value == T::from(0) {
        return Err(ConfigError::Invalid(format!("{name} 必须是正整数")));
    }
    Ok(value)
}

fn optional_positive<T>(value: Option<T>, name: &str) -> Result<Option<T>, ConfigError>
where
    T: Copy + PartialEq + From<u8>,
{
    if value == Some(T::from(0)) {
        return Err(ConfigError::Invalid(format!("{name} 必须是正整数")));
    }
    Ok(value)
}

fn retry_delays(values: Option<Vec<u64>>) -> Result<Vec<Duration>, ConfigError> {
    let values = values.unwrap_or_else(|| DEFAULT_RETRY_DELAYS_MS.to_vec());
    if values.contains(&0) {
        return Err(ConfigError::Invalid(
            "retry_delays_ms 的每个等待时间都必须是正整数".into(),
        ));
    }
    Ok(values.into_iter().map(Duration::from_millis).collect())
}

fn require_positive<T>(value: T, name: &str) -> Result<(), ConfigError>
where
    T: PartialEq + From<u8>,
{
    if value == T::from(0) {
        return Err(ConfigError::Invalid(format!("{name} 必须是正整数")));
    }
    Ok(())
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load_fixture(contents: Option<&str>) -> Result<AppConfig, ConfigError> {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(CONFIG_FILE);
        if let Some(contents) = contents {
            fs::write(&path, contents).unwrap();
        }
        load_from(&path)
    }

    #[test]
    fn explicitly_selected_config_must_exist() {
        let directory = tempfile::tempdir().unwrap();
        let missing = directory.path().join("missing.toml");
        let error = load_from(&missing).err().expect("显式缺失配置必须失败");
        assert!(matches!(error, ConfigError::MissingFile(path) if path == missing));
    }

    #[test]
    fn default_config_must_exist() {
        let error = load_fixture(None)
            .err()
            .expect("默认 config.toml 缺失必须失败");
        assert!(matches!(
            error,
            ConfigError::MissingFile(path) if path.file_name().is_some_and(|name| name == CONFIG_FILE)
        ));
    }

    #[test]
    fn config_path_must_use_toml_extension() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("config.txt");
        fs::write(&path, "protocol='responses'").unwrap();
        assert!(matches!(
            load_from(&path),
            Err(ConfigError::InvalidExtension(actual)) if actual == path
        ));
    }

    const BASE_FILE: &str = r#"
protocol = "responses"
url = "https://file.example/v1"
api_key = "file-key"
model = "file-model"
context_window_tokens = 131072
model_input_modalities = ["text"]
metrics = false
"#;

    #[test]
    fn file_supplies_llm_and_defaults() {
        let config = load_fixture(Some(BASE_FILE)).unwrap();

        assert_eq!(config.llm.protocol, Protocol::Responses);
        assert_eq!(
            config.llm.endpoint.as_str(),
            "https://file.example/v1/responses"
        );
        assert_eq!(config.llm.api_key.as_deref(), Some("file-key"));
        assert_eq!(config.llm.model, "file-model");
        assert_eq!(config.llm.context_window_tokens, 131072);
        assert_eq!(config.llm.input_modalities, InputModalities::Text);
        assert!(!config.metrics_enabled);
        assert_eq!(config.llm.anthropic_max_tokens, None);
        assert!(config.llm.extra_body.is_empty());
        assert!(config.llm.headers.is_empty());
        assert_eq!(config.llm.connect_timeout, Duration::from_millis(30_000));
        assert_eq!(config.llm.read_timeout, Duration::from_millis(600_000));
        assert_eq!(config.llm.request_timeout, Duration::from_millis(1_800_000));
        assert_eq!(
            config.llm.retry_delays,
            [1_000, 2_000, 5_000].map(Duration::from_millis)
        );
        assert_eq!(config.llm.max_retry_after, Duration::from_millis(60_000));
        assert_eq!(config.llm.requests_per_minute, None);
        assert_eq!(config.execution.llm_attempts, 5);
        assert_eq!(config.execution.max_concurrent_units, 64);
        assert_eq!(config.execution.identical_tool_call_limit, 16);
        assert_eq!(config.execution.context_safety_tokens, 2048);
        assert_eq!(config.tools.max_in_flight, 64);
        assert!(config.tools.search.enabled);
        assert_eq!(config.tools.search.max_result_bytes, 1024 * 1024);
        assert_eq!(config.tools.search.max_in_flight, 64);
        assert_eq!(config.tools.search.max_matches, 1000);
        assert_eq!(config.tools.search.max_context_lines, 100);
        assert!(config.tools.read.enabled);
        assert_eq!(config.tools.read.max_result_bytes, 1024 * 1024);
        assert_eq!(config.tools.read.max_in_flight, 64);
        assert_eq!(config.cache.max_bytes, 1024 * 1024 * 1024);
    }

    #[test]
    fn llm_endpoint_preserves_base_path_and_query_for_each_protocol() {
        for (protocol, suffix) in [
            ("completions", "chat/completions"),
            ("responses", "responses"),
            ("anthropic", "messages"),
        ] {
            for base in [
                "http://localhost:8080",
                "https://example.com/model%20api/v1///?route=one",
            ] {
                let file = BASE_FILE
                    .replace(
                        "protocol = \"responses\"",
                        &format!("protocol = \"{protocol}\""),
                    )
                    .replace("https://file.example/v1", base);
                let file = if protocol == "anthropic" {
                    format!("{file}\nanthropic_max_tokens = 16384\n")
                } else {
                    file
                };
                let config = load_fixture(Some(&file)).unwrap();
                let (url, _, _) = crate::llm::LlmClient::new(config.llm)
                    .build_request("说明", &[], &[])
                    .unwrap();
                let expected = if base.starts_with("http:") {
                    format!("http://localhost:8080/{suffix}")
                } else {
                    format!("https://example.com/model%20api/v1/{suffix}?route=one")
                };
                assert_eq!(url.as_str(), expected);
            }
        }
    }

    #[test]
    fn llm_headers_validate_names_values_duplicates_and_owned_fields() {
        let file = format!(
            "{BASE_FILE}\nheaders = {{ user-agent = 'Formic/0.3.0', x-opencode-session = 'session-sentinel' }}\n"
        );
        let config = load_fixture(Some(&file)).unwrap();
        assert_eq!(config.llm.headers["user-agent"], "Formic/0.3.0");
        assert_eq!(config.llm.headers["x-opencode-session"], "session-sentinel");
        assert!(!format!("{:?}", config.llm.headers).contains("session-sentinel"));
        for headers in [
            "{ 'bad name' = 'value' }",
            "{ 'x-session' = \"bad\\r\\nvalue\" }",
            "{ 'User-Agent' = 'first', 'user-agent' = 'second' }",
            "{ Authorization = 'owned-field-sentinel' }",
            "{ 'Proxy-Authorization' = 'owned-field-sentinel' }",
            "{ 'Content-Type' = 'owned-field-sentinel' }",
            "{ 'Content-Length' = 'owned-field-sentinel' }",
            "{ Host = 'owned-field-sentinel' }",
            "{ 'Transfer-Encoding' = 'owned-field-sentinel' }",
            "{ Connection = 'owned-field-sentinel' }",
            "{ Trailer = 'owned-field-sentinel' }",
            "{ Upgrade = 'owned-field-sentinel' }",
            "{ 'x-api-key' = 'owned-field-sentinel' }",
            "{ 'Anthropic-Version' = 'owned-field-sentinel' }",
        ] {
            let file = format!("{BASE_FILE}\nheaders = {headers}\n");
            let error = load_fixture(Some(&file))
                .err()
                .expect("无效或冲突头必须拒绝");
            assert!(matches!(error, ConfigError::Invalid(_)));
            assert!(!error.to_string().contains("owned-field-sentinel"));
        }
    }

    #[test]
    fn input_modalities_are_required_and_accept_only_current_combinations() {
        let without = BASE_FILE.replace("model_input_modalities = [\"text\"]\n", "");
        let missing = load_fixture(Some(&without))
            .err()
            .expect("输入模态必须显式声明");
        assert!(missing.to_string().contains("model_input_modalities"));

        for invalid in [
            "[]",
            "[\"image\"]",
            "[\"image\", \"text\"]",
            "[\"text\", \"image\", \"audio\"]",
        ] {
            let file = BASE_FILE.replace(
                "model_input_modalities = [\"text\"]",
                &format!("model_input_modalities = {invalid}"),
            );
            assert!(matches!(
                load_fixture(Some(&file)),
                Err(ConfigError::Invalid(_))
            ));
        }

        let file = BASE_FILE.replace(
            "model_input_modalities = [\"text\"]",
            "model_input_modalities = [\"text\", \"image\"]",
        );
        let config = load_fixture(Some(&file)).unwrap();
        assert_eq!(config.llm.input_modalities, InputModalities::TextAndImage);
    }

    #[test]
    fn extra_body_json_accepts_nested_json_and_rejects_request_fields() {
        let extra = r#"{"temperature":0.2,"reasoning":{"effort":"high"},"nullable":null}"#;
        let file = format!("{BASE_FILE}\nextra_body_json = '''{extra}'''\n");
        let config = load_fixture(Some(&file)).unwrap();
        assert_eq!(config.llm.extra_body["temperature"], 0.2);
        assert_eq!(config.llm.extra_body["reasoning"]["effort"], "high");
        assert!(config.llm.extra_body["nullable"].is_null());

        for (protocol, field) in [
            ("completions", "messages"),
            ("responses", "input"),
            ("anthropic", "max_tokens"),
        ] {
            let extra = format!(r#"{{"{field}":[]}}"#);
            let file = format!(
                "{}\nextra_body_json = '''{extra}'''\n",
                BASE_FILE.replace(
                    "protocol = \"responses\"",
                    &format!("protocol = \"{protocol}\"")
                )
            );
            let file = if protocol == "anthropic" {
                format!("{file}\nanthropic_max_tokens = 16384\n")
            } else {
                file
            };
            let error = load_fixture(Some(&file))
                .err()
                .expect("协议请求字段必须由 Formic 管理");
            assert!(error.to_string().contains(field), "{error}");
        }
    }

    #[test]
    fn extra_body_json_must_be_a_json_object() {
        for extra in ["[1,2]", "{broken"] {
            let file = format!("{BASE_FILE}\nextra_body_json = '''{extra}'''\n");
            assert!(matches!(
                load_fixture(Some(&file)),
                Err(ConfigError::Invalid(_))
            ));
        }
    }

    #[test]
    fn request_policy_is_configurable_and_empty_retry_list_disables_retries() {
        let file = format!(
            "{BASE_FILE}\nconnect_timeout_ms=11\nread_timeout_ms=22\nrequest_timeout_ms=33\nretry_delays_ms=[]\nmax_retry_after_ms=44\nrequests_per_minute=55\n[execution]\nmax_concurrent_units=7\n"
        );
        let config = load_fixture(Some(&file)).unwrap();
        assert_eq!(config.llm.connect_timeout, Duration::from_millis(11));
        assert_eq!(config.llm.read_timeout, Duration::from_millis(22));
        assert_eq!(config.llm.request_timeout, Duration::from_millis(33));
        assert!(config.llm.retry_delays.is_empty());
        assert_eq!(config.llm.max_retry_after, Duration::from_millis(44));
        assert_eq!(config.llm.requests_per_minute, Some(55));
        assert_eq!(config.execution.max_concurrent_units, 7);
    }

    #[test]
    fn request_policy_rejects_zero_values_inside_nonempty_settings() {
        for field in [
            "connect_timeout_ms=0",
            "read_timeout_ms=0",
            "request_timeout_ms=0",
            "max_retry_after_ms=0",
            "requests_per_minute=0",
            "retry_delays_ms=[1,0]",
        ] {
            let file = format!("{BASE_FILE}\n{field}\n");
            assert!(
                matches!(load_fixture(Some(&file)), Err(ConfigError::Invalid(_))),
                "应拒绝 {field}"
            );
        }
    }

    #[test]
    fn metrics_is_selected_only_by_toml() {
        let file = BASE_FILE.replace("metrics = false", "metrics = true");
        let config = load_fixture(Some(&file)).unwrap();
        assert!(config.metrics_enabled);
    }

    #[test]
    fn missing_context_names_toml_field() {
        let error = load_fixture(Some(
            "protocol='responses'\nurl='x'\nmodel='m'\nmodel_input_modalities=['text']\n",
        ))
        .err()
        .expect("应拒绝无效配置");
        let message = error.to_string();
        assert!(message.contains("context_window_tokens"), "{message}");
    }

    #[test]
    fn unknown_fields_and_zero_values_are_rejected() {
        let unknown = format!("{BASE_FILE}\nunknown = 1\n");
        assert!(matches!(
            load_fixture(Some(&unknown)),
            Err(ConfigError::Parse { .. })
        ));
        let zero = format!("{BASE_FILE}\n[tools]\nmax_result_bytes = 0\n");
        let error = load_fixture(Some(&zero)).err().expect("应拒绝无效配置");
        assert!(error.to_string().contains("tools.max_result_bytes"));
    }

    #[test]
    fn context_reserve_must_leave_input_room() {
        let file = "protocol='responses'\nurl='x'\nmodel='m'\ncontext_window_tokens=100\nmodel_input_modalities=['text']\n[execution]\ncontext_safety_tokens=100\n";
        let error = load_fixture(Some(file)).err().expect("应拒绝无效配置");
        assert!(error.to_string().contains("必须大于"));
    }

    #[test]
    fn anthropic_max_tokens_is_required_and_protocol_specific() {
        let anthropic = BASE_FILE.replace("protocol = \"responses\"", "protocol = \"anthropic\"");
        let missing = load_fixture(Some(&anthropic))
            .err()
            .expect("Anthropic 必须显式配置协议必填参数");
        assert!(
            missing.to_string().contains("anthropic_max_tokens"),
            "{missing}"
        );

        let anthropic = format!("{anthropic}\nanthropic_max_tokens=20000\n");
        let config = load_fixture(Some(&anthropic)).unwrap();
        assert_eq!(config.llm.anthropic_max_tokens, Some(20000));

        let file = format!("{BASE_FILE}\nanthropic_max_tokens=1000\n");
        let error = load_fixture(Some(&file))
            .err()
            .expect("其他协议不得携带 Anthropic 专属参数");
        assert!(error.to_string().contains("只允许用于 anthropic"));
    }

    #[test]
    fn anthropic_reserve_must_leave_input_room() {
        let file = "protocol='anthropic'\nurl='x'\nmodel='m'\ncontext_window_tokens=100\nmodel_input_modalities=['text']\nanthropic_max_tokens=90\n[execution]\ncontext_safety_tokens=10\n";
        let error = load_fixture(Some(file)).err().expect("应拒绝无效配置");
        assert!(error.to_string().contains("必须大于"));
    }

    #[test]
    fn enabled_mcp_auto_discovers_tools_and_requires_one_transport() {
        let automatic =
            format!("{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\nurl='http://localhost/mcp'\n");
        let config = load_fixture(Some(&automatic)).unwrap();
        let automatic = &config.mcp_servers["demo"];
        assert!(automatic.enabled_tools.is_none());
        assert_eq!(automatic.session_scope, SessionScope::Job);
        assert_eq!(automatic.max_in_flight, 64);
        assert_eq!(automatic.max_result_bytes, 1024 * 1024);
        assert_eq!(automatic.max_message_bytes, None);
        assert_eq!(automatic.startup_timeout, Duration::from_secs(60));
        assert_eq!(automatic.tool_timeout, Duration::from_secs(600));
        assert!(automatic.reconnect);

        let explicit_no_reconnect = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\nurl='http://localhost/mcp'\nreconnect=false\n"
        );
        let config = load_fixture(Some(&explicit_no_reconnect)).unwrap();
        assert!(!config.mcp_servers["demo"].reconnect);

        let conflict = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\nurl='http://localhost/mcp'\ncommand='server'\n"
        );
        let error = load_fixture(Some(&conflict)).err().expect("应拒绝无效配置");
        assert!(error.to_string().contains("互斥"));

        let empty = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\nurl='http://localhost/mcp'\nenabled_tools=[]\n"
        );
        let error = load_fixture(Some(&empty))
            .err()
            .expect("显式空允许列表没有可执行含义");
        assert!(error.to_string().contains("若配置则不能为空"));
    }

    #[test]
    fn mcp_http_secrets_come_directly_from_toml() {
        let file = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\nurl='http://localhost/mcp'\nbearer_token='toml-token'\nheaders={{x-key='toml-header'}}\nenabled_tools=['search']\n"
        );
        let config = load_fixture(Some(&file)).unwrap();
        let server = &config.mcp_servers["demo"];
        let McpTransportConfig::Http {
            bearer_token,
            headers,
            ..
        } = &server.transport
        else {
            panic!("应解析为 HTTP")
        };
        assert_eq!(bearer_token.as_deref(), Some("toml-token"));
        assert_eq!(headers["x-key"], "toml-header");
    }

    #[test]
    fn environment_indirection_fields_are_rejected() {
        for legacy in [
            "bearer_token_env='TOKEN'",
            "header_env={x-key='TOKEN'}",
            "env_vars={TOKEN='SOURCE_TOKEN'}",
        ] {
            let file = format!(
                "{BASE_FILE}\n[mcp_servers.demo]\nenabled=false\nurl='http://localhost/mcp'\n{legacy}\n"
            );
            assert!(matches!(
                load_fixture(Some(&file)),
                Err(ConfigError::Parse { .. })
            ));
        }
    }

    #[test]
    fn tool_concurrency_limit_inherits_server_value() {
        let file = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\ncommand='server'\nenabled_tools=['search']\nmax_in_flight=7\nmax_result_bytes=4567\n[mcp_servers.demo.tool_limits.search]\n"
        );
        let config = load_fixture(Some(&file)).unwrap();
        let limit = &config.mcp_servers["demo"].tool_limits["search"];
        assert_eq!(limit.max_in_flight, 7);
    }

    #[test]
    fn mcp_message_limit_is_optional_and_positive() {
        let explicit = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\ncommand='server'\nmax_message_bytes=8388608\n"
        );
        let config = load_fixture(Some(&explicit)).unwrap();
        assert_eq!(
            config.mcp_servers["demo"].max_message_bytes,
            Some(8_388_608)
        );

        let zero = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\ncommand='server'\nmax_message_bytes=0\n"
        );
        let error = load_fixture(Some(&zero))
            .err()
            .expect("MCP 原始消息上限必须是正整数");
        assert!(error.to_string().contains("max_message_bytes"));
    }

    #[test]
    fn result_limit_cannot_be_configured_per_tool() {
        let file = format!(
            "{BASE_FILE}\n[mcp_servers.demo]\nenabled=true\ncommand='server'\nenabled_tools=['search']\nmax_result_bytes=1024\n[mcp_servers.demo.tool_limits.search]\nmax_result_bytes=512\n"
        );
        let error = load_fixture(Some(&file))
            .err()
            .expect("公共 MCP 结果流无法提供互不相同的解码前上限");
        assert!(
            error
                .to_string()
                .contains("结果字节上限必须统一配置在 mcp_servers.demo.max_result_bytes")
        );
    }

    #[test]
    fn parse_error_does_not_expose_plaintext_api_key() {
        let error = load_fixture(Some("api_key = \"secret-value\"\nmodel = [\n"))
            .err()
            .expect("应拒绝无效配置");

        assert!(!error.to_string().contains("secret-value"));
        assert!(!format!("{error:?}").contains("secret-value"));
    }
}
