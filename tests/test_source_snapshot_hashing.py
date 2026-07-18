"""可信源快照的原生批量文件哈希边界测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app import native_file_hashing
from app.rmmz import source_snapshot
from app.rmmz.loader import resolve_game_layout


def test_native_file_hashing_reads_binary_and_unicode_paths_in_order(tmp_path: Path) -> None:
    """真实 native 批次必须返回请求顺序、实际字节数和标准 SHA-256。"""
    nested = tmp_path / "含 空格"
    nested.mkdir()
    second_content = "第二个文件".encode()
    first_content = b"first\x00binary"
    _ = (nested / "二.bin").write_bytes(second_content)
    _ = (tmp_path / "first.bin").write_bytes(first_content)
    inputs = [
        native_file_hashing.NativeFileHashInput(id="second", relative_path="含 空格/二.bin"),
        native_file_hashing.NativeFileHashInput(id="first", relative_path="first.bin"),
    ]

    results = native_file_hashing.hash_native_files(root=tmp_path, files=inputs)

    assert [result.id for result in results] == ["second", "first"]
    assert results[0].relative_path == "含 空格/二.bin"
    assert results[0].sha256 == hashlib.sha256(second_content).hexdigest()
    assert results[0].byte_size == len(second_content)
    assert results[1].sha256 == hashlib.sha256(first_content).hexdigest()
    assert results[1].byte_size == len(first_content)


def test_source_snapshot_collects_every_file_with_one_native_batch(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源快照不得逐文件调用或在 Python 中重新读取文件。"""
    layout = resolve_game_layout(minimal_game_dir)
    source_snapshot.create_source_snapshot_for_clean_game(layout)
    calls: list[tuple[Path, list[native_file_hashing.NativeFileHashInput]]] = []

    def fake_hash_native_files(
        *,
        root: Path,
        files: list[native_file_hashing.NativeFileHashInput],
    ) -> list[native_file_hashing.NativeFileHashResult]:
        calls.append((root, list(files)))
        return [
            native_file_hashing.NativeFileHashResult(
                id=item.id,
                relative_path=item.relative_path,
                sha256=f"{index:064x}",
                byte_size=1000 + index,
            )
            for index, item in enumerate(files)
        ]

    monkeypatch.setattr(source_snapshot, "hash_native_files", fake_hash_native_files)

    records = source_snapshot.collect_source_snapshot_records(layout=layout, updated_at="now")

    assert len(calls) == 1
    root, requested = calls[0]
    assert root == layout.content_root
    assert [item.id for item in requested] == [f"source_snapshot_{index:06d}" for index in range(len(requested))]
    assert [record.relative_path for record in records] == [item.relative_path for item in requested]
    assert [record.byte_size for record in records] == [1000 + index for index in range(len(requested))]
    assert all(record.updated_at == "now" for record in records)
    assert "data_origin/System.json" in {record.relative_path for record in records}
    assert "js/plugins_origin.js" in {record.relative_path for record in records}
    assert "js/plugins_source_origin/TestPlugin.js" in {record.relative_path for record in records}


def test_source_snapshot_propagates_native_hash_failure(
    minimal_game_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一 native 文件失败都必须阻止生成不完整 manifest。"""
    layout = resolve_game_layout(minimal_game_dir)
    source_snapshot.create_source_snapshot_for_clean_game(layout)

    def reject_hash_batch(
        *,
        root: Path,
        files: list[native_file_hashing.NativeFileHashInput],
    ) -> list[native_file_hashing.NativeFileHashResult]:
        del root, files
        raise RuntimeError("native hash failed")

    monkeypatch.setattr(source_snapshot, "hash_native_files", reject_hash_batch)

    with pytest.raises(RuntimeError, match="native hash failed"):
        _ = source_snapshot.collect_source_snapshot_records(layout=layout, updated_at="now")
