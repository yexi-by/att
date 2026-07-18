"""跨进程独占的游戏修改租约。"""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from .errors import MutationLeaseContendedError, MutationLeaseError

_LOCK_FILE_SUFFIX = ".mutation.lock"
_PROCESS_GUARD = threading.Lock()
_PROCESS_LOCKED_PATHS: set[Path] = set()


class GameMutationLease:
    """持有数据库同级锁文件的非阻塞独占租约。"""

    lock_path: Path
    db_path: Path

    def __init__(self, *, db_path: Path, lock_path: Path, lock_file: BinaryIO) -> None:
        self.db_path = db_path
        self.lock_path = lock_path
        self._lock_file: BinaryIO | None = lock_file

    @classmethod
    def acquire(cls, *, db_path: Path) -> Self:
        """取得指定游戏数据库的修改租约；租约被占用时立即失败。"""
        resolved_db_path = db_path.resolve()
        lock_path = mutation_lease_path(resolved_db_path)
        return cls.acquire_lock_path(lock_path=lock_path, db_path=resolved_db_path)

    @classmethod
    def acquire_lock_path(cls, *, lock_path: Path, db_path: Path | None = None) -> Self:
        """取得显式锁文件，供注册表级串行化复用。"""
        requested_db_path = db_path or lock_path
        try:
            resolved_lock_path = lock_path.resolve()
            resolved_lock_path.parent.mkdir(parents=True, exist_ok=True)
            error_db_path = requested_db_path.resolve()
        except BaseException as error:
            raise MutationLeaseError(
                db_path=requested_db_path,
                lock_path=lock_path,
                reason=f"{type(error).__name__}: {error}",
            ) from error

        with _PROCESS_GUARD:
            if resolved_lock_path in _PROCESS_LOCKED_PATHS:
                raise MutationLeaseContendedError(
                    db_path=error_db_path,
                    lock_path=resolved_lock_path,
                )
            _PROCESS_LOCKED_PATHS.add(resolved_lock_path)

        lock_file: BinaryIO | None = None
        try:
            lock_file = resolved_lock_path.open("a+b")
            _ensure_lock_byte(lock_file)
            _lock_file_nonblocking(
                lock_file=lock_file,
                db_path=error_db_path,
                lock_path=resolved_lock_path,
            )
            return cls(
                db_path=error_db_path,
                lock_path=resolved_lock_path,
                lock_file=lock_file,
            )
        except MutationLeaseContendedError:
            close_error: BaseException | None = None
            if lock_file is not None:
                close_error = _close_lock_file(lock_file)
            _forget_process_lock(resolved_lock_path)
            if close_error is not None:
                raise MutationLeaseError(
                    db_path=error_db_path,
                    lock_path=resolved_lock_path,
                    reason=f"竞争失败后关闭锁文件失败：{type(close_error).__name__}: {close_error}",
                ) from close_error
            raise
        except BaseException as error:
            close_error = None
            if lock_file is not None:
                close_error = _close_lock_file(lock_file)
            _forget_process_lock(resolved_lock_path)
            close_suffix = (
                "" if close_error is None else f"；关闭锁文件失败：{type(close_error).__name__}: {close_error}"
            )
            raise MutationLeaseError(
                db_path=error_db_path,
                lock_path=resolved_lock_path,
                reason=f"{type(error).__name__}: {error}{close_suffix}",
            ) from error

    @property
    def is_released(self) -> bool:
        """返回当前句柄是否已经释放。"""
        return self._lock_file is None

    def release(self) -> None:
        """释放操作系统锁；重复调用不产生副作用。"""
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        unlock_error: BaseException | None = None
        close_error: BaseException | None = None
        try:
            _unlock_file(lock_file)
        except BaseException as error:
            unlock_error = error
        finally:
            try:
                lock_file.close()
            except BaseException as error:
                close_error = error
            finally:
                _forget_process_lock(self.lock_path)
        if unlock_error is not None or close_error is not None:
            reasons: list[str] = []
            if unlock_error is not None:
                reasons.append(f"解锁失败：{type(unlock_error).__name__}: {unlock_error}")
            if close_error is not None:
                reasons.append(f"关闭失败：{type(close_error).__name__}: {close_error}")
            cause = close_error or unlock_error
            assert cause is not None
            raise MutationLeaseError(
                db_path=self.db_path,
                lock_path=self.lock_path,
                reason="；".join(reasons),
            ) from cause

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def mutation_lease_path(db_path: Path) -> Path:
    """返回不会被删除的数据库同级锁文件路径。"""
    return db_path.with_name(f"{db_path.name}{_LOCK_FILE_SUFFIX}")


def registry_mutation_lease_path(db_directory: Path) -> Path:
    """返回用于串行化新游戏注册与目标重判的持久锁文件。"""
    return db_directory / ".att-mz-registry.mutation.lock"


def _ensure_lock_byte(lock_file: BinaryIO) -> None:
    """确保 Windows 字节区间锁存在可锁定的首字节。"""
    _ = lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        _ = lock_file.write(b"\0")
        lock_file.flush()
        os.fsync(lock_file.fileno())
    _ = lock_file.seek(0)


def _lock_file_nonblocking(*, lock_file: BinaryIO, db_path: Path, lock_path: Path) -> None:
    """按当前平台取得首字节/整文件非阻塞独占锁。"""
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise MutationLeaseContendedError(
                db_path=db_path,
                lock_path=lock_path,
            ) from error
        return

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno not in {errno.EACCES, errno.EAGAIN}:
            raise
        raise MutationLeaseContendedError(
            db_path=db_path,
            lock_path=lock_path,
        ) from error


def _unlock_file(lock_file: BinaryIO) -> None:
    """释放当前平台的修改租约。"""
    _ = lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _forget_process_lock(lock_path: Path) -> None:
    with _PROCESS_GUARD:
        _PROCESS_LOCKED_PATHS.discard(lock_path)


def _close_lock_file(lock_file: BinaryIO) -> BaseException | None:
    try:
        lock_file.close()
    except BaseException as error:
        return error
    return None


__all__ = [
    "GameMutationLease",
    "mutation_lease_path",
    "registry_mutation_lease_path",
]
