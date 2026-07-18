//! Rayon 线程池配置。
//!
//! 本模块负责读取 Rust 原生核心线程数配置，并在需要时使用局部线程池执行并发任务。

use std::env;
use std::sync::OnceLock;

struct NativeExecutor {
    pool: rayon::ThreadPool,
}

impl NativeExecutor {
    fn new(thread_count: Option<usize>) -> Result<Self, String> {
        let mut builder = rayon::ThreadPoolBuilder::new();
        if let Some(thread_count) = thread_count {
            builder = builder.num_threads(thread_count);
        }
        let pool = builder
            .build()
            .map_err(|error| format!("Rust 线程池创建失败: {error}"))?;
        Ok(Self { pool })
    }

    fn run<F, R>(&self, job: F) -> R
    where
        F: FnOnce() -> R + Send,
        R: Send,
    {
        if self.pool.current_thread_index().is_some() {
            job()
        } else {
            self.pool.install(job)
        }
    }

    fn thread_count(&self) -> usize {
        self.pool.current_num_threads()
    }
}

struct SharedNativeExecutor {
    executor: OnceLock<Result<NativeExecutor, String>>,
}

impl SharedNativeExecutor {
    const fn new() -> Self {
        Self {
            executor: OnceLock::new(),
        }
    }

    fn get_or_init_with<F>(&self, factory: F) -> Result<&NativeExecutor, String>
    where
        F: FnOnce() -> Result<NativeExecutor, String>,
    {
        self.executor
            .get_or_init(factory)
            .as_ref()
            .map_err(Clone::clone)
    }
}

#[cfg(not(test))]
static NATIVE_EXECUTOR: SharedNativeExecutor = SharedNativeExecutor::new();

#[cfg(not(test))]
fn shared_executor() -> Result<&'static NativeExecutor, String> {
    NATIVE_EXECUTOR
        .get_or_init_with(|| read_configured_thread_count().and_then(NativeExecutor::new))
}

#[cfg(test)]
thread_local! {
    static THREAD_COUNT_OVERRIDE: std::cell::RefCell<Option<String>> =
        const { std::cell::RefCell::new(None) };
}

pub(crate) fn run_with_optional_pool<F, R>(job: F) -> Result<R, String>
where
    F: FnOnce() -> R + Send,
    R: Send,
{
    #[cfg(not(test))]
    {
        Ok(shared_executor()?.run(job))
    }
    #[cfg(test)]
    {
        let executor = NativeExecutor::new(read_configured_thread_count_for_test()?)?;
        Ok(executor.run(job))
    }
}

pub(crate) fn executor_thread_count() -> Result<usize, String> {
    #[cfg(not(test))]
    {
        Ok(shared_executor()?.thread_count())
    }
    #[cfg(test)]
    {
        Ok(NativeExecutor::new(read_configured_thread_count_for_test()?)?.thread_count())
    }
}

#[cfg(test)]
fn read_configured_thread_count_for_test() -> Result<Option<usize>, String> {
    let override_value = THREAD_COUNT_OVERRIDE.with(|value| value.borrow().clone());
    if let Some(raw_value) = override_value {
        return parse_configured_thread_count(&raw_value);
    }
    read_configured_thread_count()
}

#[cfg(test)]
pub(crate) fn with_thread_count_override_for_test<F, R>(raw_value: Option<&str>, job: F) -> R
where
    F: FnOnce() -> R,
{
    let previous_value =
        THREAD_COUNT_OVERRIDE.with(|value| value.replace(raw_value.map(str::to_owned)));
    let _guard = ThreadCountOverrideGuard { previous_value };

    job()
}

#[cfg(test)]
struct ThreadCountOverrideGuard {
    previous_value: Option<String>,
}

#[cfg(test)]
impl Drop for ThreadCountOverrideGuard {
    fn drop(&mut self) {
        THREAD_COUNT_OVERRIDE.with(|value| value.replace(self.previous_value.take()));
    }
}

pub(crate) fn read_configured_thread_count() -> Result<Option<usize>, String> {
    let raw_value = match env::var("ATT_MZ_RUST_THREADS") {
        Ok(value) => value,
        Err(env::VarError::NotPresent) => return Ok(None),
        Err(error) => return Err(format!("读取 ATT_MZ_RUST_THREADS 失败: {error}")),
    };
    parse_configured_thread_count(&raw_value)
}

pub(crate) fn parse_configured_thread_count(raw_value: &str) -> Result<Option<usize>, String> {
    let normalized_value = raw_value.trim();
    let parsed = normalized_value.parse::<usize>().map_err(|error| {
        format!("ATT_MZ_RUST_THREADS 必须是非负整数: {normalized_value}: {error}")
    })?;
    if parsed == 0 {
        return Ok(None);
    }
    Ok(Some(parsed))
}

#[cfg(test)]
mod tests {
    use super::{NativeExecutor, SharedNativeExecutor, parse_configured_thread_count};
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn thread_count_env_value_controls_configured_pool_size() {
        assert_eq!(parse_configured_thread_count("4"), Ok(Some(4)));
        assert_eq!(parse_configured_thread_count("64"), Ok(Some(64)));
        assert_eq!(parse_configured_thread_count(" 2 "), Ok(Some(2)));
        assert_eq!(parse_configured_thread_count("0"), Ok(None));
        assert!(parse_configured_thread_count("invalid").is_err());
    }

    #[test]
    fn shared_executor_factory_runs_once_across_repeated_access() {
        let shared = SharedNativeExecutor::new();
        let construction_count = AtomicUsize::new(0);

        let first = shared
            .get_or_init_with(|| {
                construction_count.fetch_add(1, Ordering::SeqCst);
                NativeExecutor::new(Some(2))
            })
            .expect("首次线程池初始化应成功") as *const NativeExecutor;
        for _ in 0..100 {
            let current = shared
                .get_or_init_with(|| {
                    construction_count.fetch_add(1, Ordering::SeqCst);
                    NativeExecutor::new(Some(4))
                })
                .expect("重复读取共享线程池应成功")
                as *const NativeExecutor;
            assert_eq!(current, first);
        }

        assert_eq!(construction_count.load(Ordering::SeqCst), 1);
    }
}
