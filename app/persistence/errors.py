"""持久化边界的稳定业务错误。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


class PersistenceBusinessError(RuntimeError):
    """可由 CLI 直接映射为结构化错误的持久化失败。"""

    default_code: str = "persistence_error"
    code: str
    details: dict[str, object]

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        normalized_code = (code or self.default_code).strip()
        if not normalized_code:
            raise ValueError("持久化错误码不能为空")
        self.code = normalized_code
        self.details = dict(details or {})


class DatabaseMigrationRequiredError(PersistenceBusinessError):
    """数据库不是严格 schema 12，运行时拒绝自动迁移。"""

    default_code: str = "database_migration_required"

    def __init__(
        self,
        *,
        db_path: Path,
        actual_version: int | str | None,
        required_version: int,
        reason: str,
    ) -> None:
        actual_text = "missing" if actual_version is None else str(actual_version)
        super().__init__(
            (
                f"数据库结构不符合当前版本，需要显式迁移后才能继续: {db_path}；"
                f"实际 schema={actual_text}，要求 schema={required_version}；{reason}"
            ),
            details={
                "db_path": str(db_path),
                "actual_version": actual_version,
                "required_version": required_version,
                "reason": reason,
            },
        )


class GameRegistrationConflictError(PersistenceBusinessError):
    """同一游戏已用不同的不可变语言配置注册。"""

    default_code: str = "game_registration_conflict"


class MutationLeaseContendedError(PersistenceBusinessError):
    """另一个修改命令正在独占当前游戏。"""

    default_code: str = "mutation_in_progress"

    def __init__(self, *, db_path: Path, lock_path: Path) -> None:
        super().__init__(
            "当前游戏正在被另一个修改命令处理，请等待该命令结束后重试",
            details={
                "db_path": str(db_path),
                "lock_path": str(lock_path),
            },
        )


class MutationLeaseError(PersistenceBusinessError):
    """修改租约文件无法安全取得或释放。"""

    default_code: str = "mutation_lock_failed"

    def __init__(self, *, db_path: Path, lock_path: Path, reason: str) -> None:
        super().__init__(
            f"无法安全管理当前游戏的修改锁：{reason}",
            details={
                "db_path": str(db_path),
                "lock_path": str(lock_path),
                "reason": reason,
            },
        )


class RecoveryRequiredError(PersistenceBusinessError):
    """存在无法自动完成的写事务，必须先显式恢复。"""

    default_code: str = "recovery_required"

    def __init__(
        self,
        message: str,
        *,
        transaction_id: str | None = None,
        state: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """保存事务标识和阻断状态，供 CLI 稳定输出。"""
        merged_details = dict(details or {})
        if transaction_id is not None:
            merged_details["transaction_id"] = transaction_id
        if state is not None:
            merged_details["state"] = state
        super().__init__(message, details=merged_details)


class TranslationRunRecoveryRequiredError(PersistenceBusinessError):
    """翻译运行终态恢复记录无法自动协调。"""

    default_code: str = "translation_run_recovery_required"

    def __init__(
        self,
        *,
        db_path: Path,
        recovery_path: Path,
        reason: str,
        run_id: str | None = None,
    ) -> None:
        details: dict[str, object] = {
            "db_path": str(db_path),
            "recovery_path": str(recovery_path),
            "reason": reason,
        }
        if run_id is not None:
            details["run_id"] = run_id
        super().__init__(
            f"翻译运行的失败终态尚未安全恢复，当前命令不能继续：{reason}",
            details=details,
        )


class TranslationRunStateConflictError(PersistenceBusinessError):
    """翻译运行状态不允许当前写操作继续。"""

    default_code: str = "translation_run_state_conflict"

    def __init__(self, message: str, *, run_id: str | None = None, reason: str) -> None:
        details: dict[str, object] = {"reason": reason}
        if run_id is not None:
            details["run_id"] = run_id
        super().__init__(message, details=details)


__all__ = [
    "DatabaseMigrationRequiredError",
    "GameRegistrationConflictError",
    "MutationLeaseContendedError",
    "MutationLeaseError",
    "PersistenceBusinessError",
    "RecoveryRequiredError",
    "TranslationRunRecoveryRequiredError",
    "TranslationRunStateConflictError",
]
