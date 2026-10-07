//! 进程内业务执行的共享契约，包括取消状态、正常终态与受控 CPU 计算。

pub(crate) mod cpu;
pub(crate) mod isolated;
pub(crate) mod llm_request;
pub(crate) mod ordered;

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use tokio::sync::watch;

/// 由进程边界发出、由业务阶段边界观察的单向取消事实。
///
/// 取消一旦请求便不会恢复。它只阻止尚未派生的工作；已经被根接管的副作用仍由
/// 相应根能力运行到明确终态。
#[derive(Clone)]
pub(crate) struct CooperativeCancellation {
    state: Arc<CooperativeCancellationState>,
}

struct CooperativeCancellationState {
    requested: AtomicBool,
    notification: watch::Sender<bool>,
}

impl Default for CooperativeCancellation {
    fn default() -> Self {
        let (notification, _) = watch::channel(false);
        Self {
            state: Arc::new(CooperativeCancellationState {
                requested: AtomicBool::new(false),
                notification,
            }),
        }
    }
}

impl CooperativeCancellation {
    pub(crate) fn request(&self) {
        if !self.state.requested.swap(true, Ordering::AcqRel) {
            self.state.notification.send_replace(true);
        }
    }

    pub(crate) fn is_requested(&self) -> bool {
        self.state.requested.load(Ordering::Acquire)
    }

    /// 等待首次取消请求；订阅发生在检查之前，不会丢失并发通知。
    pub(crate) async fn cancelled(&self) {
        let mut notification = self.state.notification.subscribe();
        loop {
            if *notification.borrow_and_update() {
                return;
            }
            if notification.changed().await.is_err() {
                return;
            }
        }
    }
}

/// 一次业务操作已经完成，或在继续派生工作前正常响应了合作式取消。
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum OperationCompletion<T> {
    Completed(T),
    Cancelled,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cancellation_is_shared_and_monotonic() {
        let first = CooperativeCancellation::default();
        let second = first.clone();
        assert!(!first.is_requested());

        second.request();

        assert!(first.is_requested());
        assert!(second.is_requested());
    }

    #[tokio::test]
    async fn cancellation_wakes_existing_and_late_waiters() {
        let cancellation = CooperativeCancellation::default();
        let waiting = cancellation.clone();
        let waiter = tokio::spawn(async move { waiting.cancelled().await });

        tokio::task::yield_now().await;
        cancellation.request();
        tokio::time::timeout(std::time::Duration::from_secs(1), waiter)
            .await
            .expect("取消应立即唤醒既有等待者")
            .expect("取消等待任务不应 panic");
        tokio::time::timeout(std::time::Duration::from_secs(1), cancellation.cancelled())
            .await
            .expect("取消后的等待者应立即返回");
    }
}
