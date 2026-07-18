"""可崩溃恢复文件写事务测试。"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch

from app.application.write_transaction import (
    JOURNAL_DIRECTORY_NAME,
    DurableFileWriteTransaction,
    FileWriteRecoveryConflictError,
    FileWriteTransactionError,
    PlannedFileWrite,
)
from app.native_file_hashing import NativeFileHashInput, NativeFileHashResult
from app.native_runtime import JsonObject, NativeRuntimeError
from app.rmmz.text_rules import coerce_json_value, ensure_json_array, ensure_json_object


class RecordingNativeFileHasher:
    """记录事务层每次 native 批次，并以测试内哈希返回确定结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        *,
        root: Path,
        files: Sequence[NativeFileHashInput],
    ) -> list[NativeFileHashResult]:
        self.calls.append(tuple(item.relative_path for item in files))
        results: list[NativeFileHashResult] = []
        for item in files:
            content = (root / item.relative_path).read_bytes()
            results.append(
                NativeFileHashResult(
                    id=item.id,
                    relative_path=item.relative_path,
                    sha256=sha256(content).hexdigest(),
                    byte_size=len(content),
                )
            )
        return results


def _windows_replace_error(winerror: int) -> OSError:
    """构造带 Windows 原生错误码的替换失败。"""
    return OSError(None, "注入 Windows 原子替换失败", None, winerror)


def _disable_retry_delay(monkeypatch: MonkeyPatch) -> None:
    """故障注入测试不等待真实退避时间。"""

    def no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr("app.application.write_transaction.time.sleep", no_sleep)


def test_file_write_transaction_uses_one_native_batch_per_validation_stage(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """各事务阶段独立批量哈希，只有逐项原子替换和恢复使用单项验证。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    existing_path = content_root / "existing.json"
    new_path = content_root / "new.json"
    _ = existing_path.write_bytes(b"old")
    hasher = RecordingNativeFileHasher()
    monkeypatch.setattr("app.application.write_transaction.hash_native_files", hasher)

    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txbatches",
        writes=[
            PlannedFileWrite(target_path=existing_path, content=b"new-existing"),
            PlannedFileWrite(target_path=new_path, content=b"new-file"),
        ],
    )

    assert [len(call) for call in hasher.calls] == [1, 4]
    assert hasher.calls[0] == ("existing.json",)
    assert hasher.calls[1] == (
        ".existing.json.att-mz-write-txbatches.stage",
        ".existing.json.att-mz-write-txbatches.backup",
        "existing.json",
        ".new.json.att-mz-write-txbatches.stage",
    )

    hasher.calls.clear()
    _ = transaction.export_manifest()
    assert [len(call) for call in hasher.calls] == [4]

    hasher.calls.clear()
    transaction.replace_targets()
    assert [len(call) for call in hasher.calls] == [4, 1, 1, 2]

    hasher.calls.clear()
    transaction.verify_replaced_targets()
    assert [len(call) for call in hasher.calls] == [2]

    hasher.calls.clear()
    _ = transaction.rollback()
    assert [len(call) for call in hasher.calls] == [3, 1, 1]

    hasher.calls.clear()
    transaction.finalize_rolled_back_cleanup()
    assert [len(call) for call in hasher.calls] == [1]


def test_committed_cleanup_rehashes_targets_in_its_own_native_batch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """数据库提交后的清理不能复用替换阶段哈希。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "System.json"
    _ = target_path.write_bytes(b"old")
    hasher = RecordingNativeFileHasher()
    monkeypatch.setattr("app.application.write_transaction.hash_native_files", hasher)
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txcommitbatch",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    transaction.replace_targets()

    hasher.calls.clear()
    transaction.mark_committed_and_cleanup()

    assert [len(call) for call in hasher.calls] == [1]
    assert hasher.calls[0] == ("System.json",)
    assert target_path.read_bytes() == b"new"
    assert not transaction.journal_path.exists()


@pytest.mark.parametrize(
    ("target_exists", "expected_error_type"),
    [
        (True, FileWriteRecoveryConflictError),
        (False, FileWriteTransactionError),
    ],
)
def test_native_hash_error_preserves_code_and_details(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    target_exists: bool,
    expected_error_type: type[FileWriteTransactionError],
) -> None:
    """native typed error 按目标冲突或事务错误分类，并原样保留机器字段。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "target.json"
    if target_exists:
        _ = target_path.write_bytes(b"old")
    expected_details: JsonObject = {}

    def fail_hash_batch(
        *,
        root: Path,
        files: Sequence[NativeFileHashInput],
    ) -> list[NativeFileHashResult]:
        _ = root
        first = files[0]
        expected_details.update(
            {
                "id": first.id,
                "relative_path": first.relative_path,
                "os_error": 32,
            }
        )
        raise NativeRuntimeError(
            code="hash_files_open_failed",
            stage="hash.files",
            message="文件被占用",
            retryable=False,
            details=expected_details,
        )

    monkeypatch.setattr("app.application.write_transaction.hash_native_files", fail_hash_batch)

    with pytest.raises(FileWriteTransactionError) as captured:
        _ = DurableFileWriteTransaction.prepare(
            mode="write_back",
            content_root=content_root,
            transaction_id=f"txnativeerror{int(target_exists)}",
            writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
        )

    assert type(captured.value) is expected_error_type
    assert captured.value.code == "hash_files_open_failed"
    assert captured.value.details == expected_details


def test_mixed_native_batch_maps_target_error_to_conflict(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """stage/backup/target 混合批次按 typed error 的目标 id 分类冲突。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "Actors.json"
    _ = target_path.write_bytes(b"old")
    hasher = RecordingNativeFileHasher()
    monkeypatch.setattr("app.application.write_transaction.hash_native_files", hasher)
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txmixednativeerror",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    expected_details: JsonObject = {}

    def reject_target_in_mixed_batch(
        *,
        root: Path,
        files: Sequence[NativeFileHashInput],
    ) -> list[NativeFileHashResult]:
        _ = root
        target_input = next(item for item in files if item.id.startswith("target_"))
        expected_details.update(
            {
                "id": target_input.id,
                "relative_path": target_input.relative_path,
            }
        )
        raise NativeRuntimeError(
            code="hash_files_path_changed",
            stage="hash.files",
            message="目标在扫描时变化",
            retryable=False,
            details=expected_details,
        )

    monkeypatch.setattr(
        "app.application.write_transaction.hash_native_files",
        reject_target_in_mixed_batch,
    )

    with pytest.raises(FileWriteRecoveryConflictError) as captured:
        transaction.replace_targets()

    assert captured.value.code == "hash_files_path_changed"
    assert captured.value.details == expected_details
    assert target_path.read_bytes() == b"old"
    assert transaction.state == "prepared"


def test_file_write_transaction_prepares_before_replacing_and_can_roll_back(tmp_path: Path) -> None:
    """全部暂存与备份校验完成前不修改目标，回滚后恢复原状。"""
    content_root = tmp_path / "game"
    _ = content_root.mkdir()
    existing_path = content_root / "data" / "Actors.json"
    _ = existing_path.parent.mkdir()
    _ = existing_path.write_bytes(b"old actors")
    new_path = content_root / "fonts" / "replacement.ttf"

    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txprepare",
        writes=[
            PlannedFileWrite(target_path=existing_path, content=b"new actors"),
            PlannedFileWrite(target_path=new_path, content=b"font bytes"),
        ],
    )

    assert transaction.state == "prepared"
    assert existing_path.read_bytes() == b"old actors"
    assert not new_path.exists()
    assert transaction.journal_path.is_file()
    assert all(
        (content_root / entry.staged_relative_path).parent == (content_root / entry.target_relative_path).parent
        for entry in transaction.entries
    )
    assert all(
        entry.backup_relative_path is None
        or (content_root / entry.backup_relative_path).parent == (content_root / entry.target_relative_path).parent
        for entry in transaction.entries
    )

    transaction.replace_targets()
    assert existing_path.read_bytes() == b"new actors"
    assert new_path.read_bytes() == b"font bytes"

    summary = transaction.rollback()
    assert summary.restored_file_count == 2
    assert existing_path.read_bytes() == b"old actors"
    assert not new_path.exists()
    transaction.finalize_rolled_back_cleanup()
    assert not transaction.journal_path.exists()


def test_file_write_transaction_failure_after_first_replace_restores_every_target(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """第二个目标替换失败时，第一个目标也必须恢复。"""
    content_root = tmp_path / "game"
    _ = content_root.mkdir()
    first_path = content_root / "first.json"
    second_path = content_root / "second.json"
    _ = first_path.write_bytes(b"first old")
    _ = second_path.write_bytes(b"second old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="rebuild_active_runtime",
        content_root=content_root,
        transaction_id="txfailure",
        writes=[
            PlannedFileWrite(target_path=first_path, content=b"first new"),
            PlannedFileWrite(target_path=second_path, content=b"second new"),
        ],
    )
    real_replace = os.replace

    def fail_second_target(source: Path, target: Path) -> None:
        if Path(target) == second_path:
            raise OSError("注入第二个目标替换失败")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_target)

    with pytest.raises(OSError, match="第二个"):
        transaction.replace_targets()

    assert transaction.state == "rolled_back"
    assert first_path.read_bytes() == b"first old"
    assert second_path.read_bytes() == b"second old"
    transaction.finalize_rolled_back_cleanup()


@pytest.mark.parametrize(
    ("winerror", "failure_count"),
    [
        (5, 1),
        (32, 2),
        (33, 1),
    ],
)
@pytest.mark.skipif(os.name != "nt", reason="Windows 原子替换错误码重试仅适用于 Windows")
def test_windows_transient_replace_error_retries_then_succeeds(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    winerror: int,
    failure_count: int,
) -> None:
    """仅 Windows 的占用类错误可在重新核对原文件后有界重试。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "Actors.json"
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id=f"txretry{winerror}",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    real_replace = os.replace
    target_attempt_count = 0

    def fail_transiently(source: Path, target: Path) -> None:
        nonlocal target_attempt_count
        if Path(target) == target_path:
            target_attempt_count += 1
            if target_attempt_count <= failure_count:
                raise _windows_replace_error(winerror)
        real_replace(source, target)

    _disable_retry_delay(monkeypatch)
    monkeypatch.setattr(os, "replace", fail_transiently)

    transaction.replace_targets()

    assert target_attempt_count == failure_count + 1
    assert target_path.read_bytes() == b"new"
    assert transaction.state == "verifying"
    transaction.mark_committed_and_cleanup()


@pytest.mark.parametrize(
    ("winerror", "expected_attempt_count"),
    [
        (5, 5),
        (6, 1),
    ],
)
@pytest.mark.skipif(os.name != "nt", reason="Windows 原子替换错误码重试仅适用于 Windows")
def test_windows_permanent_replace_error_fails_and_rolls_back(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    winerror: int,
    expected_attempt_count: int,
) -> None:
    """重试耗尽或非白名单错误均失败，并恢复此前已替换目标。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    first_path = content_root / "first.json"
    second_path = content_root / "second.json"
    _ = first_path.write_bytes(b"first old")
    _ = second_path.write_bytes(b"second old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id=f"txpermanent{winerror}",
        writes=[
            PlannedFileWrite(target_path=first_path, content=b"first new"),
            PlannedFileWrite(target_path=second_path, content=b"second new"),
        ],
    )
    real_replace = os.replace
    second_attempt_count = 0

    def fail_second_target(source: Path, target: Path) -> None:
        nonlocal second_attempt_count
        if Path(target) == second_path:
            second_attempt_count += 1
            raise _windows_replace_error(winerror)
        real_replace(source, target)

    _disable_retry_delay(monkeypatch)
    monkeypatch.setattr(os, "replace", fail_second_target)

    with pytest.raises(OSError) as captured:
        transaction.replace_targets()

    assert getattr(captured.value, "winerror", None) == winerror
    assert second_attempt_count == expected_attempt_count
    assert transaction.state == "rolled_back"
    assert first_path.read_bytes() == b"first old"
    assert second_path.read_bytes() == b"second old"
    transaction.finalize_rolled_back_cleanup()


@pytest.mark.skipif(os.name != "nt", reason="Windows 原子替换错误码重试仅适用于 Windows")
def test_windows_replace_retry_rejects_external_target_change(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """首次失败与重试之间的外部改动必须冲突失败，不能被事务覆盖。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "System.json"
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txretryconflict",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    real_replace = os.replace
    target_attempt_count = 0

    def change_target_then_fail(source: Path, target: Path) -> None:
        nonlocal target_attempt_count
        if Path(target) == target_path:
            target_attempt_count += 1
            if target_attempt_count == 1:
                _ = target_path.write_bytes(b"external")
                raise _windows_replace_error(5)
        real_replace(source, target)

    _disable_retry_delay(monkeypatch)
    monkeypatch.setattr(os, "replace", change_target_then_fail)

    with pytest.raises(FileWriteRecoveryConflictError, match="其他操作"):
        transaction.replace_targets()

    assert target_attempt_count == 1
    assert target_path.read_bytes() == b"external"
    assert transaction.state == "recovery_failed"


@pytest.mark.skipif(os.name != "nt", reason="Windows 原子替换错误码重试仅适用于 Windows")
def test_windows_transient_restore_error_retries_after_revalidation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """回滚恢复也复用有界替换重试，并在重试前核对事务新内容。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "Map001.json"
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txrestoreretry",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    transaction.replace_targets()
    real_replace = os.replace
    restore_attempt_count = 0

    def fail_first_restore(source: Path, target: Path) -> None:
        nonlocal restore_attempt_count
        if Path(target) == target_path and Path(source).name.endswith(".restore"):
            restore_attempt_count += 1
            if restore_attempt_count == 1:
                raise _windows_replace_error(32)
        real_replace(source, target)

    _disable_retry_delay(monkeypatch)
    monkeypatch.setattr(os, "replace", fail_first_restore)

    summary = transaction.rollback()

    assert restore_attempt_count == 2
    assert summary.state == "rolled_back"
    assert target_path.read_bytes() == b"old"
    transaction.finalize_rolled_back_cleanup()


@pytest.mark.skipif(os.name != "nt", reason="Windows 原子替换错误码重试仅适用于 Windows")
def test_windows_transient_journal_replace_error_uses_bounded_retry(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """journal 复用占用错误重试，但不借用游戏目标内容事实。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "System.json"
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txjournalretry",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    real_replace = os.replace
    injected_failure_count = 0

    def fail_first_journal_replaces(source: Path, target: Path) -> None:
        nonlocal injected_failure_count
        if Path(target) == transaction.journal_path and injected_failure_count < 2:
            injected_failure_count += 1
            raise _windows_replace_error(33)
        real_replace(source, target)

    _disable_retry_delay(monkeypatch)
    monkeypatch.setattr(os, "replace", fail_first_journal_replaces)

    transaction.replace_targets()

    assert injected_failure_count == 2
    assert transaction.state == "verifying"
    assert target_path.read_bytes() == b"new"
    transaction.mark_committed_and_cleanup()


def test_recover_uncommitted_transaction_handles_crash_between_replace_and_journal_update(tmp_path: Path) -> None:
    """journal 尚未标记 replaced 就崩溃时，依文件哈希判定并恢复。"""
    content_root = tmp_path / "game"
    _ = content_root.mkdir()
    target_path = content_root / "System.json"
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_terminology",
        content_root=content_root,
        transaction_id="txcrash",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    entry = transaction.entries[0]
    _ = os.replace(content_root / entry.staged_relative_path, target_path)
    assert target_path.read_bytes() == b"new"

    recovered = DurableFileWriteTransaction.load(
        journal_path=transaction.journal_path,
        content_root=content_root,
    )
    summary = recovered.recover(database_committed=False)

    assert summary.restored_file_count == 1
    assert target_path.read_bytes() == b"old"
    assert not transaction.journal_path.exists()


def test_recover_uncommitted_transaction_handles_crash_during_staging(tmp_path: Path) -> None:
    """部分文件尚未暂存完成时，恢复也只能清理产物而不能改动原文件。"""
    content_root = tmp_path / "game"
    _ = content_root.mkdir()
    first_path = content_root / "Actors.json"
    second_path = content_root / "System.json"
    _ = first_path.write_bytes(b"first old")
    _ = second_path.write_bytes(b"second old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txstaging",
        writes=[
            PlannedFileWrite(target_path=first_path, content=b"first new"),
            PlannedFileWrite(target_path=second_path, content=b"second new"),
        ],
    )
    payload = ensure_json_object(
        coerce_json_value(
            cast(
                object,
                json.loads(transaction.journal_path.read_text(encoding="utf-8")),
            )
        ),
        "write_transaction_journal",
    )
    entries = ensure_json_array(payload["entries"], "write_transaction_journal.entries")
    second_entry = ensure_json_object(entries[1], "write_transaction_journal.entries[1]")
    staged_relative_path = second_entry["staged_relative_path"]
    assert isinstance(staged_relative_path, str)
    second_stage_path = content_root / staged_relative_path
    second_stage_path.unlink()
    second_entry["staged_sha256"] = None
    payload["state"] = "preparing"
    _ = transaction.journal_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    recovered = DurableFileWriteTransaction.load(
        journal_path=transaction.journal_path,
        content_root=content_root,
    )
    summary = recovered.recover(database_committed=False)

    assert summary.restored_file_count == 0
    assert first_path.read_bytes() == b"first old"
    assert second_path.read_bytes() == b"second old"
    assert not transaction.journal_path.exists()


def test_recover_committed_transaction_keeps_new_files_and_only_cleans_artifacts(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """数据库已提交时，恢复命令只完成提交清理，不回滚新文件。"""
    content_root = tmp_path / "game"
    _ = content_root.mkdir()
    target_path = content_root / "Map001.json"
    _ = target_path.write_bytes(b"old")
    hasher = RecordingNativeFileHasher()
    monkeypatch.setattr("app.application.write_transaction.hash_native_files", hasher)
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txcommit",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    transaction.replace_targets()

    recovered = DurableFileWriteTransaction.load(
        journal_path=transaction.journal_path,
        content_root=content_root,
    )
    hasher.calls.clear()
    summary = recovered.recover(database_committed=True)

    assert [len(call) for call in hasher.calls] == [1]
    assert summary.finalized_committed_file_count == 1
    assert target_path.read_bytes() == b"new"
    assert not transaction.journal_path.exists()
    assert not list(content_root.rglob("*.backup"))


def test_transaction_rejects_target_changed_after_prepare(tmp_path: Path) -> None:
    """准备后被外部修改的目标不得被静默覆盖。"""
    content_root = tmp_path / "game"
    _ = content_root.mkdir()
    target_path = content_root / "Actors.json"
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txconflict",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    _ = target_path.write_bytes(b"bad")

    with pytest.raises(FileWriteRecoveryConflictError, match="其他操作"):
        transaction.replace_targets()

    assert target_path.read_bytes() == b"bad"
    assert transaction.state == "prepared"


def test_transaction_rejects_new_target_created_after_prepare(tmp_path: Path) -> None:
    """准备时不存在的新目标随后出现时，事务不得静默覆盖外部文件。"""
    content_root = tmp_path / "game"
    content_root.mkdir()
    target_path = content_root / "NewRuntimeMap.json"
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txnewconflict",
        writes=[PlannedFileWrite(target_path=target_path, content=b"transaction")],
    )
    _ = target_path.write_bytes(b"external")

    with pytest.raises(FileWriteRecoveryConflictError, match="本应新建"):
        transaction.replace_targets()

    assert target_path.read_bytes() == b"external"
    assert transaction.state == "prepared"


def test_transaction_rejects_target_changed_after_replace_before_database_commit(
    tmp_path: Path,
) -> None:
    """写后审计期间发生的外部修改不得进入数据库 committed 状态。"""
    content_root = tmp_path / "game"
    _ = content_root.mkdir()
    target_path = content_root / "Actors.json"
    _ = target_path.write_bytes(b"old")
    transaction = DurableFileWriteTransaction.prepare(
        mode="write_back",
        content_root=content_root,
        transaction_id="txpostaudit",
        writes=[PlannedFileWrite(target_path=target_path, content=b"new")],
    )
    transaction.replace_targets()
    _ = target_path.write_bytes(b"external")

    with pytest.raises(FileWriteRecoveryConflictError, match="哈希不匹配"):
        transaction.verify_replaced_targets()

    assert transaction.state == "verifying"


def test_load_rejects_journal_path_traversal(tmp_path: Path) -> None:
    """恢复边界不信任可编辑的 journal 路径。"""
    content_root = tmp_path / "game"
    journal_directory = content_root / JOURNAL_DIRECTORY_NAME
    _ = journal_directory.mkdir(parents=True)
    journal_path = journal_directory / "badjournal.json"
    _ = journal_path.write_text(
        json.dumps(
            {
                "version": 1,
                "transaction_id": "badjournal",
                "mode": "write_back",
                "content_root": str(content_root.resolve()),
                "state": "prepared",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "entries": [
                    {
                        "target_relative_path": "../outside.json",
                        "staged_relative_path": ".outside.att-mz-write-badjournal.stage",
                        "backup_relative_path": None,
                        "existed_before": False,
                        "original_sha256": None,
                        "staged_sha256": "0" * 64,
                        "replaced": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileWriteTransactionError, match="非法路径"):
        _ = DurableFileWriteTransaction.load(journal_path=journal_path, content_root=content_root)
