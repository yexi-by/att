//! `rusqlite` 生产存储根。

use std::cell::RefCell;
use std::error::Error;
use std::ffi::{c_int, c_void};
use std::fmt;
use std::fmt::Write as _;
use std::fs::{self, File};
use std::future::Future;
use std::io;
use std::num::NonZeroUsize;
use std::ops::{Deref, DerefMut};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, mpsc};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use rusqlite::backup::{Backup, StepResult};
use rusqlite::limits::Limit;
use rusqlite::types::{ToSql, ToSqlOutput, ValueRef};
use rusqlite::{Connection, ErrorCode, OpenFlags, Statement, Transaction, params_from_iter};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, oneshot};

use crate::diagnostic::{
    Diagnostic, DiagnosticReport, IoFailure, RelatedFailureRelation, RuntimeComponent,
    RuntimeIssue, RuntimeOperation, SafeIdentifier, SafePath, SqliteDiagnosticContext,
    SqliteDiagnosticStage, SqliteDriverFailure, SqliteDriverKind, SqliteIssue, SqliteOperation,
    SqliteProblem, SqliteTransactionState, StateEffect,
};
use crate::runtime::performance::{
    RunPerformanceCounters, SqliteTransactionControl, SqliteTransactionScope,
};
use crate::runtime::windows::{
    FileIdentity, PinnedPath, WindowsFsError, create_new_pinned_database_file,
    delete_regular_file_if_identity, pin_directory_without_reparse, pin_path_without_reparse,
};
use crate::storage::sqlite::{
    CreateDatabaseError, ExecuteFinalTransactionError, ExecuteTransactionError,
    QueryExistingDatabaseError, SnapshotDatabaseError, SqliteBatch, SqliteCommand,
    SqliteDatabaseCreator, SqliteDatabaseSnapshotter, SqliteFinalTransactionExecutor, SqliteQuery,
    SqliteQueryExecutor, SqliteRow, SqliteTransactionExecutor, SqliteTransactionPlan,
    SqliteTransactionStep, SqliteValue,
};
use crate::storage::sqlite_session::{
    SqliteInteractiveSessionFinalization, SqliteInteractiveSessionFinalizationError,
    SqliteInteractiveSessionFinalizationFailure, SqliteInteractiveSessionFinalizer,
};
use crate::storage::sqlite_transaction_session::{
    OpenSqliteTransactionSessionError, OpenedSqliteTransactionSession,
    SqliteTransactionSessionFactory, SqliteTransactionSessionOperations,
};

const STORAGE_RUNNING: u8 = 0;
const STORAGE_SHUTTING_DOWN: u8 = 1;
const STORAGE_CLOSED: u8 = 2;
const SESSION_OPEN: u8 = 0;
const SESSION_INDETERMINATE: u8 = 1;
const SESSION_FINALIZING: u8 = 2;
const SESSION_CLOSED: u8 = 3;
const SQLITE_WAIT_POLL_INTERVAL: Duration = Duration::from_millis(5);
const SQLITE_CANCELLATION_CHECK_OPERATIONS: i32 = 1_000;
const SQLITE_BACKUP_PAGES_PER_STEP: i32 = 256;
// 这些值是 SSPV 最大真实样本的 SQLite 单因素消融结果，不是项目容量限制：
// 64 KiB 页显著减少长路径 Claim 索引的 B-tree 页与写放大；3 GiB 连接缓存和
// 内存 TEMP 避免 SQLite 的 2 MiB 默认缓存把同一批索引页反复赶回磁盘。cache_size
// 只规定缓存目标；每个连接的页缓存随实际触达的页增长，不会按目标值预分配内存，
// 也不拒绝任何规模的项目。
const NEW_DATABASE_PAGE_SIZE_BYTES: i64 = 64 * 1024;
const CONNECTION_CACHE_SIZE_KIB: i64 = -(3 * 1024 * 1024);

#[derive(Clone, Default)]
struct SqliteWaitCancellation {
    requested: Arc<AtomicBool>,
}

impl SqliteWaitCancellation {
    fn request(&self) {
        self.requested.store(true, Ordering::Release);
    }

    fn is_requested(&self) -> bool {
        self.requested.load(Ordering::Acquire)
    }
}

thread_local! {
    static SQLITE_BUSY_CANCELLATION: RefCell<Option<SqliteWaitCancellation>> =
        const { RefCell::new(None) };
}

type AttSqliteCancellationProbe = Arc<dyn Fn() -> bool + Send + Sync + 'static>;

struct AttSqliteCancellationContext {
    probe: AttSqliteCancellationProbe,
    suspension_depth: AtomicUsize,
}

impl AttSqliteCancellationContext {
    fn new(probe: AttSqliteCancellationProbe) -> Self {
        Self {
            probe,
            suspension_depth: AtomicUsize::new(0),
        }
    }

    /// 同一个方法同时服务 busy 与 progress 回调，使终态 guard 对两条取消路径生效。
    fn is_cancelled(&self) -> bool {
        if self.suspension_depth.load(Ordering::Acquire) != 0 {
            return false;
        }
        match catch_unwind(AssertUnwindSafe(|| (self.probe)())) {
            Ok(cancelled) => cancelled,
            Err(payload) => {
                // 取消探针 panic 时必须停止 SQLite 工作；panic payload 可能拥有会再次
                // panic 的 Drop，因此将它遗忘，确保 Rust unwind 绝不越过 SQLite C ABI。
                std::mem::forget(payload);
                true
            }
        }
    }

    fn suspend(&self) {
        self.suspension_depth
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |depth| {
                depth.checked_add(1)
            })
            .expect("SQLite 取消暂停深度不得耗尽");
    }

    fn resume(&self) {
        self.suspension_depth
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |depth| {
                depth.checked_sub(1)
            })
            .expect("SQLite 取消暂停深度不得下溢");
    }
}

unsafe extern "C" fn att_sqlite_busy_handler(
    context: *mut c_void,
    _previous_attempts: c_int,
) -> c_int {
    let keep_waiting = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: 安装回调时传入的是 `Arc::as_ptr` 得到的稳定非空地址；连接持有该 Arc，
        // 并在释放最后一个 Arc 之前先卸载 busy handler。SQLite 只会在 handler 已安装
        // 的同步连接调用中使用这个地址。
        let context = unsafe { &*context.cast::<AttSqliteCancellationContext>() };
        if context.is_cancelled() {
            return false;
        }
        thread::sleep(SQLITE_WAIT_POLL_INTERVAL);
        !context.is_cancelled()
    }));
    match keep_waiting {
        Ok(keep_waiting) => c_int::from(keep_waiting),
        Err(payload) => {
            // C 回调不得 unwind。遗忘可能带有 panic Drop 的 payload，并停止继续等待。
            std::mem::forget(payload);
            0
        }
    }
}

fn install_att_sqlite_busy_handler(
    connection: &Connection,
    context: &Arc<AttSqliteCancellationContext>,
) -> Result<(), rusqlite::Error> {
    let context = Arc::as_ptr(context).cast_mut().cast::<c_void>();
    // SAFETY: `connection.handle()` 在本次调用期间有效；callback 使用的 context 位于 Arc
    // 的稳定分配中。成功后，`AttSqliteCancellableConnection` 会让 Arc 活到先卸载
    // callback、再关闭连接之后。callback 自身捕获全部 panic，不允许 unwind 穿过 C。
    let result = unsafe {
        rusqlite::ffi::sqlite3_busy_handler(
            connection.handle(),
            Some(att_sqlite_busy_handler),
            context,
        )
    };
    if result == rusqlite::ffi::SQLITE_OK {
        Ok(())
    } else {
        Err(rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(result),
            None,
        ))
    }
}

/// 一条直接 SQLite 连接自己的取消控制句柄。
pub(crate) struct AttSqliteCancellationHandle {
    context: Arc<AttSqliteCancellationContext>,
}

/// 已应用 ATT 可取消读写策略、并拥有独立 busy/progress 取消上下文的直接 SQLite 连接。
pub(crate) struct AttSqliteCancellableConnection {
    connection: Option<Connection>,
    cancellation: Arc<AttSqliteCancellationContext>,
}

impl AttSqliteCancellableConnection {
    pub(crate) fn cancellation_handle(&self) -> AttSqliteCancellationHandle {
        AttSqliteCancellationHandle {
            context: Arc::clone(&self.cancellation),
        }
    }

    /// 卸载引用连接级取消上下文的回调，并显式关闭 SQLite 连接。
    ///
    /// 需要确认数据库已经成为可独立发布文件的调用方不能依赖 `Drop`，因为
    /// `rusqlite::Connection::drop` 无法把关闭失败返回给调用方。
    pub(crate) fn close(mut self) -> Result<(), rusqlite::Error> {
        let connection = self
            .connection
            .take()
            .expect("ATT 可取消 SQLite 连接在显式关闭前必须存在");
        let mut callback_error = connection.progress_handler(0, None::<fn() -> bool>).err();
        if let Err(source) = connection.busy_handler(None)
            && callback_error.is_none()
        {
            callback_error = Some(source);
        }
        let close_error = match connection.close() {
            Ok(()) => None,
            Err((connection, source)) => {
                // `self.cancellation` 在返回前仍然存活，因此即使 SQLite 意外保留了回调，
                // 销毁被退回的连接时也不会引用已经释放的取消上下文。
                drop(connection);
                Some(source)
            }
        };
        match close_error.or(callback_error) {
            Some(source) => Err(source),
            None => Ok(()),
        }
    }
}

impl Deref for AttSqliteCancellableConnection {
    type Target = Connection;

    fn deref(&self) -> &Self::Target {
        self.connection
            .as_ref()
            .expect("ATT 可取消 SQLite 连接在 Drop 前必须存在")
    }
}

impl DerefMut for AttSqliteCancellableConnection {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.connection
            .as_mut()
            .expect("ATT 可取消 SQLite 连接在 Drop 前必须存在")
    }
}

impl Drop for AttSqliteCancellableConnection {
    fn drop(&mut self) {
        if let Some(connection) = self.connection.take() {
            let _ = connection.progress_handler(0, None::<fn() -> bool>);
            let _ = connection.busy_handler(None);
            drop(connection);
        }
    }
}

/// 暂停连接的 busy/progress 取消检查，供 COMMIT 与显式 ROLLBACK 确认事务终态。
///
/// 多个 guard 可以嵌套并按任意顺序销毁；最后一个 guard 销毁后才恢复取消检查。
pub(crate) struct AttSqliteCancellationSuspension {
    context: Arc<AttSqliteCancellationContext>,
}

impl Drop for AttSqliteCancellationSuspension {
    fn drop(&mut self) {
        self.context.resume();
    }
}

#[cfg(test)]
#[derive(Clone)]
struct ClaimedDatabasePathHook(Arc<dyn Fn(&Path) + Send + Sync + 'static>);

#[cfg(test)]
impl ClaimedDatabasePathHook {
    fn call(&self, path: &Path) {
        (self.0)(path);
    }
}

#[cfg(test)]
impl fmt::Debug for ClaimedDatabasePathHook {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("ClaimedDatabasePathHook(..)")
    }
}

/// SQLite 根的内部执行资源。
#[derive(Clone, Debug)]
pub(crate) struct RusqliteStorageConfiguration {
    worker_stack_bytes: NonZeroUsize,
    #[cfg(test)]
    fixed_short_worker_threads: Option<NonZeroUsize>,
    #[cfg(test)]
    claimed_database_path_hook: Option<ClaimedDatabasePathHook>,
}

impl RusqliteStorageConfiguration {
    pub(crate) fn production() -> Self {
        Self {
            worker_stack_bytes: NonZeroUsize::new(4 * 1024 * 1024).expect("产品 worker 栈必须非零"),
            #[cfg(test)]
            fixed_short_worker_threads: None,
            #[cfg(test)]
            claimed_database_path_hook: None,
        }
    }

    #[cfg(test)]
    pub(crate) fn new(
        short_worker_threads: NonZeroUsize,
        worker_stack_bytes: NonZeroUsize,
    ) -> Self {
        Self {
            worker_stack_bytes,
            fixed_short_worker_threads: Some(short_worker_threads),
            claimed_database_path_hook: None,
        }
    }

    #[cfg(test)]
    fn with_claimed_database_path_hook(
        mut self,
        hook: impl Fn(&Path) + Send + Sync + 'static,
    ) -> Self {
        self.claimed_database_path_hook = Some(ClaimedDatabasePathHook(Arc::new(hook)));
        self
    }
}

/// SQLite 生产根本身的机制错误。
#[derive(Debug)]
pub(crate) enum SqliteRuntimeError {
    Closed,
    AvailableParallelism {
        source: io::Error,
    },
    Cancelled {
        operation: &'static str,
    },
    InteractiveSessionAlreadyOpen,
    WorkerSpawn {
        worker: String,
        source: io::Error,
    },
    WorkerPanicked(&'static str),
    Io {
        operation: &'static str,
        path: PathBuf,
        source: io::Error,
    },
    WindowsFileSystem {
        operation: &'static str,
        path: PathBuf,
        source: WindowsFsError,
    },
    Driver {
        operation: &'static str,
        source: rusqlite::Error,
    },
    QueryContext {
        query_id: String,
        ordinal: usize,
        source: Box<SqliteRuntimeError>,
    },
    InvalidTarget {
        path: PathBuf,
    },
    UnexpectedArtifact {
        path: PathBuf,
    },
    InvalidValue(&'static str),
    Internal(&'static str),
    BackupIncomplete(&'static str),
    Cleanup {
        primary: Box<SqliteRuntimeError>,
        failures: Vec<SqliteRuntimeError>,
    },
}

impl SqliteRuntimeError {
    /// SQLite 生产根建立阶段尚不存在项目数据库路径时使用的完整诊断。
    pub(crate) fn startup_diagnostic_report(&self) -> DiagnosticReport {
        self.startup_diagnostic_report_with_query(None, None)
    }

    fn startup_diagnostic_report_with_query(
        &self,
        query_id: Option<SafeIdentifier>,
        query_ordinal: Option<usize>,
    ) -> DiagnosticReport {
        let runtime =
            |issue| DiagnosticReport::new(StateEffect::Unchanged, Diagnostic::runtime(issue));
        let sqlite = |problem| {
            DiagnosticReport::new(
                StateEffect::Unchanged,
                Diagnostic::sqlite(SqliteIssue::new(
                    SqliteDiagnosticContext::new(
                        SqliteDiagnosticStage::ProcessStartup,
                        SqliteOperation::Open,
                        SqliteTransactionState::NotStarted,
                    ),
                    problem,
                )),
            )
        };
        match self {
            Self::Closed => runtime(RuntimeIssue::ExecutorClosed {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::StartWorker,
            }),
            Self::AvailableParallelism { source } => runtime(RuntimeIssue::Io {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::DetectAvailableParallelism,
                failure: IoFailure::from_error(source),
            }),
            Self::Cancelled { .. } => runtime(RuntimeIssue::Cancelled {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::StartWorker,
            }),
            Self::InteractiveSessionAlreadyOpen => {
                sqlite(SqliteProblem::RootInteractiveSessionAlreadyOpen)
            }
            Self::WorkerSpawn { source, .. } => runtime(RuntimeIssue::Io {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::StartWorker,
                failure: IoFailure::from_error(source),
            }),
            Self::WorkerPanicked(_) => runtime(RuntimeIssue::WorkerPanicked {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::StartWorker,
            }),
            Self::Io { path, .. }
            | Self::WindowsFileSystem { path, .. }
            | Self::InvalidTarget { path }
            | Self::UnexpectedArtifact { path } => self.diagnostic_report(
                path,
                SqliteDiagnosticContext::new(
                    SqliteDiagnosticStage::ProcessStartup,
                    SqliteOperation::Open,
                    SqliteTransactionState::NotStarted,
                ),
                StateEffect::Unchanged,
            ),
            Self::Driver { source, .. } => sqlite(SqliteProblem::RootDriver {
                query_id,
                query_ordinal,
                failure: sqlite_driver_failure(source),
            }),
            Self::QueryContext {
                query_id: id,
                ordinal,
                source,
            } => source
                .startup_diagnostic_report_with_query(SafeIdentifier::new(id).ok(), Some(*ordinal)),
            Self::InvalidValue(_) => sqlite(SqliteProblem::RootInvalidValue),
            Self::Internal(_) => sqlite(SqliteProblem::RootInternalInvariant),
            Self::BackupIncomplete(_) => sqlite(SqliteProblem::RootBackupIncomplete),
            Self::Cleanup { primary, failures } => {
                let mut report =
                    primary.startup_diagnostic_report_with_query(query_id.clone(), query_ordinal);
                for failure in failures {
                    report = report.with_related(
                        RelatedFailureRelation::Cleanup,
                        failure
                            .startup_diagnostic_report_with_query(query_id.clone(), query_ordinal),
                    );
                }
                report
            }
        }
    }

    /// SQLite 根关闭失败时建立固定 Shutdown 诊断，不要求项目数据库路径。
    pub(crate) fn shutdown_diagnostic_report(&self) -> DiagnosticReport {
        self.shutdown_diagnostic_report_with_query(None, None)
    }

    fn shutdown_diagnostic_report_with_query(
        &self,
        query_id: Option<SafeIdentifier>,
        query_ordinal: Option<usize>,
    ) -> DiagnosticReport {
        let effect = if matches!(self, Self::Internal(_)) {
            StateEffect::OutcomeUnknown
        } else {
            StateEffect::AppliedFinalizationFailed
        };
        let runtime = |issue| DiagnosticReport::new(effect, Diagnostic::runtime(issue));
        let sqlite = |problem| {
            DiagnosticReport::new(
                effect,
                Diagnostic::sqlite(SqliteIssue::new(
                    SqliteDiagnosticContext::new(
                        SqliteDiagnosticStage::Shutdown,
                        SqliteOperation::Shutdown,
                        if effect == StateEffect::OutcomeUnknown {
                            SqliteTransactionState::OutcomeUnknown
                        } else {
                            SqliteTransactionState::FinalizationFailed
                        },
                    ),
                    problem,
                )),
            )
        };
        match self {
            Self::Closed => runtime(RuntimeIssue::ExecutorClosed {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::Shutdown,
            }),
            Self::AvailableParallelism { source } | Self::WorkerSpawn { source, .. } => {
                runtime(RuntimeIssue::Io {
                    component: RuntimeComponent::SqliteExecutor,
                    operation: RuntimeOperation::Shutdown,
                    failure: IoFailure::from_error(source),
                })
            }
            Self::Cancelled { .. } => runtime(RuntimeIssue::Cancelled {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::Shutdown,
            }),
            Self::WorkerPanicked(_) => runtime(RuntimeIssue::WorkerPanicked {
                component: RuntimeComponent::SqliteExecutor,
                operation: RuntimeOperation::Shutdown,
            }),
            Self::InteractiveSessionAlreadyOpen => {
                sqlite(SqliteProblem::RootInteractiveSessionAlreadyOpen)
            }
            Self::Io { path, .. }
            | Self::WindowsFileSystem { path, .. }
            | Self::InvalidTarget { path }
            | Self::UnexpectedArtifact { path } => self.diagnostic_report(
                path,
                SqliteDiagnosticContext::new(
                    SqliteDiagnosticStage::Shutdown,
                    SqliteOperation::Shutdown,
                    if effect == StateEffect::OutcomeUnknown {
                        SqliteTransactionState::OutcomeUnknown
                    } else {
                        SqliteTransactionState::FinalizationFailed
                    },
                ),
                effect,
            ),
            Self::Driver { source, .. } => sqlite(SqliteProblem::RootDriver {
                query_id,
                query_ordinal,
                failure: sqlite_driver_failure(source),
            }),
            Self::QueryContext {
                query_id: id,
                ordinal,
                source,
            } => source.shutdown_diagnostic_report_with_query(
                SafeIdentifier::new(id).ok(),
                Some(*ordinal),
            ),
            Self::InvalidValue(_) => sqlite(SqliteProblem::RootInvalidValue),
            Self::Internal(_) => sqlite(SqliteProblem::RootInternalInvariant),
            Self::BackupIncomplete(_) => sqlite(SqliteProblem::RootBackupIncomplete),
            Self::Cleanup { primary, failures } => {
                let mut report =
                    primary.shutdown_diagnostic_report_with_query(query_id.clone(), query_ordinal);
                for failure in failures {
                    report = report.with_related(
                        RelatedFailureRelation::Cleanup,
                        failure
                            .shutdown_diagnostic_report_with_query(query_id.clone(), query_ordinal),
                    );
                }
                report
            }
        }
    }

    /// SQLite 根在仍持有 rusqlite 闭集变体、数值代码和查询位置时建立报告。
    pub(crate) fn diagnostic_report(
        &self,
        database: &Path,
        context: SqliteDiagnosticContext,
        effect: StateEffect,
    ) -> DiagnosticReport {
        self.diagnostic_report_with_query(database, context, effect, None, None)
    }

    /// 调用方仍持有领域查询标识时，将它作为结构化位置附加到 SQLite 根报告。
    /// 无效的外部标识不会作为字符串协议进入公开诊断。
    pub(crate) fn diagnostic_report_for_query(
        &self,
        database: &Path,
        context: SqliteDiagnosticContext,
        effect: StateEffect,
        query_id: &str,
        query_ordinal: Option<usize>,
    ) -> DiagnosticReport {
        self.diagnostic_report_with_query(
            database,
            context,
            effect,
            SafeIdentifier::new(query_id).ok(),
            query_ordinal,
        )
    }

    fn diagnostic_report_with_query(
        &self,
        database: &Path,
        context: SqliteDiagnosticContext,
        effect: StateEffect,
        query_id: Option<SafeIdentifier>,
        query_ordinal: Option<usize>,
    ) -> DiagnosticReport {
        let database = SafePath::new(database);
        let report = |problem| {
            DiagnosticReport::new(
                effect,
                Diagnostic::sqlite(SqliteIssue::new(context, problem)),
            )
        };
        match self {
            Self::Closed => report(SqliteProblem::ExecutorClosed { database }),
            Self::AvailableParallelism { source } => report(SqliteProblem::Io {
                database,
                failure: IoFailure::from_error(source),
            }),
            Self::Cancelled { .. } => report(SqliteProblem::Cancelled { database }),
            Self::InteractiveSessionAlreadyOpen => {
                report(SqliteProblem::InteractiveSessionAlreadyOpen { database })
            }
            Self::WorkerSpawn { source, .. } => report(SqliteProblem::WorkerStart {
                database,
                failure: IoFailure::from_error(source),
            }),
            Self::WorkerPanicked(_) => report(SqliteProblem::WorkerPanicked { database }),
            Self::Io { source, .. } => report(SqliteProblem::Io {
                database,
                failure: IoFailure::from_error(source),
            }),
            Self::WindowsFileSystem { source, .. } => match source {
                WindowsFsError::Io { source, .. } => report(SqliteProblem::Io {
                    database,
                    failure: IoFailure::from_error(source),
                }),
                _ => report(SqliteProblem::InvalidTarget { database }),
            },
            Self::Driver { source, .. } => report(SqliteProblem::Driver {
                database,
                query_id,
                query_ordinal,
                failure: sqlite_driver_failure(source),
            }),
            Self::QueryContext {
                query_id: id,
                ordinal,
                source,
            } => source.diagnostic_report_with_query(
                Path::new(database.as_str()),
                context,
                effect,
                SafeIdentifier::new(id).ok(),
                Some(*ordinal),
            ),
            Self::InvalidTarget { .. } => report(SqliteProblem::InvalidTarget { database }),
            Self::UnexpectedArtifact { .. } => {
                report(SqliteProblem::UnexpectedArtifact { database })
            }
            Self::InvalidValue(_) => report(SqliteProblem::InvalidValue { database }),
            Self::Internal(_) => report(SqliteProblem::InternalInvariant { database }),
            Self::BackupIncomplete(_) => report(SqliteProblem::BackupIncomplete { database }),
            Self::Cleanup { primary, failures } => {
                let mut result = primary.diagnostic_report_with_query(
                    Path::new(database.as_str()),
                    context,
                    effect,
                    query_id.clone(),
                    query_ordinal,
                );
                for failure in failures {
                    result = result.with_related(
                        RelatedFailureRelation::Cleanup,
                        failure.diagnostic_report_with_query(
                            Path::new(database.as_str()),
                            context.with_operation(SqliteOperation::Cleanup),
                            effect,
                            query_id.clone(),
                            query_ordinal,
                        ),
                    );
                }
                result
            }
        }
    }

    fn driver(operation: &'static str, source: rusqlite::Error) -> Self {
        if sqlite_busy_wait_cancelled()
            && matches!(
                source.sqlite_error_code(),
                Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
            )
        {
            return Self::Cancelled { operation };
        }
        Self::Driver { operation, source }
    }

    fn io(operation: &'static str, path: &Path, source: io::Error) -> Self {
        Self::Io {
            operation,
            path: path.to_path_buf(),
            source,
        }
    }

    fn windows_file_system(operation: &'static str, path: &Path, source: WindowsFsError) -> Self {
        Self::WindowsFileSystem {
            operation,
            path: path.to_path_buf(),
            source,
        }
    }

    fn query_context(query: &SqliteQuery, ordinal: usize, source: Self) -> Self {
        Self::QueryContext {
            query_id: query.id().to_owned(),
            ordinal,
            source: Box::new(source),
        }
    }
}

impl fmt::Display for SqliteRuntimeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Closed => formatter.write_str("SQLite 存储根已经关闭"),
            Self::AvailableParallelism { source } => {
                write!(formatter, "无法探测 SQLite 可用并行度：{source}")
            }
            Self::Cancelled { operation } => {
                write!(formatter, "SQLite {operation} 在等待数据库锁时被取消")
            }
            Self::InteractiveSessionAlreadyOpen => {
                formatter.write_str("当前 SQLite 存储根已有活动交互会话")
            }
            Self::WorkerSpawn { worker, source } => {
                write!(formatter, "无法启动 SQLite 工作线程 {worker}：{source}")
            }
            Self::WorkerPanicked(worker) => {
                write!(formatter, "SQLite 工作线程 {worker} 发生 panic")
            }
            Self::Io {
                operation,
                path,
                source,
            } => write!(
                formatter,
                "SQLite {operation} {} 失败：{source}",
                path.display()
            ),
            Self::WindowsFileSystem {
                operation,
                path,
                source,
            } => write!(
                formatter,
                "SQLite {operation} {} 失败：{source}",
                path.display()
            ),
            Self::Driver { operation, source } => {
                write!(formatter, "SQLite {operation} 失败：{source}")
            }
            Self::QueryContext {
                query_id,
                ordinal,
                source,
            } => write!(
                formatter,
                "SQLite 查询 {query_id}（序号 {ordinal}）失败：{source}"
            ),
            Self::InvalidTarget { path } => {
                write!(formatter, "SQLite 目标不是普通文件：{}", path.display())
            }
            Self::UnexpectedArtifact { path } => write!(
                formatter,
                "SQLite 新建数据库之前已存在未归属本次操作的伴生文件：{}",
                path.display()
            ),
            Self::InvalidValue(reason) => write!(formatter, "SQLite 值无效：{reason}"),
            Self::Internal(reason) => write!(formatter, "SQLite 内部不变量破坏：{reason}"),
            Self::BackupIncomplete(state) => {
                write!(formatter, "SQLite online backup 未完成：{state}")
            }
            Self::Cleanup { primary, failures } => {
                write!(formatter, "{primary}；清理失败")?;
                for failure in failures {
                    write!(formatter, "；{failure}")?;
                }
                Ok(())
            }
        }
    }
}

impl Error for SqliteRuntimeError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::AvailableParallelism { source }
            | Self::WorkerSpawn { source, .. }
            | Self::Io { source, .. } => Some(source),
            Self::WindowsFileSystem { source, .. } => Some(source),
            Self::Driver { source, .. } => Some(source),
            Self::QueryContext { source, .. } => Some(source),
            Self::Cleanup { primary, .. } => Some(primary),
            Self::Closed
            | Self::Cancelled { .. }
            | Self::InteractiveSessionAlreadyOpen
            | Self::WorkerPanicked(_)
            | Self::InvalidTarget { .. }
            | Self::UnexpectedArtifact { .. }
            | Self::InvalidValue(_)
            | Self::Internal(_)
            | Self::BackupIncomplete(_) => None,
        }
    }
}

fn sqlite_driver_failure(source: &rusqlite::Error) -> SqliteDriverFailure {
    let (kind, column_index, column_name, parameter_actual, parameter_expected, changed_rows) =
        match source {
            rusqlite::Error::SqliteFailure(_, _) => (
                SqliteDriverKind::SqliteFailure,
                None,
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::SqliteSingleThreadedMode => (
                SqliteDriverKind::SingleThreadedMode,
                None,
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::FromSqlConversionFailure(index, _, _) => (
                SqliteDriverKind::ColumnConversion,
                Some(*index),
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::IntegralValueOutOfRange(index, _) => (
                SqliteDriverKind::IntegralOutOfRange,
                Some(*index),
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::Utf8Error(index, _) => (
                SqliteDriverKind::InvalidUtf8,
                Some(*index),
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::NulError(_) => {
                (SqliteDriverKind::EmbeddedNul, None, None, None, None, None)
            }
            rusqlite::Error::InvalidParameterName(_) => (
                SqliteDriverKind::InvalidParameterName,
                None,
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::InvalidPath(_) => {
                (SqliteDriverKind::InvalidPath, None, None, None, None, None)
            }
            rusqlite::Error::ExecuteReturnedResults => (
                SqliteDriverKind::ExecuteReturnedRows,
                None,
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::QueryReturnedNoRows => {
                (SqliteDriverKind::NoRows, None, None, None, None, None)
            }
            rusqlite::Error::QueryReturnedMoreThanOneRow => {
                (SqliteDriverKind::MultipleRows, None, None, None, None, None)
            }
            rusqlite::Error::InvalidColumnIndex(index) => (
                SqliteDriverKind::InvalidColumnIndex,
                Some(*index),
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::InvalidColumnName(name) => (
                SqliteDriverKind::InvalidColumnName,
                None,
                SafeIdentifier::new(name).ok(),
                None,
                None,
                None,
            ),
            rusqlite::Error::InvalidColumnType(index, name, _) => (
                SqliteDriverKind::InvalidColumnType,
                Some(*index),
                SafeIdentifier::new(name).ok(),
                None,
                None,
                None,
            ),
            rusqlite::Error::StatementChangedRows(actual) => (
                SqliteDriverKind::UnexpectedChangedRows,
                None,
                None,
                None,
                None,
                Some(*actual),
            ),
            rusqlite::Error::ToSqlConversionFailure(_) => (
                SqliteDriverKind::ParameterConversion,
                None,
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::InvalidQuery => {
                (SqliteDriverKind::InvalidQuery, None, None, None, None, None)
            }
            rusqlite::Error::UnwindingPanic => (
                SqliteDriverKind::CallbackPanicked,
                None,
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::MultipleStatement => (
                SqliteDriverKind::MultipleStatements,
                None,
                None,
                None,
                None,
                None,
            ),
            rusqlite::Error::InvalidParameterCount(actual, expected) => (
                SqliteDriverKind::InvalidParameterCount,
                None,
                None,
                Some(*actual),
                Some(*expected),
                None,
            ),
            rusqlite::Error::SqlInputError { .. } => {
                (SqliteDriverKind::SqlInput, None, None, None, None, None)
            }
            rusqlite::Error::InvalidDatabaseIndex(_) => (
                SqliteDriverKind::InvalidDatabaseIndex,
                None,
                None,
                None,
                None,
                None,
            ),
            _ => (
                SqliteDriverKind::RusqliteNonSqliteFailure,
                None,
                None,
                None,
                None,
                None,
            ),
        };
    let (primary_code, extended_code) = source.sqlite_error().map_or((None, None), |error| {
        (Some(error.extended_code & 0xff), Some(error.extended_code))
    });
    SqliteDriverFailure {
        kind,
        primary_code,
        extended_code,
        column_index,
        column_name,
        parameter_actual,
        parameter_expected,
        changed_rows,
        sql_offset: match source {
            rusqlite::Error::SqlInputError { offset, .. } => Some(*offset),
            _ => None,
        },
        database_index: match source {
            rusqlite::Error::InvalidDatabaseIndex(index) => Some(*index),
            _ => None,
        },
    }
}

fn validate_statement(
    statement: &str,
    _config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    if statement.trim().is_empty() {
        return Err(SqliteRuntimeError::InvalidValue("statement 不得为空"));
    }
    Ok(())
}

fn validate_sqlite_value(value: &SqliteValue) -> Result<(), SqliteRuntimeError> {
    match value {
        SqliteValue::Null
        | SqliteValue::Integer(_)
        | SqliteValue::Text(_)
        | SqliteValue::Blob(_) => Ok(()),
        SqliteValue::Real(number) => {
            if !number.is_finite() {
                Err(SqliteRuntimeError::InvalidValue("REAL 参数必须是有限数值"))
            } else {
                Ok(())
            }
        }
    }
}

fn validate_parameters(
    parameters: &[SqliteValue],
    _config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    for value in parameters {
        validate_sqlite_value(value)?;
    }
    Ok(())
}

fn validate_command(
    command: &SqliteCommand,
    config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    validate_statement(command.statement(), config)?;
    validate_parameters(command.parameters(), config)
}

fn validate_query(
    query: &SqliteQuery,
    config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    validate_statement(query.statement(), config)?;
    validate_parameters(query.parameters(), config)
}

fn validate_query_snapshot(
    queries: &[SqliteQuery],
    config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    if queries.is_empty() {
        return Err(SqliteRuntimeError::InvalidValue("只读快照查询集合不得为空"));
    }
    for query in queries {
        validate_query(query, config)?;
    }
    Ok(())
}

impl ToSql for SqliteValue {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        Ok(ToSqlOutput::Borrowed(match self {
            SqliteValue::Null => ValueRef::Null,
            SqliteValue::Integer(value) => ValueRef::Integer(*value),
            SqliteValue::Real(value) => ValueRef::Real(*value),
            SqliteValue::Text(value) => ValueRef::Text(value.as_bytes()),
            SqliteValue::Blob(value) => ValueRef::Blob(value),
        }))
    }
}

fn preflight_existing_file(path: &Path) -> Result<(), ExistingFileError> {
    match fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => Ok(()),
        Ok(_) => Err(ExistingFileError::InvalidTarget),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Err(ExistingFileError::NotFound),
        Err(error) => Err(ExistingFileError::Io(error)),
    }
}

enum ExistingFileError {
    NotFound,
    InvalidTarget,
    Io(io::Error),
}

fn open_existing_read_only(
    path: &Path,
    _config: &RusqliteStorageConfiguration,
) -> Result<Connection, ExistingFileErrorOrRuntime> {
    preflight_existing_file(path).map_err(ExistingFileErrorOrRuntime::Existing)?;
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|source| {
        if matches!(fs::metadata(path), Err(error) if error.kind() == io::ErrorKind::NotFound) {
            ExistingFileErrorOrRuntime::Existing(ExistingFileError::NotFound)
        } else {
            ExistingFileErrorOrRuntime::Runtime(SqliteRuntimeError::driver(
                "以只读方式打开数据库",
                source,
            ))
        }
    })?;
    connection
        .busy_handler(Some(wait_for_sqlite_unlock))
        .map_err(|source| {
            ExistingFileErrorOrRuntime::Runtime(SqliteRuntimeError::driver(
                "设置 SQLite 繁忙等待处理器",
                source,
            ))
        })?;
    apply_connection_memory_policy(&connection).map_err(ExistingFileErrorOrRuntime::Runtime)?;
    connection
        .pragma_update(None, "foreign_keys", true)
        .map_err(|source| {
            ExistingFileErrorOrRuntime::Runtime(SqliteRuntimeError::driver(
                "启用只读连接外键约束",
                source,
            ))
        })?;
    connection
        .pragma_update(None, "query_only", true)
        .map_err(|source| {
            ExistingFileErrorOrRuntime::Runtime(SqliteRuntimeError::driver("设置只读连接", source))
        })?;
    connection
        .pragma_update(None, "synchronous", "FULL")
        .map_err(|source| {
            ExistingFileErrorOrRuntime::Runtime(SqliteRuntimeError::driver(
                "设置只读 synchronous",
                source,
            ))
        })?;
    Ok(connection)
}

fn open_existing_read_write(
    path: &Path,
    config: &RusqliteStorageConfiguration,
) -> Result<Connection, ExistingFileErrorOrRuntime> {
    preflight_existing_file(path).map_err(ExistingFileErrorOrRuntime::Existing)?;
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|source| {
        if matches!(fs::metadata(path), Err(error) if error.kind() == io::ErrorKind::NotFound) {
            ExistingFileErrorOrRuntime::Existing(ExistingFileError::NotFound)
        } else {
            ExistingFileErrorOrRuntime::Runtime(SqliteRuntimeError::driver(
                "以读写方式打开数据库",
                source,
            ))
        }
    })?;
    apply_read_write_policy(&connection, config).map_err(ExistingFileErrorOrRuntime::Runtime)?;
    Ok(connection)
}

enum ExistingFileErrorOrRuntime {
    Existing(ExistingFileError),
    Runtime(SqliteRuntimeError),
}

fn apply_read_write_policy(
    connection: &Connection,
    _config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    connection
        .busy_handler(Some(wait_for_sqlite_unlock))
        .map_err(|source| SqliteRuntimeError::driver("设置 SQLite 繁忙等待处理器", source))?;
    apply_att_sqlite_read_write_policy(connection)
        .map_err(|source| SqliteRuntimeError::driver("应用 ATT SQLite 读写策略", source))
}

fn apply_connection_memory_policy(connection: &Connection) -> Result<(), SqliteRuntimeError> {
    apply_att_sqlite_connection_memory_policy(connection)
        .map_err(|source| SqliteRuntimeError::driver("应用 ATT SQLite 连接内存策略", source))
}

/// 对共用 SQLite 连接应用已经过大型样本验证的页缓存和 TEMP 存储策略。
pub(crate) fn apply_att_sqlite_connection_memory_policy(
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    connection.pragma_update(None, "cache_size", CONNECTION_CACHE_SIZE_KIB)?;
    connection.pragma_update(None, "temp_store", "MEMORY")?;
    Ok(())
}

fn apply_new_database_page_policy(connection: &Connection) -> Result<(), SqliteRuntimeError> {
    apply_att_sqlite_new_database_page_policy(connection)
        .map_err(|source| SqliteRuntimeError::driver("设置新数据库 page_size", source))
}

/// 对读写连接应用 ATT 的统一缓存、约束、日志和持久化策略。
pub(crate) fn apply_att_sqlite_read_write_policy(
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    apply_att_sqlite_read_write_policy_with_cancellation(connection, sqlite_busy_wait_cancelled)
}

fn apply_att_sqlite_read_write_policy_with_cancellation(
    connection: &Connection,
    is_cancelled: impl Fn() -> bool,
) -> Result<(), rusqlite::Error> {
    apply_att_sqlite_connection_memory_policy(connection)?;
    connection.pragma_update(None, "foreign_keys", true)?;
    apply_att_sqlite_wal_policy_with_cancellation(connection, &is_cancelled)?;
    connection.pragma_update(None, "synchronous", "FULL")?;
    Ok(())
}

fn apply_att_sqlite_wal_policy_with_cancellation(
    connection: &Connection,
    is_cancelled: &impl Fn() -> bool,
) -> Result<(), rusqlite::Error> {
    loop {
        let result = connection
            .query_row("PRAGMA journal_mode", [], |row| row.get::<_, String>(0))
            .and_then(|journal_mode| {
                if journal_mode.eq_ignore_ascii_case("wal") {
                    Ok(())
                } else {
                    connection.pragma_update(None, "journal_mode", "WAL")
                }
            });
        match result {
            Err(source)
                if source.sqlite_error_code() == Some(ErrorCode::DatabaseBusy)
                    && !is_cancelled() =>
            {
                // `PRAGMA journal_mode` 在 WAL 数据库已有写事务时可能不调用连接的
                // busy handler，而是直接返回 SQLITE_BUSY。这里补上与 busy handler
                // 相同的无固定期限、可取消等待，避免连接初始化提前失败。
                thread::sleep(SQLITE_WAIT_POLL_INTERVAL);
            }
            result => return result,
        }
    }
}

/// 为直接使用 `rusqlite` 的领域连接安装无固定超时、可由调用方取消的繁忙等待。
///
/// busy 与 progress handler 共享连接持有的稳定取消上下文；同一线程上的其他连接不会
/// 覆盖它，连接移动到其他线程后也仍读取自己的取消事实。
pub(crate) fn apply_att_sqlite_cancellable_read_write_policy(
    connection: Connection,
    is_cancelled: impl Fn() -> bool + Send + Sync + 'static,
) -> Result<AttSqliteCancellableConnection, rusqlite::Error> {
    let probe: AttSqliteCancellationProbe = Arc::new(is_cancelled);
    let cancellation = Arc::new(AttSqliteCancellationContext::new(probe));
    if let Err(source) = install_att_sqlite_busy_handler(&connection, &cancellation) {
        let _ = connection.busy_handler(None);
        drop(connection);
        return Err(source);
    }
    let progress_cancellation = Arc::clone(&cancellation);
    if let Err(source) = connection.progress_handler(
        SQLITE_CANCELLATION_CHECK_OPERATIONS,
        Some(move || progress_cancellation.is_cancelled()),
    ) {
        let _ = connection.busy_handler(None);
        drop(connection);
        return Err(source);
    }
    if let Err(source) = apply_att_sqlite_read_write_policy_with_cancellation(&connection, || {
        cancellation.is_cancelled()
    }) {
        let _ = connection.progress_handler(0, None::<fn() -> bool>);
        let _ = connection.busy_handler(None);
        drop(connection);
        return Err(source);
    }
    Ok(AttSqliteCancellableConnection {
        connection: Some(connection),
        cancellation,
    })
}

/// 暂停一条直接连接的 busy/progress 取消检查，直到最后一个嵌套 guard 销毁。
pub(crate) fn suspend_att_sqlite_cancellation(
    cancellation: &AttSqliteCancellationHandle,
) -> AttSqliteCancellationSuspension {
    let context = Arc::clone(&cancellation.context);
    context.suspend();
    AttSqliteCancellationSuspension { context }
}

/// 在新数据库创建首个表和进入 WAL 模式前设置统一物理页大小。
pub(crate) fn apply_att_sqlite_new_database_page_policy(
    connection: &Connection,
) -> Result<(), rusqlite::Error> {
    connection.pragma_update(None, "page_size", NEW_DATABASE_PAGE_SIZE_BYTES)
}

fn wait_for_sqlite_unlock(_previous_attempts: i32) -> bool {
    if sqlite_busy_wait_cancelled() {
        return false;
    }
    thread::sleep(SQLITE_WAIT_POLL_INTERVAL);
    !sqlite_busy_wait_cancelled()
}

fn sqlite_busy_wait_cancelled() -> bool {
    SQLITE_BUSY_CANCELLATION.with(|cancellation| {
        cancellation
            .borrow()
            .as_ref()
            .is_some_and(SqliteWaitCancellation::is_requested)
    })
}

fn install_sqlite_busy_cancellation(cancellation: SqliteWaitCancellation) {
    SQLITE_BUSY_CANCELLATION.with(|slot| {
        *slot.borrow_mut() = Some(cancellation);
    });
}

fn read_query_rows_unchecked(
    connection: &Connection,
    query: &SqliteQuery,
) -> Result<Vec<SqliteRow>, SqliteRuntimeError> {
    let mut statement = connection
        .prepare(query.statement())
        .map_err(|source| SqliteRuntimeError::driver("准备查询", source))?;
    read_prepared_query_rows(&mut statement, query.parameters())
}

fn read_prepared_query_rows(
    statement: &mut Statement<'_>,
    parameters: &[SqliteValue],
) -> Result<Vec<SqliteRow>, SqliteRuntimeError> {
    let column_count = statement.column_count();
    let mut cursor = statement
        .query(params_from_iter(parameters.iter()))
        .map_err(|source| SqliteRuntimeError::driver("绑定查询参数", source))?;
    let mut result = Vec::new();

    while let Some(row) = cursor
        .next()
        .map_err(|source| SqliteRuntimeError::driver("读取查询行", source))?
    {
        result.push(own_sqlite_row(row, column_count)?);
    }
    Ok(result)
}

fn own_sqlite_row(
    row: &rusqlite::Row<'_>,
    column_count: usize,
) -> Result<SqliteRow, SqliteRuntimeError> {
    let mut values = Vec::with_capacity(column_count);
    for index in 0..column_count {
        let value = match row
            .get_ref(index)
            .map_err(|source| SqliteRuntimeError::driver("读取查询列", source))?
        {
            ValueRef::Null => SqliteValue::Null,
            ValueRef::Integer(value) => SqliteValue::Integer(value),
            ValueRef::Real(value) => SqliteValue::Real(value),
            ValueRef::Text(value) => SqliteValue::Text(
                std::str::from_utf8(value)
                    .map_err(|_| SqliteRuntimeError::InvalidValue("TEXT 不是 UTF-8"))?
                    .to_owned(),
            ),
            ValueRef::Blob(value) => SqliteValue::Blob(value.to_vec()),
        };
        values.push(value);
    }
    Ok(SqliteRow::new(values))
}

fn read_query_rows(
    connection: &Connection,
    query: &SqliteQuery,
    config: &RusqliteStorageConfiguration,
) -> Result<Vec<SqliteRow>, SqliteRuntimeError> {
    validate_query(query, config)?;
    read_query_rows_unchecked(connection, query)
}

fn run_query_existing(
    path: &Path,
    query: &SqliteQuery,
    config: &RusqliteStorageConfiguration,
) -> Result<Vec<SqliteRow>, QueryExistingDatabaseError<SqliteRuntimeError>> {
    let connection =
        open_existing_read_only(path, config).map_err(|error| map_query_open_error(path, error))?;
    read_query_rows(&connection, query, config).map_err(QueryExistingDatabaseError::QueryFailed)
}

fn map_query_open_error(
    path: &Path,
    error: ExistingFileErrorOrRuntime,
) -> QueryExistingDatabaseError<SqliteRuntimeError> {
    match error {
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::NotFound) => {
            QueryExistingDatabaseError::NotFound
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::InvalidTarget) => {
            QueryExistingDatabaseError::QueryFailed(SqliteRuntimeError::InvalidTarget {
                path: path.to_path_buf(),
            })
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::Io(source)) => {
            QueryExistingDatabaseError::QueryFailed(SqliteRuntimeError::io(
                "检查数据库",
                path,
                source,
            ))
        }
        ExistingFileErrorOrRuntime::Runtime(source) => {
            QueryExistingDatabaseError::QueryFailed(source)
        }
    }
}

pub(crate) fn execute_transaction_control(
    connection: &Connection,
    performance: &RunPerformanceCounters,
    scope: SqliteTransactionScope,
    control: SqliteTransactionControl,
    statement: &'static str,
) -> rusqlite::Result<()> {
    performance.sqlite_control_attempted(scope, control);
    let result = connection.execute_batch(statement);
    if result.is_ok() {
        performance.sqlite_control_succeeded(scope, control);
    }
    result
}

pub(crate) fn begin_cancellable_transaction<'connection>(
    connection: &'connection mut AttSqliteCancellableConnection,
    performance: &RunPerformanceCounters,
    scope: SqliteTransactionScope,
) -> rusqlite::Result<Transaction<'connection>> {
    performance.sqlite_control_attempted(scope, SqliteTransactionControl::Begin);
    let result = connection.transaction();
    if result.is_ok() {
        performance.sqlite_control_succeeded(scope, SqliteTransactionControl::Begin);
    }
    result
}

fn rollback_query_snapshot(
    connection: &Connection,
    primary: SqliteRuntimeError,
    performance: &RunPerformanceCounters,
    scope: SqliteTransactionScope,
) -> SqliteRuntimeError {
    if connection.is_autocommit() {
        return primary;
    }
    match execute_transaction_control(
        connection,
        performance,
        scope,
        SqliteTransactionControl::Rollback,
        "ROLLBACK",
    ) {
        Ok(()) => primary,
        Err(source) => SqliteRuntimeError::Cleanup {
            primary: Box::new(primary),
            failures: vec![SqliteRuntimeError::driver("回滚只读查询快照", source)],
        },
    }
}

fn read_queries_in_snapshot(
    connection: &Connection,
    queries: &[SqliteQuery],
    config: &RusqliteStorageConfiguration,
    performance: &RunPerformanceCounters,
    mut after_query: impl FnMut(usize),
) -> Result<Vec<Vec<SqliteRow>>, SqliteRuntimeError> {
    validate_query_snapshot(queries, config)?;
    execute_transaction_control(
        connection,
        performance,
        SqliteTransactionScope::ReadSnapshot,
        SqliteTransactionControl::Begin,
        "BEGIN",
    )
    .map_err(|source| SqliteRuntimeError::driver("开始只读查询快照", source))?;

    let mut results = Vec::with_capacity(queries.len());
    for (index, query) in queries.iter().enumerate() {
        match read_query_rows_unchecked(connection, query) {
            Ok(rows) => results.push(rows),
            Err(primary) => {
                let primary = SqliteRuntimeError::query_context(query, index, primary);
                return Err(rollback_query_snapshot(
                    connection,
                    primary,
                    performance,
                    SqliteTransactionScope::ReadSnapshot,
                ));
            }
        }
        after_query(index);
    }

    if let Err(source) = execute_transaction_control(
        connection,
        performance,
        SqliteTransactionScope::ReadSnapshot,
        SqliteTransactionControl::Commit,
        "COMMIT",
    ) {
        let primary = SqliteRuntimeError::driver("结束只读查询快照", source);
        return Err(rollback_query_snapshot(
            connection,
            primary,
            performance,
            SqliteTransactionScope::ReadSnapshot,
        ));
    }
    if !connection.is_autocommit() {
        return Err(rollback_query_snapshot(
            connection,
            SqliteRuntimeError::Internal("只读查询快照提交后仍非 autocommit"),
            performance,
            SqliteTransactionScope::ReadSnapshot,
        ));
    }
    Ok(results)
}

fn run_query_snapshot_existing(
    path: &Path,
    queries: &[SqliteQuery],
    config: &RusqliteStorageConfiguration,
    performance: &RunPerformanceCounters,
) -> Result<Vec<Vec<SqliteRow>>, QueryExistingDatabaseError<SqliteRuntimeError>> {
    validate_query_snapshot(queries, config).map_err(QueryExistingDatabaseError::QueryFailed)?;
    let connection =
        open_existing_read_only(path, config).map_err(|error| map_query_open_error(path, error))?;
    read_queries_in_snapshot(&connection, queries, config, performance, |_| {})
        .map_err(QueryExistingDatabaseError::QueryFailed)
}

fn validate_transaction_plan(
    plan: &SqliteTransactionPlan,
    config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    for step in plan.steps() {
        match step {
            SqliteTransactionStep::Execute(command) => validate_command(command, config)?,
            SqliteTransactionStep::ExecuteMany(batch) => validate_batch(batch, config)?,
            SqliteTransactionStep::ExecuteManyExactlyOne(batch) => {
                validate_non_bulk_batch(batch, config, "bulk INSERT 不支持 ExactlyOne 语义")?;
            }
            SqliteTransactionStep::RequireNoRowsMany(batch) => {
                validate_non_bulk_batch(batch, config, "bulk INSERT 不支持 Require 语义")?;
            }
            SqliteTransactionStep::RequireNoRows(query) => {
                validate_query(query, config)?;
            }
            SqliteTransactionStep::RequireNoRowsReturningFirstRow(query) => {
                validate_query(query, config)?;
            }
        }
    }
    Ok(())
}

fn validate_batch(
    batch: &SqliteBatch,
    config: &RusqliteStorageConfiguration,
) -> Result<(), SqliteRuntimeError> {
    validate_statement(batch.statement(), config)?;
    validate_parameters(batch.shared_parameters(), config)?;
    if let Some((statement_prefix, row_parameter_count, parameter_values)) =
        batch.bulk_insert_spec()
    {
        validate_statement(statement_prefix, config)?;
        if row_parameter_count == 0 {
            return Err(SqliteRuntimeError::Internal(
                "bulk INSERT 每行参数数量不得为零",
            ));
        }
        batch
            .shared_parameters()
            .len()
            .checked_add(row_parameter_count)
            .ok_or(SqliteRuntimeError::Internal("bulk INSERT 单行参数数量溢出"))?;
        if parameter_values.len() % row_parameter_count != 0 {
            return Err(SqliteRuntimeError::Internal(
                "bulk INSERT 扁平参数无法整除为完整行",
            ));
        }
        validate_parameters(parameter_values, config)?;
    } else {
        for parameters in batch.parameter_rows() {
            validate_parameters(parameters, config)?;
        }
    }
    Ok(())
}

fn validate_non_bulk_batch(
    batch: &SqliteBatch,
    config: &RusqliteStorageConfiguration,
    unsupported_reason: &'static str,
) -> Result<(), SqliteRuntimeError> {
    validate_batch(batch, config)?;
    if batch.bulk_insert_spec().is_some() {
        return Err(SqliteRuntimeError::Internal(unsupported_reason));
    }
    Ok(())
}

fn rollback_after_failure(
    connection: &Connection,
    primary: SqliteRuntimeError,
    performance: &RunPerformanceCounters,
    scope: SqliteTransactionScope,
) -> Result<SqliteRuntimeError, SqliteRuntimeError> {
    match execute_transaction_control(
        connection,
        performance,
        scope,
        SqliteTransactionControl::Rollback,
        "ROLLBACK",
    ) {
        Ok(()) if connection.is_autocommit() => Ok(primary),
        Ok(()) => Err(SqliteRuntimeError::Cleanup {
            primary: Box::new(primary),
            failures: vec![SqliteRuntimeError::Internal(
                "回滚返回成功后连接仍处于事务中",
            )],
        }),
        Err(source) => Err(SqliteRuntimeError::Cleanup {
            primary: Box::new(primary),
            failures: vec![SqliteRuntimeError::driver("回滚事务", source)],
        }),
    }
}

fn rollback_requirement_failure(
    connection: &Connection,
    performance: &RunPerformanceCounters,
    scope: SqliteTransactionScope,
) -> Result<(), ExecuteTransactionError<SqliteRuntimeError>> {
    match execute_transaction_control(
        connection,
        performance,
        scope,
        SqliteTransactionControl::Rollback,
        "ROLLBACK",
    ) {
        Ok(()) if connection.is_autocommit() => Err(ExecuteTransactionError::RequirementFailed),
        Ok(()) => Err(ExecuteTransactionError::OutcomeUnknown(
            SqliteRuntimeError::Internal("事务条件失败回滚后仍非 autocommit"),
        )),
        Err(source) => Err(ExecuteTransactionError::OutcomeUnknown(
            SqliteRuntimeError::driver("回滚事务条件失败", source),
        )),
    }
}

fn rollback_requirement_failure_with_row(
    connection: &Connection,
    query_id: String,
    row: SqliteRow,
    performance: &RunPerformanceCounters,
    scope: SqliteTransactionScope,
) -> Result<(), ExecuteTransactionError<SqliteRuntimeError>> {
    match execute_transaction_control(
        connection,
        performance,
        scope,
        SqliteTransactionControl::Rollback,
        "ROLLBACK",
    ) {
        Ok(()) if connection.is_autocommit() => {
            Err(ExecuteTransactionError::RequirementFailedWithRow { query_id, row })
        }
        Ok(()) => Err(
            ExecuteTransactionError::RequirementFailedWithRowOutcomeUnknown {
                query_id,
                row,
                source: Box::new(SqliteRuntimeError::Internal(
                    "事务条件失败回滚后仍非 autocommit",
                )),
            },
        ),
        Err(source) => Err(
            ExecuteTransactionError::RequirementFailedWithRowOutcomeUnknown {
                query_id,
                row,
                source: Box::new(SqliteRuntimeError::driver("回滚事务条件失败", source)),
            },
        ),
    }
}

fn run_transaction(
    path: &Path,
    plan: &SqliteTransactionPlan,
    config: &RusqliteStorageConfiguration,
    performance: &RunPerformanceCounters,
) -> Result<(), ExecuteTransactionError<SqliteRuntimeError>> {
    validate_transaction_plan(plan, config).map_err(ExecuteTransactionError::NotCommitted)?;
    let connection = open_transaction_connection(path, config)?;
    run_transaction_on_connection(&connection, plan, performance)
}

fn open_transaction_connection(
    path: &Path,
    config: &RusqliteStorageConfiguration,
) -> Result<Connection, ExecuteTransactionError<SqliteRuntimeError>> {
    open_existing_read_write(path, config).map_err(|error| match error {
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::NotFound) => {
            ExecuteTransactionError::NotFound
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::InvalidTarget) => {
            ExecuteTransactionError::NotCommitted(SqliteRuntimeError::InvalidTarget {
                path: path.to_path_buf(),
            })
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::Io(source)) => {
            ExecuteTransactionError::NotCommitted(SqliteRuntimeError::io(
                "检查数据库",
                path,
                source,
            ))
        }
        ExistingFileErrorOrRuntime::Runtime(source) => {
            ExecuteTransactionError::NotCommitted(source)
        }
    })
}

fn bind_batch_parameters(
    statement: &mut Statement<'_>,
    first_index: usize,
    parameters: &[SqliteValue],
    operation: &'static str,
) -> Result<(), SqliteRuntimeError> {
    for (offset, parameter) in parameters.iter().enumerate() {
        let index = first_index
            .checked_add(offset)
            .ok_or(SqliteRuntimeError::Internal("SQLite 参数编号溢出"))?;
        statement
            .raw_bind_parameter(index, parameter)
            .map_err(|source| SqliteRuntimeError::driver(operation, source))?;
    }
    Ok(())
}

fn bind_batch_shared_parameters(
    statement: &mut Statement<'_>,
    batch: &SqliteBatch,
    operation: &'static str,
) -> Result<usize, SqliteRuntimeError> {
    let shared_count = batch.shared_parameters().len();
    let parameter_count = statement.parameter_count();
    if shared_count > parameter_count {
        return Err(SqliteRuntimeError::driver(
            operation,
            rusqlite::Error::InvalidParameterCount(shared_count, parameter_count),
        ));
    }
    bind_batch_parameters(statement, 1, batch.shared_parameters(), operation)?;
    shared_count
        .checked_add(1)
        .ok_or(SqliteRuntimeError::Internal("SQLite 参数编号溢出"))
}

fn bind_batch_row_parameters(
    statement: &mut Statement<'_>,
    first_index: usize,
    parameters: &[SqliteValue],
    operation: &'static str,
) -> Result<(), SqliteRuntimeError> {
    let shared_count = first_index
        .checked_sub(1)
        .ok_or(SqliteRuntimeError::Internal("SQLite 参数编号无效"))?;
    let provided = shared_count
        .checked_add(parameters.len())
        .ok_or(SqliteRuntimeError::Internal("SQLite 参数数量溢出"))?;
    let expected = statement.parameter_count();
    if provided != expected {
        return Err(SqliteRuntimeError::driver(
            operation,
            rusqlite::Error::InvalidParameterCount(provided, expected),
        ));
    }
    bind_batch_parameters(statement, first_index, parameters, operation)
}

fn sqlite_variable_limit(connection: &Connection) -> Result<usize, SqliteRuntimeError> {
    let limit = connection
        .limit(Limit::SQLITE_LIMIT_VARIABLE_NUMBER)
        .map_err(|source| SqliteRuntimeError::driver("读取 SQLite 变量上限", source))?;
    usize::try_from(limit)
        .ok()
        .filter(|limit| *limit > 0)
        .ok_or(SqliteRuntimeError::Internal("SQLite 变量上限必须为正整数"))
}

fn bulk_insert_rows_per_statement(
    variable_limit: usize,
    shared_parameter_count: usize,
    row_parameter_count: usize,
) -> Result<usize, SqliteRuntimeError> {
    if row_parameter_count == 0 {
        return Err(SqliteRuntimeError::Internal(
            "bulk INSERT 每行参数数量不得为零",
        ));
    }
    let single_row_parameter_count = shared_parameter_count
        .checked_add(row_parameter_count)
        .ok_or(SqliteRuntimeError::Internal("bulk INSERT 单行参数数量溢出"))?;
    let available = variable_limit.saturating_sub(shared_parameter_count);
    let rows = available / row_parameter_count;
    if rows == 0 {
        return Err(SqliteRuntimeError::driver(
            "规划 bulk INSERT 变量分块",
            rusqlite::Error::InvalidParameterCount(single_row_parameter_count, variable_limit),
        ));
    }
    Ok(rows)
}

fn build_bulk_insert_statement(
    statement_prefix: &str,
    shared_parameter_count: usize,
    row_parameter_count: usize,
    row_count: usize,
) -> Result<String, SqliteRuntimeError> {
    if row_count == 0 {
        return Err(SqliteRuntimeError::Internal(
            "bulk INSERT 语句不得包含零个 VALUES 元组",
        ));
    }
    let tuple_parameter_count = shared_parameter_count
        .checked_add(row_parameter_count)
        .ok_or(SqliteRuntimeError::Internal("bulk INSERT 单行参数数量溢出"))?;
    let mut statement = String::with_capacity(
        statement_prefix
            .len()
            .saturating_add(
                row_count
                    .saturating_mul(tuple_parameter_count)
                    .saturating_mul(8),
            )
            .saturating_add(row_count.saturating_mul(4))
            .saturating_add(8),
    );
    statement.push_str(statement_prefix);
    statement.push_str(" VALUES ");
    for row_index in 0..row_count {
        if row_index > 0 {
            statement.push_str(", ");
        }
        statement.push('(');
        for tuple_index in 0..tuple_parameter_count {
            if tuple_index > 0 {
                statement.push_str(", ");
            }
            let parameter_index = if tuple_index < shared_parameter_count {
                tuple_index + 1
            } else {
                let row_parameter_index = tuple_index - shared_parameter_count;
                shared_parameter_count
                    .checked_add(
                        row_index
                            .checked_mul(row_parameter_count)
                            .ok_or(SqliteRuntimeError::Internal("bulk INSERT 参数编号溢出"))?,
                    )
                    .and_then(|index| index.checked_add(row_parameter_index))
                    .and_then(|index| index.checked_add(1))
                    .ok_or(SqliteRuntimeError::Internal("bulk INSERT 参数编号溢出"))?
            };
            write!(&mut statement, "?{parameter_index}")
                .expect("写入 String 的 bulk INSERT 参数编号不得失败");
        }
        statement.push(')');
    }
    Ok(statement)
}

fn execute_bulk_insert(
    connection: &Connection,
    batch: &SqliteBatch,
) -> Result<usize, SqliteRuntimeError> {
    let Some((statement_prefix, row_parameter_count, parameter_values)) = batch.bulk_insert_spec()
    else {
        return Err(SqliteRuntimeError::Internal(
            "非 bulk 批次不得进入 bulk INSERT 执行器",
        ));
    };
    if parameter_values.is_empty() {
        return Ok(0);
    }

    let shared_parameter_count = batch.shared_parameters().len();
    let rows_per_statement = bulk_insert_rows_per_statement(
        sqlite_variable_limit(connection)?,
        shared_parameter_count,
        row_parameter_count,
    )?;
    let row_count = parameter_values.len() / row_parameter_count;
    let regular_row_count = rows_per_statement.min(row_count);
    let regular_statement = build_bulk_insert_statement(
        statement_prefix,
        shared_parameter_count,
        row_parameter_count,
        regular_row_count,
    )?;
    let mut executed_statements = 0_usize;
    let values_per_statement = rows_per_statement
        .checked_mul(row_parameter_count)
        .ok_or(SqliteRuntimeError::Internal("bulk INSERT 分块参数数量溢出"))?;
    for parameter_chunk in parameter_values.chunks(values_per_statement) {
        let chunk_row_count = parameter_chunk.len() / row_parameter_count;
        let tail_statement = (chunk_row_count != regular_row_count)
            .then(|| {
                build_bulk_insert_statement(
                    statement_prefix,
                    shared_parameter_count,
                    row_parameter_count,
                    chunk_row_count,
                )
            })
            .transpose()?;
        let sql = tail_statement.as_deref().unwrap_or(&regular_statement);
        let mut statement = connection
            .prepare_cached(sql)
            .map_err(|source| SqliteRuntimeError::driver("准备 bulk INSERT", source))?;
        statement
            .execute(params_from_iter(
                batch
                    .shared_parameters()
                    .iter()
                    .chain(parameter_chunk.iter()),
            ))
            .map_err(|source| SqliteRuntimeError::driver("执行 bulk INSERT", source))?;
        executed_statements = executed_statements
            .checked_add(1)
            .ok_or(SqliteRuntimeError::Internal("bulk INSERT 批次数量溢出"))?;
    }
    Ok(executed_statements)
}

pub(crate) fn run_transaction_on_connection(
    connection: &Connection,
    plan: &SqliteTransactionPlan,
    performance: &RunPerformanceCounters,
) -> Result<(), ExecuteTransactionError<SqliteRuntimeError>> {
    execute_transaction_control(
        connection,
        performance,
        SqliteTransactionScope::WritePlan,
        SqliteTransactionControl::Begin,
        "BEGIN IMMEDIATE",
    )
    .map_err(|source| {
        ExecuteTransactionError::NotCommitted(SqliteRuntimeError::driver("开始写事务", source))
    })?;

    for step in plan.steps() {
        let mut failure_row = None;
        let requirement_satisfied = match step {
            SqliteTransactionStep::Execute(command) => (|| {
                let mut statement = connection
                    .prepare_cached(command.statement())
                    .map_err(|source| SqliteRuntimeError::driver("准备事务命令", source))?;
                statement
                    .execute(params_from_iter(command.parameters().iter()))
                    .map(|_| true)
                    .map_err(|source| SqliteRuntimeError::driver("执行事务命令", source))
            })(),
            SqliteTransactionStep::ExecuteMany(batch) => (|| {
                if batch.bulk_insert_spec().is_some() {
                    execute_bulk_insert(connection, batch)?;
                    return Ok(true);
                }
                let mut statement = connection
                    .prepare_cached(batch.statement())
                    .map_err(|source| SqliteRuntimeError::driver("准备批量命令", source))?;
                let first_row_parameter =
                    bind_batch_shared_parameters(&mut statement, batch, "绑定批量命令公共参数")?;
                for parameters in batch.parameter_rows() {
                    bind_batch_row_parameters(
                        &mut statement,
                        first_row_parameter,
                        parameters,
                        "绑定批量命令参数",
                    )?;
                    statement
                        .raw_execute()
                        .map_err(|source| SqliteRuntimeError::driver("执行批量命令", source))?;
                }
                Ok(true)
            })(),
            SqliteTransactionStep::ExecuteManyExactlyOne(batch) => (|| {
                let mut statement = connection
                    .prepare_cached(batch.statement())
                    .map_err(|source| SqliteRuntimeError::driver("准备精确批量命令", source))?;
                let first_row_parameter = bind_batch_shared_parameters(
                    &mut statement,
                    batch,
                    "绑定精确批量命令公共参数",
                )?;
                for parameters in batch.parameter_rows() {
                    bind_batch_row_parameters(
                        &mut statement,
                        first_row_parameter,
                        parameters,
                        "绑定精确批量命令参数",
                    )?;
                    let affected = statement
                        .raw_execute()
                        .map_err(|source| SqliteRuntimeError::driver("执行精确批量命令", source))?;
                    if affected != 1 {
                        return Ok(false);
                    }
                }
                Ok(true)
            })(),
            SqliteTransactionStep::RequireNoRows(query) => (|| {
                let mut statement = connection
                    .prepare_cached(query.statement())
                    .map_err(|source| SqliteRuntimeError::driver("准备事务条件查询", source))?;
                statement
                    .exists(params_from_iter(query.parameters().iter()))
                    .map(|exists| !exists)
                    .map_err(|source| SqliteRuntimeError::driver("执行事务条件查询", source))
            })(),
            SqliteTransactionStep::RequireNoRowsReturningFirstRow(query) => (|| {
                let mut statement =
                    connection
                        .prepare_cached(query.statement())
                        .map_err(|source| {
                            SqliteRuntimeError::driver("准备带诊断行的事务条件查询", source)
                        })?;
                let column_count = statement.column_count();
                let mut rows = statement
                    .query(params_from_iter(query.parameters().iter()))
                    .map_err(|source| {
                        SqliteRuntimeError::driver("绑定带诊断行的事务条件查询参数", source)
                    })?;
                let Some(row) = rows.next().map_err(|source| {
                    SqliteRuntimeError::driver("执行带诊断行的事务条件查询", source)
                })?
                else {
                    return Ok(true);
                };
                failure_row = Some((query.id().to_owned(), own_sqlite_row(row, column_count)?));
                Ok(false)
            })(),
            SqliteTransactionStep::RequireNoRowsMany(batch) => (|| {
                let mut statement = connection
                    .prepare_cached(batch.statement())
                    .map_err(|source| SqliteRuntimeError::driver("准备批量事务条件查询", source))?;
                let first_row_parameter = bind_batch_shared_parameters(
                    &mut statement,
                    batch,
                    "绑定批量事务条件公共参数",
                )?;
                for parameters in batch.parameter_rows() {
                    bind_batch_row_parameters(
                        &mut statement,
                        first_row_parameter,
                        parameters,
                        "绑定批量事务条件参数",
                    )?;
                    let exists = {
                        let mut rows = statement.raw_query();
                        rows.next().map(|row| row.is_some()).map_err(|source| {
                            SqliteRuntimeError::driver("执行批量事务条件查询", source)
                        })?
                    };
                    if exists {
                        return Ok(false);
                    }
                }
                Ok(true)
            })(),
        };

        match requirement_satisfied {
            Ok(true) => {}
            Ok(false) => {
                return match failure_row {
                    Some((query_id, row)) => rollback_requirement_failure_with_row(
                        connection,
                        query_id,
                        row,
                        performance,
                        SqliteTransactionScope::WritePlan,
                    ),
                    None => rollback_requirement_failure(
                        connection,
                        performance,
                        SqliteTransactionScope::WritePlan,
                    ),
                };
            }
            Err(primary) => {
                return match rollback_after_failure(
                    connection,
                    primary,
                    performance,
                    SqliteTransactionScope::WritePlan,
                ) {
                    Ok(primary) => Err(ExecuteTransactionError::NotCommitted(primary)),
                    Err(source) => Err(ExecuteTransactionError::OutcomeUnknown(source)),
                };
            }
        }
        if connection.is_autocommit() {
            return Err(ExecuteTransactionError::OutcomeUnknown(
                SqliteRuntimeError::Internal("事务步骤意外结束了根事务"),
            ));
        }
    }

    match execute_transaction_control(
        connection,
        performance,
        SqliteTransactionScope::WritePlan,
        SqliteTransactionControl::Commit,
        "COMMIT",
    ) {
        Ok(()) if connection.is_autocommit() => Ok(()),
        Ok(()) => Err(ExecuteTransactionError::OutcomeUnknown(
            SqliteRuntimeError::Internal("COMMIT 成功后连接仍非 autocommit"),
        )),
        Err(source) if connection.is_autocommit() => Err(ExecuteTransactionError::OutcomeUnknown(
            SqliteRuntimeError::driver("提交写事务", source),
        )),
        Err(source) => {
            let primary = SqliteRuntimeError::driver("提交写事务", source);
            match rollback_after_failure(
                connection,
                primary,
                performance,
                SqliteTransactionScope::WritePlan,
            ) {
                Ok(primary) => Err(ExecuteTransactionError::NotCommitted(primary)),
                Err(source) => Err(ExecuteTransactionError::OutcomeUnknown(source)),
            }
        }
    }
}

fn run_final_transaction(
    path: &Path,
    plan: &SqliteTransactionPlan,
    config: &RusqliteStorageConfiguration,
    performance: &RunPerformanceCounters,
) -> Result<(), ExecuteFinalTransactionError<SqliteRuntimeError>> {
    validate_transaction_plan(plan, config).map_err(ExecuteFinalTransactionError::NotCommitted)?;
    let connection = open_transaction_connection(path, config).map_err(|error| match error {
        ExecuteTransactionError::NotFound => ExecuteFinalTransactionError::NotFound,
        ExecuteTransactionError::RequirementFailed => {
            ExecuteFinalTransactionError::RequirementFailed
        }
        ExecuteTransactionError::RequirementFailedWithRow { query_id, row } => {
            ExecuteFinalTransactionError::RequirementFailedWithRow { query_id, row }
        }
        ExecuteTransactionError::RequirementFailedWithRowOutcomeUnknown {
            query_id,
            row,
            source,
        } => ExecuteFinalTransactionError::RequirementFailedWithRowOutcomeUnknown {
            query_id,
            row,
            source,
        },
        ExecuteTransactionError::NotCommitted(source) => {
            ExecuteFinalTransactionError::NotCommitted(source)
        }
        ExecuteTransactionError::OutcomeUnknown(source) => {
            ExecuteFinalTransactionError::OutcomeUnknown(source)
        }
    })?;
    let transaction = run_transaction_on_connection(&connection, plan, performance);
    let close_error = connection
        .close()
        .err()
        .map(|(_connection, source)| SqliteRuntimeError::driver("关闭最终事务连接", source));

    match (transaction, close_error) {
        (Ok(()), None) => Ok(()),
        (Ok(()), Some(source)) => {
            Err(ExecuteFinalTransactionError::CommittedButFinalizationFailed(source))
        }
        (Err(error), None) => Err(map_final_transaction_error(error)),
        (Err(ExecuteTransactionError::NotFound), Some(close)) => Err(
            ExecuteFinalTransactionError::OutcomeUnknown(SqliteRuntimeError::Cleanup {
                primary: Box::new(SqliteRuntimeError::Internal(
                    "连接打开后的事务意外返回 NotFound",
                )),
                failures: vec![close],
            }),
        ),
        (Err(ExecuteTransactionError::RequirementFailed), Some(close)) => Err(
            ExecuteFinalTransactionError::NotCommitted(SqliteRuntimeError::Cleanup {
                primary: Box::new(SqliteRuntimeError::Internal("事务条件未满足并已确认回滚")),
                failures: vec![close],
            }),
        ),
        (Err(ExecuteTransactionError::RequirementFailedWithRow { query_id, row }), Some(close)) => {
            Err(
                ExecuteFinalTransactionError::RequirementFailedWithRowAndFinalizationFailed {
                    query_id,
                    row,
                    source: Box::new(close),
                },
            )
        }
        (
            Err(ExecuteTransactionError::RequirementFailedWithRowOutcomeUnknown {
                query_id,
                row,
                source: primary,
            }),
            Some(close),
        ) => Err(
            ExecuteFinalTransactionError::RequirementFailedWithRowOutcomeUnknown {
                query_id,
                row,
                source: Box::new(SqliteRuntimeError::Cleanup {
                    primary,
                    failures: vec![close],
                }),
            },
        ),
        (Err(ExecuteTransactionError::NotCommitted(primary)), Some(close)) => Err(
            ExecuteFinalTransactionError::NotCommitted(SqliteRuntimeError::Cleanup {
                primary: Box::new(primary),
                failures: vec![close],
            }),
        ),
        (Err(ExecuteTransactionError::OutcomeUnknown(primary)), Some(close)) => Err(
            ExecuteFinalTransactionError::OutcomeUnknown(SqliteRuntimeError::Cleanup {
                primary: Box::new(primary),
                failures: vec![close],
            }),
        ),
    }
}

fn map_final_transaction_error(
    error: ExecuteTransactionError<SqliteRuntimeError>,
) -> ExecuteFinalTransactionError<SqliteRuntimeError> {
    match error {
        ExecuteTransactionError::NotFound => ExecuteFinalTransactionError::NotFound,
        ExecuteTransactionError::RequirementFailed => {
            ExecuteFinalTransactionError::RequirementFailed
        }
        ExecuteTransactionError::RequirementFailedWithRow { query_id, row } => {
            ExecuteFinalTransactionError::RequirementFailedWithRow { query_id, row }
        }
        ExecuteTransactionError::RequirementFailedWithRowOutcomeUnknown {
            query_id,
            row,
            source,
        } => ExecuteFinalTransactionError::RequirementFailedWithRowOutcomeUnknown {
            query_id,
            row,
            source,
        },
        ExecuteTransactionError::NotCommitted(source) => {
            ExecuteFinalTransactionError::NotCommitted(source)
        }
        ExecuteTransactionError::OutcomeUnknown(source) => {
            ExecuteFinalTransactionError::OutcomeUnknown(source)
        }
    }
}

fn sidecar_path(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(suffix);
    PathBuf::from(value)
}

fn database_artifact_paths(path: &Path) -> [PathBuf; 4] {
    [
        path.to_path_buf(),
        sidecar_path(path, "-journal"),
        sidecar_path(path, "-wal"),
        sidecar_path(path, "-shm"),
    ]
}

struct TrackedDatabaseArtifact {
    path: PathBuf,
    identity: FileIdentity,
    is_main: bool,
}

struct CleanupAssessment {
    residual: bool,
    unknown: bool,
    failures: Vec<SqliteRuntimeError>,
}

impl CleanupAssessment {
    fn clean() -> Self {
        Self {
            residual: false,
            unknown: false,
            failures: Vec::new(),
        }
    }
}

fn windows_not_found(error: &WindowsFsError) -> bool {
    matches!(
        error,
        WindowsFsError::Io { source, .. } if source.kind() == io::ErrorKind::NotFound
    )
}

fn observe_sidecar_artifacts(
    paths: &[PathBuf],
) -> (Vec<TrackedDatabaseArtifact>, CleanupAssessment) {
    let mut tracked = Vec::new();
    let mut assessment = CleanupAssessment::clean();
    for path in paths {
        match pin_path_without_reparse(path) {
            Ok(pinned) => match pinned.metadata() {
                Ok(metadata) if metadata.is_file() => match FileIdentity::of(pinned.file(), path) {
                    Ok(identity) => tracked.push(TrackedDatabaseArtifact {
                        path: path.clone(),
                        identity,
                        is_main: false,
                    }),
                    Err(source) => {
                        assessment.unknown = true;
                        assessment
                            .failures
                            .push(SqliteRuntimeError::windows_file_system(
                                "确认 SQLite 伴生文件物理身份",
                                path,
                                source,
                            ));
                    }
                },
                Ok(_) => {
                    assessment.residual = true;
                    assessment
                        .failures
                        .push(SqliteRuntimeError::InvalidTarget { path: path.clone() });
                }
                Err(source) => {
                    assessment.unknown = true;
                    assessment
                        .failures
                        .push(SqliteRuntimeError::windows_file_system(
                            "检查 SQLite 伴生文件类型",
                            path,
                            source,
                        ));
                }
            },
            Err(source) if windows_not_found(&source) => {}
            Err(source) => {
                assessment.unknown = true;
                assessment
                    .failures
                    .push(SqliteRuntimeError::windows_file_system(
                        "固定 SQLite 伴生文件物理身份",
                        path,
                        source,
                    ));
            }
        }
    }
    (tracked, assessment)
}

fn cleanup_database_artifacts(
    paths: &[PathBuf; 4],
    tracked: &[TrackedDatabaseArtifact],
    mut assessment: CleanupAssessment,
) -> CleanupAssessment {
    for artifact in tracked {
        if let Err(source) = delete_regular_file_if_identity(&artifact.path, artifact.identity) {
            if windows_not_found(&source) && !artifact.is_main {
                continue;
            }
            if artifact.is_main
                && (matches!(source, WindowsFsError::FileIdentityChanged { .. })
                    || windows_not_found(&source))
            {
                assessment.unknown = true;
            } else {
                assessment.residual = true;
            }
            assessment
                .failures
                .push(SqliteRuntimeError::windows_file_system(
                    "按已确认的物理身份删除 SQLite 文件",
                    &artifact.path,
                    source,
                ));
        }
    }

    for candidate in paths {
        match fs::symlink_metadata(candidate) {
            Ok(_) => assessment.residual = true,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => {
                assessment.unknown = true;
                assessment.failures.push(SqliteRuntimeError::io(
                    "确认 SQLite 清理目标是否存在",
                    candidate,
                    error,
                ));
            }
        }
    }
    assessment
}

fn creation_failure(
    primary: SqliteRuntimeError,
    cleanup: CleanupAssessment,
) -> CreateDatabaseError<SqliteRuntimeError> {
    let source = if cleanup.failures.is_empty() {
        primary
    } else {
        SqliteRuntimeError::Cleanup {
            primary: Box::new(primary),
            failures: cleanup.failures,
        }
    };
    if cleanup.unknown {
        CreateDatabaseError::OutcomeUnknown(source)
    } else if cleanup.residual {
        CreateDatabaseError::ResidualArtifact(source)
    } else {
        CreateDatabaseError::NotCreated(source)
    }
}

struct DatabaseInitializationFailure {
    primary: SqliteRuntimeError,
    sidecars: Vec<TrackedDatabaseArtifact>,
    assessment: CleanupAssessment,
}

fn initialize_new_database(
    path: &Path,
    sidecar_paths: &[PathBuf],
    commands: &[SqliteCommand],
    config: &RusqliteStorageConfiguration,
    performance: &RunPerformanceCounters,
) -> Result<(), Box<DatabaseInitializationFailure>> {
    let connection = match Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(connection) => connection,
        Err(source) => {
            let (sidecars, assessment) = observe_sidecar_artifacts(sidecar_paths);
            return Err(Box::new(DatabaseInitializationFailure {
                primary: SqliteRuntimeError::driver("打开新数据库", source),
                sidecars,
                assessment,
            }));
        }
    };
    let result = (|| {
        // page_size 必须在新数据库创建首个表、进入 WAL 模式之前设置；现存数据库
        // 保持自己的物理页格式并自然可用，不做迁移或 VACUUM。
        apply_new_database_page_policy(&connection)?;
        apply_read_write_policy(&connection, config)?;
        execute_transaction_control(
            &connection,
            performance,
            SqliteTransactionScope::DatabaseInitialization,
            SqliteTransactionControl::Begin,
            "BEGIN IMMEDIATE",
        )
        .map_err(|source| SqliteRuntimeError::driver("开始新数据库事务", source))?;

        for command in commands {
            if let Err(source) = connection.execute(
                command.statement(),
                params_from_iter(command.parameters().iter()),
            ) {
                let primary = SqliteRuntimeError::driver("初始化新数据库", source);
                return Err(
                    match rollback_after_failure(
                        &connection,
                        primary,
                        performance,
                        SqliteTransactionScope::DatabaseInitialization,
                    ) {
                        Ok(primary) | Err(primary) => primary,
                    },
                );
            }
            if connection.is_autocommit() {
                return Err(SqliteRuntimeError::Internal("初始化命令意外结束了根事务"));
            }
        }

        match execute_transaction_control(
            &connection,
            performance,
            SqliteTransactionScope::DatabaseInitialization,
            SqliteTransactionControl::Commit,
            "COMMIT",
        ) {
            Ok(()) if connection.is_autocommit() => Ok(()),
            Ok(()) => Err(SqliteRuntimeError::Internal(
                "初始化 COMMIT 成功后连接仍非 autocommit",
            )),
            Err(source) => {
                let primary = SqliteRuntimeError::driver("提交新数据库", source);
                if connection.is_autocommit() {
                    Err(primary)
                } else {
                    Err(
                        match rollback_after_failure(
                            &connection,
                            primary,
                            performance,
                            SqliteTransactionScope::DatabaseInitialization,
                        ) {
                            Ok(primary) | Err(primary) => primary,
                        },
                    )
                }
            }
        }
    })();
    match result {
        Ok(()) => Ok(()),
        Err(primary) => {
            let (sidecars, assessment) = observe_sidecar_artifacts(sidecar_paths);
            Err(Box::new(DatabaseInitializationFailure {
                primary,
                sidecars,
                assessment,
            }))
        }
    }
}

#[derive(Clone, Copy)]
enum DatabaseTargetPurpose {
    Create,
    Snapshot,
}

impl DatabaseTargetPurpose {
    const fn absolute_path_operation(self) -> &'static str {
        match self {
            Self::Create => "建立新数据库绝对路径",
            Self::Snapshot => "建立快照目标绝对路径",
        }
    }

    const fn pin_parent_operation(self) -> &'static str {
        match self {
            Self::Create => "固定新数据库父目录",
            Self::Snapshot => "固定快照目标父目录",
        }
    }

    const fn claim_operation(self) -> &'static str {
        match self {
            Self::Create => "原子占有并固定新数据库路径",
            Self::Snapshot => "原子占有并固定快照目标路径",
        }
    }

    const fn identity_operation(self) -> &'static str {
        match self {
            Self::Create => "读取新数据库初始物理身份",
            Self::Snapshot => "读取快照目标初始物理身份",
        }
    }

    const fn held_identity_operation(self) -> &'static str {
        match self {
            Self::Create => "复核固定的新数据库物理身份",
            Self::Snapshot => "复核固定的快照目标物理身份",
        }
    }

    const fn final_path_operation(self) -> &'static str {
        match self {
            Self::Create => "复核新数据库最终路径身份",
            Self::Snapshot => "复核快照目标最终路径身份",
        }
    }

    const fn final_identity_operation(self) -> &'static str {
        match self {
            Self::Create => "读取新数据库最终物理身份",
            Self::Snapshot => "读取快照目标最终物理身份",
        }
    }

    const fn identity_changed_message(self) -> &'static str {
        match self {
            Self::Create => "新数据库物理身份在初始化期间发生变化",
            Self::Snapshot => "快照目标物理身份在 online backup 期间发生变化",
        }
    }
}

enum DatabaseTargetClaimError {
    AlreadyExists,
    NotCreated(SqliteRuntimeError),
    OutcomeUnknown(SqliteRuntimeError),
}

/// 从原子占有路径开始，到 SQLite 操作和最终复核结束为止固定同一个目标对象。
struct ClaimedDatabaseTarget {
    _parent: PinnedPath,
    file: File,
    path: PathBuf,
    identity: FileIdentity,
}

fn claim_database_target(
    requested_path: &Path,
    purpose: DatabaseTargetPurpose,
    config: &RusqliteStorageConfiguration,
) -> Result<ClaimedDatabaseTarget, DatabaseTargetClaimError> {
    let absolute = std::path::absolute(requested_path).map_err(|source| {
        DatabaseTargetClaimError::NotCreated(SqliteRuntimeError::io(
            purpose.absolute_path_operation(),
            requested_path,
            source,
        ))
    })?;
    let parent_path = absolute.parent().ok_or_else(|| {
        DatabaseTargetClaimError::NotCreated(SqliteRuntimeError::InvalidTarget {
            path: absolute.clone(),
        })
    })?;
    let file_name = absolute.file_name().ok_or_else(|| {
        DatabaseTargetClaimError::NotCreated(SqliteRuntimeError::InvalidTarget {
            path: absolute.clone(),
        })
    })?;
    let parent = pin_directory_without_reparse(parent_path).map_err(|source| {
        DatabaseTargetClaimError::NotCreated(SqliteRuntimeError::windows_file_system(
            purpose.pin_parent_operation(),
            parent_path,
            source,
        ))
    })?;
    let stable_path = parent.resolved_path().join(file_name);
    let file = match create_new_pinned_database_file(&stable_path) {
        Ok(file) => file,
        Err(WindowsFsError::Io { source, .. }) if source.kind() == io::ErrorKind::AlreadyExists => {
            return Err(DatabaseTargetClaimError::AlreadyExists);
        }
        Err(source) => {
            return Err(DatabaseTargetClaimError::NotCreated(
                SqliteRuntimeError::windows_file_system(
                    purpose.claim_operation(),
                    &stable_path,
                    source,
                ),
            ));
        }
    };
    let identity = FileIdentity::of(&file, &stable_path).map_err(|source| {
        DatabaseTargetClaimError::OutcomeUnknown(SqliteRuntimeError::windows_file_system(
            purpose.identity_operation(),
            &stable_path,
            source,
        ))
    })?;

    #[cfg(test)]
    if let Some(hook) = &config.claimed_database_path_hook {
        hook.call(&stable_path);
    }
    #[cfg(not(test))]
    let _ = config;

    Ok(ClaimedDatabaseTarget {
        _parent: parent,
        file,
        path: stable_path,
        identity,
    })
}

fn verify_claimed_database_identity(
    claim: &ClaimedDatabaseTarget,
    purpose: DatabaseTargetPurpose,
) -> Result<(), SqliteRuntimeError> {
    let held_identity = FileIdentity::of(&claim.file, &claim.path).map_err(|source| {
        SqliteRuntimeError::windows_file_system(
            purpose.held_identity_operation(),
            &claim.path,
            source,
        )
    })?;
    if held_identity != claim.identity {
        return Err(SqliteRuntimeError::Internal(
            purpose.identity_changed_message(),
        ));
    }

    // 原占有句柄和父目录链仍未释放；这次按路径复核因此既检查最终可见名称，
    // 又不会在“复核”和返回之间重新打开一个可替换窗口。
    let final_path = pin_path_without_reparse(&claim.path).map_err(|source| {
        SqliteRuntimeError::windows_file_system(purpose.final_path_operation(), &claim.path, source)
    })?;
    let final_identity = FileIdentity::of(final_path.file(), &claim.path).map_err(|source| {
        SqliteRuntimeError::windows_file_system(
            purpose.final_identity_operation(),
            &claim.path,
            source,
        )
    })?;
    if final_identity == claim.identity {
        Ok(())
    } else {
        Err(SqliteRuntimeError::Internal(
            purpose.identity_changed_message(),
        ))
    }
}

fn run_create_database(
    path: &Path,
    commands: &[SqliteCommand],
    config: &RusqliteStorageConfiguration,
    performance: &RunPerformanceCounters,
) -> Result<(), CreateDatabaseError<SqliteRuntimeError>> {
    for command in commands {
        validate_command(command, config).map_err(CreateDatabaseError::NotCreated)?;
    }
    let claim = match claim_database_target(path, DatabaseTargetPurpose::Create, config) {
        Ok(claim) => claim,
        Err(DatabaseTargetClaimError::AlreadyExists) => {
            return Err(CreateDatabaseError::AlreadyExists);
        }
        Err(DatabaseTargetClaimError::NotCreated(source)) => {
            return Err(CreateDatabaseError::NotCreated(source));
        }
        Err(DatabaseTargetClaimError::OutcomeUnknown(source)) => {
            return Err(CreateDatabaseError::OutcomeUnknown(source));
        }
    };
    let stable_path = claim.path.clone();
    let main_identity = claim.identity;
    let paths = database_artifact_paths(&stable_path);
    let main = TrackedDatabaseArtifact {
        path: stable_path.clone(),
        identity: main_identity,
        is_main: true,
    };
    for sidecar in &paths[1..] {
        match fs::symlink_metadata(sidecar) {
            Ok(_) => {
                drop(claim);
                let cleanup = cleanup_database_artifacts(
                    &paths,
                    std::slice::from_ref(&main),
                    CleanupAssessment {
                        residual: true,
                        unknown: false,
                        failures: Vec::new(),
                    },
                );
                return Err(creation_failure(
                    SqliteRuntimeError::UnexpectedArtifact {
                        path: sidecar.clone(),
                    },
                    cleanup,
                ));
            }
            Err(source) if source.kind() == io::ErrorKind::NotFound => {}
            Err(source) => {
                drop(claim);
                let cleanup = cleanup_database_artifacts(
                    &paths,
                    std::slice::from_ref(&main),
                    CleanupAssessment {
                        residual: false,
                        unknown: true,
                        failures: Vec::new(),
                    },
                );
                return Err(creation_failure(
                    SqliteRuntimeError::io("确认 SQLite 伴生路径初始不存在", sidecar, source),
                    cleanup,
                ));
            }
        }
    }

    match initialize_new_database(&stable_path, &paths[1..], commands, config, performance) {
        Ok(()) => verify_claimed_database_identity(&claim, DatabaseTargetPurpose::Create)
            .map_err(CreateDatabaseError::OutcomeUnknown),
        Err(failure) => {
            let failure = *failure;
            let mut tracked = vec![main];
            tracked.extend(failure.sidecars);
            drop(claim);
            let cleanup = cleanup_database_artifacts(&paths, &tracked, failure.assessment);
            Err(creation_failure(failure.primary, cleanup))
        }
    }
}

fn snapshot_failure(
    primary: SqliteRuntimeError,
    cleanup: CleanupAssessment,
) -> SnapshotDatabaseError<SqliteRuntimeError> {
    let source = if cleanup.failures.is_empty() {
        primary
    } else {
        SqliteRuntimeError::Cleanup {
            primary: Box::new(primary),
            failures: cleanup.failures,
        }
    };
    if cleanup.unknown {
        SnapshotDatabaseError::OutcomeUnknown(source)
    } else if cleanup.residual {
        SnapshotDatabaseError::ResidualArtifact(source)
    } else {
        SnapshotDatabaseError::NotCreated(source)
    }
}

fn snapshot_source_connection(
    path: &Path,
    config: &RusqliteStorageConfiguration,
) -> Result<Connection, SnapshotDatabaseError<SqliteRuntimeError>> {
    open_existing_read_only(path, config).map_err(|error| match error {
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::NotFound) => {
            SnapshotDatabaseError::SourceNotFound
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::InvalidTarget) => {
            SnapshotDatabaseError::NotCreated(SqliteRuntimeError::InvalidTarget {
                path: path.to_path_buf(),
            })
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::Io(source)) => {
            SnapshotDatabaseError::NotCreated(SqliteRuntimeError::io(
                "检查快照源数据库",
                path,
                source,
            ))
        }
        ExistingFileErrorOrRuntime::Runtime(source) => SnapshotDatabaseError::NotCreated(source),
    })
}

fn run_snapshot_database(
    source_path: &Path,
    destination_path: &Path,
    config: &RusqliteStorageConfiguration,
) -> Result<(), SnapshotDatabaseError<SqliteRuntimeError>> {
    let source = snapshot_source_connection(source_path, config)?;
    let claim =
        match claim_database_target(destination_path, DatabaseTargetPurpose::Snapshot, config) {
            Ok(claim) => claim,
            Err(DatabaseTargetClaimError::AlreadyExists) => {
                return Err(SnapshotDatabaseError::DestinationAlreadyExists);
            }
            Err(DatabaseTargetClaimError::NotCreated(source)) => {
                return Err(SnapshotDatabaseError::NotCreated(source));
            }
            Err(DatabaseTargetClaimError::OutcomeUnknown(source)) => {
                return Err(SnapshotDatabaseError::OutcomeUnknown(source));
            }
        };
    let stable_path = claim.path.clone();
    let main_identity = claim.identity;
    let paths = database_artifact_paths(&stable_path);
    let main = TrackedDatabaseArtifact {
        path: stable_path.clone(),
        identity: main_identity,
        is_main: true,
    };
    for sidecar in &paths[1..] {
        match fs::symlink_metadata(sidecar) {
            Ok(_) => {
                drop(claim);
                let cleanup = cleanup_database_artifacts(
                    &paths,
                    std::slice::from_ref(&main),
                    CleanupAssessment {
                        residual: true,
                        unknown: false,
                        failures: Vec::new(),
                    },
                );
                return Err(snapshot_failure(
                    SqliteRuntimeError::UnexpectedArtifact {
                        path: sidecar.clone(),
                    },
                    cleanup,
                ));
            }
            Err(source) if source.kind() == io::ErrorKind::NotFound => {}
            Err(source) => {
                drop(claim);
                let cleanup = cleanup_database_artifacts(
                    &paths,
                    std::slice::from_ref(&main),
                    CleanupAssessment {
                        residual: false,
                        unknown: true,
                        failures: Vec::new(),
                    },
                );
                return Err(snapshot_failure(
                    SqliteRuntimeError::io("确认快照目标伴生路径初始不存在", sidecar, source),
                    cleanup,
                ));
            }
        }
    }

    let mut destination = match Connection::open_with_flags(
        &stable_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(connection) => connection,
        Err(source) => {
            let (sidecars, assessment) = observe_sidecar_artifacts(&paths[1..]);
            let mut tracked = vec![main];
            tracked.extend(sidecars);
            drop(claim);
            let cleanup = cleanup_database_artifacts(&paths, &tracked, assessment);
            return Err(snapshot_failure(
                SqliteRuntimeError::driver("打开快照目标数据库", source),
                cleanup,
            ));
        }
    };
    let result = (|| {
        destination
            .busy_handler(Some(wait_for_sqlite_unlock))
            .map_err(|source| SqliteRuntimeError::driver("设置快照目标繁忙等待处理器", source))?;
        apply_connection_memory_policy(&destination)?;
        {
            // 目标必须在 online backup 完成后再进入 WAL。SQLite 不允许向页大小
            // 已由 WAL 固定、且与源不同的目标执行备份；先复制才能让目标继承源页大小。
            let backup = Backup::new(&source, &mut destination)
                .map_err(|source| SqliteRuntimeError::driver("建立 online backup", source))?;
            loop {
                if sqlite_busy_wait_cancelled() {
                    break Err(SqliteRuntimeError::Cancelled {
                        operation: "执行 online backup",
                    });
                }
                match backup
                    .step(SQLITE_BACKUP_PAGES_PER_STEP)
                    .map_err(|source| SqliteRuntimeError::driver("执行 online backup", source))?
                {
                    StepResult::Done => break Ok(()),
                    StepResult::More => {}
                    StepResult::Busy | StepResult::Locked => {
                        thread::sleep(SQLITE_WAIT_POLL_INTERVAL);
                    }
                    _ => break Err(SqliteRuntimeError::BackupIncomplete("未知状态")),
                }
            }
        }?;
        apply_read_write_policy(&destination, config)
    })();
    drop(destination);
    drop(source);

    if let Err(primary) = result {
        let (sidecars, assessment) = observe_sidecar_artifacts(&paths[1..]);
        let mut tracked = vec![main];
        tracked.extend(sidecars);
        drop(claim);
        let cleanup = cleanup_database_artifacts(&paths, &tracked, assessment);
        return Err(snapshot_failure(primary, cleanup));
    }

    verify_claimed_database_identity(&claim, DatabaseTargetPurpose::Snapshot)
        .map_err(SnapshotDatabaseError::OutcomeUnknown)
}

type TransactionSessionResult = Result<(), ExecuteTransactionError<SqliteRuntimeError>>;

enum TransactionSessionCommand {
    Execute {
        plan: SqliteTransactionPlan,
        response: oneshot::Sender<TransactionSessionResult>,
    },
}

#[derive(Clone, Copy)]
enum TransactionSessionState {
    Ready,
    Indeterminate,
}

fn process_transaction_session_command(
    connection: &Connection,
    config: &RusqliteStorageConfiguration,
    performance: &RunPerformanceCounters,
    state: &mut TransactionSessionState,
    lifecycle: &AtomicU8,
    command: TransactionSessionCommand,
) {
    let TransactionSessionCommand::Execute { plan, response } = command;
    if matches!(state, TransactionSessionState::Indeterminate) {
        let _ = response.send(Err(ExecuteTransactionError::OutcomeUnknown(
            SqliteRuntimeError::Internal("事务计划会话已经处于结果未知状态"),
        )));
        return;
    }

    let result = if connection.is_autocommit() {
        validate_transaction_plan(&plan, config)
            .map_err(ExecuteTransactionError::NotCommitted)
            .and_then(|()| run_transaction_on_connection(connection, &plan, performance))
    } else {
        Err(ExecuteTransactionError::NotCommitted(
            SqliteRuntimeError::Internal("活动事务中不能执行完整事务计划"),
        ))
    };
    match &result {
        // RequirementFailedWithRow 与 RequirementFailed 同为“整个事务已确认回滚”的
        // 确定终态；携带诊断行不改变回滚事实，会话保持可用。
        Ok(())
        | Err(ExecuteTransactionError::RequirementFailed)
        | Err(ExecuteTransactionError::RequirementFailedWithRow { .. })
        | Err(ExecuteTransactionError::NotCommitted(_))
            if connection.is_autocommit() =>
        {
            *state = TransactionSessionState::Ready;
        }
        _ => {
            *state = TransactionSessionState::Indeterminate;
            lifecycle.store(SESSION_INDETERMINATE, Ordering::Release);
        }
    }
    let _ = response.send(result);
}

type InteractiveFinalizationResult = Result<
    SqliteInteractiveSessionFinalization,
    SqliteInteractiveSessionFinalizationError<SqliteRuntimeError>,
>;

fn finalize_interactive_connection(
    connection: Connection,
    lifecycle: &AtomicU8,
    performance: &RunPerformanceCounters,
) -> InteractiveFinalizationResult {
    let had_unclosed_transaction = !connection.is_autocommit();
    let primary = if had_unclosed_transaction {
        match execute_transaction_control(
            &connection,
            performance,
            SqliteTransactionScope::Interactive,
            SqliteTransactionControl::Rollback,
            "ROLLBACK",
        ) {
            Ok(()) if connection.is_autocommit() => None,
            Ok(()) => Some(SqliteInteractiveSessionFinalizationFailure::OutcomeUnknown(
                SqliteRuntimeError::Internal("回滚成功后交互式连接仍非 autocommit"),
            )),
            Err(source) if connection.is_autocommit() => {
                Some(SqliteInteractiveSessionFinalizationFailure::OutcomeUnknown(
                    SqliteRuntimeError::driver("终结交互式事务", source),
                ))
            }
            Err(source) => Some(SqliteInteractiveSessionFinalizationFailure::CleanupFailed(
                SqliteRuntimeError::driver("终结交互式事务", source),
            )),
        }
    } else {
        None
    };
    let close_failure = match connection.close() {
        Ok(()) => None,
        Err((_connection, source)) => Some(SqliteRuntimeError::driver("关闭交互式连接", source)),
    };
    lifecycle.store(SESSION_CLOSED, Ordering::Release);
    match (primary, close_failure) {
        (None, None) => Ok(SqliteInteractiveSessionFinalization::new(
            had_unclosed_transaction,
        )),
        (None, Some(source)) => Err(SqliteInteractiveSessionFinalizationError::new(
            SqliteInteractiveSessionFinalizationFailure::CleanupFailed(source),
            None,
        )),
        (Some(primary), connection_close) => Err(SqliteInteractiveSessionFinalizationError::new(
            primary,
            connection_close,
        )),
    }
}

fn panicked_finalization_result() -> InteractiveFinalizationResult {
    Err(SqliteInteractiveSessionFinalizationError::new(
        SqliteInteractiveSessionFinalizationFailure::OutcomeUnknown(
            SqliteRuntimeError::WorkerPanicked("交互式 actor"),
        ),
        None,
    ))
}

fn run_interactive_actor(
    connection: Connection,
    config: Arc<RusqliteStorageConfiguration>,
    performance: Arc<RunPerformanceCounters>,
    commands: async_channel::Receiver<TransactionSessionCommand>,
    control: mpsc::Receiver<()>,
    lifecycle: Arc<AtomicU8>,
) -> InteractiveFinalizationResult {
    let mut state = if connection.is_autocommit() {
        TransactionSessionState::Ready
    } else {
        TransactionSessionState::Indeterminate
    };
    while let Ok(command) = commands.recv_blocking() {
        process_transaction_session_command(
            &connection,
            &config,
            &performance,
            &mut state,
            &lifecycle,
            command,
        );
    }
    let _ = control.recv();
    finalize_interactive_connection(connection, &lifecycle, &performance)
}

struct InteractiveSessionSlotState {
    accepting: bool,
    opening: bool,
    active: Option<Arc<InteractiveSessionControl>>,
}

struct InteractiveSessionSlot {
    state: Mutex<InteractiveSessionSlotState>,
    changed: Condvar,
}

impl InteractiveSessionSlot {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(InteractiveSessionSlotState {
                accepting: true,
                opening: false,
                active: None,
            }),
            changed: Condvar::new(),
        })
    }

    fn begin_open(&self) -> Result<(), SqliteRuntimeError> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if !state.accepting {
            return Err(SqliteRuntimeError::Closed);
        }
        if state.opening || state.active.is_some() {
            return Err(SqliteRuntimeError::InteractiveSessionAlreadyOpen);
        }
        state.opening = true;
        Ok(())
    }

    fn complete_open(&self, control: Arc<InteractiveSessionControl>) -> bool {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        debug_assert!(state.opening && state.active.is_none());
        state.opening = false;
        state.active = Some(control);
        self.changed.notify_all();
        state.accepting
    }

    fn abort_open(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.opening = false;
        self.changed.notify_all();
    }

    fn recover_open_panic(&self) -> Option<Arc<InteractiveSessionControl>> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.opening = false;
        self.changed.notify_all();
        state.active.clone()
    }

    fn complete(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.active = None;
        self.changed.notify_all();
    }

    fn begin_shutdown(&self) -> Option<Arc<InteractiveSessionControl>> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.accepting = false;
        state.active.clone()
    }

    fn wait_until_empty(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        while state.opening || state.active.is_some() {
            state = self
                .changed
                .wait(state)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
    }
}

struct InteractiveFinalizationResources {
    command_receiver: async_channel::Receiver<TransactionSessionCommand>,
    control: mpsc::Sender<()>,
}

struct InteractiveSessionControl {
    lifecycle: Arc<AtomicU8>,
    resources: Mutex<Option<InteractiveFinalizationResources>>,
}

impl InteractiveSessionControl {
    fn initiate(self: &Arc<Self>) {
        let resources = self
            .resources
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        let Some(resources) = resources else {
            return;
        };
        self.lifecycle.store(SESSION_FINALIZING, Ordering::Release);
        resources.command_receiver.close();
        let _ = resources.control.send(());
    }
}

/// 在一个固定 `rusqlite` 连接上连续执行完整事务计划。
pub(crate) struct RusqliteTransactionSessionOperations {
    commands: async_channel::Sender<TransactionSessionCommand>,
    lifecycle: Arc<AtomicU8>,
}

impl RusqliteTransactionSessionOperations {
    async fn await_transaction(
        &self,
        plan: SqliteTransactionPlan,
    ) -> Result<(), ExecuteTransactionError<SqliteRuntimeError>> {
        match self.lifecycle.load(Ordering::Acquire) {
            SESSION_OPEN => {}
            SESSION_INDETERMINATE => {
                return Err(ExecuteTransactionError::OutcomeUnknown(
                    SqliteRuntimeError::Internal("事务计划会话已经处于结果未知状态"),
                ));
            }
            SESSION_FINALIZING | SESSION_CLOSED => {
                return Err(ExecuteTransactionError::NotCommitted(
                    SqliteRuntimeError::Closed,
                ));
            }
            _ => {
                return Err(ExecuteTransactionError::NotCommitted(
                    SqliteRuntimeError::Internal("事务计划会话生命周期值无效"),
                ));
            }
        }
        let (response, receiver) = oneshot::channel();
        self.commands
            .send(TransactionSessionCommand::Execute { plan, response })
            .await
            .map_err(|_| ExecuteTransactionError::NotCommitted(SqliteRuntimeError::Closed))?;
        receiver.await.unwrap_or_else(|_| {
            Err(ExecuteTransactionError::OutcomeUnknown(
                SqliteRuntimeError::WorkerPanicked("交互式 actor"),
            ))
        })
    }
}

impl SqliteTransactionSessionOperations for RusqliteTransactionSessionOperations {
    type Error = SqliteRuntimeError;

    fn execute_transaction(
        &self,
        plan: SqliteTransactionPlan,
    ) -> impl Future<Output = Result<(), ExecuteTransactionError<Self::Error>>> + Send {
        self.await_transaction(plan)
    }
}

/// `rusqlite` 交互式会话的唯一终结令牌。
pub(crate) struct RusqliteInteractiveSessionFinalizer {
    control: Arc<InteractiveSessionControl>,
    report: Option<oneshot::Receiver<InteractiveFinalizationResult>>,
}

impl RusqliteInteractiveSessionFinalizer {
    fn initiate(&mut self) -> oneshot::Receiver<InteractiveFinalizationResult> {
        self.control.initiate();
        self.report.take().expect("终结令牌必须拥有唯一报告接收端")
    }
}

impl SqliteInteractiveSessionFinalizer for RusqliteInteractiveSessionFinalizer {
    type Error = SqliteRuntimeError;

    fn finalize(mut self) -> impl Future<Output = InteractiveFinalizationResult> + Send {
        let receiver = self.initiate();
        async move {
            receiver
                .await
                .unwrap_or_else(|_| panicked_finalization_result())
        }
    }
}

impl Drop for RusqliteInteractiveSessionFinalizer {
    fn drop(&mut self) {
        self.control.initiate();
        if let Some(report) = self.report.take() {
            drop(report);
        }
    }
}

enum ShortJob {
    Create {
        path: PathBuf,
        commands: Vec<SqliteCommand>,
        response: oneshot::Sender<Result<(), CreateDatabaseError<SqliteRuntimeError>>>,
        #[cfg(test)]
        panic_after_operation: bool,
    },
    Snapshot {
        source: PathBuf,
        destination: PathBuf,
        response: oneshot::Sender<Result<(), SnapshotDatabaseError<SqliteRuntimeError>>>,
        #[cfg(test)]
        panic_after_operation: bool,
    },
    Query {
        path: PathBuf,
        query: SqliteQuery,
        response:
            oneshot::Sender<Result<Vec<SqliteRow>, QueryExistingDatabaseError<SqliteRuntimeError>>>,
    },
    QuerySnapshot {
        path: PathBuf,
        queries: Vec<SqliteQuery>,
        response: oneshot::Sender<
            Result<Vec<Vec<SqliteRow>>, QueryExistingDatabaseError<SqliteRuntimeError>>,
        >,
    },
    Transaction {
        path: PathBuf,
        plan: SqliteTransactionPlan,
        response: oneshot::Sender<Result<(), ExecuteTransactionError<SqliteRuntimeError>>>,
        #[cfg(test)]
        panic_after_operation: bool,
    },
}

/// 已经占用唯一短操作执行宽度的工作。
///
/// 许可覆盖等待 worker、实际 SQLite 操作和结果投递；因此语义通道无需再拥有独立容量，
/// 排队与执行中的短操作总数始终不超过 worker 数。
struct AdmittedShortJob {
    job: ShortJob,
    _permit: OwnedSemaphorePermit,
}

fn run_short_worker(
    receiver: async_channel::Receiver<AdmittedShortJob>,
    config: Arc<RusqliteStorageConfiguration>,
    performance: Arc<RunPerformanceCounters>,
    cancellation: SqliteWaitCancellation,
) {
    install_sqlite_busy_cancellation(cancellation.clone());
    while let Ok(admitted) = receiver.recv_blocking() {
        let AdmittedShortJob {
            job,
            _permit: permit,
        } = admitted;
        match job {
            ShortJob::Create {
                path,
                commands,
                response,
                #[cfg(test)]
                panic_after_operation,
            } => {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    let result = run_create_database(&path, &commands, &config, &performance);
                    #[cfg(test)]
                    if panic_after_operation {
                        panic!("测试注入：建库副作用后 panic");
                    }
                    result
                }))
                .unwrap_or_else(|_| {
                    Err(CreateDatabaseError::OutcomeUnknown(
                        SqliteRuntimeError::WorkerPanicked("短操作"),
                    ))
                });
                let _ = response.send(result);
            }
            ShortJob::Snapshot {
                source,
                destination,
                response,
                #[cfg(test)]
                panic_after_operation,
            } => {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    let result = run_snapshot_database(&source, &destination, &config);
                    #[cfg(test)]
                    if panic_after_operation {
                        panic!("测试注入：数据库快照副作用后 panic");
                    }
                    result
                }))
                .unwrap_or_else(|_| {
                    Err(SnapshotDatabaseError::OutcomeUnknown(
                        SqliteRuntimeError::WorkerPanicked("短操作"),
                    ))
                });
                let _ = response.send(result);
            }
            ShortJob::Query {
                path,
                query,
                response,
            } => {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    run_query_existing(&path, &query, &config)
                }))
                .unwrap_or_else(|_| {
                    Err(QueryExistingDatabaseError::QueryFailed(
                        SqliteRuntimeError::WorkerPanicked("短操作"),
                    ))
                });
                let _ = response.send(result);
            }
            ShortJob::QuerySnapshot {
                path,
                queries,
                response,
            } => {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    run_query_snapshot_existing(&path, &queries, &config, &performance)
                }))
                .unwrap_or_else(|_| {
                    Err(QueryExistingDatabaseError::QueryFailed(
                        SqliteRuntimeError::WorkerPanicked("短操作"),
                    ))
                });
                let _ = response.send(result);
            }
            ShortJob::Transaction {
                path,
                plan,
                response,
                #[cfg(test)]
                panic_after_operation,
            } => {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    let result = run_transaction(&path, &plan, &config, &performance);
                    #[cfg(test)]
                    if panic_after_operation {
                        panic!("测试注入：短事务副作用后 panic");
                    }
                    result
                }))
                .unwrap_or_else(|_| {
                    Err(ExecuteTransactionError::OutcomeUnknown(
                        SqliteRuntimeError::WorkerPanicked("短操作"),
                    ))
                });
                let _ = response.send(result);
            }
        }
        drop(permit);
    }
}

fn open_transaction_session(
    path: &Path,
    config: Arc<RusqliteStorageConfiguration>,
    performance: Arc<RunPerformanceCounters>,
    slot: Arc<InteractiveSessionSlot>,
    cancellation: SqliteWaitCancellation,
) -> Result<
    OpenedSqliteTransactionSession<
        RusqliteTransactionSessionOperations,
        RusqliteInteractiveSessionFinalizer,
    >,
    OpenSqliteTransactionSessionError<SqliteRuntimeError>,
> {
    install_sqlite_busy_cancellation(cancellation.clone());
    let connection = open_existing_read_write(path, &config).map_err(|error| match error {
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::NotFound) => {
            OpenSqliteTransactionSessionError::NotFound
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::InvalidTarget) => {
            OpenSqliteTransactionSessionError::OpenFailed(SqliteRuntimeError::InvalidTarget {
                path: path.to_path_buf(),
            })
        }
        ExistingFileErrorOrRuntime::Existing(ExistingFileError::Io(source)) => {
            OpenSqliteTransactionSessionError::OpenFailed(SqliteRuntimeError::io(
                "检查交互式数据库",
                path,
                source,
            ))
        }
        ExistingFileErrorOrRuntime::Runtime(source) => {
            OpenSqliteTransactionSessionError::OpenFailed(source)
        }
    })?;
    let (command_sender, command_receiver) = async_channel::bounded(1);
    let actor_receiver = command_receiver.clone();
    let (control_sender, control_receiver) = mpsc::channel();
    let (start_sender, start_receiver) = mpsc::sync_channel(0);
    let lifecycle = Arc::new(AtomicU8::new(SESSION_OPEN));
    let actor_lifecycle = Arc::clone(&lifecycle);
    let actor_config = Arc::clone(&config);
    let (report_sender, report_receiver) = oneshot::channel();
    let control = Arc::new(InteractiveSessionControl {
        lifecycle: Arc::clone(&lifecycle),
        resources: Mutex::new(Some(InteractiveFinalizationResources {
            command_receiver,
            control: control_sender,
        })),
    });
    let actor_slot = Arc::clone(&slot);
    let actor_cancellation = cancellation;
    let actor = thread::Builder::new()
        .name("att-sqlite-session".to_owned())
        .stack_size(config.worker_stack_bytes.get())
        .spawn(move || {
            install_sqlite_busy_cancellation(actor_cancellation);
            let result = if start_receiver.recv().is_err() {
                panicked_finalization_result()
            } else {
                catch_unwind(AssertUnwindSafe(|| {
                    run_interactive_actor(
                        connection,
                        actor_config,
                        performance,
                        actor_receiver,
                        control_receiver,
                        actor_lifecycle,
                    )
                }))
                .unwrap_or_else(|_| panicked_finalization_result())
            };
            actor_slot.complete();
            let _ = report_sender.send(result);
        })
        .map_err(|source| {
            OpenSqliteTransactionSessionError::OpenFailed(SqliteRuntimeError::WorkerSpawn {
                worker: "interactive".to_owned(),
                source,
            })
        })?;
    // actor 自行上报终态并释放唯一会话槽，不需要第二个回收线程持有 join handle。
    drop(actor);
    let accepted = slot.complete_open(Arc::clone(&control));
    let _ = start_sender.send(());
    if !accepted {
        control.initiate();
        return Err(OpenSqliteTransactionSessionError::OpenFailed(
            SqliteRuntimeError::Closed,
        ));
    }
    let operations = Arc::new(RusqliteTransactionSessionOperations {
        commands: command_sender,
        lifecycle: Arc::clone(&lifecycle),
    });
    let finalizer = RusqliteInteractiveSessionFinalizer {
        control,
        report: Some(report_receiver),
    };
    Ok(OpenedSqliteTransactionSession::new(operations, finalizer))
}

struct RusqliteStorageInner {
    config: Arc<RusqliteStorageConfiguration>,
    performance: Arc<RunPerformanceCounters>,
    accepting: Arc<AtomicBool>,
    wait_cancellation: SqliteWaitCancellation,
    lifecycle: AtomicU8,
    short_admission: Arc<Semaphore>,
    short_sender: async_channel::Sender<AdmittedShortJob>,
    short_workers: Mutex<Option<Vec<JoinHandle<()>>>>,
    interactive_session: Arc<InteractiveSessionSlot>,
}

impl Drop for RusqliteStorageInner {
    fn drop(&mut self) {
        self.accepting.store(false, Ordering::Release);
        self.wait_cancellation.request();
        self.short_admission.close();
        self.short_sender.close();
        if self.lifecycle.load(Ordering::Acquire) == STORAGE_CLOSED {
            return;
        }

        let short_workers = self
            .short_workers
            .get_mut()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
            .unwrap_or_default();
        if short_workers.is_empty() {
            return;
        }
        let interactive_session = Arc::clone(&self.interactive_session);
        if let Some(control) = interactive_session.begin_shutdown() {
            control.initiate();
        }
        let stack_size = self.config.worker_stack_bytes.get();
        let _ = thread::Builder::new()
            .name("att-sqlite-drop-shutdown".to_owned())
            .stack_size(stack_size)
            .spawn(move || {
                for worker in short_workers {
                    let _ = worker.join();
                }
                interactive_session.wait_until_empty();
            });
    }
}

/// 共享短操作 worker 与唯一交互会话的 `rusqlite` 生产根。
///
/// 主根同时打开的连接最多为 `2 * short_worker_width + 1`：每个短操作 worker 在
/// online backup 中至多同时持有源、目标两个连接，唯一交互会话再持有一个连接。
#[derive(Clone)]
pub(crate) struct RusqliteStorage {
    inner: Arc<RusqliteStorageInner>,
}

/// 主 SQLite 根完成关闭后，用一个独立连接提交最终运行方案的短生命周期根。
///
/// 该连接只在主根关闭完成后打开，不与主根的连接生命周期叠加。
#[derive(Clone)]
pub(crate) struct RusqliteFinalTransactionExecutor {
    config: Arc<RusqliteStorageConfiguration>,
    performance: Arc<RunPerformanceCounters>,
    wait_cancellation: SqliteWaitCancellation,
}

impl RusqliteFinalTransactionExecutor {
    #[cfg(test)]
    pub(crate) fn new(config: RusqliteStorageConfiguration) -> Self {
        Self::new_with_performance(config, Arc::new(RunPerformanceCounters::default()))
    }

    pub(crate) fn new_with_performance(
        config: RusqliteStorageConfiguration,
        performance: Arc<RunPerformanceCounters>,
    ) -> Self {
        Self {
            config: Arc::new(config),
            performance,
            wait_cancellation: SqliteWaitCancellation::default(),
        }
    }

    /// 同步请求终止尚未取得的 SQLite 锁。
    ///
    /// 最终事务拥有独立取消域，调用方只应在最终化期间收到新的终止请求时调用；
    /// 业务阶段已经消费过的取消事实不会自动污染随后开始的运行方案保存。
    pub(crate) fn cancel_waits(&self) {
        self.wait_cancellation.request();
    }
}

impl RusqliteStorage {
    #[cfg(test)]
    pub(crate) fn start(config: RusqliteStorageConfiguration) -> Result<Self, SqliteRuntimeError> {
        Self::start_with_performance(config, Arc::new(RunPerformanceCounters::default()))
    }

    pub(crate) fn start_with_performance(
        config: RusqliteStorageConfiguration,
        performance: Arc<RunPerformanceCounters>,
    ) -> Result<Self, SqliteRuntimeError> {
        Self::start_with_available_parallelism(
            config,
            performance,
            std::thread::available_parallelism,
        )
    }

    fn start_with_available_parallelism<F>(
        config: RusqliteStorageConfiguration,
        performance: Arc<RunPerformanceCounters>,
        available_parallelism: F,
    ) -> Result<Self, SqliteRuntimeError>
    where
        F: FnOnce() -> Result<NonZeroUsize, io::Error>,
    {
        #[cfg(test)]
        let short_worker_threads = match config.fixed_short_worker_threads {
            Some(worker_threads) => worker_threads.get(),
            None => available_parallelism()
                .map_err(|source| SqliteRuntimeError::AvailableParallelism { source })?
                .get()
                .min(4),
        };
        #[cfg(not(test))]
        let short_worker_threads = available_parallelism()
            .map_err(|source| SqliteRuntimeError::AvailableParallelism { source })?
            .get()
            .min(4);

        let config = Arc::new(config);
        let accepting = Arc::new(AtomicBool::new(true));
        let wait_cancellation = SqliteWaitCancellation::default();
        let short_admission = Arc::new(Semaphore::new(short_worker_threads));
        let interactive_session = InteractiveSessionSlot::new();
        let (short_sender, short_receiver) = async_channel::unbounded();

        let mut short_workers: Vec<JoinHandle<()>> = Vec::with_capacity(short_worker_threads);
        for index in 0..short_worker_threads {
            let worker_receiver = short_receiver.clone();
            let worker_config = Arc::clone(&config);
            let worker_performance = Arc::clone(&performance);
            let worker_cancellation = wait_cancellation.clone();
            let worker = match thread::Builder::new()
                .name(format!("att-sqlite-short-{index}"))
                .stack_size(config.worker_stack_bytes.get())
                .spawn(move || {
                    run_short_worker(
                        worker_receiver,
                        worker_config,
                        worker_performance,
                        worker_cancellation,
                    )
                }) {
                Ok(worker) => worker,
                Err(source) => {
                    short_sender.close();
                    for worker in short_workers {
                        let _ = worker.join();
                    }
                    return Err(SqliteRuntimeError::WorkerSpawn {
                        worker: format!("short-{index}"),
                        source,
                    });
                }
            };
            short_workers.push(worker);
        }

        Ok(Self {
            inner: Arc::new(RusqliteStorageInner {
                config,
                performance,
                accepting,
                wait_cancellation,
                lifecycle: AtomicU8::new(STORAGE_RUNNING),
                short_admission,
                short_sender,
                short_workers: Mutex::new(Some(short_workers)),
                interactive_session,
            }),
        })
    }

    fn ensure_accepting(&self) -> Result<(), SqliteRuntimeError> {
        if self.inner.accepting.load(Ordering::Acquire) {
            Ok(())
        } else {
            Err(SqliteRuntimeError::Closed)
        }
    }

    /// 同步请求终止本根中尚未取得的 short worker 许可或 SQLite 锁。
    ///
    /// 该调用不等待 worker，也不关闭通道；进程信号处理可以先调用它打破业务
    /// future 与异步 `shutdown` 之间的循环等待，再让既有业务路径收敛到明确终态。
    pub(crate) fn cancel_waits(&self) {
        self.inner.wait_cancellation.request();
        self.inner.short_admission.close();
    }

    pub(crate) async fn shutdown(&self) -> Result<(), SqliteRuntimeError> {
        match self.inner.lifecycle.compare_exchange(
            STORAGE_RUNNING,
            STORAGE_SHUTTING_DOWN,
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => {}
            Err(STORAGE_CLOSED) => return Ok(()),
            Err(_) => {
                return Err(SqliteRuntimeError::Internal(
                    "SQLite shutdown 已经在另一调用中进行",
                ));
            }
        }
        self.inner.accepting.store(false, Ordering::Release);
        self.cancel_waits();
        self.inner.short_sender.close();
        if let Some(control) = self.inner.interactive_session.begin_shutdown() {
            control.initiate();
        }
        let short_workers = self
            .inner
            .short_workers
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
            .unwrap_or_default();
        let inner = Arc::clone(&self.inner);
        let (response, receiver) = oneshot::channel();
        thread::Builder::new()
            .name("att-sqlite-shutdown".to_owned())
            .stack_size(self.inner.config.worker_stack_bytes.get())
            .spawn(move || {
                let mut panic = false;
                for worker in short_workers {
                    panic |= worker.join().is_err();
                }
                inner.interactive_session.wait_until_empty();
                inner.lifecycle.store(STORAGE_CLOSED, Ordering::Release);
                let result = if panic {
                    Err(SqliteRuntimeError::WorkerPanicked("shutdown"))
                } else {
                    Ok(())
                };
                let _ = response.send(result);
            })
            .map_err(|source| SqliteRuntimeError::WorkerSpawn {
                worker: "shutdown".to_owned(),
                source,
            })?;
        receiver
            .await
            .unwrap_or(Err(SqliteRuntimeError::WorkerPanicked("shutdown")))
    }
}

async fn acquire_short_admission(
    admission: Arc<Semaphore>,
) -> Result<OwnedSemaphorePermit, SqliteRuntimeError> {
    admission
        .acquire_owned()
        .await
        .map_err(|_| SqliteRuntimeError::Cancelled {
            operation: "等待 SQLite 短操作执行许可",
        })
}

impl SqliteDatabaseCreator for RusqliteStorage {
    type Error = SqliteRuntimeError;

    fn create_new_database(
        &self,
        path: PathBuf,
        commands: Vec<SqliteCommand>,
    ) -> impl Future<Output = Result<(), CreateDatabaseError<Self::Error>>> + Send {
        let sender = self.inner.short_sender.clone();
        let admission = Arc::clone(&self.inner.short_admission);
        let accepting = self.ensure_accepting();
        async move {
            accepting.map_err(CreateDatabaseError::NotCreated)?;
            let permit = acquire_short_admission(admission)
                .await
                .map_err(CreateDatabaseError::NotCreated)?;
            let (response, receiver) = oneshot::channel();
            sender
                .send(AdmittedShortJob {
                    job: ShortJob::Create {
                        path,
                        commands,
                        response,
                        #[cfg(test)]
                        panic_after_operation: false,
                    },
                    _permit: permit,
                })
                .await
                .map_err(|_| CreateDatabaseError::NotCreated(SqliteRuntimeError::Closed))?;
            receiver
                .await
                .unwrap_or(Err(CreateDatabaseError::OutcomeUnknown(
                    SqliteRuntimeError::WorkerPanicked("短操作"),
                )))
        }
    }
}

impl SqliteDatabaseSnapshotter for RusqliteStorage {
    type Error = SqliteRuntimeError;

    fn snapshot_database(
        &self,
        source: PathBuf,
        destination: PathBuf,
    ) -> impl Future<Output = Result<(), SnapshotDatabaseError<Self::Error>>> + Send {
        let sender = self.inner.short_sender.clone();
        let admission = Arc::clone(&self.inner.short_admission);
        let accepting = self.ensure_accepting();
        async move {
            accepting.map_err(SnapshotDatabaseError::NotCreated)?;
            let permit = acquire_short_admission(admission)
                .await
                .map_err(SnapshotDatabaseError::NotCreated)?;
            let (response, receiver) = oneshot::channel();
            sender
                .send(AdmittedShortJob {
                    job: ShortJob::Snapshot {
                        source,
                        destination,
                        response,
                        #[cfg(test)]
                        panic_after_operation: false,
                    },
                    _permit: permit,
                })
                .await
                .map_err(|_| SnapshotDatabaseError::NotCreated(SqliteRuntimeError::Closed))?;
            receiver
                .await
                .unwrap_or(Err(SnapshotDatabaseError::OutcomeUnknown(
                    SqliteRuntimeError::WorkerPanicked("短操作"),
                )))
        }
    }
}

impl SqliteQueryExecutor for RusqliteStorage {
    type Error = SqliteRuntimeError;

    fn query_existing_database(
        &self,
        path: PathBuf,
        query: SqliteQuery,
    ) -> impl Future<Output = Result<Vec<SqliteRow>, QueryExistingDatabaseError<Self::Error>>> + Send
    {
        let sender = self.inner.short_sender.clone();
        let admission = Arc::clone(&self.inner.short_admission);
        let accepting = self.ensure_accepting();
        async move {
            accepting.map_err(QueryExistingDatabaseError::QueryFailed)?;
            let permit = acquire_short_admission(admission)
                .await
                .map_err(QueryExistingDatabaseError::QueryFailed)?;
            let (response, receiver) = oneshot::channel();
            sender
                .send(AdmittedShortJob {
                    job: ShortJob::Query {
                        path,
                        query,
                        response,
                    },
                    _permit: permit,
                })
                .await
                .map_err(|_| QueryExistingDatabaseError::QueryFailed(SqliteRuntimeError::Closed))?;
            receiver
                .await
                .unwrap_or(Err(QueryExistingDatabaseError::QueryFailed(
                    SqliteRuntimeError::WorkerPanicked("短操作"),
                )))
        }
    }

    fn query_existing_database_snapshot(
        &self,
        path: PathBuf,
        queries: Vec<SqliteQuery>,
    ) -> impl Future<Output = Result<Vec<Vec<SqliteRow>>, QueryExistingDatabaseError<Self::Error>>> + Send
    {
        let sender = self.inner.short_sender.clone();
        let admission = Arc::clone(&self.inner.short_admission);
        let accepting = self.ensure_accepting();
        async move {
            accepting.map_err(QueryExistingDatabaseError::QueryFailed)?;
            let permit = acquire_short_admission(admission)
                .await
                .map_err(QueryExistingDatabaseError::QueryFailed)?;
            let (response, receiver) = oneshot::channel();
            sender
                .send(AdmittedShortJob {
                    job: ShortJob::QuerySnapshot {
                        path,
                        queries,
                        response,
                    },
                    _permit: permit,
                })
                .await
                .map_err(|_| QueryExistingDatabaseError::QueryFailed(SqliteRuntimeError::Closed))?;
            receiver
                .await
                .unwrap_or(Err(QueryExistingDatabaseError::QueryFailed(
                    SqliteRuntimeError::WorkerPanicked("短操作"),
                )))
        }
    }
}

impl SqliteTransactionExecutor for RusqliteStorage {
    type Error = SqliteRuntimeError;

    fn execute_transaction(
        &self,
        path: PathBuf,
        plan: SqliteTransactionPlan,
    ) -> impl Future<Output = Result<(), ExecuteTransactionError<Self::Error>>> + Send {
        let sender = self.inner.short_sender.clone();
        let admission = Arc::clone(&self.inner.short_admission);
        let accepting = self.ensure_accepting();
        async move {
            accepting.map_err(ExecuteTransactionError::NotCommitted)?;
            let permit = acquire_short_admission(admission)
                .await
                .map_err(ExecuteTransactionError::NotCommitted)?;
            let (response, receiver) = oneshot::channel();
            sender
                .send(AdmittedShortJob {
                    job: ShortJob::Transaction {
                        path,
                        plan,
                        response,
                        #[cfg(test)]
                        panic_after_operation: false,
                    },
                    _permit: permit,
                })
                .await
                .map_err(|_| ExecuteTransactionError::NotCommitted(SqliteRuntimeError::Closed))?;
            receiver
                .await
                .unwrap_or(Err(ExecuteTransactionError::OutcomeUnknown(
                    SqliteRuntimeError::WorkerPanicked("短操作"),
                )))
        }
    }
}

impl SqliteFinalTransactionExecutor for RusqliteFinalTransactionExecutor {
    type Error = SqliteRuntimeError;

    fn execute_final_transaction(
        &self,
        path: PathBuf,
        plan: SqliteTransactionPlan,
    ) -> impl Future<Output = Result<(), ExecuteFinalTransactionError<Self::Error>>> + Send {
        let config = Arc::clone(&self.config);
        let performance = Arc::clone(&self.performance);
        let wait_cancellation = self.wait_cancellation.clone();
        async move {
            let (response, receiver) = oneshot::channel();
            let stack_size = config.worker_stack_bytes.get();
            thread::Builder::new()
                .name("att-sqlite-final-transaction".to_owned())
                .stack_size(stack_size)
                .spawn(move || {
                    install_sqlite_busy_cancellation(wait_cancellation);
                    let result = catch_unwind(AssertUnwindSafe(|| {
                        run_final_transaction(&path, &plan, &config, &performance)
                    }))
                    .unwrap_or_else(|_| {
                        Err(ExecuteFinalTransactionError::OutcomeUnknown(
                            SqliteRuntimeError::WorkerPanicked("最终短事务"),
                        ))
                    });
                    let _ = response.send(result);
                })
                .map_err(|source| {
                    ExecuteFinalTransactionError::NotCommitted(SqliteRuntimeError::WorkerSpawn {
                        worker: "final-transaction".to_owned(),
                        source,
                    })
                })?;

            receiver
                .await
                .unwrap_or(Err(ExecuteFinalTransactionError::OutcomeUnknown(
                    SqliteRuntimeError::WorkerPanicked("最终短事务"),
                )))
        }
    }
}

impl SqliteTransactionSessionFactory for RusqliteStorage {
    type Operations = RusqliteTransactionSessionOperations;
    type Finalizer = RusqliteInteractiveSessionFinalizer;
    type Error = SqliteRuntimeError;

    async fn open_existing_transaction_session(
        &self,
        path: PathBuf,
    ) -> Result<
        OpenedSqliteTransactionSession<Self::Operations, Self::Finalizer>,
        OpenSqliteTransactionSessionError<Self::Error>,
    > {
        let accepting = self.ensure_accepting();
        let config = Arc::clone(&self.inner.config);
        let performance = Arc::clone(&self.inner.performance);
        let slot = Arc::clone(&self.inner.interactive_session);
        let wait_cancellation = self.inner.wait_cancellation.clone();
        accepting.map_err(OpenSqliteTransactionSessionError::OpenFailed)?;
        slot.begin_open()
            .map_err(OpenSqliteTransactionSessionError::OpenFailed)?;
        let (response, receiver) = oneshot::channel();
        let worker_slot = Arc::clone(&slot);
        if let Err(source) = thread::Builder::new()
            .name("att-sqlite-open".to_owned())
            .stack_size(config.worker_stack_bytes.get())
            .spawn(move || {
                let result = match catch_unwind(AssertUnwindSafe(|| {
                    open_transaction_session(
                        &path,
                        config,
                        performance,
                        Arc::clone(&worker_slot),
                        wait_cancellation,
                    )
                })) {
                    Ok(result) => {
                        if result.is_err() {
                            worker_slot.abort_open();
                        }
                        result
                    }
                    Err(_) => {
                        if let Some(control) = worker_slot.recover_open_panic() {
                            control.initiate();
                        }
                        Err(OpenSqliteTransactionSessionError::OpenFailed(
                            SqliteRuntimeError::WorkerPanicked("事务计划会话打开"),
                        ))
                    }
                };
                let _ = response.send(result);
            })
        {
            slot.abort_open();
            return Err(OpenSqliteTransactionSessionError::OpenFailed(
                SqliteRuntimeError::WorkerSpawn {
                    worker: "transaction-session-open".to_owned(),
                    source,
                },
            ));
        }
        receiver
            .await
            .unwrap_or(Err(OpenSqliteTransactionSessionError::OpenFailed(
                SqliteRuntimeError::WorkerPanicked("事务计划会话打开"),
            )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::task::{Context, Poll};

    use crate::storage::sqlite::SqliteBatch;
    use futures_util::task::noop_waker_ref;

    #[test]
    fn driver_leaf_preserves_database_query_column_codes_and_transaction() {
        let source = SqliteRuntimeError::QueryContext {
            query_id: "load_units".to_owned(),
            ordinal: 3,
            source: Box::new(SqliteRuntimeError::Driver {
                operation: "query",
                source: rusqlite::Error::InvalidColumnIndex(5),
            }),
        };
        let report = source.diagnostic_report(
            Path::new("database.sqlite"),
            SqliteDiagnosticContext::new(
                crate::diagnostic::SqliteDiagnosticStage::Translate,
                SqliteOperation::Query,
                crate::diagnostic::SqliteTransactionState::Active,
            ),
            StateEffect::Unchanged,
        );
        assert_eq!(
            serde_json::to_value(report).expect("报告应可序列化"),
            serde_json::json!({
                "effect": "unchanged",
                "primary": {
                    "code": "sqlite.driver",
                    "stage": "translate",
                    "issue": {
                        "family": "sqlite",
                        "details": {
                            "context": {
                                "stage": "translate",
                                "operation": "query",
                                "transaction": "active"
                            },
                            "problem": {
                                "kind": "driver",
                                "database": "database.sqlite",
                                "query_id": "load_units",
                                "query_ordinal": 3,
                                "failure": {
                                    "kind": "invalid_column_index",
                                    "primary_code": null,
                                    "extended_code": null,
                                    "column_index": 5,
                                    "column_name": null,
                                    "parameter_actual": null,
                                    "parameter_expected": null,
                                    "changed_rows": null,
                                    "sql_offset": null,
                                    "database_index": null
                                }
                            }
                        }
                    },
                    "resolution": "retry"
                },
                "related": []
            })
        );
    }

    struct TestDirectory(tempfile::TempDir);

    impl TestDirectory {
        fn new() -> Self {
            Self(tempfile::tempdir().expect("测试目录应可创建"))
        }

        fn database(&self, name: &str) -> PathBuf {
            self.0.path().join(name)
        }
    }

    fn nonzero(value: usize) -> NonZeroUsize {
        NonZeroUsize::new(value).expect("测试值必须非零")
    }

    async fn wait_until(description: &'static str, mut condition: impl FnMut() -> bool) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while !condition() {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("等待{description}超时"));
    }

    fn release_write_lock_after(database: &Path, delay: Duration) -> JoinHandle<()> {
        let blocker = Connection::open(database).expect("应建立测试写锁连接");
        blocker
            .execute_batch("BEGIN IMMEDIATE")
            .expect("测试写锁应可取得");
        thread::spawn(move || {
            thread::sleep(delay);
            blocker.execute_batch("ROLLBACK").expect("测试写锁应可释放");
        })
    }

    fn run_bounded_long_query(connection: &Connection) -> rusqlite::Result<i64> {
        connection.query_row(
            "WITH RECURSIVE sequence(value) AS (
                 VALUES(0)
                 UNION ALL
                 SELECT value + 1 FROM sequence WHERE value < 100000
             )
             SELECT sum(value) FROM sequence",
            [],
            |row| row.get(0),
        )
    }

    #[test]
    fn same_thread_cancellable_connections_use_their_own_busy_contexts() {
        let directory = TestDirectory::new();
        let database = directory.database("direct-independent-busy.db");
        Connection::open(&database)
            .unwrap()
            .execute_batch("CREATE TABLE value(id INTEGER PRIMARY KEY)")
            .unwrap();

        let first_cancellation = Arc::new(AtomicBool::new(false));
        let first_wait_cancellation = Arc::clone(&first_cancellation);
        let first = apply_att_sqlite_cancellable_read_write_policy(
            Connection::open(&database).unwrap(),
            move || first_wait_cancellation.load(Ordering::Acquire),
        )
        .unwrap();
        let second_cancellation = Arc::new(AtomicBool::new(true));
        let second_wait_cancellation = Arc::clone(&second_cancellation);
        let second = apply_att_sqlite_cancellable_read_write_policy(
            Connection::open(&database).unwrap(),
            move || second_wait_cancellation.load(Ordering::Acquire),
        )
        .unwrap();
        // 这项测试只验证 raw busy callback 的 context；progress 的共享 context 由下面的
        // 嵌套长查询测试覆盖。
        first.progress_handler(0, None::<fn() -> bool>).unwrap();
        second.progress_handler(0, None::<fn() -> bool>).unwrap();

        let release = release_write_lock_after(&database, Duration::from_millis(75));
        first
            .execute("INSERT INTO value DEFAULT VALUES", [])
            .expect("第二条连接已取消不得误取消第一条连接的 busy 等待");
        release.join().unwrap();

        first_cancellation.store(true, Ordering::Release);
        second_cancellation.store(false, Ordering::Release);
        let release = release_write_lock_after(&database, Duration::from_millis(200));
        let error = first
            .execute("INSERT INTO value DEFAULT VALUES", [])
            .expect_err("第一条连接自己的取消必须停止 busy 等待");
        assert!(matches!(
            error.sqlite_error_code(),
            Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
        ));
        release.join().unwrap();

        let release = release_write_lock_after(&database, Duration::from_millis(75));
        second
            .execute("INSERT INTO value DEFAULT VALUES", [])
            .expect("第一条连接已取消不得误取消第二条连接的 busy 等待");
        release.join().unwrap();

        first_cancellation.store(false, Ordering::Release);
        second_cancellation.store(true, Ordering::Release);
        let release = release_write_lock_after(&database, Duration::from_millis(200));
        let error = second
            .execute("INSERT INTO value DEFAULT VALUES", [])
            .expect_err("第二条连接自己的取消必须停止 busy 等待");
        assert!(matches!(
            error.sqlite_error_code(),
            Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
        ));
        release.join().unwrap();
    }

    #[test]
    fn cancellable_connection_drop_releases_its_captured_resources() {
        let captured = Arc::new(());
        let weak = Arc::downgrade(&captured);
        let callback_capture = Arc::clone(&captured);
        let connection = apply_att_sqlite_cancellable_read_write_policy(
            Connection::open_in_memory().unwrap(),
            move || {
                let _ = Arc::strong_count(&callback_capture);
                false
            },
        )
        .unwrap();
        drop(captured);
        assert!(
            weak.upgrade().is_some(),
            "连接存活时 progress/busy probe 应持有命令资源"
        );

        drop(connection);
        assert!(
            weak.upgrade().is_none(),
            "连接销毁后不得由 busy/progress context 继续持有上一命令资源"
        );
    }

    #[test]
    fn direct_connection_does_not_replace_the_ordinary_tls_busy_cancellation() {
        let ordinary_cancellation = SqliteWaitCancellation::default();
        install_sqlite_busy_cancellation(ordinary_cancellation.clone());
        ordinary_cancellation.request();
        let connection = apply_att_sqlite_cancellable_read_write_policy(
            Connection::open_in_memory().unwrap(),
            || false,
        )
        .unwrap();
        assert!(
            sqlite_busy_wait_cancelled(),
            "直接连接不得覆盖普通 runtime worker 的 TLS 取消事实"
        );
        drop(connection);
        assert!(
            sqlite_busy_wait_cancelled(),
            "直接连接销毁也不得改写普通 TLS 取消事实"
        );
        install_sqlite_busy_cancellation(SqliteWaitCancellation::default());
    }

    #[test]
    fn cancellable_policy_setup_failure_releases_its_context() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("BEGIN").unwrap();
        let captured = Arc::new(());
        let weak = Arc::downgrade(&captured);
        let callback_capture = Arc::clone(&captured);

        let result = apply_att_sqlite_cancellable_read_write_policy(connection, move || {
            let _ = Arc::strong_count(&callback_capture);
            true
        });
        drop(captured);
        assert!(result.is_err(), "事务内修改 journal 策略必须失败");
        assert!(
            weak.upgrade().is_none(),
            "策略设置失败必须卸载 progress/busy callback 并释放命令资源"
        );
    }

    #[test]
    fn nested_cancellation_suspensions_restore_only_after_lifo_outer_drop() {
        let cancellation = Arc::new(AtomicBool::new(false));
        let wait_cancellation = Arc::clone(&cancellation);
        let connection = apply_att_sqlite_cancellable_read_write_policy(
            Connection::open_in_memory().unwrap(),
            move || wait_cancellation.load(Ordering::Acquire),
        )
        .unwrap();
        let handle = connection.cancellation_handle();
        cancellation.store(true, Ordering::Release);

        let outer = suspend_att_sqlite_cancellation(&handle);
        let inner = suspend_att_sqlite_cancellation(&handle);
        drop(inner);
        assert_eq!(
            run_bounded_long_query(&connection).expect("外层 guard 存活时 progress 取消仍应暂停"),
            5_000_050_000
        );
        drop(outer);
        let error = run_bounded_long_query(&connection)
            .expect_err("最外层 guard 销毁后必须恢复 progress 取消");
        assert_eq!(
            error.sqlite_error_code(),
            Some(ErrorCode::OperationInterrupted)
        );
    }

    #[test]
    fn nested_cancellation_suspensions_restore_only_after_out_of_order_last_drop() {
        let cancellation = Arc::new(AtomicBool::new(false));
        let wait_cancellation = Arc::clone(&cancellation);
        let connection = apply_att_sqlite_cancellable_read_write_policy(
            Connection::open_in_memory().unwrap(),
            move || wait_cancellation.load(Ordering::Acquire),
        )
        .unwrap();
        let handle = connection.cancellation_handle();
        cancellation.store(true, Ordering::Release);

        let outer = suspend_att_sqlite_cancellation(&handle);
        let inner = suspend_att_sqlite_cancellation(&handle);
        drop(outer);
        assert_eq!(
            run_bounded_long_query(&connection)
                .expect("后创建的 guard 存活时乱序销毁不得恢复 progress 取消"),
            5_000_050_000
        );
        drop(inner);
        let error = run_bounded_long_query(&connection)
            .expect_err("最后一个 guard 销毁后必须恢复 progress 取消");
        assert_eq!(
            error.sqlite_error_code(),
            Some(ErrorCode::OperationInterrupted)
        );
    }

    #[test]
    fn cancellable_policy_waits_for_an_opening_lock_and_succeeds_after_release() {
        let directory = TestDirectory::new();
        let database = directory.database("direct-open-wait.db");
        let blocker = Connection::open(&database).unwrap();
        blocker
            .execute_batch(
                "CREATE TABLE value(id INTEGER PRIMARY KEY);
                 BEGIN EXCLUSIVE",
            )
            .unwrap();
        let (started_sender, started_receiver) = mpsc::sync_channel(0);
        let (result_sender, result_receiver) = mpsc::sync_channel(1);
        let worker = thread::spawn(move || {
            let connection = Connection::open(database).unwrap();
            let result = apply_att_sqlite_cancellable_read_write_policy(connection, move || {
                let _ = started_sender.try_send(());
                false
            })
            .map(drop);
            result_sender.send(result).unwrap();
        });
        started_receiver
            .recv_timeout(Duration::from_secs(2))
            .expect("打开策略必须进入 busy handler");
        assert!(
            result_receiver
                .recv_timeout(Duration::from_millis(100))
                .is_err(),
            "打开策略遇到外部锁时必须等待，不能沿用 SQLite 默认 Busy"
        );

        blocker.execute_batch("ROLLBACK").unwrap();
        result_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("锁释放后打开策略应及时继续")
            .expect("锁释放后打开策略应成功");
        worker.join().unwrap();
    }

    #[test]
    fn cancellable_policy_stops_waiting_for_an_opening_lock_after_cancellation() {
        let directory = TestDirectory::new();
        let database = directory.database("direct-open-cancel.db");
        let blocker = Connection::open(&database).unwrap();
        blocker
            .execute_batch(
                "CREATE TABLE value(id INTEGER PRIMARY KEY);
                 BEGIN EXCLUSIVE",
            )
            .unwrap();
        let cancellation = Arc::new(AtomicBool::new(false));
        let worker_cancellation = Arc::clone(&cancellation);
        let (started_sender, started_receiver) = mpsc::sync_channel(0);
        let (result_sender, result_receiver) = mpsc::sync_channel(1);
        let worker = thread::spawn(move || {
            let connection = Connection::open(database).unwrap();
            let result = apply_att_sqlite_cancellable_read_write_policy(connection, move || {
                let _ = started_sender.try_send(());
                worker_cancellation.load(Ordering::Acquire)
            })
            .map(drop);
            result_sender.send(result).unwrap();
        });
        started_receiver
            .recv_timeout(Duration::from_secs(2))
            .expect("打开策略必须进入 busy handler");
        assert!(
            result_receiver
                .recv_timeout(Duration::from_millis(100))
                .is_err(),
            "发出取消前打开策略应继续等待外部锁"
        );

        cancellation.store(true, Ordering::Release);
        let error = result_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("取消后打开策略应及时停止等待")
            .expect_err("外部锁仍存在时取消必须让打开策略失败");
        assert!(matches!(
            error.sqlite_error_code(),
            Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
        ));
        blocker.execute_batch("ROLLBACK").unwrap();
        worker.join().unwrap();
    }

    #[test]
    fn cancellable_direct_connection_interrupts_an_active_query() {
        let directory = TestDirectory::new();
        let database = directory.database("direct-query-cancel.db");
        let cancellation = Arc::new(AtomicBool::new(false));
        let worker_cancellation = Arc::clone(&cancellation);
        let query_started = Arc::new(AtomicBool::new(false));
        let worker_query_started = Arc::clone(&query_started);
        let (active_sender, active_receiver) = mpsc::sync_channel(1);
        let (result_sender, result_receiver) = mpsc::sync_channel(1);

        let worker = thread::spawn(move || {
            let connection = Connection::open(database).expect("应建立直接 SQLite 连接");
            let connection =
                apply_att_sqlite_cancellable_read_write_policy(connection, move || {
                    if worker_query_started.load(Ordering::Acquire) {
                        let _ = active_sender.try_send(());
                    }
                    worker_cancellation.load(Ordering::Acquire)
                })
                .expect("应安装可取消直接连接策略");
            query_started.store(true, Ordering::Release);
            let result = connection.query_row(
                "WITH RECURSIVE sequence(value) AS (
                     VALUES(0)
                     UNION ALL
                     SELECT value + 1 FROM sequence WHERE value < 1000000000
                 )
                 SELECT sum(value) FROM sequence",
                [],
                |row| row.get::<_, i64>(0),
            );
            result_sender.send(result).expect("应返回长查询结果");
        });

        active_receiver
            .recv_timeout(Duration::from_secs(2))
            .expect("长查询必须进入 progress handler");
        cancellation.store(true, Ordering::Release);
        let error = result_receiver
            .recv_timeout(Duration::from_secs(1))
            .expect("取消后长查询必须及时返回")
            .expect_err("取消必须中断正在执行的长查询");
        assert_eq!(
            error.sqlite_error_code(),
            Some(ErrorCode::OperationInterrupted)
        );
        worker.join().expect("长查询 worker 不应 panic");
    }

    fn configuration_with_workers(worker_threads: usize) -> RusqliteStorageConfiguration {
        RusqliteStorageConfiguration::new(nonzero(worker_threads), nonzero(1024 * 1024))
    }

    fn configuration() -> RusqliteStorageConfiguration {
        configuration_with_workers(2)
    }

    #[test]
    fn production_parallelism_probe_failure_is_a_typed_os_diagnostic() {
        let error = match RusqliteStorage::start_with_available_parallelism(
            RusqliteStorageConfiguration::production(),
            Arc::new(RunPerformanceCounters::default()),
            || Err(io::Error::from_raw_os_error(5)),
        ) {
            Ok(_) => panic!("并行度探测失败时不得启动 SQLite 根"),
            Err(error) => error,
        };
        assert!(matches!(
            &error,
            SqliteRuntimeError::AvailableParallelism { source }
                if source.raw_os_error() == Some(5)
        ));
        let report = error.startup_diagnostic_report();
        let wire = serde_json::to_value(report).expect("启动诊断必须可序列化");
        assert_eq!(
            wire.pointer("/primary/issue/details/failure/raw_os_code"),
            Some(&serde_json::json!(5))
        );
    }

    #[tokio::test]
    async fn production_short_worker_width_caps_available_parallelism_at_four() {
        let storage = RusqliteStorage::start_with_available_parallelism(
            RusqliteStorageConfiguration::production(),
            Arc::new(RunPerformanceCounters::default()),
            || Ok(nonzero(12)),
        )
        .expect("并行度探测成功时 SQLite 根应启动");
        assert_eq!(storage.inner.short_admission.available_permits(), 4);
        assert_eq!(
            storage
                .inner
                .short_workers
                .lock()
                .expect("worker 记录锁不应中毒")
                .as_ref()
                .expect("运行中的根必须持有 worker")
                .len(),
            4
        );
        storage.shutdown().await.expect("SQLite 根应关闭");
    }

    fn schema_commands() -> Vec<SqliteCommand> {
        vec![SqliteCommand::new(
            "CREATE TABLE values_table (id INTEGER PRIMARY KEY, n INTEGER, r REAL, t TEXT, b BLOB, z BLOB)",
            Vec::new(),
        )]
    }

    fn create_foreign_guard_database(path: &Path) {
        let connection = Connection::open(path).expect("外来 SQLite 应可建立");
        connection
            .execute_batch(
                "CREATE TABLE foreign_guard (value TEXT NOT NULL);\
                 INSERT INTO foreign_guard (value) VALUES ('foreign-unchanged');",
            )
            .expect("外来 SQLite 标记应可写入");
    }

    fn read_foreign_guard(path: &Path) -> String {
        Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .expect("外来 SQLite 必须仍位于原路径")
        .query_row("SELECT value FROM foreign_guard", [], |row| row.get(0))
        .expect("外来 SQLite 标记不得被改写")
    }

    #[tokio::test]
    async fn performance_counters_cover_every_successful_transaction_control_scope() {
        let directory = TestDirectory::new();
        let database = directory.database("performance-success.db");
        let config = configuration();
        let performance = Arc::new(RunPerformanceCounters::default());
        let storage =
            RusqliteStorage::start_with_performance(config.clone(), Arc::clone(&performance))
                .expect("带性能计数器的 SQLite 根应可启动");

        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("建库事务应成功");
        storage
            .query_existing_database_snapshot(
                database.clone(),
                vec![SqliteQuery::new("SELECT 1", Vec::new())],
            )
            .await
            .expect("只读快照事务应成功");
        storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                    SqliteCommand::new("INSERT INTO values_table (id) VALUES (1)", Vec::new()),
                )]),
            )
            .await
            .expect("写计划事务应成功");

        let opened = storage
            .open_existing_transaction_session(database.clone())
            .await
            .expect("事务计划会话应可打开");
        let (_operations, finalizer) = opened.into_parts();
        finalizer.finalize().await.expect("事务计划会话应可终结");
        storage.shutdown().await.expect("SQLite 根应关闭");

        RusqliteFinalTransactionExecutor::new_with_performance(config, Arc::clone(&performance))
            .execute_final_transaction(
                database,
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                    SqliteCommand::new("UPDATE values_table SET n = 1 WHERE id = 1", Vec::new()),
                )]),
            )
            .await
            .expect("最终写计划事务应成功");

        let transactions = performance.snapshot().sqlite_transactions;
        assert_eq!(transactions.read_snapshot.begin.attempted, 1);
        assert_eq!(transactions.read_snapshot.begin.succeeded, 1);
        assert_eq!(transactions.read_snapshot.commit.attempted, 1);
        assert_eq!(transactions.read_snapshot.commit.succeeded, 1);
        assert_eq!(transactions.read_snapshot.rollback.attempted, 0);

        assert_eq!(transactions.write_plan.begin.attempted, 2);
        assert_eq!(transactions.write_plan.begin.succeeded, 2);
        assert_eq!(transactions.write_plan.commit.attempted, 2);
        assert_eq!(transactions.write_plan.commit.succeeded, 2);
        assert_eq!(transactions.write_plan.rollback.attempted, 0);

        assert_eq!(transactions.database_initialization.begin.attempted, 1);
        assert_eq!(transactions.database_initialization.begin.succeeded, 1);
        assert_eq!(transactions.database_initialization.commit.attempted, 1);
        assert_eq!(transactions.database_initialization.commit.succeeded, 1);
        assert_eq!(transactions.database_initialization.rollback.attempted, 0);

        assert_eq!(transactions.interactive.begin.attempted, 0);
        assert_eq!(transactions.interactive.commit.attempted, 0);
        assert_eq!(transactions.interactive.rollback.attempted, 0);
        assert_eq!(transactions.attempted_total(), 8);
    }

    #[tokio::test]
    async fn failed_write_plan_counts_the_real_successful_rollback() {
        let directory = TestDirectory::new();
        let database = directory.database("performance-rollback.db");
        let performance = Arc::new(RunPerformanceCounters::default());
        let storage =
            RusqliteStorage::start_with_performance(configuration(), Arc::clone(&performance))
                .expect("带性能计数器的 SQLite 根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("测试数据库应可创建");

        let result = storage
            .execute_transaction(
                database,
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "INSERT INTO values_table (id) VALUES (1)",
                        Vec::new(),
                    )),
                    SqliteTransactionStep::RequireNoRows(SqliteQuery::new(
                        "SELECT 1 FROM values_table WHERE id = 1",
                        Vec::new(),
                    )),
                ]),
            )
            .await;
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::RequirementFailed)
        ));
        storage.shutdown().await.expect("SQLite 根应关闭");

        let write = performance.snapshot().sqlite_transactions.write_plan;
        assert_eq!(write.begin.attempted, 1);
        assert_eq!(write.begin.succeeded, 1);
        assert_eq!(write.commit.attempted, 0);
        assert_eq!(write.rollback.attempted, 1);
        assert_eq!(write.rollback.succeeded, 1);
    }

    #[tokio::test]
    async fn default_storage_and_final_executor_counters_are_instance_isolated() {
        let directory = TestDirectory::new();
        let first_database = directory.database("performance-isolation-first.db");
        let first = RusqliteStorage::start(configuration()).expect("首个 SQLite 根应启动");
        let second = RusqliteStorage::start(configuration()).expect("第二个 SQLite 根应启动");
        assert!(!Arc::ptr_eq(
            &first.inner.performance,
            &second.inner.performance
        ));

        first
            .create_new_database(first_database, schema_commands())
            .await
            .expect("只应在首个实例上创建数据库");
        assert_eq!(
            first
                .inner
                .performance
                .snapshot()
                .sqlite_transactions
                .database_initialization
                .begin
                .attempted,
            1
        );
        assert_eq!(
            second
                .inner
                .performance
                .snapshot()
                .sqlite_transactions
                .attempted_total(),
            0
        );

        let first_final = RusqliteFinalTransactionExecutor::new(configuration());
        let second_final = RusqliteFinalTransactionExecutor::new(configuration());
        assert!(!Arc::ptr_eq(
            &first_final.performance,
            &second_final.performance
        ));
        first.shutdown().await.expect("首个 SQLite 根应关闭");
        second.shutdown().await.expect("第二个 SQLite 根应关闭");
    }

    #[test]
    fn read_write_policy_enables_foreign_key_constraints() {
        let connection = Connection::open_in_memory().expect("内存数据库应可打开");
        apply_read_write_policy(&connection, &configuration()).expect("读写策略应可应用");

        let foreign_keys: i64 = connection
            .query_row("PRAGMA foreign_keys", [], |row| row.get(0))
            .expect("应可读取 foreign_keys 状态");
        assert_eq!(foreign_keys, 1);

        connection
            .execute_batch(
                "CREATE TABLE parent (id INTEGER PRIMARY KEY);\
                 CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));",
            )
            .expect("外键测试表应可创建");
        let error = connection
            .execute("INSERT INTO child (parent_id) VALUES (1)", [])
            .expect_err("不存在的父行必须触发外键约束");
        assert!(matches!(error, rusqlite::Error::SqliteFailure(_, _)));
    }

    #[test]
    fn read_write_policy_applies_the_benchmarked_connection_memory_policy() {
        let connection = Connection::open_in_memory().expect("内存数据库应可打开");
        apply_read_write_policy(&connection, &configuration()).expect("读写策略应可应用");

        let cache_size: i64 = connection
            .query_row("PRAGMA cache_size", [], |row| row.get(0))
            .expect("应可读取 cache_size");
        let temp_store: i64 = connection
            .query_row("PRAGMA temp_store", [], |row| row.get(0))
            .expect("应可读取 temp_store");
        assert_eq!(cache_size, CONNECTION_CACHE_SIZE_KIB);
        assert_eq!(temp_store, 2, "SQLite MEMORY TEMP 模式应返回枚举值 2");
    }

    #[tokio::test]
    async fn newly_created_database_uses_the_benchmarked_page_size() {
        let directory = TestDirectory::new();
        let database = directory.database("page-size.db");
        let storage = RusqliteStorage::start(configuration()).expect("SQLite 根应启动");

        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage.shutdown().await.expect("SQLite 根应关闭");

        let page_size: i64 = Connection::open(database)
            .expect("新数据库应可重开")
            .query_row("PRAGMA page_size", [], |row| row.get(0))
            .expect("应可读取 page_size");
        assert_eq!(page_size, NEW_DATABASE_PAGE_SIZE_BYTES);
    }

    #[test]
    fn read_only_policy_enables_foreign_key_constraints() {
        let directory = TestDirectory::new();
        let database = directory.database("read-only-foreign-keys.db");
        Connection::open(&database)
            .expect("测试数据库应可创建")
            .close()
            .expect("测试数据库应可关闭");

        let connection = match open_existing_read_only(&database, &configuration()) {
            Ok(connection) => connection,
            Err(_) => panic!("现存数据库应可只读打开"),
        };
        let foreign_keys: i64 = connection
            .query_row("PRAGMA foreign_keys", [], |row| row.get(0))
            .expect("应可读取 foreign_keys 状态");
        assert_eq!(foreign_keys, 1);
        let cache_size: i64 = connection
            .query_row("PRAGMA cache_size", [], |row| row.get(0))
            .expect("应可读取只读连接 cache_size");
        let temp_store: i64 = connection
            .query_row("PRAGMA temp_store", [], |row| row.get(0))
            .expect("应可读取只读连接 temp_store");
        assert_eq!(cache_size, CONNECTION_CACHE_SIZE_KIB);
        assert_eq!(temp_store, 2);
    }

    #[test]
    fn driver_diagnostic_exposes_primary_and_extended_codes_but_not_driver_text() {
        let source = rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_CONSTRAINT_UNIQUE),
            Some("SQL_AND_PARAMETER_BODY_SENTINEL".to_owned()),
        );
        let report = SqliteRuntimeError::Driver {
            operation: "execute_transaction",
            source,
        }
        .diagnostic_report(
            Path::new("D:/projects/game/project.db"),
            SqliteDiagnosticContext::new(
                SqliteDiagnosticStage::Translate,
                SqliteOperation::Execute,
                SqliteTransactionState::NotStarted,
            ),
            StateEffect::ProgressPreserved,
        );
        let serialized = serde_json::to_string(&report).expect("诊断应可序列化");

        assert!(!serialized.contains("SQL_AND_PARAMETER_BODY_SENTINEL"));
        assert!(serialized.contains("\"primary_code\":19"));
        assert!(serialized.contains("\"extended_code\":2067"));
        assert!(serialized.contains("\"operation\":\"execute\""));
    }

    #[test]
    fn multi_query_read_uses_one_consistent_wal_snapshot() {
        let directory = TestDirectory::new();
        let database = directory.database("read-snapshot-wal.db");
        let writer_path = database.clone();
        let initializer = Connection::open(&database).expect("测试数据库应可创建");
        initializer
            .pragma_update(None, "journal_mode", "WAL")
            .expect("测试数据库应可启用 WAL");
        initializer
            .execute_batch(
                "CREATE TABLE snapshot_value (value INTEGER NOT NULL);\
                 INSERT INTO snapshot_value VALUES (1);",
            )
            .expect("测试数据应可建立");
        drop(initializer);

        let reader = open_existing_read_only(&database, &configuration())
            .unwrap_or_else(|_| panic!("测试数据库应可只读打开"));
        let (start_writer, writer_started) = mpsc::sync_channel(0);
        let (writer_finished, wait_writer) = mpsc::sync_channel(0);
        let writer = thread::spawn(move || {
            writer_started.recv().expect("应通知 writer 开始");
            let connection = Connection::open(writer_path).expect("writer 应可打开数据库");
            connection
                .execute("UPDATE snapshot_value SET value = 2", [])
                .expect("WAL writer 应可在 reader 快照期间提交");
            writer_finished.send(()).expect("应通知 writer 完成");
        });

        let queries = [
            SqliteQuery::new("SELECT value FROM snapshot_value", Vec::new()),
            SqliteQuery::new("SELECT value FROM snapshot_value", Vec::new()),
        ];
        let performance = RunPerformanceCounters::default();
        let results = read_queries_in_snapshot(
            &reader,
            &queries,
            &configuration(),
            &performance,
            |completed_index| {
                if completed_index == 0 {
                    start_writer.send(()).expect("应释放 writer");
                    wait_writer.recv().expect("应等待 writer 提交");
                }
            },
        )
        .expect("两条查询应共享同一 WAL 视图");
        writer.join().expect("writer 不应 panic");

        let expected = vec![SqliteRow::new(vec![SqliteValue::Integer(1)])];
        assert_eq!(results, [expected.clone(), expected]);
        let current: i64 = Connection::open(&database)
            .expect("应可重新打开测试数据库")
            .query_row("SELECT value FROM snapshot_value", [], |row| row.get(0))
            .expect("应可读取 writer 的新值");
        assert_eq!(current, 2);
    }

    #[test]
    fn multi_query_read_rolls_back_query_failure_and_reports_commit_failure_as_read_error() {
        let connection = Connection::open_in_memory().expect("内存数据库应可打开");
        let query_error = read_queries_in_snapshot(
            &connection,
            &[
                SqliteQuery::new("SELECT 1", Vec::new()).with_id("snapshot.first"),
                SqliteQuery::new("SELECT missing FROM absent", Vec::new())
                    .with_id("snapshot.missing_table"),
            ],
            &configuration(),
            &RunPerformanceCounters::default(),
            |_| {},
        )
        .expect_err("第二条查询失败必须终止快照");
        assert!(connection.is_autocommit(), "查询失败后必须结束读事务");
        assert!(matches!(
            &query_error,
            SqliteRuntimeError::QueryContext {
                query_id,
                ordinal: 1,
                source,
            } if query_id == "snapshot.missing_table"
                && matches!(source.as_ref(), SqliteRuntimeError::Driver {
                    operation: "准备查询",
                    ..
                })
        ));
        let report = query_error.diagnostic_report(
            Path::new("D:/projects/game/project.db"),
            SqliteDiagnosticContext::new(
                SqliteDiagnosticStage::Project,
                SqliteOperation::Query,
                SqliteTransactionState::RolledBack,
            ),
            StateEffect::Unchanged,
        );
        let wire = serde_json::to_value(report).expect("查询诊断必须可序列化");
        assert_eq!(
            wire.pointer("/primary/issue/details/problem/query_id"),
            Some(&serde_json::json!("snapshot.missing_table"))
        );
        assert_eq!(
            wire.pointer("/primary/issue/details/problem/query_ordinal"),
            Some(&serde_json::json!(1))
        );

        let commit_error = read_queries_in_snapshot(
            &connection,
            &[SqliteQuery::new("SELECT 1", Vec::new())],
            &configuration(),
            &RunPerformanceCounters::default(),
            |_| {
                connection
                    .execute_batch("ROLLBACK")
                    .expect("测试注入应先结束读事务")
            },
        )
        .expect_err("被测试注入提前结束的事务必须令 COMMIT 失败");
        assert!(connection.is_autocommit());
        assert!(matches!(
            commit_error,
            SqliteRuntimeError::Driver {
                operation: "结束只读查询快照",
                ..
            }
        ));
    }

    #[tokio::test]
    async fn multi_query_read_preserves_order_without_row_caps() {
        let directory = TestDirectory::new();
        let database = directory.database("read-snapshot-budget.db");
        Connection::open(&database)
            .expect("测试数据库应可创建")
            .close()
            .expect("测试数据库应可关闭");
        let storage = RusqliteStorage::start(configuration()).expect("SQLite 根应可启动");

        let results = storage
            .query_existing_database_snapshot(
                database.clone(),
                (0_i64..8)
                    .map(|value| SqliteQuery::new("SELECT ?1", vec![SqliteValue::Integer(value)]))
                    .collect(),
            )
            .await
            .expect("超过四组查询应在同一快照中保持调用顺序");
        assert_eq!(
            results,
            (0_i64..8)
                .map(|value| vec![SqliteRow::new(vec![SqliteValue::Integer(value)])])
                .collect::<Vec<_>>()
        );

        let rows = storage
            .query_existing_database_snapshot(
                database,
                vec![SqliteQuery::new("SELECT 1 UNION ALL SELECT 2", Vec::new())],
            )
            .await
            .expect("查询结果不得被 ATT 行数预算拒绝");
        assert_eq!(rows[0].len(), 2);
        storage.shutdown().await.expect("SQLite 根应可关闭");
    }

    #[cfg(feature = "release-stress")]
    #[tokio::test]
    async fn release_stress_large_query_and_batch_have_no_att_capacity_limit() {
        const ROWS: usize = 230_000;
        let directory = TestDirectory::new();
        let database = directory.database("large-batch.db");
        let storage = RusqliteStorage::start(configuration()).expect("SQLite 根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let parameter_sets = (1..=ROWS)
            .map(|id| vec![SqliteValue::Integer(id as i64)])
            .collect();
        storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::ExecuteMany(
                    SqliteBatch::bulk_insert(
                        "INSERT INTO values_table (id)",
                        1,
                        Vec::new(),
                        parameter_sets,
                    ),
                )]),
            )
            .await
            .expect("230,000 组参数必须在一个事务内成功");
        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT id FROM values_table ORDER BY id", Vec::new()),
            )
            .await
            .expect("230,000 行必须完整返回");
        assert_eq!(rows.len(), ROWS);
        assert_eq!(
            rows.last(),
            Some(&SqliteRow::new(vec![SqliteValue::Integer(ROWS as i64)]))
        );
        storage.shutdown().await.expect("SQLite 根应可关闭");
    }

    #[test]
    fn bulk_insert_uses_connection_variable_limit_and_reuses_shared_parameters() {
        let connection = Connection::open_in_memory().expect("内存数据库应可打开");
        connection
            .execute_batch(
                "CREATE TABLE bulk_values (owner TEXT NOT NULL, id INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY (owner, id))",
            )
            .expect("bulk 测试表应可创建");
        let bundled_limit = connection
            .limit(Limit::SQLITE_LIMIT_VARIABLE_NUMBER)
            .expect("bundled SQLite 应公开变量上限");
        assert_eq!(
            sqlite_variable_limit(&connection).expect("运行时应读取同一真实上限"),
            usize::try_from(bundled_limit).expect("SQLite 变量上限应为正整数")
        );
        connection
            .set_limit(Limit::SQLITE_LIMIT_VARIABLE_NUMBER, 5)
            .expect("测试应可降低当前连接变量上限");

        let batch = SqliteBatch::bulk_insert_flat(
            "INSERT INTO bulk_values (owner, id, label)",
            2,
            vec![SqliteValue::Text("builtin".to_owned())],
            (1_i64..=5)
                .flat_map(|id| {
                    [
                        SqliteValue::Integer(id),
                        SqliteValue::Text(format!("value-{id}")),
                    ]
                })
                .collect(),
        );
        validate_batch(&batch, &configuration()).expect("bulk 描述应合法");
        let executed =
            execute_bulk_insert(&connection, &batch).expect("低变量上限应自动切成多个真实 INSERT");

        assert_eq!(
            executed, 3,
            "1 个 shared 加每行 2 个变量时每块只能容纳 2 行"
        );
        let rows = connection
            .prepare("SELECT owner, id, label FROM bulk_values ORDER BY id")
            .expect("结果查询应可准备")
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .expect("结果查询应可执行")
            .collect::<Result<Vec<_>, _>>()
            .expect("结果行应可读取");
        assert_eq!(rows.len(), 5);
        assert!(rows.iter().all(|(owner, _, _)| owner == "builtin"));
        assert_eq!(
            build_bulk_insert_statement("INSERT INTO bulk_values (owner, id, label)", 1, 2, 2,)
                .expect("bulk SQL 应可生成"),
            "INSERT INTO bulk_values (owner, id, label) VALUES (?1, ?2, ?3), (?1, ?4, ?5)"
        );
    }

    #[test]
    fn flat_bulk_insert_rejects_zero_width_and_incomplete_tail() {
        for batch in [
            SqliteBatch::bulk_insert_flat(
                "INSERT INTO values_table (id)",
                0,
                Vec::new(),
                vec![SqliteValue::Integer(1)],
            ),
            SqliteBatch::bulk_insert_flat(
                "INSERT INTO values_table (id, value)",
                2,
                Vec::new(),
                vec![SqliteValue::Integer(1)],
            ),
        ] {
            assert!(matches!(
                validate_batch(&batch, &configuration()),
                Err(SqliteRuntimeError::Internal(_))
            ));
        }
    }

    #[test]
    fn bulk_insert_failure_in_later_chunk_rolls_back_the_whole_transaction() {
        let connection = Connection::open_in_memory().expect("内存数据库应可打开");
        connection
            .execute_batch("CREATE TABLE bulk_values (id INTEGER PRIMARY KEY)")
            .expect("bulk 测试表应可创建");
        connection
            .set_limit(Limit::SQLITE_LIMIT_VARIABLE_NUMBER, 1)
            .expect("测试应强制每块只有一行");
        let plan = SqliteTransactionPlan::new(vec![SqliteTransactionStep::ExecuteMany(
            SqliteBatch::bulk_insert_flat(
                "INSERT INTO bulk_values (id)",
                1,
                Vec::new(),
                vec![
                    SqliteValue::Integer(1),
                    SqliteValue::Integer(2),
                    SqliteValue::Integer(1),
                ],
            ),
        )]);
        validate_transaction_plan(&plan, &configuration()).expect("测试计划应合法");

        let result =
            run_transaction_on_connection(&connection, &plan, &RunPerformanceCounters::default());

        assert!(matches!(
            result,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Driver {
                    operation: "执行 bulk INSERT",
                    ..
                }
            ))
        ));
        assert!(connection.is_autocommit(), "失败后必须确认事务已回滚");
        let rows: i64 = connection
            .query_row("SELECT COUNT(*) FROM bulk_values", [], |row| row.get(0))
            .expect("回滚后应可查询");
        assert_eq!(rows, 0, "较早块的成功写入不得逃出失败事务");
    }

    #[test]
    fn bulk_insert_is_rejected_by_exactly_one_and_requirement_steps() {
        let batch = SqliteBatch::bulk_insert(
            "INSERT INTO values_table (id)",
            1,
            Vec::new(),
            vec![vec![SqliteValue::Integer(1)]],
        );
        for step in [
            SqliteTransactionStep::ExecuteManyExactlyOne(batch.clone()),
            SqliteTransactionStep::RequireNoRowsMany(batch.clone()),
        ] {
            let error = validate_transaction_plan(
                &SqliteTransactionPlan::new(vec![step]),
                &configuration(),
            )
            .expect_err("bulk INSERT 不得被解释为 ExactlyOne 或 Require");
            assert!(matches!(error, SqliteRuntimeError::Internal(_)));
        }
    }

    #[test]
    fn typed_rusqlite_parameter_count_keeps_safe_actual_and_expected_counts() {
        let source = SqliteRuntimeError::driver(
            "绑定批量命令参数",
            rusqlite::Error::InvalidParameterCount(3, 5),
        );
        let report = source.diagnostic_report(
            Path::new("D:/projects/game/project.db"),
            SqliteDiagnosticContext::new(
                SqliteDiagnosticStage::Extract,
                SqliteOperation::Execute,
                SqliteTransactionState::Active,
            ),
            StateEffect::Unchanged,
        );
        let wire = serde_json::to_value(report).expect("参数计数诊断必须可序列化");
        assert_eq!(
            wire.pointer("/primary/issue/details/problem/failure/kind"),
            Some(&serde_json::json!("invalid_parameter_count"))
        );
        assert_eq!(
            wire.pointer("/primary/issue/details/problem/failure/parameter_actual"),
            Some(&serde_json::json!(3))
        );
        assert_eq!(
            wire.pointer("/primary/issue/details/problem/failure/parameter_expected"),
            Some(&serde_json::json!(5))
        );
    }

    #[test]
    fn multi_query_read_rejects_empty_but_accepts_more_than_four_queries() {
        assert!(matches!(
            run_query_snapshot_existing(
                Path::new("C:/不存在/且不应访问.db"),
                &[],
                &configuration(),
                &RunPerformanceCounters::default(),
            ),
            Err(QueryExistingDatabaseError::QueryFailed(
                SqliteRuntimeError::InvalidValue("只读快照查询集合不得为空")
            ))
        ));

        let queries = (0..8)
            .map(|_| SqliteQuery::new("SELECT 1", Vec::new()))
            .collect::<Vec<_>>();
        validate_query_snapshot(&queries, &configuration())
            .expect("同一快照可执行任意数量的非空查询集合");
    }

    #[tokio::test]
    async fn online_backup_is_create_only_and_copies_one_consistent_database() {
        let directory = TestDirectory::new();
        let source = directory.database("source.db");
        let destination = directory.database("snapshot.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(source.clone(), schema_commands())
            .await
            .expect("源数据库应可创建");
        storage
            .execute_transaction(
                source.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                    SqliteCommand::new(
                        "INSERT INTO values_table (id, t) VALUES (?1, ?2)",
                        vec![
                            SqliteValue::Integer(1),
                            SqliteValue::Text("冻结值".to_owned()),
                        ],
                    ),
                )]),
            )
            .await
            .expect("源数据应可提交");

        storage
            .snapshot_database(source.clone(), destination.clone())
            .await
            .expect("online backup 应成功");
        let source_connection = Connection::open_with_flags(
            &source,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .expect("应可读取源数据库物理参数");
        let destination_connection = Connection::open_with_flags(
            &destination,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .expect("应可读取快照物理参数");
        let source_page_size: i64 = source_connection
            .query_row("PRAGMA page_size", [], |row| row.get(0))
            .expect("应可读取源页大小");
        let destination_page_size: i64 = destination_connection
            .query_row("PRAGMA page_size", [], |row| row.get(0))
            .expect("应可读取快照页大小");
        let destination_journal_mode: String = destination_connection
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .expect("应可读取快照 journal_mode");
        assert_eq!(source_page_size, NEW_DATABASE_PAGE_SIZE_BYTES);
        assert_eq!(destination_page_size, source_page_size);
        assert_eq!(destination_journal_mode, "wal");
        drop(destination_connection);
        drop(source_connection);
        let rows = storage
            .query_existing_database(
                destination.clone(),
                SqliteQuery::new("SELECT id, t FROM values_table", Vec::new()),
            )
            .await
            .expect("快照应可独立读取");
        assert_eq!(
            rows,
            vec![SqliteRow::new(vec![
                SqliteValue::Integer(1),
                SqliteValue::Text("冻结值".to_owned()),
            ])]
        );

        let error = storage
            .snapshot_database(source, destination)
            .await
            .expect_err("现存目标绝不能被覆盖");
        assert!(matches!(
            error,
            SnapshotDatabaseError::DestinationAlreadyExists
        ));
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn online_backup_reports_missing_source_and_cleans_invalid_copy() {
        let directory = TestDirectory::new();
        let missing = directory.database("missing.db");
        let destination = directory.database("missing-snapshot.db");
        let invalid = directory.database("invalid.db");
        let invalid_destination = directory.database("invalid-snapshot.db");
        fs::write(&invalid, b"not a sqlite database").expect("无效源文件应可创建");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");

        assert!(matches!(
            storage
                .snapshot_database(missing, destination.clone())
                .await,
            Err(SnapshotDatabaseError::SourceNotFound)
        ));
        assert!(!destination.exists());

        let error = storage
            .snapshot_database(invalid, invalid_destination.clone())
            .await
            .expect_err("无效 SQLite 源必须失败");
        assert!(matches!(error, SnapshotDatabaseError::NotCreated(_)));
        assert!(!invalid_destination.exists());
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn create_query_and_transaction_preserve_all_sqlite_values() {
        let directory = TestDirectory::new();
        let database = directory.database("中文 数据库.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");

        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::ExecuteMany(
                    SqliteBatch::new(
                        "INSERT INTO values_table (id, n, r, t, b, z) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                        vec![vec![
                            SqliteValue::Integer(1),
                            SqliteValue::Integer(-9),
                            SqliteValue::Real(1.25),
                            SqliteValue::Text("文本".to_owned()),
                            SqliteValue::Blob(vec![0, 1, 255]),
                            SqliteValue::Null,
                        ]],
                    ),
                )]),
            )
            .await
            .expect("批量事务应可提交");

        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new(
                    "SELECT n, r, t, b, z FROM values_table ORDER BY id",
                    Vec::new(),
                ),
            )
            .await
            .expect("查询应成功");
        assert_eq!(
            rows,
            vec![SqliteRow::new(vec![
                SqliteValue::Integer(-9),
                SqliteValue::Real(1.25),
                SqliteValue::Text("文本".to_owned()),
                SqliteValue::Blob(vec![0, 1, 255]),
                SqliteValue::Null,
            ])]
        );
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn final_transaction_uses_an_independent_connection_after_main_root_shutdown() {
        let directory = TestDirectory::new();
        let database = directory.database("final-transaction.db");
        let config = configuration();
        let storage = RusqliteStorage::start(config.clone()).expect("主根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage.shutdown().await.expect("主根必须先确认关闭");

        let final_executor = RusqliteFinalTransactionExecutor::new(config);
        final_executor
            .execute_final_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                    SqliteCommand::new(
                        "INSERT INTO values_table (id, t) VALUES (?1, ?2)",
                        vec![
                            SqliteValue::Integer(1),
                            SqliteValue::Text("最终方案".to_owned()),
                        ],
                    ),
                )]),
            )
            .await
            .expect("独立最终事务必须提交并显式关闭连接");

        let connection = Connection::open(&database).expect("最终事务结束后数据库应可重开");
        let stored: String = connection
            .query_row("SELECT t FROM values_table WHERE id = 1", [], |row| {
                row.get(0)
            })
            .expect("最终事务结果应已提交");
        assert_eq!(stored, "最终方案");
        assert!(connection.close().is_ok(), "验证连接应可关闭");
    }

    #[tokio::test]
    async fn batch_requirements_and_exact_updates_follow_natural_parameter_order() {
        let directory = TestDirectory::new();
        let database = directory.database("ordered-batches.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "INSERT INTO values_table (id, n) VALUES (1, 0)",
                        Vec::new(),
                    )),
                    SqliteTransactionStep::RequireNoRowsMany(SqliteBatch::new(
                        "SELECT 1 FROM values_table WHERE id = ?1 AND n <> ?2",
                        vec![
                            vec![SqliteValue::Integer(1), SqliteValue::Integer(0)],
                            vec![SqliteValue::Integer(2), SqliteValue::Integer(0)],
                        ],
                    )),
                    SqliteTransactionStep::ExecuteManyExactlyOne(SqliteBatch::new(
                        "UPDATE values_table SET n = ?1 WHERE id = 1 AND n = ?2",
                        vec![
                            vec![SqliteValue::Integer(1), SqliteValue::Integer(0)],
                            vec![SqliteValue::Integer(2), SqliteValue::Integer(1)],
                            vec![SqliteValue::Integer(3), SqliteValue::Integer(2)],
                        ],
                    )),
                ]),
            )
            .await
            .expect("批量步骤应按自然参数顺序提交");

        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT n FROM values_table WHERE id = 1", Vec::new()),
            )
            .await
            .expect("提交后应可查询");
        assert_eq!(rows, vec![SqliteRow::new(vec![SqliteValue::Integer(3)])]);
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn require_no_rows_many_reports_the_earliest_result_and_rolls_back() {
        let directory = TestDirectory::new();
        let database = directory.database("ordered-requirement.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                    SqliteCommand::new(
                        "INSERT INTO values_table (id, n) VALUES (1, 0)",
                        Vec::new(),
                    ),
                )]),
            )
            .await
            .expect("基础行应可提交");

        let result = storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "UPDATE values_table SET n = 9 WHERE id = 1",
                        Vec::new(),
                    )),
                    SqliteTransactionStep::RequireNoRowsMany(SqliteBatch::new(
                        "SELECT CASE ?1 WHEN 1 THEN 1 ELSE abs(-9223372036854775808) END",
                        vec![vec![SqliteValue::Integer(1)], vec![SqliteValue::Integer(2)]],
                    )),
                ]),
            )
            .await;
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::RequirementFailed)
        ));

        let rows = storage
            .query_existing_database(
                database.clone(),
                SqliteQuery::new("SELECT n FROM values_table WHERE id = 1", Vec::new()),
            )
            .await
            .expect("回滚后应可查询");
        assert_eq!(rows, vec![SqliteRow::new(vec![SqliteValue::Integer(0)])]);

        let result = storage
            .execute_transaction(
                database,
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::RequireNoRowsMany(
                    SqliteBatch::new(
                        "SELECT CASE ?1 WHEN 1 THEN 1 ELSE abs(-9223372036854775808) END",
                        vec![vec![SqliteValue::Integer(2)], vec![SqliteValue::Integer(1)]],
                    ),
                )]),
            )
            .await;
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Driver { .. }
            ))
        ));
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn execute_many_exactly_one_rejects_zero_or_multiple_rows_and_rolls_back() {
        let directory = TestDirectory::new();
        let database = directory.database("exactly-one-count.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::ExecuteMany(
                    SqliteBatch::new(
                        "INSERT INTO values_table (id, n) VALUES (?1, 0)",
                        vec![vec![SqliteValue::Integer(1)], vec![SqliteValue::Integer(2)]],
                    ),
                )]),
            )
            .await
            .expect("基础行应可提交");

        let zero_rows = storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::ExecuteManyExactlyOne(SqliteBatch::new(
                        "UPDATE values_table SET n = ?1 WHERE id = ?2",
                        vec![
                            vec![SqliteValue::Integer(7), SqliteValue::Integer(1)],
                            vec![SqliteValue::Integer(8), SqliteValue::Integer(99)],
                        ],
                    )),
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "UPDATE values_table SET n = 10 WHERE id = 2",
                        Vec::new(),
                    )),
                ]),
            )
            .await;
        assert!(matches!(
            zero_rows,
            Err(ExecuteTransactionError::RequirementFailed)
        ));

        let multiple_rows = storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::ExecuteManyExactlyOne(SqliteBatch::new(
                        "UPDATE values_table SET n = ?1 WHERE n = ?2",
                        vec![vec![SqliteValue::Integer(3), SqliteValue::Integer(0)]],
                    )),
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "UPDATE values_table SET n = 10 WHERE id = 2",
                        Vec::new(),
                    )),
                ]),
            )
            .await;
        assert!(matches!(
            multiple_rows,
            Err(ExecuteTransactionError::RequirementFailed)
        ));

        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT id, n FROM values_table ORDER BY id", Vec::new()),
            )
            .await
            .expect("两笔失败事务后应可查询");
        assert_eq!(
            rows,
            vec![
                SqliteRow::new(vec![SqliteValue::Integer(1), SqliteValue::Integer(0)]),
                SqliteRow::new(vec![SqliteValue::Integer(2), SqliteValue::Integer(0)]),
            ]
        );
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn execute_many_exactly_one_driver_error_rolls_back_earlier_parameter_sets() {
        let directory = TestDirectory::new();
        let database = directory.database("exactly-one-driver.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![SqliteTransactionStep::ExecuteMany(
                    SqliteBatch::new(
                        "INSERT INTO values_table (id) VALUES (?1)",
                        vec![vec![SqliteValue::Integer(1)], vec![SqliteValue::Integer(2)]],
                    ),
                )]),
            )
            .await
            .expect("基础行应可提交");

        let result = storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::ExecuteManyExactlyOne(SqliteBatch::new(
                        "UPDATE values_table SET id = ?1 WHERE id = ?2",
                        vec![
                            vec![SqliteValue::Integer(3), SqliteValue::Integer(1)],
                            vec![SqliteValue::Integer(2), SqliteValue::Integer(3)],
                        ],
                    )),
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "INSERT INTO values_table (id) VALUES (4)",
                        Vec::new(),
                    )),
                ]),
            )
            .await;
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Driver { .. }
            ))
        ));

        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT id FROM values_table ORDER BY id", Vec::new()),
            )
            .await
            .expect("驱动失败回滚后应可查询");
        assert_eq!(
            rows,
            vec![
                SqliteRow::new(vec![SqliteValue::Integer(1)]),
                SqliteRow::new(vec![SqliteValue::Integer(2)]),
            ]
        );
        storage.shutdown().await.expect("根应可关闭");
    }

    #[test]
    fn requirement_rollback_failure_is_outcome_unknown() {
        let connection = Connection::open_in_memory().expect("内存数据库应可打开");
        let performance = RunPerformanceCounters::default();

        let result = rollback_requirement_failure(
            &connection,
            &performance,
            SqliteTransactionScope::WritePlan,
        );

        assert!(matches!(
            result,
            Err(ExecuteTransactionError::OutcomeUnknown(
                SqliteRuntimeError::Driver { .. }
            ))
        ));
        let rollback = performance
            .snapshot()
            .sqlite_transactions
            .write_plan
            .rollback;
        assert_eq!(rollback.attempted, 1);
        assert_eq!(rollback.succeeded, 0);
    }

    #[tokio::test]
    async fn missing_query_never_creates_a_database() {
        let directory = TestDirectory::new();
        let database = directory.database("missing.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");

        let result = storage
            .query_existing_database(database.clone(), SqliteQuery::new("SELECT 1", Vec::new()))
            .await;
        assert!(matches!(result, Err(QueryExistingDatabaseError::NotFound)));
        assert!(!database.exists());
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn concurrent_create_only_has_exactly_one_winner() {
        let directory = TestDirectory::new();
        let database = directory.database("race.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");

        let (first, second) = tokio::join!(
            storage.create_new_database(database.clone(), schema_commands()),
            storage.create_new_database(database.clone(), schema_commands()),
        );
        let successes = usize::from(first.is_ok()) + usize::from(second.is_ok());
        let already_exists = usize::from(matches!(first, Err(CreateDatabaseError::AlreadyExists)))
            + usize::from(matches!(second, Err(CreateDatabaseError::AlreadyExists)));
        assert_eq!(successes, 1);
        assert_eq!(already_exists, 1);
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn failed_creation_removes_main_file_and_all_sidecars() {
        let directory = TestDirectory::new();
        let database = directory.database("cleanup.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        let mut commands = schema_commands();
        commands.push(SqliteCommand::new("INVALID SQL", Vec::new()));

        let result = storage
            .create_new_database(database.clone(), commands)
            .await;
        assert!(
            matches!(&result, Err(CreateDatabaseError::NotCreated(_))),
            "实际结果：{result:?}"
        );
        for path in [
            database.clone(),
            sidecar_path(&database, "-journal"),
            sidecar_path(&database, "-wal"),
            sidecar_path(&database, "-shm"),
        ] {
            assert!(!path.exists(), "创建失败后不得残留 {}", path.display());
        }
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn preexisting_sidecar_is_never_claimed_or_deleted_by_database_creation() {
        let directory = TestDirectory::new();
        let database = directory.database("foreign-sidecar.db");
        let foreign = sidecar_path(&database, "-wal");
        fs::write(&foreign, b"foreign").expect("外来伴生文件应可建立");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");

        let result = storage
            .create_new_database(database.clone(), schema_commands())
            .await;
        assert!(
            matches!(
                &result,
            Err(CreateDatabaseError::ResidualArtifact(
                SqliteRuntimeError::UnexpectedArtifact { path }
            )) if path.file_name() == foreign.file_name()
            ),
            "实际结果：{result:?}"
        );
        assert!(!database.exists(), "本次占有的主文件应按身份清理");
        assert_eq!(fs::read(&foreign).expect("外来文件不得被删除"), b"foreign");
        storage.shutdown().await.expect("根应可关闭");
    }

    #[test]
    fn cleanup_refuses_a_path_replaced_after_identity_was_captured() {
        let directory = TestDirectory::new();
        let database = directory.database("replaced-cleanup.db");
        fs::write(&database, b"owned").expect("本次文件应可建立");
        let pinned = pin_path_without_reparse(&database).expect("本次文件应可固定");
        let identity = FileIdentity::of(pinned.file(), &database).expect("物理身份应可读取");
        drop(pinned);
        fs::remove_file(&database).expect("应可模拟外部替换");
        fs::write(&database, b"foreign").expect("外来替换文件应可建立");
        let paths = database_artifact_paths(&database);
        let assessment = cleanup_database_artifacts(
            &paths,
            &[TrackedDatabaseArtifact {
                path: database.clone(),
                identity,
                is_main: true,
            }],
            CleanupAssessment::clean(),
        );

        assert!(assessment.unknown, "身份变化后不得声称已知清理结果");
        assert!(assessment.residual, "外来替换对象应作为残留可见");
        assert_eq!(
            fs::read(database).expect("外来替换对象不得被误删"),
            b"foreign"
        );
    }

    #[tokio::test]
    async fn production_create_keeps_the_claimed_identity_pinned_during_initialization() {
        let directory = TestDirectory::new();
        let database = directory.database("pinned-create.db");
        let foreign = directory.database("pinned-create-foreign.db");
        create_foreign_guard_database(&foreign);
        let attempted = Arc::new(AtomicBool::new(false));
        let replacement_blocked = Arc::new(AtomicBool::new(false));
        let hook_attempted = Arc::clone(&attempted);
        let hook_replacement_blocked = Arc::clone(&replacement_blocked);
        let hook_foreign = foreign.clone();
        let config = configuration().with_claimed_database_path_hook(move |claimed| {
            hook_attempted.store(true, Ordering::Release);
            match fs::remove_file(claimed) {
                Ok(()) => fs::rename(&hook_foreign, claimed)
                    .expect("未固定身份时应能把外来 SQLite 换入创建窗口"),
                Err(_) => hook_replacement_blocked.store(true, Ordering::Release),
            }
        });
        let storage = RusqliteStorage::start(config).expect("根应可启动");

        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("替换尝试不得破坏本次数据库初始化");
        storage.shutdown().await.expect("根应可关闭");

        assert!(
            attempted.load(Ordering::Acquire),
            "测试必须穿过生产创建路径的占有后窗口"
        );
        assert!(
            replacement_blocked.load(Ordering::Acquire),
            "初始化期间必须阻止目标被删除或替换"
        );
        assert_eq!(read_foreign_guard(&foreign), "foreign-unchanged");
        let connection = Connection::open_with_flags(
            database,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .expect("创建结果应可读取");
        let initialized: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = 'values_table'",
                [],
                |row| row.get(0),
            )
            .expect("应可确认本次 schema");
        let foreign_tables: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = 'foreign_guard'",
                [],
                |row| row.get(0),
            )
            .expect("应可确认没有写入外来数据库");
        assert_eq!(initialized, 1);
        assert_eq!(foreign_tables, 0);
    }

    #[tokio::test]
    async fn production_snapshot_keeps_the_claimed_identity_pinned_during_backup() {
        let directory = TestDirectory::new();
        let source = directory.database("pinned-snapshot-source.db");
        let destination = directory.database("pinned-snapshot.db");
        let foreign = directory.database("pinned-snapshot-foreign.db");
        let source_connection = Connection::open(&source).expect("快照源应可建立");
        source_connection
            .execute_batch(
                "CREATE TABLE source_value (value TEXT NOT NULL);\
                 INSERT INTO source_value (value) VALUES ('source-copy');",
            )
            .expect("快照源数据应可建立");
        drop(source_connection);
        create_foreign_guard_database(&foreign);

        let attempted = Arc::new(AtomicBool::new(false));
        let replacement_blocked = Arc::new(AtomicBool::new(false));
        let hook_attempted = Arc::clone(&attempted);
        let hook_replacement_blocked = Arc::clone(&replacement_blocked);
        let hook_foreign = foreign.clone();
        let config = configuration().with_claimed_database_path_hook(move |claimed| {
            hook_attempted.store(true, Ordering::Release);
            match fs::remove_file(claimed) {
                Ok(()) => fs::rename(&hook_foreign, claimed)
                    .expect("未固定身份时应能把外来 SQLite 换入备份窗口"),
                Err(_) => hook_replacement_blocked.store(true, Ordering::Release),
            }
        });
        let storage = RusqliteStorage::start(config).expect("根应可启动");

        storage
            .snapshot_database(source, destination.clone())
            .await
            .expect("替换尝试不得破坏 online backup");
        storage.shutdown().await.expect("根应可关闭");

        assert!(
            attempted.load(Ordering::Acquire),
            "测试必须穿过生产快照路径的占有后窗口"
        );
        assert!(
            replacement_blocked.load(Ordering::Acquire),
            "online backup 期间必须阻止目标被删除或替换"
        );
        assert_eq!(read_foreign_guard(&foreign), "foreign-unchanged");
        let connection = Connection::open_with_flags(
            destination,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .expect("快照结果应可读取");
        let copied: String = connection
            .query_row("SELECT value FROM source_value", [], |row| row.get(0))
            .expect("快照应包含源数据");
        let foreign_tables: i64 = connection
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name = 'foreign_guard'",
                [],
                |row| row.get(0),
            )
            .expect("应可确认没有覆盖外来数据库");
        assert_eq!(copied, "source-copy");
        assert_eq!(foreign_tables, 0);
    }

    #[tokio::test]
    async fn busy_writer_waits_until_the_database_lock_is_released() {
        let directory = TestDirectory::new();
        let database = directory.database("busy.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let blocker = Connection::open(&database).expect("测试锁连接应可打开");
        blocker
            .execute_batch("BEGIN EXCLUSIVE")
            .expect("测试连接应可占有写锁");

        let pending_storage = storage.clone();
        let pending_database = database.clone();
        let pending = tokio::spawn(async move {
            pending_storage
                .execute_transaction(
                    pending_database,
                    SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                        SqliteCommand::new("INSERT INTO values_table (id) VALUES (1)", Vec::new()),
                    )]),
                )
                .await
        });
        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(!pending.is_finished(), "写事务应在真实数据库锁处自然等待");
        blocker.execute_batch("ROLLBACK").expect("测试锁应可释放");
        drop(blocker);
        pending
            .await
            .expect("等待任务不应 panic")
            .expect("释放数据库锁后事务应成功");
        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT id FROM values_table", Vec::new()),
            )
            .await
            .expect("解锁后应可查询");
        assert_eq!(rows, vec![SqliteRow::new(vec![SqliteValue::Integer(1)])]);
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn shutdown_cancels_sqlite_busy_wait_without_a_local_deadline() {
        let directory = TestDirectory::new();
        let database = directory.database("busy-cancel.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let blocker = Connection::open(&database).expect("测试锁连接应可打开");
        blocker
            .execute_batch("BEGIN EXCLUSIVE")
            .expect("测试连接应可占有写锁");
        let pending_storage = storage.clone();
        let pending = tokio::spawn(async move {
            pending_storage
                .execute_transaction(
                    database,
                    SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                        SqliteCommand::new("INSERT INTO values_table (id) VALUES (1)", Vec::new()),
                    )]),
                )
                .await
        });
        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(!pending.is_finished(), "写事务应在真实数据库锁处等待");

        tokio::time::timeout(Duration::from_secs(1), storage.shutdown())
            .await
            .expect("shutdown 必须及时中断 SQLite busy 等待")
            .expect("SQLite 根应关闭");
        let result = pending.await.expect("等待任务不应 panic");
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Cancelled { .. }
            ))
        ));
        blocker.execute_batch("ROLLBACK").expect("测试锁应可释放");
    }

    #[tokio::test]
    async fn synchronous_cancel_waits_breaks_a_busy_operation_before_shutdown() {
        let directory = TestDirectory::new();
        let database = directory.database("busy-command-cancel.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let blocker = Connection::open(&database).expect("测试锁连接应可打开");
        blocker
            .execute_batch("BEGIN EXCLUSIVE")
            .expect("测试连接应可占有写锁");

        let pending_storage = storage.clone();
        let pending_database = database.clone();
        let pending = tokio::spawn(async move {
            pending_storage
                .execute_transaction(
                    pending_database,
                    SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                        SqliteCommand::new("INSERT INTO values_table (id) VALUES (1)", Vec::new()),
                    )]),
                )
                .await
        });
        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(!pending.is_finished(), "写事务应仍在等待数据库锁");

        storage.cancel_waits();
        let result = tokio::time::timeout(Duration::from_secs(1), pending)
            .await
            .expect("同步取消必须在 shutdown 之前唤醒 busy wait")
            .expect("等待任务不应 panic");
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Cancelled { .. }
            ))
        ));

        blocker.execute_batch("ROLLBACK").expect("测试锁应可释放");
        drop(blocker);
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn synchronous_cancel_waits_breaks_short_worker_backpressure() {
        let directory = TestDirectory::new();
        let database = directory.database("worker-admission-cancel.db");
        let storage = RusqliteStorage::start(configuration_with_workers(1))
            .expect("单 worker 测试根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let blocker = Connection::open(&database).expect("测试锁连接应可打开");
        blocker
            .execute_batch("BEGIN EXCLUSIVE")
            .expect("测试连接应占有写锁");

        let active_storage = storage.clone();
        let active_database = database.clone();
        let active = tokio::spawn(async move {
            active_storage
                .execute_transaction(
                    active_database,
                    SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                        SqliteCommand::new("INSERT INTO values_table (id) VALUES (1)", Vec::new()),
                    )]),
                )
                .await
        });
        wait_until("首个短操作占用唯一 worker 许可", || {
            storage.inner.short_admission.available_permits() == 0
        })
        .await;

        let pending_storage = storage.clone();
        let pending = tokio::spawn(async move {
            pending_storage
                .query_existing_database(database, SqliteQuery::new("SELECT 1", Vec::new()))
                .await
        });
        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(!pending.is_finished(), "查询饱和时只能等待 worker 许可");

        storage.cancel_waits();
        let result = tokio::time::timeout(Duration::from_secs(1), pending)
            .await
            .expect("同步取消必须唤醒 worker 许可等待")
            .expect("等待任务不应 panic");
        assert!(matches!(
            result,
            Err(QueryExistingDatabaseError::QueryFailed(
                SqliteRuntimeError::Cancelled {
                    operation: "等待 SQLite 短操作执行许可"
                }
            ))
        ));

        let active_result = tokio::time::timeout(Duration::from_secs(1), active)
            .await
            .expect("同步取消必须唤醒在途 SQLite busy wait")
            .expect("在途任务不应 panic");
        assert!(matches!(
            active_result,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Cancelled { .. }
            ))
        ));
        blocker.execute_batch("ROLLBACK").expect("测试锁应可释放");
        drop(blocker);
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn synchronous_cancel_waits_breaks_snapshot_lock_wait() {
        let directory = TestDirectory::new();
        let source = directory.database("snapshot-command-cancel-source.db");
        let destination = directory.database("snapshot-command-cancel-target.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(source.clone(), schema_commands())
            .await
            .expect("源数据库应可创建");
        let blocker = Connection::open(&source).expect("测试锁连接应可打开");
        blocker
            .pragma_update(None, "journal_mode", "DELETE")
            .expect("测试源应切换到会阻塞 reader 的日志模式");
        blocker
            .execute_batch("BEGIN EXCLUSIVE")
            .expect("测试连接应可占有独占锁");

        let pending_storage = storage.clone();
        let pending_destination = destination.clone();
        let pending = tokio::spawn(async move {
            pending_storage
                .snapshot_database(source, pending_destination)
                .await
        });
        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(!pending.is_finished(), "快照应仍在等待源数据库锁");

        storage.cancel_waits();
        let result = tokio::time::timeout(Duration::from_secs(1), pending)
            .await
            .expect("同步取消必须唤醒快照锁等待")
            .expect("等待任务不应 panic");
        assert!(matches!(
            result,
            Err(SnapshotDatabaseError::NotCreated(
                SqliteRuntimeError::Cancelled { .. }
            ))
        ));
        assert!(!destination.exists(), "取消的快照不得留下目标文件");

        blocker.execute_batch("ROLLBACK").expect("测试锁应可释放");
        drop(blocker);
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn final_transaction_has_an_independent_cancellable_busy_wait() {
        let directory = TestDirectory::new();
        let database = directory.database("final-busy-command-cancel.db");
        let config = configuration();
        let storage = RusqliteStorage::start(config.clone()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        storage.shutdown().await.expect("主根应先关闭");

        let blocker = Connection::open(&database).expect("测试锁连接应可打开");
        blocker
            .execute_batch("BEGIN EXCLUSIVE")
            .expect("测试连接应可占有写锁");
        let final_executor = RusqliteFinalTransactionExecutor::new(config);
        let pending_executor = final_executor.clone();
        let pending_database = database.clone();
        let pending = tokio::spawn(async move {
            pending_executor
                .execute_final_transaction(
                    pending_database,
                    SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                        SqliteCommand::new("INSERT INTO values_table (id) VALUES (1)", Vec::new()),
                    )]),
                )
                .await
        });
        tokio::time::sleep(Duration::from_millis(25)).await;
        assert!(!pending.is_finished(), "最终事务应仍在等待数据库锁");

        final_executor.cancel_waits();
        let result = tokio::time::timeout(Duration::from_secs(1), pending)
            .await
            .expect("最终事务取消必须唤醒 busy wait")
            .expect("等待任务不应 panic");
        assert!(matches!(
            result,
            Err(ExecuteFinalTransactionError::NotCommitted(
                SqliteRuntimeError::Cancelled { .. }
            ))
        ));
        blocker.execute_batch("ROLLBACK").expect("测试锁应可释放");
    }

    #[tokio::test]
    async fn requirement_failure_stops_later_steps_and_rolls_back() {
        let directory = TestDirectory::new();
        let database = directory.database("requirement.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");

        let result = storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "INSERT INTO values_table (id) VALUES (1)",
                        Vec::new(),
                    )),
                    SqliteTransactionStep::RequireNoRows(SqliteQuery::new(
                        "SELECT 1 FROM values_table WHERE id = 1",
                        Vec::new(),
                    )),
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "INSERT INTO values_table (id) VALUES (2)",
                        Vec::new(),
                    )),
                ]),
            )
            .await;
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::RequirementFailed)
        ));
        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT id FROM values_table", Vec::new()),
            )
            .await
            .expect("回滚后应可查询");
        assert!(rows.is_empty());
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn typed_requirement_failure_returns_the_owned_first_row_after_rollback() {
        let directory = TestDirectory::new();
        let database = directory.database("typed-requirement.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");

        let result = storage
            .execute_transaction(
                database.clone(),
                SqliteTransactionPlan::new(vec![
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "INSERT INTO values_table (id, t) VALUES (7, 'diagnostic')",
                        Vec::new(),
                    )),
                    SqliteTransactionStep::RequireNoRowsReturningFirstRow(
                        SqliteQuery::new("SELECT id, t FROM values_table WHERE id = 7", Vec::new())
                            .with_id("test.typed_requirement"),
                    ),
                    SqliteTransactionStep::Execute(SqliteCommand::new(
                        "INSERT INTO values_table (id) VALUES (8)",
                        Vec::new(),
                    )),
                ]),
            )
            .await;
        let Err(ExecuteTransactionError::RequirementFailedWithRow { query_id, row }) = result
        else {
            panic!("typed guard 必须返回确认回滚后的拥有型诊断行")
        };
        assert_eq!(query_id, "test.typed_requirement");
        assert_eq!(
            row,
            SqliteRow::new(vec![
                SqliteValue::Integer(7),
                SqliteValue::Text("diagnostic".to_owned()),
            ])
        );
        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT id FROM values_table", Vec::new()),
            )
            .await
            .expect("回滚后应可查询");
        assert!(rows.is_empty(), "命中诊断行的事务必须完整回滚");
        storage.shutdown().await.expect("根应可关闭");
    }

    #[test]
    fn final_transaction_mapping_preserves_typed_requirement_facts() {
        let row = SqliteRow::new(vec![SqliteValue::Integer(7)]);
        let confirmed = map_final_transaction_error(
            ExecuteTransactionError::<SqliteRuntimeError>::RequirementFailedWithRow {
                query_id: "test.guard".to_owned(),
                row: row.clone(),
            },
        );
        assert!(matches!(
            confirmed,
            ExecuteFinalTransactionError::RequirementFailedWithRow {
                query_id,
                row: actual,
            } if query_id == "test.guard" && actual == row
        ));

        let unknown = map_final_transaction_error(
            ExecuteTransactionError::RequirementFailedWithRowOutcomeUnknown {
                query_id: "test.guard".to_owned(),
                row: row.clone(),
                source: Box::new(SqliteRuntimeError::Internal("rollback outcome unknown")),
            },
        );
        assert!(matches!(
            unknown,
            ExecuteFinalTransactionError::RequirementFailedWithRowOutcomeUnknown {
                query_id,
                row: actual,
                source,
            } if query_id == "test.guard"
                && actual == row
                && matches!(
                    *source,
                    SqliteRuntimeError::Internal("rollback outcome unknown")
                )
        ));
    }

    #[test]
    fn finalization_error_preserves_the_primary_and_connection_close_failures() {
        let error = SqliteInteractiveSessionFinalizationError::new(
            SqliteInteractiveSessionFinalizationFailure::OutcomeUnknown(
                SqliteRuntimeError::Internal("回滚结果未知"),
            ),
            Some(SqliteRuntimeError::Internal("关闭失败")),
        );

        assert!(
            Error::source(&error)
                .expect("主失败应保留在错误链")
                .to_string()
                .contains("回滚结果未知")
        );
        let message = error.to_string();
        assert!(message.contains("回滚结果未知"));
        assert!(message.contains("关闭失败"));
    }

    #[tokio::test]
    async fn accepted_short_operation_survives_future_drop() {
        let directory = TestDirectory::new();
        let database = directory.database("accepted-drop.db");
        let config = configuration_with_workers(1);
        let storage = RusqliteStorage::start(config).expect("根应可启动");
        let mut future = Box::pin(storage.create_new_database(
            database.clone(),
            vec![SqliteCommand::new(
                "CREATE TABLE accepted_result AS WITH RECURSIVE counter(x) AS (VALUES(1) UNION ALL SELECT x + 1 FROM counter WHERE x < 2000000) SELECT max(x) AS value FROM counter",
                Vec::new(),
            )],
        ));
        let mut context = Context::from_waker(noop_waker_ref());
        assert!(matches!(future.as_mut().poll(&mut context), Poll::Pending));
        drop(future);

        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT value FROM accepted_result", Vec::new()),
            )
            .await
            .expect("已入队建库的 Future 丢弃后仍应完成");
        assert_eq!(
            rows,
            vec![SqliteRow::new(vec![SqliteValue::Integer(2_000_000)])]
        );
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn accepted_create_worker_panic_after_side_effect_is_outcome_unknown() {
        let directory = TestDirectory::new();
        let database = directory.database("create-panic-after-effect.db");
        let config = configuration_with_workers(1);
        let storage = RusqliteStorage::start(config).expect("根应可启动");
        let (response, receiver) = oneshot::channel();
        let permit = acquire_short_admission(Arc::clone(&storage.inner.short_admission))
            .await
            .expect("测试任务应取得执行许可");
        storage
            .inner
            .short_sender
            .send(AdmittedShortJob {
                job: ShortJob::Create {
                    path: database.clone(),
                    commands: schema_commands(),
                    response,
                    panic_after_operation: true,
                },
                _permit: permit,
            })
            .await
            .expect("建库任务应被 worker 接管");
        let result = receiver.await.expect("worker 应返回 panic 终态");
        assert!(matches!(
            result,
            Err(CreateDatabaseError::OutcomeUnknown(
                SqliteRuntimeError::WorkerPanicked("短操作")
            ))
        ));
        let connection = Connection::open(database).expect("panic 前建立的数据库应依然存在");
        let table_count: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'values_table'",
                [],
                |row| row.get(0),
            )
            .expect("应可观测 panic 前的建库副作用");
        assert_eq!(table_count, 1);
        drop(connection);
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn accepted_transaction_worker_panic_after_commit_is_outcome_unknown() {
        let directory = TestDirectory::new();
        let database = directory.database("transaction-panic-after-effect.db");
        let config = configuration_with_workers(1);
        let storage = RusqliteStorage::start(config).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let (response, receiver) = oneshot::channel();
        let permit = acquire_short_admission(Arc::clone(&storage.inner.short_admission))
            .await
            .expect("测试任务应取得执行许可");
        storage
            .inner
            .short_sender
            .send(AdmittedShortJob {
                job: ShortJob::Transaction {
                    path: database.clone(),
                    plan: SqliteTransactionPlan::new(vec![SqliteTransactionStep::Execute(
                        SqliteCommand::new(
                            "INSERT INTO values_table (id, t) VALUES (1, 'committed')",
                            Vec::new(),
                        ),
                    )]),
                    response,
                    panic_after_operation: true,
                },
                _permit: permit,
            })
            .await
            .expect("短事务应被 worker 接管");
        let result = receiver.await.expect("worker 应返回 panic 终态");
        assert!(matches!(
            result,
            Err(ExecuteTransactionError::OutcomeUnknown(
                SqliteRuntimeError::WorkerPanicked("短操作")
            ))
        ));
        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT t FROM values_table WHERE id = 1", Vec::new()),
            )
            .await
            .expect("应可观测 panic 前的提交副作用");
        assert_eq!(
            rows,
            vec![SqliteRow::new(vec![SqliteValue::Text(
                "committed".to_owned()
            )])]
        );
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn transaction_session_reuses_one_connection_and_keeps_independent_transactions() {
        let directory = TestDirectory::new();
        let database = directory.database("transaction-session.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let opened = <RusqliteStorage as SqliteTransactionSessionFactory>::open_existing_transaction_session(
            &storage,
            database.clone(),
        )
        .await
        .expect("事务计划会话应可打开");
        let (operations, finalizer) = opened.into_parts();

        operations
            .execute_transaction(SqliteTransactionPlan::new(vec![
                SqliteTransactionStep::Execute(SqliteCommand::new(
                    "CREATE TEMP TABLE run_state (value INTEGER NOT NULL)",
                    Vec::new(),
                )),
                SqliteTransactionStep::Execute(SqliteCommand::new(
                    "INSERT INTO run_state (value) VALUES (?1)",
                    vec![SqliteValue::Integer(1)],
                )),
            ]))
            .await
            .expect("首个事务应提交");

        let rejected = operations
            .execute_transaction(SqliteTransactionPlan::new(vec![
                SqliteTransactionStep::Execute(SqliteCommand::new(
                    "INSERT INTO run_state (value) VALUES (?1)",
                    vec![SqliteValue::Integer(2)],
                )),
                SqliteTransactionStep::RequireNoRows(SqliteQuery::new("SELECT 1", Vec::new())),
            ]))
            .await;
        assert!(matches!(
            rejected,
            Err(ExecuteTransactionError::RequirementFailed)
        ));

        // 带诊断行的条件失败同为“已确认回滚”终态,不得把会话毒化为结果未知。
        let rejected_with_row = operations
            .execute_transaction(SqliteTransactionPlan::new(vec![
                SqliteTransactionStep::Execute(SqliteCommand::new(
                    "INSERT INTO run_state (value) VALUES (?1)",
                    vec![SqliteValue::Integer(3)],
                )),
                SqliteTransactionStep::RequireNoRowsReturningFirstRow(SqliteQuery::new(
                    "SELECT 41, 'diagnostic'",
                    Vec::new(),
                )),
            ]))
            .await;
        assert!(matches!(
            rejected_with_row,
            Err(ExecuteTransactionError::RequirementFailedWithRow { .. })
        ));

        operations
            .execute_transaction(SqliteTransactionPlan::new(vec![
                SqliteTransactionStep::Execute(SqliteCommand::new(
                    "INSERT INTO values_table (id, n) SELECT 1, COUNT(*) FROM run_state",
                    Vec::new(),
                )),
            ]))
            .await
            .expect("条件失败回滚后同一会话必须仍可提交后续事务");

        let report = finalizer.finalize().await.expect("事务计划会话应可终结");
        assert!(!report.had_unclosed_transaction());
        let rows = storage
            .query_existing_database(
                database,
                SqliteQuery::new("SELECT n FROM values_table WHERE id = 1", Vec::new()),
            )
            .await
            .expect("应可读取会话提交结果");
        assert_eq!(
            rows,
            vec![SqliteRow::new(vec![SqliteValue::Integer(1)])],
            "TEMP 表证明三个事务复用了同一连接，条件失败事务的值必须已回滚"
        );
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn single_transaction_session_rejects_a_second_open_and_allows_reopen() {
        let directory = TestDirectory::new();
        let database = directory.database("single-interactive-session.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let first = storage
            .open_existing_transaction_session(database.clone())
            .await
            .expect("第一个会话应可打开");
        let (operations, finalizer) = first.into_parts();
        assert!(matches!(
            storage
                .open_existing_transaction_session(database.clone())
                .await,
            Err(OpenSqliteTransactionSessionError::OpenFailed(
                SqliteRuntimeError::InteractiveSessionAlreadyOpen
            ))
        ));
        let report = finalizer.finalize().await.expect("第一个会话应可终结");
        assert!(!report.had_unclosed_transaction());
        assert!(matches!(
            operations
                .execute_transaction(SqliteTransactionPlan::new(Vec::new()))
                .await,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Closed
            ))
        ));
        let reopened = storage
            .open_existing_transaction_session(database)
            .await
            .expect("终结后应可打开新会话");
        let (_operations, finalizer) = reopened.into_parts();
        finalizer.finalize().await.expect("新会话应可终结");
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn dropping_the_unique_finalizer_still_closes_the_session_once() {
        let directory = TestDirectory::new();
        let database = directory.database("dropped-finalizer.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database.clone(), schema_commands())
            .await
            .expect("数据库应可创建");
        let opened = storage
            .open_existing_transaction_session(database.clone())
            .await
            .expect("会话应可打开");
        let (operations, finalizer) = opened.into_parts();

        drop(finalizer);
        wait_until("丢弃的终结令牌完成清理", || {
            let state = storage
                .inner
                .interactive_session
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            !state.opening && state.active.is_none()
        })
        .await;
        assert!(matches!(
            operations
                .execute_transaction(SqliteTransactionPlan::new(Vec::new()))
                .await,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Closed
            ))
        ));
        let reopened = storage
            .open_existing_transaction_session(database)
            .await
            .expect("丢弃的令牌清理后应可重新打开");
        let (_operations, finalizer) = reopened.into_parts();
        finalizer.finalize().await.expect("新会话应可终结");
        storage.shutdown().await.expect("根应可关闭");
    }

    #[tokio::test]
    async fn shutdown_and_unique_finalizer_can_initiate_the_same_session_concurrently() {
        let directory = TestDirectory::new();
        let database = directory.database("concurrent-finalization.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database, schema_commands())
            .await
            .expect("数据库应可创建");
        let opened = storage
            .open_existing_transaction_session(directory.database("concurrent-finalization.db"))
            .await
            .expect("会话应可打开");
        let (_operations, finalizer) = opened.into_parts();
        let finalization = tokio::spawn(finalizer.finalize());
        let shutdown = storage.shutdown();
        let (shutdown_result, report) = tokio::join!(shutdown, finalization);
        shutdown_result.expect("并发终结时根应可关闭");
        let report = report
            .expect("唯一 finalizer 任务不应 panic")
            .expect("会话应可终结");
        assert!(!report.had_unclosed_transaction());
    }

    #[tokio::test]
    async fn finalizer_can_claim_the_report_after_the_last_storage_handle_is_dropped() {
        let directory = TestDirectory::new();
        let database = directory.database("drop-storage-session.db");
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        storage
            .create_new_database(database, schema_commands())
            .await
            .expect("数据库应可创建");
        let opened = storage
            .open_existing_transaction_session(directory.database("drop-storage-session.db"))
            .await
            .expect("会话应可打开");
        let (operations, finalizer) = opened.into_parts();
        drop(storage);

        let report = tokio::time::timeout(Duration::from_secs(5), finalizer.finalize())
            .await
            .expect("最后一个 storage 句柄丢弃后仍应产生终结报告")
            .expect("会话应可终结");
        assert!(!report.had_unclosed_transaction());
        assert!(matches!(
            operations
                .execute_transaction(SqliteTransactionPlan::new(Vec::new()))
                .await,
            Err(ExecuteTransactionError::NotCommitted(
                SqliteRuntimeError::Closed
            ))
        ));
    }

    #[test]
    fn root_futures_are_send() {
        fn assert_send(_: impl Send) {}

        let directory = TestDirectory::new();
        let storage = RusqliteStorage::start(configuration()).expect("根应可启动");
        assert_send(storage.create_new_database(directory.database("send.db"), schema_commands()));
        assert_send(storage.snapshot_database(
            directory.database("send.db"),
            directory.database("send-copy.db"),
        ));
        assert_send(storage.query_existing_database(
            directory.database("send.db"),
            SqliteQuery::new("SELECT 1", Vec::new()),
        ));
        assert_send(storage.execute_transaction(
            directory.database("send.db"),
            SqliteTransactionPlan::new(Vec::new()),
        ));
        assert_send(
            <RusqliteStorage as SqliteTransactionSessionFactory>::open_existing_transaction_session(
                &storage,
                directory.database("send.db"),
            ),
        );
        drop(storage);
    }
}
