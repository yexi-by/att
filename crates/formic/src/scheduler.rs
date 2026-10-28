//! 工具注册表与唯一执行入口。注册表在 worker 启动前冻结；Scheduler 只限制活动资源，
//! 有界收件箱满时调用方等待，不限制任务总量或工具调用总量。

use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use serde_json::Value;
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot};
use tokio_util::sync::CancellationToken;

use crate::cache::{CacheLookup, FlightState, ToolCache, wait_for_flight};
use crate::config::{CacheConfig, ToolsConfig};
use crate::llm::{InputModalities, ToolSpec};
use crate::mcp::{McpManager, McpStartupError, McpTool};
use crate::output_access::WorkerOutputAccess;
use crate::tools::{self, BuiltinTool, PreparedBuiltinCall, Roots, ToolOutput};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolSource {
    Builtin,
    Mcp { server: String, remote_name: String },
}

#[derive(Clone)]
enum ExecutionTarget {
    Builtin {
        executor: BuiltinTool,
        max_in_flight: usize,
    },
    Mcp {
        tool: McpTool,
        server_name: String,
        server_max_in_flight: usize,
        tool_max_in_flight: usize,
        max_result_bytes: usize,
    },
}

#[derive(Clone)]
struct RegisteredTool {
    spec: ToolSpec,
    source: ToolSource,
    target: ExecutionTarget,
}

/// 冻结的模型可见工具目录。BTreeMap 确保名称和 schema 顺序在整个作业内稳定。
#[derive(Clone)]
pub struct ToolRegistry {
    specs: Arc<[ToolSpec]>,
    entries: Arc<BTreeMap<String, RegisteredTool>>,
    mcp: Option<McpManager>,
}

impl ToolRegistry {
    pub fn builtins(
        config: &ToolsConfig,
        output_access: WorkerOutputAccess,
        input_modalities: InputModalities,
    ) -> Self {
        let mut entries = BTreeMap::new();
        for registration in tools::registrations(config, output_access, input_modalities) {
            entries.insert(
                registration.spec.name.clone(),
                RegisteredTool {
                    spec: registration.spec,
                    source: ToolSource::Builtin,
                    target: ExecutionTarget::Builtin {
                        executor: registration.executor,
                        max_in_flight: registration.max_in_flight,
                    },
                },
            );
        }
        Self::freeze(entries, None)
    }

    pub fn with_mcp(
        config: &ToolsConfig,
        manager: McpManager,
        output_access: WorkerOutputAccess,
        input_modalities: InputModalities,
    ) -> Result<Self, RegistryError> {
        let mut registry = Self::builtins(config, output_access, input_modalities);
        let mut entries = (*registry.entries).clone();
        for registration in manager.registrations()? {
            if entries.contains_key(&registration.model_name) {
                return Err(RegistryError::Collision(registration.model_name));
            }
            entries.insert(
                registration.model_name.clone(),
                RegisteredTool {
                    spec: registration.spec,
                    source: ToolSource::Mcp {
                        server: registration.server_name.clone(),
                        remote_name: registration.remote_name.clone(),
                    },
                    target: ExecutionTarget::Mcp {
                        tool: registration.tool,
                        server_name: registration.server_name,
                        server_max_in_flight: registration.server_max_in_flight,
                        tool_max_in_flight: registration.tool_max_in_flight,
                        max_result_bytes: registration.max_result_bytes,
                    },
                },
            );
        }
        registry = Self::freeze(entries, Some(manager));
        Ok(registry)
    }

    fn freeze(entries: BTreeMap<String, RegisteredTool>, mcp: Option<McpManager>) -> Self {
        let specs: Vec<ToolSpec> = entries.values().map(|entry| entry.spec.clone()).collect();
        Self {
            specs: specs.into(),
            entries: Arc::new(entries),
            mcp,
        }
    }

    pub fn specs(&self) -> Arc<[ToolSpec]> {
        Arc::clone(&self.specs)
    }

    pub fn source(&self, name: &str) -> Option<&ToolSource> {
        self.entries.get(name).map(|entry| &entry.source)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum RegistryError {
    #[error(transparent)]
    Mcp(#[from] McpStartupError),
    #[error("工具名 {0} 同时由多个来源注册")]
    Collision(String),
}

/// worker 持有的轻量句柄。
#[derive(Clone)]
pub struct Scheduler {
    inbox: mpsc::Sender<Request>,
    registry: ToolRegistry,
}

struct Request {
    unit: u64,
    name: String,
    arguments: Value,
    cancel: CancellationToken,
    reply: oneshot::Sender<Result<ToolResponse, SchedulerError>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CacheDisposition {
    Disabled,
    Bypassed,
    Hit,
    Miss,
    Joined,
}

#[derive(Debug, Clone)]
pub struct ToolResponse {
    pub content: String,
    pub images: Vec<crate::image_input::ToolImage>,
    pub cache: CacheDisposition,
    pub cache_evictions: u64,
    pub wait_ms: u64,
    pub execution_ms: u64,
    pub mcp_server: Option<String>,
    pub mcp_current_in_flight: Option<u64>,
    pub mcp_peak_in_flight: Option<u64>,
}

#[derive(thiserror::Error, Debug)]
pub enum SchedulerError {
    #[error("调度器不可用")]
    Gone,
    #[error("工具调用在执行前已取消")]
    Cancelled,
    #[error("工具执行任务异常结束：{0}")]
    Execution(String),
    #[error("{0}")]
    Mcp(String),
}

impl Scheduler {
    pub fn start(
        registry: ToolRegistry,
        roots: Roots,
        tools_config: &ToolsConfig,
        cache_config: &CacheConfig,
        inbox_capacity: usize,
    ) -> Self {
        let (tx, mut rx) = mpsc::channel::<Request>(inbox_capacity);
        let roots = Arc::new(roots);
        let global = Arc::new(Semaphore::new(tools_config.max_in_flight));
        let cache = cache_config
            .enabled
            .then(|| ToolCache::new(cache_config.max_bytes));
        let mut runtime_targets = BTreeMap::new();
        let mut mcp_server_permits: BTreeMap<String, Arc<Semaphore>> = BTreeMap::new();
        let mut mcp_server_in_flight: BTreeMap<String, Arc<InFlightTracker>> = BTreeMap::new();
        for entry in registry.entries.values() {
            if let ExecutionTarget::Mcp {
                server_name,
                server_max_in_flight,
                ..
            } = &entry.target
            {
                mcp_server_permits
                    .entry(server_name.clone())
                    .or_insert_with(|| Arc::new(Semaphore::new(*server_max_in_flight)));
                mcp_server_in_flight
                    .entry(server_name.clone())
                    .or_insert_with(|| Arc::new(InFlightTracker::default()));
            }
        }
        for (name, entry) in registry.entries.iter() {
            match &entry.target {
                ExecutionTarget::Builtin {
                    executor,
                    max_in_flight,
                } => {
                    runtime_targets.insert(
                        name.clone(),
                        RuntimeTarget::Builtin {
                            executor: executor.clone(),
                            permits: Arc::new(Semaphore::new(*max_in_flight)),
                        },
                    );
                }
                ExecutionTarget::Mcp {
                    tool,
                    server_name,
                    tool_max_in_flight,
                    max_result_bytes,
                    ..
                } => {
                    runtime_targets.insert(
                        name.clone(),
                        RuntimeTarget::Mcp {
                            tool: tool.clone(),
                            server_name: server_name.clone(),
                            server_permits: Arc::clone(
                                mcp_server_permits
                                    .get(server_name)
                                    .expect("MCP server 信号量已创建"),
                            ),
                            tool_permits: Arc::new(Semaphore::new(*tool_max_in_flight)),
                            in_flight: Arc::clone(
                                mcp_server_in_flight
                                    .get(server_name)
                                    .expect("MCP server 在途计数器已创建"),
                            ),
                            max_result_bytes: *max_result_bytes,
                        },
                    );
                }
            }
        }
        let runtime_targets = Arc::new(runtime_targets);
        tokio::spawn(async move {
            while let Some(request) = rx.recv().await {
                if request.cancel.is_cancelled() {
                    let _ = request.reply.send(Err(SchedulerError::Cancelled));
                    continue;
                }
                let roots = Arc::clone(&roots);
                let global = Arc::clone(&global);
                let cache = cache.clone();
                let targets = Arc::clone(&runtime_targets);
                tokio::spawn(async move {
                    let result = dispatch(
                        request.unit,
                        &request.name,
                        request.arguments,
                        &request.cancel,
                        roots,
                        targets,
                        global,
                        cache,
                    )
                    .await;
                    let _ = request.reply.send(result);
                });
            }
        });
        Self {
            inbox: tx,
            registry,
        }
    }

    #[cfg(test)]
    pub fn specs(&self) -> Arc<[ToolSpec]> {
        self.registry.specs()
    }

    pub fn source(&self, name: &str) -> Option<&ToolSource> {
        self.registry.source(name)
    }

    pub async fn finish_unit(&self, unit: u64) {
        if let Some(manager) = &self.registry.mcp {
            manager.finish_unit(unit).await;
        }
    }

    pub async fn shutdown(&self) {
        if let Some(manager) = &self.registry.mcp {
            manager.shutdown().await;
        }
    }

    pub async fn execute(
        &self,
        unit: u64,
        name: &str,
        arguments: Value,
        cancel: CancellationToken,
    ) -> Result<ToolResponse, SchedulerError> {
        let (reply, result) = oneshot::channel();
        tokio::select! {
            _ = cancel.cancelled() => return Err(SchedulerError::Cancelled),
            sent = self.inbox.send(Request {
                unit,
                name: name.to_string(),
                arguments,
                cancel: cancel.clone(),
                reply,
            }) => sent.map_err(|_| SchedulerError::Gone)?,
        }
        tokio::select! {
            _ = cancel.cancelled() => Err(SchedulerError::Cancelled),
            received = result => received.map_err(|_| SchedulerError::Gone)?,
        }
    }
}

enum RuntimeTarget {
    Builtin {
        executor: BuiltinTool,
        permits: Arc<Semaphore>,
    },
    Mcp {
        tool: McpTool,
        server_name: String,
        server_permits: Arc<Semaphore>,
        tool_permits: Arc<Semaphore>,
        in_flight: Arc<InFlightTracker>,
        max_result_bytes: usize,
    },
}

#[derive(Default)]
struct InFlightTracker {
    current: AtomicU64,
    peak: AtomicU64,
}

impl InFlightTracker {
    fn enter(&self) {
        let current = self.current.fetch_add(1, Ordering::Relaxed) + 1;
        self.peak.fetch_max(current, Ordering::Relaxed);
    }

    fn leave(&self) -> (u64, u64) {
        let current = self.current.fetch_sub(1, Ordering::Relaxed) - 1;
        (current, self.peak.load(Ordering::Relaxed))
    }
}

// 调度入口显式接收已经解析的调用事实与四个独立能力，避免建立无语义的参数字段袋。
#[allow(clippy::too_many_arguments)]
async fn dispatch(
    _unit: u64,
    name: &str,
    arguments: Value,
    cancel: &CancellationToken,
    roots: Arc<Roots>,
    targets: Arc<BTreeMap<String, RuntimeTarget>>,
    global: Arc<Semaphore>,
    cache: Option<ToolCache>,
) -> Result<ToolResponse, SchedulerError> {
    let started = Instant::now();
    let Some(target) = targets.get(name) else {
        return Ok(ToolResponse {
            content: format!("错误：未知工具 {name}"),
            images: Vec::new(),
            cache: CacheDisposition::Bypassed,
            cache_evictions: 0,
            wait_ms: 0,
            execution_ms: 0,
            mcp_server: None,
            mcp_current_in_flight: None,
            mcp_peak_in_flight: None,
        });
    };
    match target {
        RuntimeTarget::Builtin { executor, permits } => {
            let prepared = executor.prepare(&arguments);
            let cache_key = cache.as_ref().and_then(|_| {
                prepared
                    .as_ref()
                    .ok()
                    .and_then(PreparedBuiltinCall::canonical_cache_key)
                    .map(|arguments| format!("{name}\n{arguments}"))
            });
            if let (Some(cache), Some(key)) = (&cache, cache_key) {
                loop {
                    match cache.lookup(&key).await {
                        CacheLookup::Hit(output) => {
                            return Ok(cached_response(output, CacheDisposition::Hit, started));
                        }
                        CacheLookup::Join(receiver) => {
                            let state = tokio::select! {
                                _ = cancel.cancelled() => return Err(SchedulerError::Cancelled),
                                state = wait_for_flight(receiver) => state,
                            };
                            match state {
                                FlightState::Complete(output) => {
                                    return Ok(cached_response(
                                        output,
                                        CacheDisposition::Joined,
                                        started,
                                    ));
                                }
                                FlightState::Aborted => continue,
                                FlightState::Pending => unreachable!("等待函数不返回 Pending"),
                            }
                        }
                        CacheLookup::Owner(sender) => {
                            let permits = match acquire_permits(permits, &global, cancel).await {
                                Ok(permits) => permits,
                                Err(error) => {
                                    cache.abort(&key, sender).await;
                                    return Err(error);
                                }
                            };
                            let wait_ms = elapsed_ms(started);
                            let result =
                                execute_builtin(prepared, roots, permits, cancel.clone()).await;
                            match result {
                                Ok((output, execution_ms)) => {
                                    let output = Arc::new(output);
                                    let evictions =
                                        cache.complete(&key, sender, Arc::clone(&output)).await;
                                    return Ok(ToolResponse {
                                        content: output.content.clone(),
                                        images: output.images.clone(),
                                        cache: CacheDisposition::Miss,
                                        cache_evictions: evictions,
                                        wait_ms,
                                        execution_ms,
                                        mcp_server: None,
                                        mcp_current_in_flight: None,
                                        mcp_peak_in_flight: None,
                                    });
                                }
                                Err(error) => {
                                    cache.abort(&key, sender).await;
                                    return Err(error);
                                }
                            }
                        }
                    }
                }
            }

            let permits = acquire_permits(permits, &global, cancel).await?;
            let wait_ms = elapsed_ms(started);
            let (output, execution_ms) =
                execute_builtin(prepared, roots, permits, cancel.clone()).await?;
            Ok(ToolResponse {
                content: output.content,
                images: output.images,
                cache: if cache.is_some() {
                    CacheDisposition::Bypassed
                } else {
                    CacheDisposition::Disabled
                },
                cache_evictions: 0,
                wait_ms,
                execution_ms,
                mcp_server: None,
                mcp_current_in_flight: None,
                mcp_peak_in_flight: None,
            })
        }
        RuntimeTarget::Mcp {
            tool,
            server_name,
            server_permits,
            tool_permits,
            in_flight,
            max_result_bytes,
        } => {
            let permits = acquire_permits(tool_permits, server_permits, cancel).await?;
            let wait_ms = elapsed_ms(started);
            crate::metrics::gauge_add(&crate::metrics::TOOL_INFLIGHT, 1);
            in_flight.enter();
            let execution_started = Instant::now();
            let called = tokio::select! {
                _ = cancel.cancelled() => Err(SchedulerError::Cancelled),
                result = tool.call(_unit, arguments, *max_result_bytes) => {
                    result.map_err(|error| SchedulerError::Mcp(error.to_string()))
                }
            };
            drop(permits);
            let (current_in_flight, peak_in_flight) = in_flight.leave();
            crate::metrics::gauge_add(&crate::metrics::TOOL_INFLIGHT, -1);
            let output = called?;
            Ok(ToolResponse {
                content: output.content,
                images: output.images,
                cache: CacheDisposition::Bypassed,
                cache_evictions: 0,
                wait_ms,
                execution_ms: elapsed_ms(execution_started),
                mcp_server: Some(server_name.clone()),
                mcp_current_in_flight: Some(current_in_flight),
                mcp_peak_in_flight: Some(peak_in_flight),
            })
        }
    }
}

fn cached_response(
    output: Arc<ToolOutput>,
    disposition: CacheDisposition,
    started: Instant,
) -> ToolResponse {
    ToolResponse {
        content: output.content.clone(),
        images: output.images.clone(),
        cache: disposition,
        cache_evictions: 0,
        wait_ms: elapsed_ms(started),
        execution_ms: 0,
        mcp_server: None,
        mcp_current_in_flight: None,
        mcp_peak_in_flight: None,
    }
}

async fn acquire_permits(
    tool: &Arc<Semaphore>,
    global: &Arc<Semaphore>,
    cancel: &CancellationToken,
) -> Result<(OwnedSemaphorePermit, OwnedSemaphorePermit), SchedulerError> {
    let tool_permit = tokio::select! {
        _ = cancel.cancelled() => return Err(SchedulerError::Cancelled),
        permit = Arc::clone(tool).acquire_owned() => permit.expect("工具信号量与调度器同生命周期"),
    };
    let global_permit = tokio::select! {
        _ = cancel.cancelled() => return Err(SchedulerError::Cancelled),
        permit = Arc::clone(global).acquire_owned() => permit.expect("全局信号量与调度器同生命周期"),
    };
    Ok((tool_permit, global_permit))
}

async fn execute_builtin(
    prepared: Result<PreparedBuiltinCall, String>,
    roots: Arc<Roots>,
    permits: (OwnedSemaphorePermit, OwnedSemaphorePermit),
    cancel: CancellationToken,
) -> Result<(ToolOutput, u64), SchedulerError> {
    crate::metrics::gauge_add(&crate::metrics::TOOL_INFLIGHT, 1);
    let is_search = prepared.as_ref().is_ok_and(PreparedBuiltinCall::is_search);
    let task_cancel = cancel.clone();
    let joined = tokio::task::spawn_blocking(move || {
        let _permits = permits;
        let started = Instant::now();
        let output = match prepared {
            Ok(call) => call.execute_cancellable(&roots, &task_cancel),
            Err(message) => ToolOutput::error(message),
        };
        let elapsed = elapsed_ms(started);
        if is_search {
            crate::metrics::observe_search_ms(elapsed);
        }
        (output, elapsed)
    })
    .await;
    crate::metrics::gauge_add(&crate::metrics::TOOL_INFLIGHT, -1);
    let result = joined.map_err(|error| SchedulerError::Execution(error.to_string()))?;
    if cancel.is_cancelled() {
        Err(SchedulerError::Cancelled)
    } else {
        Ok(result)
    }
}

fn elapsed_ms(started: Instant) -> u64 {
    started.elapsed().as_millis().try_into().unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{ReadToolConfig, SearchToolConfig};
    use crate::output::RecordFormat;
    use std::fs;

    fn configs(cache_enabled: bool) -> (ToolsConfig, CacheConfig) {
        (
            ToolsConfig {
                max_in_flight: 2,
                search: SearchToolConfig {
                    enabled: true,
                    max_result_bytes: 32768,
                    max_in_flight: 2,
                    max_matches: 100,
                    max_context_lines: 20,
                },
                read: ReadToolConfig {
                    enabled: true,
                    max_result_bytes: 32768,
                    max_in_flight: 2,
                },
            },
            CacheConfig {
                enabled: cache_enabled,
                max_bytes: 1024 * 1024,
            },
        )
    }

    fn scheduler(cache_enabled: bool) -> (tempfile::TempDir, Scheduler) {
        let directory = tempfile::tempdir().unwrap();
        let input = directory.path().join("data");
        let output = directory.path().join("out");
        fs::create_dir_all(&input).unwrap();
        fs::create_dir_all(&output).unwrap();
        fs::write(input.join("a.txt"), "苹果\n").unwrap();
        let (tools, cache) = configs(cache_enabled);
        let registry =
            ToolRegistry::builtins(&tools, WorkerOutputAccess::Published, InputModalities::Text);
        let scheduler = Scheduler::start(
            registry,
            Roots {
                input: crate::tools::ReadRoot::open(input).unwrap(),
                output: Some(crate::tools::ReadRoot::open(output).unwrap()),
                output_format: RecordFormat::Markdown,
            },
            &tools,
            &cache,
            2,
        );
        (directory, scheduler)
    }

    #[tokio::test]
    async fn input_call_is_cached_but_output_is_not() {
        let (_directory, scheduler) = scheduler(true);
        let arguments = serde_json::json!({"scope":"input","path":"a.txt"});
        let first = scheduler
            .execute(1, "read", arguments.clone(), CancellationToken::new())
            .await
            .unwrap();
        let second = scheduler
            .execute(2, "read", arguments, CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(first.cache, CacheDisposition::Miss);
        assert_eq!(second.cache, CacheDisposition::Hit);
    }

    #[tokio::test]
    async fn invalid_builtin_arguments_are_uncached_and_valid_calls_still_execute() {
        for cache_enabled in [false, true] {
            let (_directory, scheduler) = scheduler(cache_enabled);
            let expected = if cache_enabled {
                CacheDisposition::Bypassed
            } else {
                CacheDisposition::Disabled
            };
            for (name, arguments) in [
                (
                    "read",
                    serde_json::json!({"scope":"input","path":"../a.txt"}),
                ),
                (
                    "search",
                    serde_json::json!({"scope":"input","pattern":"x","context":-1}),
                ),
            ] {
                for unit in [1, 2] {
                    let response = scheduler
                        .execute(unit, name, arguments.clone(), CancellationToken::new())
                        .await
                        .unwrap();
                    assert!(
                        response.content.starts_with("错误："),
                        "{}",
                        response.content
                    );
                    assert_eq!(response.cache, expected);
                }
            }
            let response = scheduler
                .execute(
                    3,
                    "read",
                    serde_json::json!({"scope":"input","path":"a.txt"}),
                    CancellationToken::new(),
                )
                .await
                .unwrap();
            assert!(
                !response.content.starts_with("错误："),
                "{}",
                response.content
            );
        }
    }

    #[tokio::test]
    async fn queued_cancel_does_not_execute() {
        let (_directory, scheduler) = scheduler(false);
        let token = CancellationToken::new();
        token.cancel();
        let error = scheduler
            .execute(
                1,
                "read",
                serde_json::json!({"scope":"input","path":"a.txt"}),
                token,
            )
            .await
            .unwrap_err();
        assert!(matches!(error, SchedulerError::Cancelled));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn thousand_units_wait_for_bounded_inbox_without_capacity_errors() {
        let (_directory, scheduler) = scheduler(false);
        let mut tasks = tokio::task::JoinSet::new();
        for unit in 1..=1000 {
            let scheduler = scheduler.clone();
            tasks.spawn(async move {
                scheduler
                    .execute(
                        unit,
                        "read",
                        serde_json::json!({"scope":"input","path":"a.txt"}),
                        CancellationToken::new(),
                    )
                    .await
            });
        }
        let mut completed = 0;
        while let Some(joined) = tasks.join_next().await {
            let response = joined.unwrap().unwrap();
            assert!(response.content.contains("苹果"));
            completed += 1;
        }
        assert_eq!(completed, 1000, "总任务量不受有界活动窗口限制");
    }

    #[test]
    fn registry_order_is_stable() {
        let (tools, _) = configs(false);
        let registry =
            ToolRegistry::builtins(&tools, WorkerOutputAccess::Published, InputModalities::Text);
        let specs = registry.specs();
        let names: Vec<&str> = specs.iter().map(|spec| spec.name.as_str()).collect();
        assert_eq!(names, ["read", "search"]);
        assert_eq!(registry.source("read"), Some(&ToolSource::Builtin));
    }
}
