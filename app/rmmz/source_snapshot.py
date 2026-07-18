"""游戏可信源快照管理。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.native_file_hashing import NativeFileHashInput, hash_native_files
from app.rmmz.schema import GameLayout


@dataclass(frozen=True, slots=True)
class SourceSnapshotFileRecord:
    """单个可信源快照文件的数据库记录。"""

    relative_path: str
    sha256: str
    byte_size: int
    updated_at: str


def create_source_snapshot_for_clean_game(layout: GameLayout) -> None:
    """从干净游戏目录创建完整可信源快照。"""
    from .loader import validate_data_directory_integrity

    validate_clean_source_snapshot_target(layout)
    try:
        validate_data_directory_integrity(data_dir=layout.data_dir, role="激活数据目录")
        _ = shutil.copytree(layout.data_dir, layout.data_origin_dir)
        validate_data_directory_integrity(data_dir=layout.data_origin_dir, role="原始 data 备份")

        layout.plugins_origin_path.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(layout.plugins_path, layout.plugins_origin_path)
        active_plugin_source_dir = layout.js_dir / "plugins"
        layout.plugin_source_origin_dir.mkdir(parents=True, exist_ok=True)
        for source_path in _iter_direct_plugin_source_files(active_plugin_source_dir):
            snapshot_path = layout.plugin_source_origin_dir / source_path.name
            _ = shutil.copy2(source_path, snapshot_path)
        validate_source_snapshot_files(layout)
    except Exception:
        remove_source_snapshot_artifacts(layout)
        raise


def validate_clean_source_snapshot_target(layout: GameLayout) -> None:
    """确认游戏目录尚未存在任何可信源快照文件。"""
    existing_paths = [
        path
        for path in (
            layout.data_origin_dir,
            layout.plugins_origin_path,
            layout.plugin_source_origin_dir,
        )
        if path.exists()
    ]
    if existing_paths:
        relative_paths = [path.relative_to(layout.content_root).as_posix() for path in existing_paths]
        raise FileExistsError(
            "add-game 只支持未生成可信源快照的干净游戏目录；已存在 "
            + "、".join(sorted(relative_paths))
            + "，请换用干净原始游戏目录"
        )


def remove_source_snapshot_artifacts(layout: GameLayout) -> None:
    """移除本次注册创建的可信源快照文件。"""
    if layout.data_origin_dir.exists():
        shutil.rmtree(layout.data_origin_dir)
    if layout.plugins_origin_path.exists():
        layout.plugins_origin_path.unlink()
    if layout.plugin_source_origin_dir.exists():
        shutil.rmtree(layout.plugin_source_origin_dir)


def validate_source_snapshot_files(layout: GameLayout) -> None:
    """校验可信源快照文件在磁盘上完整存在。"""
    from .loader import validate_data_directory_integrity

    validate_data_directory_integrity(data_dir=layout.data_origin_dir, role="原始 data 备份")
    if not layout.plugins_origin_path.is_file():
        raise FileNotFoundError(f"缺少原始插件配置备份: {layout.plugins_origin_path}")
    if not layout.plugin_source_origin_dir.is_dir():
        raise NotADirectoryError(f"缺少原始插件源码备份目录: {layout.plugin_source_origin_dir}")


def collect_source_snapshot_records(
    *,
    layout: GameLayout,
    updated_at: str,
) -> list[SourceSnapshotFileRecord]:
    """收集可信源快照当前磁盘文件 hash，用于写入数据库 manifest。"""
    validate_source_snapshot_files(layout)
    snapshot_paths = [
        *sorted(
            (
                file_path
                for file_path in layout.data_origin_dir.iterdir()
                if file_path.is_file() and file_path.suffix.lower() == ".json"
            ),
            key=lambda path: path.name,
        ),
        layout.plugins_origin_path,
        *sorted(_iter_direct_plugin_source_files(layout.plugin_source_origin_dir), key=lambda path: path.name),
    ]
    hash_inputs = [
        NativeFileHashInput(
            id=f"source_snapshot_{index:06d}",
            relative_path=file_path.relative_to(layout.content_root).as_posix(),
        )
        for index, file_path in enumerate(snapshot_paths)
    ]
    hash_results = hash_native_files(root=layout.content_root, files=hash_inputs)
    return [
        SourceSnapshotFileRecord(
            relative_path=result.relative_path,
            sha256=result.sha256,
            byte_size=result.byte_size,
            updated_at=updated_at,
        )
        for result in hash_results
    ]


def validate_source_snapshot_manifest(
    *,
    layout: GameLayout,
    records: list[SourceSnapshotFileRecord],
) -> None:
    """确认数据库 manifest 与磁盘可信源快照一致。"""
    validate_source_snapshot_files(layout)
    expected = {
        record.relative_path: record for record in collect_source_snapshot_records(layout=layout, updated_at="")
    }
    actual = {record.relative_path: record for record in records}
    missing = sorted(set(expected) - set(actual))
    stale = sorted(set(actual) - set(expected))
    mismatched = sorted(
        relative_path
        for relative_path, expected_record in expected.items()
        if relative_path in actual
        and (
            actual[relative_path].sha256 != expected_record.sha256
            or actual[relative_path].byte_size != expected_record.byte_size
        )
    )
    if missing or stale or mismatched:
        parts: list[str] = []
        if missing:
            parts.append("缺少 " + "、".join(missing[:20]))
        if stale:
            parts.append("多出 " + "、".join(stale[:20]))
        if mismatched:
            parts.append("hash 不一致 " + "、".join(mismatched[:20]))
        raise RuntimeError("可信源快照 manifest 与磁盘文件不一致：" + "；".join(parts))


def _iter_direct_plugin_source_files(source_dir: Path) -> list[Path]:
    """列出 `js/plugins` 直接 JS 文件。"""
    if not source_dir.is_dir():
        return []
    return sorted(
        (file_path for file_path in source_dir.glob("*.js") if file_path.is_file()),
        key=lambda path: path.name,
    )


__all__ = [
    "SourceSnapshotFileRecord",
    "collect_source_snapshot_records",
    "create_source_snapshot_for_clean_game",
    "remove_source_snapshot_artifacts",
    "validate_clean_source_snapshot_target",
    "validate_source_snapshot_files",
    "validate_source_snapshot_manifest",
]
