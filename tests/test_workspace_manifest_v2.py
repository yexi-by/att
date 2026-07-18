"""Agent 工作区 manifest v2 绑定与安全清理测试。"""

import asyncio
import errno
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch

from app.agent_toolkit import AgentToolkitService
from app.agent_toolkit.services import workspace as workspace_service
from app.persistence import GameRegistry
from app.plugin_text.exporter import export_plugins_json_file
from app.rmmz.schema import GameData
from app.rmmz.text_rules import (
    JsonObject,
    coerce_json_value,
    ensure_json_array,
    ensure_json_object,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SETTING_PATH = ROOT / "setting.example.toml"


def _read_manifest(workspace: Path) -> JsonObject:
    value = cast(object, json.loads((workspace / "manifest.json").read_text(encoding="utf-8")))
    return ensure_json_object(coerce_json_value(value), "manifest")


def _workspace_staging_paths(parent: Path) -> list[Path]:
    return list(parent.glob(".att-mz-workspace-*.tmp"))


async def test_workspace_prepare_publishes_into_preexisting_empty_target(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """既有空目录仍受支持，但用户只能看到最终含 manifest 的完整目录。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "ok"
    assert report.summary["workspace"] == str(workspace.resolve())
    assert report.summary["manifest"] == str((workspace / "manifest.json").resolve())
    assert (workspace / "manifest.json").is_file()
    assert (workspace / "plugins.json").is_file()
    assert _workspace_staging_paths(tmp_path) == []


@pytest.mark.parametrize("precreate_empty_target", [False, True])
async def test_workspace_prepare_failure_never_exposes_partial_target(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    precreate_empty_target: bool,
) -> None:
    """生成中途失败只能留下原来的缺失或空目标，且必须清除同级暂存区。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    if precreate_empty_target:
        workspace.mkdir()

    async def injected_write_failure(*, game_data: GameData, output_path: Path) -> None:
        _ = (game_data, output_path)
        raise OSError("injected workspace generation failure")

    monkeypatch.setattr(workspace_service, "export_plugins_json_file", injected_write_failure)

    with pytest.raises(OSError, match="injected workspace generation failure"):
        _ = await service.prepare_agent_workspace(
            game_title=record.game_title,
            output_dir=workspace,
            command_codes=None,
        )

    assert workspace.exists() is precreate_empty_target
    if precreate_empty_target:
        assert list(workspace.iterdir()) == []
    assert _workspace_staging_paths(tmp_path) == []


async def test_workspace_prepare_cancellation_removes_staging_without_exposing_target(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """任务取消也必须执行暂存区清理，目标路径保持不存在。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"

    async def injected_cancellation(*, game_data: GameData, output_path: Path) -> None:
        _ = (game_data, output_path)
        raise asyncio.CancelledError

    monkeypatch.setattr(workspace_service, "export_plugins_json_file", injected_cancellation)

    with pytest.raises(asyncio.CancelledError):
        _ = await service.prepare_agent_workspace(
            game_title=record.game_title,
            output_dir=workspace,
            command_codes=None,
        )

    assert not workspace.exists()
    assert _workspace_staging_paths(tmp_path) == []


@pytest.mark.parametrize("precreate_empty_target", [False, True])
async def test_workspace_prepare_publish_cross_volume_failure_restores_original_target(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    *,
    precreate_empty_target: bool,
) -> None:
    """即使目录发布返回跨卷错误，也不得暴露半成品或遗留暂存区。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    if precreate_empty_target:
        workspace.mkdir()
    original_replace = os.replace

    def injected_cross_volume_failure(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        if Path(source).name.startswith(".att-mz-workspace-") and Path(destination) == workspace:
            raise OSError(errno.EXDEV, "injected cross-volume publish failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", injected_cross_volume_failure)

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_publish_failed"}
    assert workspace.exists() is precreate_empty_target
    if precreate_empty_target:
        assert list(workspace.iterdir()) == []
    assert _workspace_staging_paths(tmp_path) == []


async def test_workspace_prepare_preserves_target_created_during_generation(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """目标在生成期间被创建时，发布必须失败且保留对方写入的内容。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    sentinel = workspace / "owned-by-other-process.txt"
    original_export_plugins = export_plugins_json_file
    target_created = False

    async def create_target_before_write(*, game_data: GameData, output_path: Path) -> None:
        nonlocal target_created
        if not target_created:
            workspace.mkdir()
            _ = sentinel.write_text("preserve", encoding="utf-8")
            target_created = True
        await original_export_plugins(game_data=game_data, output_path=output_path)

    monkeypatch.setattr(workspace_service, "export_plugins_json_file", create_target_before_write)

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_target_changed"}
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert not (workspace / "manifest.json").exists()
    assert _workspace_staging_paths(tmp_path) == []


async def test_workspace_prepare_does_not_overwrite_replaced_empty_target(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """原空目录被另一空目录替换后也必须按身份变化拒绝发布。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_export_plugins = export_plugins_json_file
    target_replaced = False

    async def replace_target_before_write(*, game_data: GameData, output_path: Path) -> None:
        nonlocal target_replaced
        if not target_replaced:
            workspace.rmdir()
            workspace.mkdir()
            target_replaced = True
        await original_export_plugins(game_data=game_data, output_path=output_path)

    monkeypatch.setattr(workspace_service, "export_plugins_json_file", replace_target_before_write)

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_target_changed"}
    assert list(workspace.iterdir()) == []
    assert _workspace_staging_paths(tmp_path) == []


async def test_workspace_prepare_rejects_link_target_without_touching_destination(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """目标工作区本身是链接时，不得解析链接后把文件写进真实目录。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    sentinel = real_workspace / "sentinel.txt"
    _ = sentinel.write_text("preserve", encoding="utf-8")
    linked_workspace = tmp_path / "linked-workspace"
    try:
        linked_workspace.symlink_to(real_workspace, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前测试环境不能创建目录符号链接: {error}")

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=linked_workspace,
        command_codes=None,
    )

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_path_unsafe"}
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert list(real_workspace.iterdir()) == [sentinel]
    assert _workspace_staging_paths(tmp_path) == []


async def test_workspace_prepare_rejects_linked_parent_before_creating_children(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """目标经过链接父目录时，连链接后的缺失父目录也不得创建。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前测试环境不能创建目录符号链接: {error}")
    workspace = linked_parent / "new-parent" / "workspace"

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_path_unsafe"}
    assert not (real_parent / "new-parent").exists()
    assert _workspace_staging_paths(real_parent) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 专项测试")
async def test_workspace_prepare_rejects_junction_parent_before_creating_children(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """普通 Windows 用户可创建的 junction 也不得成为工作区写入旁路。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    real_parent = tmp_path / "junction-real-parent"
    real_parent.mkdir()
    junction_parent = tmp_path / "junction-parent"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction_parent), str(real_parent)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not junction_parent.is_junction():
        pytest.skip(f"当前测试环境不能创建 junction: {result.stderr or result.stdout}")
    workspace = junction_parent / "new-parent" / "workspace"

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_path_unsafe"}
    assert not (real_parent / "new-parent").exists()
    assert _workspace_staging_paths(real_parent) == []


async def test_workspace_prepare_rejects_nonempty_target_without_staging(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """既有非空目标保持原语义，且在拒绝前不得创建暂存目录。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "sentinel.txt"
    _ = sentinel.write_text("preserve", encoding="utf-8")

    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_not_empty"}
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert _workspace_staging_paths(tmp_path) == []


async def test_workspace_manifest_v2_uses_relative_paths_and_survives_move(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """manifest 不泄露绝对游戏路径，移动工作区后仍能按声明安全清理。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace-original"
    report = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    assert report.status == "ok"

    manifest = _read_manifest(workspace)
    assert manifest["contract_version"] == 2
    assert manifest["game_id"] == record.game_id
    raw_files = ensure_json_array(manifest["files"], "manifest.files")
    raw_layout = ensure_json_object(manifest["layout"], "manifest.layout")
    files = [item for item in raw_files if isinstance(item, str)]
    assert len(files) == len(raw_files)
    assert all(not Path(item).is_absolute() and not Path(item).drive for item in files)
    assert all((workspace / item).exists() for item in files)
    assert all(
        isinstance(value, str) and not Path(value).is_absolute()
        for key, value in raw_layout.items()
        if key.endswith(("root", "dir", "path"))
    )
    assert str(minimal_game_dir.resolve()) not in json.dumps(manifest, ensure_ascii=False)

    moved_workspace = tmp_path / "移动 后的 工作区" / "deep" / "workspace"
    moved_workspace.parent.mkdir(parents=True)
    _ = shutil.move(str(workspace), str(moved_workspace))
    cleanup_report = await service.cleanup_agent_workspace(workspace=moved_workspace)

    assert cleanup_report.status == "ok"
    assert not (moved_workspace / "manifest.json").exists()
    assert list(moved_workspace.iterdir()) == []


async def test_workspace_cleanup_rejects_traversal_before_deleting_any_file(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """任一声明路径越界时必须保留 manifest 和全部工作区证据。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    manifest = _read_manifest(workspace)
    raw_files = ensure_json_array(manifest.get("files"), "manifest.files")
    raw_files.append("../outside.txt")
    _ = (workspace / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sentinel = workspace / "plugins.json"
    assert sentinel.exists()

    report = await service.cleanup_agent_workspace(workspace=workspace)

    assert report.status == "error"
    assert {error.code for error in report.errors} == {"manifest_path_unsafe"}
    assert (workspace / "manifest.json").exists()
    assert sentinel.exists()


async def test_workspace_rejects_legacy_manifest_and_wrong_game_binding(
    minimal_game_dir: Path,
    minimal_english_game_dir: Path,
    tmp_path: Path,
) -> None:
    """旧 manifest 必须要求重建，v2 也不能用于另一个稳定 game_id。"""
    registry = GameRegistry(tmp_path / "db")
    japanese = await registry.register_game(minimal_game_dir, source_language="ja")
    english = await registry.register_game(minimal_english_game_dir, source_language="en")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=japanese.game_title,
        output_dir=workspace,
        command_codes=None,
    )

    wrong_game_report = await service.validate_agent_workspace(
        game_title=english.game_title,
        workspace=workspace,
    )
    assert "manifest_game_mismatch" in {error.code for error in wrong_game_report.errors}

    manifest = _read_manifest(workspace)
    manifest["contract_version"] = 1
    _ = (workspace / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    validation_report = await service.validate_agent_workspace(
        game_title=japanese.game_title,
        workspace=workspace,
    )
    cleanup_report = await service.cleanup_agent_workspace(workspace=workspace)

    assert "manifest_version_unsupported" in {error.code for error in validation_report.errors}
    assert cleanup_report.status == "error"
    assert (workspace / "manifest.json").exists()


async def test_workspace_cleanup_rejects_declared_symlink_without_deleting_files(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """manifest 声明路径被换成符号链接时，清理必须在删除前失败。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    link_path = workspace / "plugins.json"
    target_path = workspace / "event-commands.json"
    link_path.unlink()
    try:
        link_path.symlink_to(target_path)
    except OSError as error:
        pytest.skip(f"当前测试环境不能创建普通符号链接: {error}")
    untouched_path = workspace / "placeholder-rules.json"

    report = await service.cleanup_agent_workspace(workspace=workspace)

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"manifest_path_unsafe"}
    assert (workspace / "manifest.json").exists()
    assert link_path.is_symlink()
    assert target_path.exists()
    assert untouched_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 专项测试")
async def test_workspace_cleanup_rejects_declared_junction_without_deleting_files(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """manifest 声明目录被换成 junction 时，清理必须保留工作区证据。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    junction_path = workspace / "terminology"
    junction_target = tmp_path / "junction-target"
    _ = shutil.move(str(junction_path), str(junction_target))
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction_path), str(junction_target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not junction_path.is_junction():
        pytest.skip(f"当前测试环境不能创建 junction: {result.stderr or result.stdout}")

    report = await service.cleanup_agent_workspace(workspace=workspace)

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"manifest_path_unsafe"}
    assert (workspace / "manifest.json").exists()
    assert junction_path.is_junction()
    assert (junction_target / "field-terms.json").exists()


async def test_workspace_cleanup_rejects_linked_workspace_root(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区根目录本身是链接时不得解析到真实目录后继续删除。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace-real"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    linked_workspace = tmp_path / "workspace-link"
    try:
        linked_workspace.symlink_to(workspace, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前测试环境不能创建目录符号链接: {error}")

    report = await service.cleanup_agent_workspace(workspace=linked_workspace)

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"manifest_path_unsafe"}
    assert (workspace / "manifest.json").exists()
    assert (workspace / "plugins.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 专项测试")
async def test_workspace_cleanup_rejects_junction_workspace_root(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """工作区根目录本身是 junction 时必须保留真实目录全部文件。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace-real"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    junction_workspace = tmp_path / "workspace-junction"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction_workspace), str(workspace)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not junction_workspace.is_junction():
        pytest.skip(f"当前测试环境不能创建 junction: {result.stderr or result.stdout}")

    report = await service.cleanup_agent_workspace(workspace=junction_workspace)

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"manifest_path_unsafe"}
    assert (workspace / "manifest.json").exists()
    assert (workspace / "plugins.json").exists()


async def test_workspace_cleanup_failure_keeps_manifest(
    minimal_game_dir: Path,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """删除任一声明文件失败时必须保留 manifest 供修复后重试。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    failure_path = workspace / "plugins.json"
    original_unlink = Path.unlink

    def injected_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == failure_path:
            raise OSError("injected workspace cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", injected_unlink)

    report = await service.cleanup_agent_workspace(workspace=workspace)

    assert report.status == "error"
    assert {item.code for item in report.errors} == {"workspace_cleanup_failed"}
    assert (workspace / "manifest.json").exists()
    assert failure_path.exists()


async def test_workspace_validation_rejects_changed_source_snapshot_digest(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """源快照记录变化后，旧工作区必须明确报告 stale。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    async with await registry.open_game(record.game_title) as session:
        async with session.connection.execute(
            "SELECT relative_path FROM source_snapshot_files ORDER BY relative_path LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        relative_path = cast(object, row[0])
        assert isinstance(relative_path, str)
        snapshot_path = session.content_root / relative_path
        changed_content = snapshot_path.read_bytes() + b"\n"
        _ = snapshot_path.write_bytes(changed_content)
        _ = await session.connection.execute(
            """
            UPDATE source_snapshot_files
            SET sha256 = ?, byte_size = ?
            WHERE relative_path = ?
            """,
            (hashlib.sha256(changed_content).hexdigest(), len(changed_content), relative_path),
        )
        await session.connection.commit()

    report = await service.validate_agent_workspace(
        game_title=record.game_title,
        workspace=workspace,
    )

    assert {item.code for item in report.errors} == {"manifest_source_snapshot_stale"}


async def test_workspace_validation_rejects_changed_language_fingerprint(
    minimal_game_dir: Path,
    tmp_path: Path,
) -> None:
    """数据库语言配置变化后不得读取旧工作区规则。"""
    registry = GameRegistry(tmp_path / "db")
    record = await registry.register_game(minimal_game_dir, source_language="ja")
    service = AgentToolkitService(game_registry=registry, setting_path=EXAMPLE_SETTING_PATH)
    workspace = tmp_path / "workspace"
    _ = await service.prepare_agent_workspace(
        game_title=record.game_title,
        output_dir=workspace,
        command_codes=None,
    )
    async with await registry.open_game(record.game_title) as session:
        _ = await session.connection.execute(
            "UPDATE language_settings SET additional_source_languages = ? WHERE settings_key = 'current'",
            ('["en"]',),
        )
        await session.connection.commit()

    report = await service.validate_agent_workspace(
        game_title=record.game_title,
        workspace=workspace,
    )

    assert {item.code for item in report.errors} == {"manifest_language_profile_mismatch"}
