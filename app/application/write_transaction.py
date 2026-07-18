"""可崩溃恢复的游戏文件写事务。

本模块只负责文件系统事务：所有新内容与原文件备份都先在目标文件
同目录持久化，然后才逐个原子替换。数据库中的写事务记录由应用层在
文件审计通过后单独提交，恢复时以数据库的已提交状态为最终依据。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from app.native_file_hashing import NativeFileHashInput, NativeFileHashResult, hash_native_files
from app.native_runtime import NativeRuntimeError
from app.rmmz.text_rules import JsonObject, JsonValue, ensure_json_array, ensure_json_object

type FileWriteTransactionState = Literal[
    "preparing",
    "prepared",
    "replacing",
    "verifying",
    "committed",
    "rolling_back",
    "rolled_back",
    "recovery_failed",
]
type _FilePathKind = Literal["missing", "regular", "link", "other"]

JOURNAL_VERSION = 1
JOURNAL_DIRECTORY_NAME = ".att-mz-write-transactions"
ARTIFACT_MARKER = ".att-mz-write-"
_WINDOWS_REPLACE_RETRY_WINERRORS = frozenset({5, 32, 33})
_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.05, 0.1)


class FileWriteTransactionError(RuntimeError):
    """写事务准备、替换或恢复失败。"""

    code: str | None
    details: dict[str, object]

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class FileWriteRecoveryConflictError(FileWriteTransactionError):
    """目标文件已被本事务之外的操作修改，不能安全自动恢复。"""


@dataclass(frozen=True, slots=True)
class _FileHashRequest:
    """一次 native 哈希批次中的文件及其失败分类。"""

    id: str
    path: Path
    conflict_on_failure: bool


@dataclass(frozen=True, slots=True)
class PlannedFileWrite:
    """一个待持久化的文件内容。"""

    target_path: Path
    content: bytes | None = None
    source_path: Path | None = None

    @classmethod
    def from_text(cls, *, target_path: Path, content: str) -> "PlannedFileWrite":
        """构造 UTF-8 文本写入。"""
        return cls(target_path=target_path, content=content.encode("utf-8"))

    @classmethod
    def from_source(cls, *, target_path: Path, source_path: Path) -> "PlannedFileWrite":
        """构造来自已有文件的二进制写入。"""
        return cls(target_path=target_path, source_path=source_path)


@dataclass(frozen=True, slots=True)
class FileWriteJournalEntry:
    """写事务 journal 中的单文件状态。"""

    target_relative_path: str
    staged_relative_path: str
    backup_relative_path: str | None
    existed_before: bool
    original_sha256: str | None
    staged_sha256: str | None
    replaced: bool = False


@dataclass(frozen=True, slots=True)
class FileWriteManifestEntry:
    """可持久到数据库的单文件恢复清单。

    这些字段是恢复所需的完整事实，不包含 journal 中的进度提示。
    """

    target_relative_path: str
    staged_relative_path: str
    backup_relative_path: str | None
    existed_before: bool
    original_sha256: str | None
    target_sha256: str


@dataclass(frozen=True, slots=True)
class FileWriteRecoverySummary:
    """文件事务恢复摘要。"""

    transaction_id: str
    restored_file_count: int
    finalized_committed_file_count: int
    state: FileWriteTransactionState


class DurableFileWriteTransaction:
    """带哈希、同目录备份和崩溃 journal 的文件写事务。"""

    transaction_id: str
    mode: str
    content_root: Path
    journal_path: Path
    state: FileWriteTransactionState
    created_at: str
    updated_at: str
    entries: list[FileWriteJournalEntry]

    def __init__(
        self,
        *,
        transaction_id: str,
        mode: str,
        content_root: Path,
        journal_path: Path,
        state: FileWriteTransactionState,
        created_at: str,
        updated_at: str,
        entries: list[FileWriteJournalEntry],
    ) -> None:
        self.transaction_id = transaction_id
        self.mode = mode
        self.content_root = content_root
        self.journal_path = journal_path
        self.state = state
        self.created_at = created_at
        self.updated_at = updated_at
        self.entries = entries

    @classmethod
    def prepare(
        cls,
        *,
        mode: str,
        content_root: Path,
        writes: list[PlannedFileWrite],
        transaction_id: str | None = None,
    ) -> "DurableFileWriteTransaction":
        """暂存全部新内容和原文件备份，且在任何目标替换前完成哈希校验。"""
        if not writes:
            raise ValueError("写事务至少需要一个目标文件")
        resolved_root = content_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"写事务内容根目录无效: {resolved_root}")

        resolved_transaction_id = transaction_id or new_file_write_transaction_id()
        _validate_transaction_id(resolved_transaction_id)
        journal_path = file_write_transaction_journal_path(
            content_root=resolved_root,
            transaction_id=resolved_transaction_id,
        )
        journal_directory = journal_path.parent
        journal_directory.mkdir(parents=True, exist_ok=True)
        if not journal_directory.is_dir() or journal_directory.resolve(strict=True) != journal_directory:
            raise FileWriteTransactionError(f"写事务 journal 目录不能是符号链接或目录联接: {journal_directory}")
        if journal_path.exists():
            raise FileExistsError(f"写事务 journal 已存在: {journal_path}")

        normalized_writes = _normalize_writes(
            writes=writes,
            content_root=resolved_root,
        )
        initial_entries = _build_initial_entries(
            content_root=resolved_root,
            transaction_id=resolved_transaction_id,
            writes=normalized_writes,
        )
        timestamp = _timestamp_text()
        transaction = cls(
            transaction_id=resolved_transaction_id,
            mode=mode,
            content_root=resolved_root,
            journal_path=journal_path,
            state="preparing",
            created_at=timestamp,
            updated_at=timestamp,
            entries=initial_entries,
        )
        transaction._persist_journal()
        try:
            for write, entry in zip(normalized_writes, transaction.entries, strict=True):
                _prepare_entry(
                    content_root=resolved_root,
                    write=write,
                    entry=entry,
                )
            transaction.entries = transaction._hash_and_validate_prepared_entries(
                record_staged_hashes=True,
            )
            transaction._set_state("prepared")
            return transaction
        except BaseException:
            transaction._cleanup_uncommitted_artifacts()
            raise

    @classmethod
    def load(cls, *, journal_path: Path, content_root: Path) -> "DurableFileWriteTransaction":
        """从崩溃 journal 恢复事务内存状态。"""
        resolved_root = content_root.resolve(strict=True)
        resolved_journal_path = journal_path.resolve(strict=True)
        expected_journal_directory = (resolved_root / JOURNAL_DIRECTORY_NAME).resolve()
        if resolved_journal_path.parent != expected_journal_directory:
            raise FileWriteTransactionError(f"写事务 journal 不在当前游戏目录内: {resolved_journal_path}")
        payload = ensure_json_object(
            cast(JsonValue, json.loads(resolved_journal_path.read_text(encoding="utf-8"))),
            "write_transaction_journal",
        )
        expected_journal_keys = {
            "version",
            "transaction_id",
            "mode",
            "content_root",
            "state",
            "created_at",
            "updated_at",
            "entries",
        }
        if set(payload) != expected_journal_keys:
            raise FileWriteTransactionError(f"写事务 journal 字段必须严格为 {sorted(expected_journal_keys)}")
        version = payload.get("version")
        if isinstance(version, bool) or version != JOURNAL_VERSION:
            raise FileWriteTransactionError(f"不支持的写事务 journal 版本: {version!r}")
        transaction_id = _read_json_str(payload, "transaction_id")
        _validate_transaction_id(transaction_id)
        journal_root = Path(_read_json_str(payload, "content_root")).resolve(strict=True)
        if journal_root != resolved_root:
            raise FileWriteTransactionError("写事务 journal 与当前游戏内容目录不匹配")
        entries = [
            _parse_journal_entry(
                ensure_json_object(item, f"write_transaction_journal.entries[{index}]"),
                content_root=resolved_root,
                transaction_id=transaction_id,
            )
            for index, item in enumerate(ensure_json_array(payload.get("entries"), "write_transaction_journal.entries"))
        ]
        if not entries:
            raise FileWriteTransactionError("写事务 journal 没有文件条目")
        target_paths = [entry.target_relative_path for entry in entries]
        if len(target_paths) != len(set(target_paths)):
            raise FileWriteTransactionError("写事务 journal 包含重复目标文件")
        return cls(
            transaction_id=transaction_id,
            mode=_read_json_str(payload, "mode"),
            content_root=resolved_root,
            journal_path=resolved_journal_path,
            state=_parse_state(_read_json_str(payload, "state")),
            created_at=_read_json_str(payload, "created_at"),
            updated_at=_read_json_str(payload, "updated_at"),
            entries=entries,
        )

    @classmethod
    def from_manifest(
        cls,
        *,
        transaction_id: str,
        mode: str,
        content_root: Path,
        journal_path: Path,
        database_committed: bool,
        created_at: str,
        updated_at: str,
        entries: Sequence[FileWriteManifestEntry],
    ) -> "DurableFileWriteTransaction":
        """仅依赖数据库持久化清单重建恢复上下文。"""
        resolved_root = content_root.resolve(strict=True)
        _validate_transaction_id(transaction_id)
        expected_journal_path = file_write_transaction_journal_path(
            content_root=resolved_root,
            transaction_id=transaction_id,
        )
        if journal_path.resolve() != expected_journal_path.resolve():
            raise FileWriteTransactionError("数据库写事务 journal 路径与事务标识不匹配")

        restored_entries = [
            _journal_entry_from_manifest(
                entry=entry,
                content_root=resolved_root,
                transaction_id=transaction_id,
            )
            for entry in entries
        ]
        target_paths = [entry.target_relative_path for entry in restored_entries]
        if len(target_paths) != len(set(target_paths)):
            raise FileWriteTransactionError("数据库写事务清单包含重复目标文件")
        return cls(
            transaction_id=transaction_id,
            mode=mode,
            content_root=resolved_root,
            journal_path=expected_journal_path,
            state="committed" if database_committed else "prepared",
            created_at=created_at,
            updated_at=updated_at,
            entries=restored_entries,
        )

    def export_manifest(self) -> tuple[FileWriteManifestEntry, ...]:
        """导出可在 journal 消失后独立恢复的严格文件清单。"""
        if self.state != "prepared":
            raise FileWriteTransactionError(f"写事务状态 {self.state} 不允许导出恢复清单")
        self._verify_prepared_entries()
        manifest_entries: list[FileWriteManifestEntry] = []
        for entry in self.entries:
            if entry.staged_sha256 is None:
                raise FileWriteTransactionError("已准备写事务缺少目标哈希")
            manifest_entries.append(
                FileWriteManifestEntry(
                    target_relative_path=entry.target_relative_path,
                    staged_relative_path=entry.staged_relative_path,
                    backup_relative_path=entry.backup_relative_path,
                    existed_before=entry.existed_before,
                    original_sha256=entry.original_sha256,
                    target_sha256=entry.staged_sha256,
                )
            )
        return tuple(manifest_entries)

    def rollback_pre_database_crash(self) -> FileWriteRecoverySummary:
        """仅使用未进入数据库 prepared 状态的可验证 journal 恢复旧文件。"""
        self._validate_pre_database_recovery_journal()
        return self.rollback()

    @contextmanager
    def staged_runtime_view(self, *, game_path: Path) -> Generator[Path]:
        """物化一个只读审计用运行视图，不替换真实目标。"""
        if self.state != "prepared":
            raise FileWriteTransactionError(f"写事务状态 {self.state} 不允许构建暂存运行视图")
        self._verify_prepared_entries()
        resolved_game_path = game_path.resolve(strict=True)
        try:
            content_relative_path = self.content_root.relative_to(resolved_game_path)
        except ValueError as error:
            raise FileWriteTransactionError("写事务内容目录不属于当前游戏目录") from error

        with tempfile.TemporaryDirectory(prefix="att_mz_staged_runtime_") as temporary_directory:
            staged_game_path = Path(temporary_directory) / "game"
            staged_content_root = staged_game_path / content_relative_path
            staged_content_root.mkdir(parents=True, exist_ok=True)
            _clone_runtime_inputs(
                game_path=resolved_game_path,
                content_root=self.content_root,
                staged_game_path=staged_game_path,
                staged_content_root=staged_content_root,
            )
            for entry in self.entries:
                staged_source_path = self._resolve_relative_path(entry.staged_relative_path)
                view_target_path = staged_content_root / Path(entry.target_relative_path)
                view_target_path.parent.mkdir(parents=True, exist_ok=True)
                if view_target_path.exists() or view_target_path.is_symlink():
                    view_target_path.unlink()
                _ = shutil.copyfile(staged_source_path, view_target_path)
            yield staged_game_path

    def replace_targets(self) -> None:
        """在全部准备校验通过后原子替换目标文件。"""
        if self.state != "prepared":
            raise FileWriteTransactionError(f"写事务状态 {self.state} 不允许替换文件")
        self._verify_prepared_entries()
        self._set_state("replacing")
        try:
            for index, entry in enumerate(self.entries):
                target_path = self._resolve_relative_path(entry.target_relative_path)
                staged_path = self._resolve_relative_path(entry.staged_relative_path)
                self._assert_target_path_fact_before_replace(entry=entry, target_path=target_path)
                _replace_atomically_with_windows_retry(
                    source_path=staged_path,
                    target_path=target_path,
                    before_retry=lambda entry=entry, target_path=target_path: (
                        self._assert_original_target_unchanged_before_retry(
                            entry=entry,
                            target_path=target_path,
                        )
                    ),
                )
                _fsync_directory(target_path.parent)
                self._verify_single_replaced_target(entry=entry, target_path=target_path)
                self.entries[index] = replace(entry, replaced=True)
                self._persist_journal()
            self._verify_committed_targets()
            self._set_state("verifying")
        except BaseException:
            _ = self.rollback()
            raise

    def verify_replaced_targets(self) -> None:
        """在数据库提交前再次确认全部目标仍是本事务写入的内容。"""
        if self.state != "verifying":
            raise FileWriteTransactionError(f"写事务状态 {self.state} 不允许校验替换结果")
        self._verify_committed_targets()

    def mark_committed_and_cleanup(self) -> None:
        """数据库状态已提交后，标记文件事务完成并清理备份。"""
        if self.state not in {"verifying", "committed"}:
            raise FileWriteTransactionError(f"写事务状态 {self.state} 不允许提交")
        self._verify_committed_targets()
        if self.state != "committed":
            self._set_state("committed")
        self._cleanup_artifacts(remove_journal=True)

    def rollback(self) -> FileWriteRecoverySummary:
        """按备份逆序恢复所有已替换文件。"""
        if self.state == "committed":
            raise FileWriteTransactionError("已提交的写事务不能回滚")
        self._set_state("rolling_back")
        restored_count = 0
        try:
            current_hashes = self._hash_and_validate_rollback_sources()
            for entry in reversed(self.entries):
                if self._restore_entry(
                    entry,
                    current_hash=current_hashes.get(entry.target_relative_path),
                ):
                    restored_count += 1
            self._verify_rolled_back_targets()
            self._set_state("rolled_back")
            return FileWriteRecoverySummary(
                transaction_id=self.transaction_id,
                restored_file_count=restored_count,
                finalized_committed_file_count=0,
                state="rolled_back",
            )
        except BaseException:
            self._set_state("recovery_failed")
            raise

    def finalize_rolled_back_cleanup(self) -> None:
        """数据库已记录回滚后清理 journal 和事务产物。"""
        if self.state != "rolled_back":
            raise FileWriteTransactionError(f"写事务状态 {self.state} 未完成回滚")
        self._verify_rolled_back_targets()
        self._cleanup_artifacts(remove_journal=True)

    def recover(self, *, database_committed: bool) -> FileWriteRecoverySummary:
        """依数据库最终状态完成提交清理或回滚。"""
        if database_committed:
            self._verify_committed_targets()
            finalized_count = len(self.entries)
            if self.state != "committed":
                self._set_state("committed")
            self._cleanup_artifacts(remove_journal=True)
            return FileWriteRecoverySummary(
                transaction_id=self.transaction_id,
                restored_file_count=0,
                finalized_committed_file_count=finalized_count,
                state="committed",
            )
        summary = self.rollback()
        self.finalize_rolled_back_cleanup()
        return summary

    def _set_state(self, state: FileWriteTransactionState) -> None:
        self.state = state
        self.updated_at = _timestamp_text()
        self._persist_journal()

    def _persist_journal(self) -> None:
        payload: JsonObject = {
            "version": JOURNAL_VERSION,
            "transaction_id": self.transaction_id,
            "mode": self.mode,
            "content_root": str(self.content_root),
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "entries": [
                {
                    "target_relative_path": entry.target_relative_path,
                    "staged_relative_path": entry.staged_relative_path,
                    "backup_relative_path": entry.backup_relative_path,
                    "existed_before": entry.existed_before,
                    "original_sha256": entry.original_sha256,
                    "staged_sha256": entry.staged_sha256,
                    "replaced": entry.replaced,
                }
                for entry in self.entries
            ],
        }
        _write_json_atomically(self.journal_path, payload)

    def _resolve_relative_path(self, relative_path: str) -> Path:
        return _resolve_journal_relative_path(
            content_root=self.content_root,
            relative_path=relative_path,
        )

    def _resolve_required_backup_path(self, entry: FileWriteJournalEntry) -> Path:
        if entry.backup_relative_path is None:
            raise FileWriteTransactionError("已存在目标缺少备份路径")
        return self._resolve_relative_path(entry.backup_relative_path)

    def _verify_prepared_entries(self) -> None:
        _ = self._hash_and_validate_prepared_entries(record_staged_hashes=False)

    def _hash_and_validate_prepared_entries(
        self,
        *,
        record_staged_hashes: bool,
    ) -> list[FileWriteJournalEntry]:
        """一次批量验证暂存、备份和当前目标，并可记录新暂存哈希。"""
        requests: list[_FileHashRequest] = []
        for index, entry in enumerate(self.entries):
            staged_path = self._resolve_relative_path(entry.staged_relative_path)
            if not record_staged_hashes and entry.staged_sha256 is None:
                raise FileWriteTransactionError("已准备写事务缺少暂存文件哈希")
            if _file_path_kind(staged_path) != "regular":
                raise FileWriteTransactionError(f"写事务暂存文件缺失或哈希不匹配: {staged_path}")
            requests.append(
                _FileHashRequest(
                    id=_hash_request_id("stage", index),
                    path=staged_path,
                    conflict_on_failure=False,
                )
            )
            if entry.existed_before:
                if entry.backup_relative_path is None or entry.original_sha256 is None:
                    raise FileWriteTransactionError("已存在目标缺少备份信息")
                backup_path = self._resolve_relative_path(entry.backup_relative_path)
                if _file_path_kind(backup_path) != "regular":
                    raise FileWriteTransactionError(f"写事务备份缺失或哈希不匹配: {backup_path}")
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("backup", index),
                        path=backup_path,
                        conflict_on_failure=False,
                    )
                )

            target_path = self._resolve_relative_path(entry.target_relative_path)
            target_kind = _file_path_kind(target_path)
            if entry.existed_before:
                if entry.original_sha256 is None or target_kind != "regular":
                    raise FileWriteRecoveryConflictError(f"目标文件在替换前已消失: {target_path}")
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("target", index),
                        path=target_path,
                        conflict_on_failure=True,
                    )
                )
            elif target_kind != "missing":
                raise FileWriteRecoveryConflictError(f"本应新建的目标文件已被其他操作创建: {target_path}")

        hashes = _hash_file_batch(
            root=self.content_root,
            requests=requests,
            context="验证已准备写事务",
        )
        updated_entries: list[FileWriteJournalEntry] = []
        for index, entry in enumerate(self.entries):
            staged_hash = hashes[_hash_request_id("stage", index)].sha256
            if entry.staged_sha256 is not None and staged_hash != entry.staged_sha256:
                staged_path = self._resolve_relative_path(entry.staged_relative_path)
                raise FileWriteTransactionError(f"写事务暂存文件缺失或哈希不匹配: {staged_path}")
            if entry.existed_before:
                assert entry.original_sha256 is not None
                backup_hash = hashes[_hash_request_id("backup", index)].sha256
                if backup_hash != entry.original_sha256:
                    backup_path = self._resolve_required_backup_path(entry)
                    raise FileWriteTransactionError(f"写事务备份缺失或哈希不匹配: {backup_path}")
                target_hash = hashes[_hash_request_id("target", index)].sha256
                if target_hash != entry.original_sha256:
                    target_path = self._resolve_relative_path(entry.target_relative_path)
                    raise FileWriteRecoveryConflictError(f"目标文件在替换前已被其他操作修改: {target_path}")
            updated_entries.append(replace(entry, staged_sha256=staged_hash) if record_staged_hashes else entry)
        return updated_entries

    def _validate_pre_database_recovery_journal(self) -> None:
        """验证 preparing 崩溃 journal 只会恢复本事务绑定且未被外部修改的目标。"""
        if self.state not in {"preparing", "prepared", "rolling_back", "rolled_back", "recovery_failed"}:
            raise FileWriteTransactionError(f"数据库未准备写事务不允许恢复 journal 状态 {self.state}")
        requests: list[_FileHashRequest] = []
        for index, entry in enumerate(self.entries):
            if entry.replaced:
                raise FileWriteTransactionError("数据库未准备写事务的 journal 不得声明已替换目标")
            if self.state == "prepared" and entry.staged_sha256 is None:
                raise FileWriteTransactionError("prepared 写事务 journal 缺少暂存文件哈希")

            target_path = self._resolve_relative_path(entry.target_relative_path)
            target_kind = _file_path_kind(target_path)
            if entry.existed_before:
                if entry.original_sha256 is None or target_kind != "regular":
                    raise FileWriteRecoveryConflictError(f"目标文件在替换前已消失: {target_path}")
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("target", index),
                        path=target_path,
                        conflict_on_failure=True,
                    )
                )
            elif target_kind != "missing":
                raise FileWriteRecoveryConflictError(f"本应新建的目标文件已被其他操作创建: {target_path}")

            staged_path = self._resolve_relative_path(entry.staged_relative_path)
            staged_kind = _file_path_kind(staged_path)
            if staged_kind not in {"missing", "regular"}:
                raise FileWriteTransactionError(f"写事务暂存产物不是普通文件: {staged_path}")
            if staged_kind == "missing" and entry.staged_sha256 is not None:
                raise FileWriteTransactionError(f"写事务暂存产物缺失或哈希不匹配: {staged_path}")
            if staged_kind == "regular":
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("stage", index),
                        path=staged_path,
                        conflict_on_failure=False,
                    )
                )

            if entry.backup_relative_path is None:
                continue
            backup_path = self._resolve_relative_path(entry.backup_relative_path)
            backup_kind = _file_path_kind(backup_path)
            if backup_kind not in {"missing", "regular"}:
                raise FileWriteTransactionError(f"写事务备份产物不是普通文件: {backup_path}")
            if backup_kind == "regular":
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("backup", index),
                        path=backup_path,
                        conflict_on_failure=False,
                    )
                )
            elif entry.staged_sha256 is not None:
                raise FileWriteTransactionError(f"已暂存写事务缺少原文件备份: {backup_path}")

        hashes = _hash_file_batch(
            root=self.content_root,
            requests=requests,
            context="验证数据库准备前崩溃 journal",
        )
        for index, entry in enumerate(self.entries):
            if entry.existed_before:
                assert entry.original_sha256 is not None
                if hashes[_hash_request_id("target", index)].sha256 != entry.original_sha256:
                    target_path = self._resolve_relative_path(entry.target_relative_path)
                    raise FileWriteRecoveryConflictError(f"目标文件在替换前已被其他操作修改: {target_path}")
            stage_id = _hash_request_id("stage", index)
            if entry.staged_sha256 is not None and hashes[stage_id].sha256 != entry.staged_sha256:
                staged_path = self._resolve_relative_path(entry.staged_relative_path)
                raise FileWriteTransactionError(f"写事务暂存产物缺失或哈希不匹配: {staged_path}")
            backup_id = _hash_request_id("backup", index)
            if backup_id in hashes and hashes[backup_id].sha256 != entry.original_sha256:
                backup_path = self._resolve_required_backup_path(entry)
                raise FileWriteTransactionError(f"写事务备份产物哈希不匹配: {backup_path}")

    def _assert_target_path_fact_before_replace(
        self,
        *,
        entry: FileWriteJournalEntry,
        target_path: Path,
    ) -> None:
        """替换瞬间再次用不跟随链接的路径事实保护新建目标。"""
        target_kind = _file_path_kind(target_path)
        if entry.existed_before and target_kind != "regular":
            raise FileWriteRecoveryConflictError(f"目标文件在替换前已消失: {target_path}")
        if not entry.existed_before and target_kind != "missing":
            raise FileWriteRecoveryConflictError(f"本应新建的目标文件已被其他操作创建: {target_path}")

    def _assert_original_target_unchanged_before_retry(
        self,
        *,
        entry: FileWriteJournalEntry,
        target_path: Path,
    ) -> None:
        """Windows 替换重试前重新核对目标类型和准备时原内容。"""
        self._assert_target_path_fact_before_replace(entry=entry, target_path=target_path)
        if not entry.existed_before:
            return
        if entry.original_sha256 is None:
            raise FileWriteTransactionError("已存在目标缺少原文件哈希")
        current_hash = _hash_file_batch(
            root=self.content_root,
            requests=[
                _FileHashRequest(
                    id="retry_target",
                    path=target_path,
                    conflict_on_failure=True,
                )
            ],
            context="验证 Windows 原子替换重试目标",
        )["retry_target"].sha256
        if current_hash != entry.original_sha256:
            raise FileWriteRecoveryConflictError(f"目标文件在替换重试前已被其他操作修改: {target_path}")

    def _verify_single_replaced_target(
        self,
        *,
        entry: FileWriteJournalEntry,
        target_path: Path,
    ) -> None:
        """每次原子替换后立即以单项 native 请求验证目标。"""
        if entry.staged_sha256 is None or _file_path_kind(target_path) != "regular":
            raise FileWriteTransactionError(f"目标文件替换后哈希不匹配: {target_path}")
        result = _hash_file_batch(
            root=self.content_root,
            requests=[
                _FileHashRequest(
                    id="replaced_target",
                    path=target_path,
                    conflict_on_failure=False,
                )
            ],
            context="验证单个替换目标",
        )["replaced_target"]
        if result.sha256 != entry.staged_sha256:
            raise FileWriteTransactionError(f"目标文件替换后哈希不匹配: {target_path}")

    def _verify_committed_targets(self) -> None:
        requests: list[_FileHashRequest] = []
        for index, entry in enumerate(self.entries):
            target_path = self._resolve_relative_path(entry.target_relative_path)
            if entry.staged_sha256 is None or _file_path_kind(target_path) != "regular":
                raise FileWriteRecoveryConflictError(f"已提交写事务的目标文件哈希不匹配: {target_path}")
            requests.append(
                _FileHashRequest(
                    id=_hash_request_id("target", index),
                    path=target_path,
                    conflict_on_failure=True,
                )
            )
        hashes = _hash_file_batch(
            root=self.content_root,
            requests=requests,
            context="验证已提交写事务目标",
        )
        for index, entry in enumerate(self.entries):
            if hashes[_hash_request_id("target", index)].sha256 != entry.staged_sha256:
                target_path = self._resolve_relative_path(entry.target_relative_path)
                raise FileWriteRecoveryConflictError(f"已提交写事务的目标文件哈希不匹配: {target_path}")

    def _verify_rolled_back_targets(self) -> None:
        requests: list[_FileHashRequest] = []
        for index, entry in enumerate(self.entries):
            target_path = self._resolve_relative_path(entry.target_relative_path)
            target_kind = _file_path_kind(target_path)
            if entry.existed_before:
                if target_kind != "regular" or entry.original_sha256 is None:
                    raise FileWriteRecoveryConflictError(f"回滚后原文件缺失: {target_path}")
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("target", index),
                        path=target_path,
                        conflict_on_failure=True,
                    )
                )
            elif target_kind != "missing":
                raise FileWriteRecoveryConflictError(f"回滚后本应不存在的文件仍存在: {target_path}")
        hashes = _hash_file_batch(
            root=self.content_root,
            requests=requests,
            context="验证写事务回滚结果",
        )
        for index, entry in enumerate(self.entries):
            if entry.existed_before and hashes[_hash_request_id("target", index)].sha256 != entry.original_sha256:
                target_path = self._resolve_relative_path(entry.target_relative_path)
                raise FileWriteRecoveryConflictError(f"回滚后原文件哈希不匹配: {target_path}")

    def _hash_and_validate_rollback_sources(self) -> dict[str, str | None]:
        """一次批量确认回滚所需当前目标与备份，并返回本阶段快照。"""
        requests: list[_FileHashRequest] = []
        target_kinds: dict[str, _FilePathKind] = {}
        backup_kinds: dict[str, _FilePathKind] = {}
        for index, entry in enumerate(self.entries):
            target_path = self._resolve_relative_path(entry.target_relative_path)
            target_kind = _file_path_kind(target_path)
            target_kinds[entry.target_relative_path] = target_kind
            if target_kind == "regular":
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("target", index),
                        path=target_path,
                        conflict_on_failure=True,
                    )
                )
            elif target_kind != "missing":
                raise FileWriteRecoveryConflictError(f"待恢复目标不是普通文件: {target_path}")

            if not entry.existed_before:
                continue
            if entry.original_sha256 is None or entry.backup_relative_path is None:
                raise FileWriteTransactionError("已存在目标缺少恢复信息")
            backup_path = self._resolve_relative_path(entry.backup_relative_path)
            backup_kind = _file_path_kind(backup_path)
            backup_kinds[entry.target_relative_path] = backup_kind
            if backup_kind == "regular":
                requests.append(
                    _FileHashRequest(
                        id=_hash_request_id("backup", index),
                        path=backup_path,
                        conflict_on_failure=False,
                    )
                )
            elif backup_kind != "missing":
                raise FileWriteTransactionError(f"原文件备份不是普通文件: {backup_path}")

        hashes = _hash_file_batch(
            root=self.content_root,
            requests=requests,
            context="验证写事务回滚来源",
        )
        current_hashes: dict[str, str | None] = {}
        for index, entry in enumerate(self.entries):
            target_hash = (
                hashes[_hash_request_id("target", index)].sha256
                if target_kinds[entry.target_relative_path] == "regular"
                else None
            )
            current_hashes[entry.target_relative_path] = target_hash
            if entry.existed_before:
                assert entry.original_sha256 is not None
                if target_hash not in {entry.original_sha256, entry.staged_sha256, None}:
                    target_path = self._resolve_relative_path(entry.target_relative_path)
                    raise FileWriteRecoveryConflictError(f"待恢复目标已被其他操作修改: {target_path}")
                backup_hash = (
                    hashes[_hash_request_id("backup", index)].sha256
                    if backup_kinds[entry.target_relative_path] == "regular"
                    else None
                )
                if backup_hash is not None and backup_hash != entry.original_sha256:
                    backup_path = self._resolve_required_backup_path(entry)
                    raise FileWriteTransactionError(f"原文件备份缺失或损坏: {backup_path}")
                if target_hash != entry.original_sha256 and backup_hash != entry.original_sha256:
                    backup_path = self._resolve_required_backup_path(entry)
                    raise FileWriteTransactionError(f"原文件备份缺失或损坏: {backup_path}")
            elif target_hash is not None and (entry.staged_sha256 is None or target_hash != entry.staged_sha256):
                target_path = self._resolve_relative_path(entry.target_relative_path)
                raise FileWriteRecoveryConflictError(f"待删除的事务新文件已被其他操作修改: {target_path}")
        return current_hashes

    def _restore_entry(
        self,
        entry: FileWriteJournalEntry,
        *,
        current_hash: str | None,
    ) -> bool:
        target_path = self._resolve_relative_path(entry.target_relative_path)
        if entry.existed_before:
            if entry.original_sha256 is None or entry.backup_relative_path is None:
                raise FileWriteTransactionError("已存在目标缺少恢复信息")
            if current_hash == entry.original_sha256:
                return False
            if entry.staged_sha256 is None or current_hash not in {entry.staged_sha256, None}:
                raise FileWriteRecoveryConflictError(f"待恢复目标已被其他操作修改: {target_path}")
            backup_path = self._resolve_relative_path(entry.backup_relative_path)
            if _file_path_kind(backup_path) != "regular":
                raise FileWriteTransactionError(f"原文件备份缺失或损坏: {backup_path}")
            restore_path = _artifact_path(target_path, self.transaction_id, "restore")
            _copy_file_durably(source_path=backup_path, target_path=restore_path)
            _replace_atomically_with_windows_retry(
                source_path=restore_path,
                target_path=target_path,
                before_retry=lambda: self._assert_rollback_target_unchanged_before_retry(
                    target_path=target_path,
                    expected_current_hash=current_hash,
                ),
            )
            _fsync_directory(target_path.parent)
            if _file_path_kind(target_path) != "regular":
                raise FileWriteTransactionError(f"原文件恢复后哈希不匹配: {target_path}")
            restored_hash = _hash_file_batch(
                root=self.content_root,
                requests=[
                    _FileHashRequest(
                        id="restored_target",
                        path=target_path,
                        conflict_on_failure=False,
                    )
                ],
                context="验证单个回滚目标",
            )["restored_target"].sha256
            if restored_hash != entry.original_sha256:
                raise FileWriteTransactionError(f"原文件恢复后哈希不匹配: {target_path}")
            return True

        if current_hash is None:
            return False
        if entry.staged_sha256 is None or current_hash != entry.staged_sha256:
            raise FileWriteRecoveryConflictError(f"待删除的事务新文件已被其他操作修改: {target_path}")
        target_kind = _file_path_kind(target_path)
        if target_kind == "missing":
            return False
        if target_kind != "regular":
            raise FileWriteRecoveryConflictError(f"待删除的事务新文件不是普通文件: {target_path}")
        target_path.unlink()
        _fsync_directory(target_path.parent)
        return True

    def _assert_rollback_target_unchanged_before_retry(
        self,
        *,
        target_path: Path,
        expected_current_hash: str | None,
    ) -> None:
        """回滚替换重试前确认目标仍是本事务的新内容或仍不存在。"""
        target_kind = _file_path_kind(target_path)
        if expected_current_hash is None:
            if target_kind != "missing":
                raise FileWriteRecoveryConflictError(f"待恢复目标在重试前已被其他操作创建: {target_path}")
            return
        if target_kind != "regular":
            raise FileWriteRecoveryConflictError(f"待恢复目标在重试前已被其他操作修改: {target_path}")
        current_hash = _hash_file_batch(
            root=self.content_root,
            requests=[
                _FileHashRequest(
                    id="rollback_retry_target",
                    path=target_path,
                    conflict_on_failure=True,
                )
            ],
            context="验证 Windows 回滚替换重试目标",
        )["rollback_retry_target"].sha256
        if current_hash != expected_current_hash:
            raise FileWriteRecoveryConflictError(f"待恢复目标在重试前已被其他操作修改: {target_path}")

    def _cleanup_uncommitted_artifacts(self) -> None:
        self._cleanup_artifacts(remove_journal=True)

    def _cleanup_artifacts(self, *, remove_journal: bool) -> None:
        for entry in self.entries:
            for relative_path in (entry.staged_relative_path, entry.backup_relative_path):
                if relative_path is None:
                    continue
                artifact_path = self._resolve_relative_path(relative_path)
                artifact_path.unlink(missing_ok=True)
            target_path = self._resolve_relative_path(entry.target_relative_path)
            restore_path = _artifact_path(target_path, self.transaction_id, "restore")
            restore_path.unlink(missing_ok=True)
        if remove_journal:
            self.journal_path.unlink(missing_ok=True)
            _fsync_directory(self.journal_path.parent)
            try:
                self.journal_path.parent.rmdir()
            except OSError:
                pass


def _normalize_writes(*, writes: list[PlannedFileWrite], content_root: Path) -> list[PlannedFileWrite]:
    normalized: list[PlannedFileWrite] = []
    seen_targets: set[Path] = set()
    for index, write in enumerate(writes):
        if (write.content is None) == (write.source_path is None):
            raise ValueError(f"写事务第 {index} 个文件必须且只能包含 content 或 source_path")
        target_path = _resolve_target_path(content_root=content_root, target_path=write.target_path)
        if target_path in seen_targets:
            raise ValueError(f"写事务包含重复目标文件: {target_path}")
        seen_targets.add(target_path)
        if target_path.is_symlink():
            raise FileWriteTransactionError(f"写事务不允许替换符号链接: {target_path}")
        source_path = write.source_path
        if source_path is not None:
            resolved_source = source_path.resolve(strict=True)
            if not resolved_source.is_file():
                raise FileNotFoundError(f"写事务源文件不是普通文件: {resolved_source}")
            source_path = resolved_source
        normalized.append(
            PlannedFileWrite(
                target_path=target_path,
                content=write.content,
                source_path=source_path,
            )
        )
    return normalized


def _journal_entry_from_manifest(
    *,
    entry: FileWriteManifestEntry,
    content_root: Path,
    transaction_id: str,
) -> FileWriteJournalEntry:
    """把数据库恢复清单收窄为文件事务条目并重新校验路径。"""
    for field_name, value in (
        ("target_relative_path", entry.target_relative_path),
        ("staged_relative_path", entry.staged_relative_path),
    ):
        if not value:
            raise FileWriteTransactionError(f"数据库写事务清单 {field_name} 为空")
    if not _is_sha256(entry.target_sha256):
        raise FileWriteTransactionError("数据库写事务清单包含非法目标 SHA-256")
    if entry.existed_before:
        if entry.backup_relative_path is None or entry.original_sha256 is None:
            raise FileWriteTransactionError("数据库写事务清单中的原文件缺少备份或哈希")
        if not _is_sha256(entry.original_sha256):
            raise FileWriteTransactionError("数据库写事务清单包含非法原文件 SHA-256")
    elif entry.backup_relative_path is not None or entry.original_sha256 is not None:
        raise FileWriteTransactionError("数据库写事务清单中的新文件不得带原文件信息")

    target_path = _resolve_journal_relative_path(
        content_root=content_root,
        relative_path=entry.target_relative_path,
    )
    staged_path = _resolve_journal_relative_path(
        content_root=content_root,
        relative_path=entry.staged_relative_path,
    )
    if staged_path != _artifact_path(target_path, transaction_id, "stage"):
        raise FileWriteTransactionError("数据库写事务清单的暂存路径与事务标识不匹配")
    if entry.backup_relative_path is not None:
        backup_path = _resolve_journal_relative_path(
            content_root=content_root,
            relative_path=entry.backup_relative_path,
        )
        if backup_path != _artifact_path(target_path, transaction_id, "backup"):
            raise FileWriteTransactionError("数据库写事务清单的备份路径与事务标识不匹配")
    return FileWriteJournalEntry(
        target_relative_path=entry.target_relative_path,
        staged_relative_path=entry.staged_relative_path,
        backup_relative_path=entry.backup_relative_path,
        existed_before=entry.existed_before,
        original_sha256=entry.original_sha256,
        staged_sha256=entry.target_sha256,
        replaced=False,
    )


def _clone_runtime_inputs(
    *,
    game_path: Path,
    content_root: Path,
    staged_game_path: Path,
    staged_content_root: Path,
) -> None:
    """硬链接未变的运行输入，仅在文件系统不支持时复制。"""
    for directory_name in ("data", "js", "fonts"):
        source_directory = content_root / directory_name
        if not source_directory.exists():
            continue
        _clone_tree_with_hardlinks(
            source_directory=source_directory,
            target_directory=staged_content_root / directory_name,
        )

    package_candidates = {
        game_path / "package.json": staged_game_path / "package.json",
        content_root / "package.json": staged_content_root / "package.json",
    }
    for source_path, target_path in package_candidates.items():
        if not source_path.is_file() or target_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _link_or_copy_file(source_path=source_path, target_path=target_path)


def _clone_tree_with_hardlinks(*, source_directory: Path, target_directory: Path) -> None:
    if source_directory.is_symlink() or not source_directory.is_dir():
        raise FileWriteTransactionError(f"暂存运行视图不允许复用符号链接目录: {source_directory}")
    target_directory.mkdir(parents=True, exist_ok=True)
    for source_path in source_directory.iterdir():
        if source_path.is_symlink():
            raise FileWriteTransactionError(f"暂存运行视图不允许复用符号链接: {source_path}")
        target_path = target_directory / source_path.name
        if source_path.is_dir():
            _clone_tree_with_hardlinks(
                source_directory=source_path,
                target_directory=target_path,
            )
        elif source_path.is_file():
            _link_or_copy_file(source_path=source_path, target_path=target_path)


def _link_or_copy_file(*, source_path: Path, target_path: Path) -> None:
    try:
        os.link(source_path, target_path)
    except OSError:
        _ = shutil.copyfile(source_path, target_path)


def new_file_write_transaction_id() -> str:
    """生成可同时用于数据库与文件名的事务标识。"""
    return uuid.uuid4().hex


def file_write_transaction_journal_path(*, content_root: Path, transaction_id: str) -> Path:
    """在文件落盘前计算稳定 journal 路径，供数据库预先记录。"""
    _validate_transaction_id(transaction_id)
    return content_root.resolve() / JOURNAL_DIRECTORY_NAME / f"{transaction_id}.json"


def _build_initial_entries(
    *,
    content_root: Path,
    transaction_id: str,
    writes: Sequence[PlannedFileWrite],
) -> list[FileWriteJournalEntry]:
    """先读取全部目标路径事实，再用一次 native 批量记录已有目标哈希。"""
    target_kinds: list[_FilePathKind] = []
    requests: list[_FileHashRequest] = []
    for index, write in enumerate(writes):
        target_path = write.target_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_kind = _file_path_kind(target_path)
        if target_kind not in {"missing", "regular"}:
            raise FileWriteTransactionError(f"写事务目标不是普通文件: {target_path}")
        target_kinds.append(target_kind)
        if target_kind == "regular":
            requests.append(
                _FileHashRequest(
                    id=_hash_request_id("target", index),
                    path=target_path,
                    conflict_on_failure=True,
                )
            )
    hashes = _hash_file_batch(
        root=content_root,
        requests=requests,
        context="记录写事务原目标",
    )

    entries: list[FileWriteJournalEntry] = []
    for index, (write, target_kind) in enumerate(zip(writes, target_kinds, strict=True)):
        target_path = write.target_path
        existed_before = target_kind == "regular"
        original_sha256 = hashes[_hash_request_id("target", index)].sha256 if existed_before else None
        staged_path = _artifact_path(target_path, transaction_id, "stage")
        backup_path = _artifact_path(target_path, transaction_id, "backup") if existed_before else None
        entries.append(
            FileWriteJournalEntry(
                target_relative_path=_relative_path_text(content_root, target_path),
                staged_relative_path=_relative_path_text(content_root, staged_path),
                backup_relative_path=(
                    _relative_path_text(content_root, backup_path) if backup_path is not None else None
                ),
                existed_before=existed_before,
                original_sha256=original_sha256,
                staged_sha256=None,
            )
        )
    return entries


def _prepare_entry(
    *,
    content_root: Path,
    write: PlannedFileWrite,
    entry: FileWriteJournalEntry,
) -> None:
    staged_path = _resolve_journal_relative_path(
        content_root=content_root,
        relative_path=entry.staged_relative_path,
    )
    if write.content is not None:
        _write_bytes_durably(target_path=staged_path, content=write.content)
    elif write.source_path is not None:
        _copy_file_durably(source_path=write.source_path, target_path=staged_path)
    else:
        raise AssertionError("已校验写操作缺少内容")

    if entry.existed_before:
        if entry.backup_relative_path is None or entry.original_sha256 is None:
            raise FileWriteTransactionError("已存在目标缺少备份信息")
        target_path = _resolve_journal_relative_path(
            content_root=content_root,
            relative_path=entry.target_relative_path,
        )
        if _file_path_kind(target_path) != "regular":
            raise FileWriteRecoveryConflictError(f"目标文件在备份前已消失: {target_path}")
        backup_path = _resolve_journal_relative_path(
            content_root=content_root,
            relative_path=entry.backup_relative_path,
        )
        _copy_file_durably(source_path=target_path, target_path=backup_path)


def _resolve_target_path(*, content_root: Path, target_path: Path) -> Path:
    if not target_path.is_absolute():
        target_path = content_root / target_path
    if target_path.is_symlink():
        raise FileWriteTransactionError(f"写事务不允许替换符号链接: {target_path}")
    resolved_target = target_path.resolve(strict=False)
    try:
        _ = resolved_target.relative_to(content_root)
    except ValueError as error:
        raise FileWriteTransactionError(f"写事务目标越出游戏内容目录: {resolved_target}") from error
    return resolved_target


def _resolve_journal_relative_path(*, content_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise FileWriteTransactionError(f"写事务 journal 包含非法路径: {relative_path}")
    target_path = content_root.joinpath(relative)
    resolved_parent = target_path.parent.resolve(strict=True)
    resolved_target = resolved_parent / target_path.name
    try:
        _ = resolved_target.relative_to(content_root)
    except ValueError as error:
        raise FileWriteTransactionError(f"写事务 journal 路径越出内容目录: {relative_path}") from error
    return resolved_target


def _relative_path_text(content_root: Path, target_path: Path) -> str:
    return target_path.relative_to(content_root).as_posix()


def _artifact_path(target_path: Path, transaction_id: str, kind: str) -> Path:
    return target_path.with_name(f".{target_path.name}{ARTIFACT_MARKER}{transaction_id}.{kind}")


def _file_path_kind(path: Path) -> _FilePathKind:
    """用 lstat 获取不跟随最终链接的路径事实。"""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as error:
        raise FileWriteTransactionError(f"无法读取写事务路径状态: {path}") from error
    if stat.S_ISLNK(path_stat.st_mode) or path.is_junction():
        return "link"
    if stat.S_ISREG(path_stat.st_mode):
        return "regular"
    return "other"


def _hash_request_id(role: str, index: int) -> str:
    return f"{role}_{index:06d}"


def _hash_file_batch(
    *,
    root: Path,
    requests: Sequence[_FileHashRequest],
    context: str,
) -> dict[str, NativeFileHashResult]:
    """执行一个阶段内的唯一 native 哈希批次，并映射稳定事务错误。"""
    if not requests:
        return {}
    request_ids = [request.id for request in requests]
    if len(request_ids) != len(set(request_ids)):
        raise FileWriteTransactionError(f"{context}包含重复的文件哈希请求标识")
    inputs = [
        NativeFileHashInput(
            id=request.id,
            relative_path=_relative_path_text(root, request.path),
        )
        for request in requests
    ]
    try:
        results = hash_native_files(root=root, files=inputs)
    except NativeRuntimeError as error:
        error_type: type[FileWriteTransactionError] = (
            FileWriteRecoveryConflictError
            if _native_hash_error_is_conflict(
                error=error,
                root=root,
                requests=requests,
            )
            else FileWriteTransactionError
        )
        raise error_type(
            f"{context}的原生文件哈希失败 [{error.code}]: {error}",
            code=error.code,
            details=error.details,
        ) from error
    except Exception as error:
        raise FileWriteTransactionError(f"{context}的原生文件哈希失败: {error}") from error
    return {result.id: result for result in results}


def _native_hash_error_is_conflict(
    *,
    error: NativeRuntimeError,
    root: Path,
    requests: Sequence[_FileHashRequest],
) -> bool:
    """按 typed error 绑定的 id/路径判断失败是否属于外部目标冲突。"""
    error_id = error.details.get("id")
    if isinstance(error_id, str):
        for request in requests:
            if request.id == error_id:
                return request.conflict_on_failure
    error_relative_path = error.details.get("relative_path")
    if isinstance(error_relative_path, str):
        for request in requests:
            if _relative_path_text(root, request.path) == error_relative_path:
                return request.conflict_on_failure
    return bool(requests) and all(request.conflict_on_failure for request in requests)


def _copy_file_durably(*, source_path: Path, target_path: Path) -> None:
    if target_path.exists():
        raise FileExistsError(f"写事务产物已存在: {target_path}")
    with source_path.open("rb") as source, target_path.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    _fsync_directory(target_path.parent)


def _write_bytes_durably(*, target_path: Path, content: bytes) -> None:
    if target_path.exists():
        raise FileExistsError(f"写事务产物已存在: {target_path}")
    with target_path.open("xb") as target:
        _ = target.write(content)
        target.flush()
        os.fsync(target.fileno())
    _fsync_directory(target_path.parent)


def _replace_atomically_with_windows_retry(
    *,
    source_path: Path,
    target_path: Path,
    before_retry: Callable[[], None] | None = None,
) -> None:
    """仅为 Windows 短暂占用错误有界重试原子替换。"""
    retry_index = 0
    while True:
        try:
            os.replace(source_path, target_path)
            return
        except OSError as error:
            winerror = getattr(error, "winerror", None)
            if (
                os.name != "nt"
                or winerror not in _WINDOWS_REPLACE_RETRY_WINERRORS
                or retry_index >= len(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS)
            ):
                raise
            time.sleep(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS[retry_index])
            retry_index += 1
            if before_retry is not None:
                before_retry()


def _write_json_atomically(target_path: Path, payload: JsonObject) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=target_path.parent,
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)
        _ = temporary_file.write(encoded)
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
    try:
        # journal 是事务自身元数据，不套用游戏目标文件的内容事实校验。
        _replace_atomically_with_windows_retry(
            source_path=temporary_path,
            target_path=target_path,
        )
        _fsync_directory(target_path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Windows 不保证对目录句柄支持 fsync，文件本身仍已同步。
        pass
    finally:
        os.close(descriptor)


def _parse_journal_entry(
    payload: JsonObject,
    *,
    content_root: Path,
    transaction_id: str,
) -> FileWriteJournalEntry:
    expected_entry_keys = {
        "target_relative_path",
        "staged_relative_path",
        "backup_relative_path",
        "existed_before",
        "original_sha256",
        "staged_sha256",
        "replaced",
    }
    if set(payload) != expected_entry_keys:
        raise FileWriteTransactionError(f"写事务 journal 文件条目字段必须严格为 {sorted(expected_entry_keys)}")
    target_relative_path = _read_json_str(payload, "target_relative_path")
    staged_relative_path = _read_json_str(payload, "staged_relative_path")
    backup_value = payload.get("backup_relative_path")
    if backup_value is not None and not isinstance(backup_value, str):
        raise FileWriteTransactionError("write_transaction_journal.backup_relative_path 必须是字符串或空值")
    existed_before = payload.get("existed_before")
    replaced_value = payload.get("replaced")
    if not isinstance(existed_before, bool) or not isinstance(replaced_value, bool):
        raise FileWriteTransactionError("write_transaction_journal 文件布尔状态无效")
    original_value = payload.get("original_sha256")
    if original_value is not None and not isinstance(original_value, str):
        raise FileWriteTransactionError("write_transaction_journal.original_sha256 必须是字符串或空值")
    staged_value = payload.get("staged_sha256")
    if staged_value is not None and not isinstance(staged_value, str):
        raise FileWriteTransactionError("write_transaction_journal.staged_sha256 必须是字符串或空值")
    for digest in (original_value, staged_value):
        if digest is not None and not _is_sha256(digest):
            raise FileWriteTransactionError("写事务 journal 包含非法 SHA-256")

    target_path = _resolve_journal_relative_path(content_root=content_root, relative_path=target_relative_path)
    staged_path = _resolve_journal_relative_path(content_root=content_root, relative_path=staged_relative_path)
    expected_staged_path = _artifact_path(target_path, transaction_id, "stage")
    if staged_path != expected_staged_path:
        raise FileWriteTransactionError("写事务 journal 的暂存路径与事务标识不匹配")
    if backup_value is not None:
        backup_path = _resolve_journal_relative_path(content_root=content_root, relative_path=backup_value)
        if backup_path != _artifact_path(target_path, transaction_id, "backup"):
            raise FileWriteTransactionError("写事务 journal 的备份路径与事务标识不匹配")
    if existed_before != (backup_value is not None and original_value is not None):
        raise FileWriteTransactionError("写事务 journal 的原文件与备份状态不一致")
    return FileWriteJournalEntry(
        target_relative_path=target_relative_path,
        staged_relative_path=staged_relative_path,
        backup_relative_path=backup_value,
        existed_before=existed_before,
        original_sha256=original_value,
        staged_sha256=staged_value,
        replaced=replaced_value,
    )


def _read_json_str(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise FileWriteTransactionError(f"write_transaction_journal.{key} 必须是非空字符串")
    return value


def _parse_state(value: str) -> FileWriteTransactionState:
    if value in {
        "preparing",
        "prepared",
        "replacing",
        "verifying",
        "committed",
        "rolling_back",
        "rolled_back",
        "recovery_failed",
    }:
        return cast(FileWriteTransactionState, value)
    raise FileWriteTransactionError(f"未知写事务状态: {value}")


def _validate_transaction_id(value: str) -> None:
    if not value or len(value) > 64 or not all(character.isascii() and character.isalnum() for character in value):
        raise ValueError("写事务标识只能包含 1-64 个 ASCII 字母或数字")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _timestamp_text() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "DurableFileWriteTransaction",
    "FileWriteManifestEntry",
    "FileWriteRecoveryConflictError",
    "FileWriteRecoverySummary",
    "FileWriteTransactionError",
    "FileWriteTransactionState",
    "JOURNAL_DIRECTORY_NAME",
    "PlannedFileWrite",
    "file_write_transaction_journal_path",
    "new_file_write_transaction_id",
]
