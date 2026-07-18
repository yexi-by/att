"""发布路径与 workflow 权限边界的轻量自测，不构建发行包。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from scripts.build_release import (
    assert_clean_payload,
    create_release_zip,
    normalize_runtime_install_metadata,
    remove_managed_tree,
    reset_directory,
    validate_output_directory,
    validate_zip_name,
)

ROOT = Path(__file__).resolve().parents[1]


def expect_rejected(operation: Callable[[], object], *, label: str) -> None:
    """确认不可信输入被显式拒绝。"""
    try:
        _ = operation()
    except FileNotFoundError, RuntimeError, ValueError:
        return
    raise AssertionError(f"安全自测未拒绝：{label}")


def create_junction(link: Path, target: Path) -> None:
    """使用 PowerShell 创建无需符号链接权限的 Windows junction。"""
    command = (
        "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
        "-Path $env:ATT_MZ_SELFTEST_LINK -Target $env:ATT_MZ_SELFTEST_TARGET | Out-Null"
    )
    env = os.environ.copy()
    env["ATT_MZ_SELFTEST_LINK"] = str(link)
    env["ATT_MZ_SELFTEST_TARGET"] = str(target)
    subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def assert_path_guards() -> None:
    """覆盖 root、越界、嵌套、链接和受管删除边界。"""
    with tempfile.TemporaryDirectory(prefix="att-mz-release-safety-root-") as root_text:
        with tempfile.TemporaryDirectory(prefix="att-mz-release-safety-outside-") as outside_text:
            root = Path(root_text)
            outside = Path(outside_text)
            outside_sentinel = outside / "must-survive.txt"
            outside_sentinel.write_text("outside", encoding="utf-8")
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            direct = root / "dist-a"
            if validate_output_directory(direct, workspace_root=root) != direct:
                raise AssertionError("有效直接子目录解析错误")
            expect_rejected(lambda: validate_output_directory(root, workspace_root=root), label="workspace root")
            expect_rejected(
                lambda: validate_output_directory(root / "outer" / "nested", workspace_root=root),
                label="nested output",
            )
            expect_rejected(
                lambda: validate_output_directory(Path("..") / "outside", workspace_root=root),
                label="parent traversal",
            )
            expect_rejected(
                lambda: validate_output_directory(outside / "dist", workspace_root=root),
                label="outside output",
            )

            file_output = root / "file-output"
            file_output.write_text("not a directory", encoding="utf-8")
            expect_rejected(
                lambda: validate_output_directory(file_output, workspace_root=root),
                label="file output",
            )

            direct.mkdir()
            (direct / "sentinel.txt").write_text("sentinel", encoding="utf-8")
            reset_directory(direct, managed_root=root)
            if not direct.is_dir() or any(direct.iterdir()):
                raise AssertionError("安全目录重建结果异常")

            nested_junction = direct / "nested-junction"
            create_junction(nested_junction, outside)
            try:
                expect_rejected(
                    lambda: remove_managed_tree(direct, managed_root=root),
                    label="nested reparse deletion",
                )
                if not outside.is_dir() or outside_sentinel.read_text(encoding="utf-8") != "outside":
                    raise AssertionError("嵌套 junction 删除自测破坏了外部目标")
            finally:
                nested_stat = nested_junction.stat(follow_symlinks=False)
                if not (getattr(nested_stat, "st_file_attributes", 0) & reparse_flag):
                    raise AssertionError("拒绝清理不是 reparse point 的嵌套自测 junction")
                os.rmdir(nested_junction)

            junction = root / "junction-output"
            create_junction(junction, outside)
            try:
                junction_stat = junction.stat(follow_symlinks=False)
                if not (getattr(junction_stat, "st_file_attributes", 0) & reparse_flag):
                    raise AssertionError("junction 自测前置条件失败")
                expect_rejected(
                    lambda: validate_output_directory(junction, workspace_root=root),
                    label="reparse output",
                )
                expect_rejected(
                    lambda: remove_managed_tree(junction, managed_root=root),
                    label="reparse deletion",
                )
            finally:
                current_stat = junction.stat(follow_symlinks=False)
                if not (getattr(current_stat, "st_file_attributes", 0) & reparse_flag):
                    raise AssertionError("拒绝清理不是 reparse point 的自测 junction")
                os.rmdir(junction)


def assert_zip_guards() -> None:
    """覆盖 basename、绝对路径、分隔符和父目录语义。"""
    if validate_zip_name("att-mz-windows-x86_64.zip") != "att-mz-windows-x86_64.zip":
        raise AssertionError("有效 ZIP basename 被改写")
    invalid_names = (
        "",
        "..",
        "../artifact.zip",
        "..\\artifact.zip",
        "nested/artifact.zip",
        "nested\\artifact.zip",
        "C:\\absolute.zip",
        "artifact..zip",
        "artifact.txt",
    )
    for name in invalid_names:
        expect_rejected(lambda name=name: validate_zip_name(name), label=f"zip={name!r}")


def _write_runtime_metadata_fixture(runtime: Path, *, timestamp: int) -> Path:
    """创建除 uv 安装时间外完全相同的最小运行时元数据。"""
    site_packages = runtime / "Lib" / "site-packages"
    package = site_packages / "example"
    dist_info = site_packages / "example-1.0.dist-info"
    package.mkdir(parents=True)
    dist_info.mkdir()
    (package / "module.pyc").write_bytes(b"stable-pyc")
    (dist_info / "direct_url.json").write_text(
        '{"url":"file:///temporary/build/path"}\n', encoding="utf-8", newline="\n"
    )
    cache_payload = json.dumps(
        {"timestamp": {"secs_since_epoch": timestamp, "nanos_since_epoch": timestamp}},
        separators=(",", ":"),
        sort_keys=True,
    )
    (dist_info / "uv_cache.json").write_text(cache_payload + "\n", encoding="utf-8", newline="\n")
    record = dist_info / "RECORD"
    record.write_text(
        "\n".join(
            (
                "example-1.0.dist-info/uv_cache.json,sha256=timestamp-dependent,1",
                "example/module.pyc,sha256=stable,10",
                "example-1.0.dist-info/direct_url.json,sha256=build-path-dependent,1",
                "example-1.0.dist-info/RECORD,,",
            )
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return record


def _create_minimal_release_payload(release: Path) -> Path:
    """创建足以验证安装残留拒绝逻辑的发行目录。"""
    directory_names = {"data", "fonts", "logs", "outputs", "prompts", "runtime", "skills"}
    root_names = {
        "LICENSE",
        "README.md",
        "att-mz.exe",
        "build-manifest.json",
        "custom_placeholder_rules.json",
        *directory_names,
        "setting.example.toml",
        "setting.toml",
    }
    for name in sorted(root_names):
        path = release / name
        if name in directory_names:
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
    cache = release / "runtime" / "Lib" / "site-packages" / "example-1.0.dist-info" / "uv_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('{"timestamp":1}\n', encoding="utf-8", newline="\n")
    return cache


def assert_runtime_metadata_determinism() -> None:
    """确认 uv 的墙钟缓存不会进入运行时、RECORD 或最终发行树。"""
    with tempfile.TemporaryDirectory(prefix="att-mz-release-metadata-") as temp_text:
        root = Path(temp_text)
        release_a = root / "a" / "att-mz"
        release_b = root / "b" / "att-mz"
        runtime_a = release_a / "runtime"
        runtime_b = release_b / "runtime"
        record_a = _write_runtime_metadata_fixture(runtime_a, timestamp=1)
        record_b = _write_runtime_metadata_fixture(runtime_b, timestamp=2)

        normalize_runtime_install_metadata(runtime_a)
        normalize_runtime_install_metadata(runtime_b)

        files_a = {
            path.relative_to(runtime_a).as_posix(): path.read_bytes() for path in runtime_a.rglob("*") if path.is_file()
        }
        files_b = {
            path.relative_to(runtime_b).as_posix(): path.read_bytes() for path in runtime_b.rglob("*") if path.is_file()
        }
        if files_a != files_b or record_a.read_bytes() != record_b.read_bytes():
            raise AssertionError("仅 uv 安装时间不同的运行时在净化后仍不一致")
        if any(path.name in {"direct_url.json", "uv_cache.json"} for path in runtime_a.rglob("*")):
            raise AssertionError("运行时净化后仍残留安装器私有元数据")
        if b"uv_cache.json" in record_a.read_bytes() or b"direct_url.json" in record_a.read_bytes():
            raise AssertionError("规范化 RECORD 仍引用已删除安装元数据")
        record_lines = record_a.read_text(encoding="utf-8").splitlines()
        if record_lines != sorted(record_lines):
            raise AssertionError("规范化 RECORD 未按路径稳定排序")
        zip_a = root / "a.zip"
        zip_b = root / "b.zip"
        create_release_zip(release_a, zip_a)
        create_release_zip(release_b, zip_b)
        if zip_a.read_bytes() != zip_b.read_bytes():
            raise AssertionError("仅 uv 安装时间不同的同名发行树生成了不同 ZIP")

        release = root / "att-mz"
        cache = _create_minimal_release_payload(release)
        try:
            assert_clean_payload(release)
        except RuntimeError as error:
            if cache.name not in str(error):
                raise AssertionError("发行净化拒绝了错误目标") from error
        else:
            raise AssertionError("发行净化未拒绝 uv_cache.json")


def assert_workflow_contract() -> None:
    """静态确认构建与发布权限分离且发布 job 不会重建。"""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    preflight = (ROOT / ".github" / "workflows" / "release-preflight.yml").read_text(encoding="utf-8")
    handoff_script = (ROOT / "scripts" / "verify_release_handoff.ps1").read_text(encoding="utf-8")
    lock = json.loads((ROOT / "release-toolchain.lock.json").read_text(encoding="utf-8"))
    download_sha = lock["actions"]["actions/download-artifact"]
    expected_download = f"uses: actions/download-artifact@{download_sha}"
    workflow_header = workflow.split("jobs:\n", maxsplit=1)[0]
    if "permissions:\n  contents: read" not in workflow_header or "contents: write" in workflow_header:
        raise AssertionError("release workflow 顶层权限必须保持 contents:read")
    if "  verify_release:\n" not in workflow or "  publish_release:\n" not in workflow:
        raise AssertionError("release workflow 未拆成 verify_release/publish_release")
    verify_section, publish_section = workflow.split("  publish_release:\n", maxsplit=1)
    if "permissions:\n      contents: read" not in verify_section:
        raise AssertionError("verify_release 不是 contents:read")
    if "permissions:\n      contents: write" not in publish_section:
        raise AssertionError("publish_release 不是 contents:write")
    if "scripts\\build_release.py" in publish_section or "scripts/build_release.py" in publish_section:
        raise AssertionError("publish_release 禁止再次调用 build_release.py")
    if "actions/upload-artifact@" not in verify_section or expected_download not in publish_section:
        raise AssertionError("跨 job artifact handoff 不完整或 download action 未锁定")
    if "actions/download-artifact@" in verify_section or "actions/upload-artifact@" in publish_section:
        raise AssertionError("artifact upload/download 未按 verify/publish 边界分离")
    if "softprops/action-gh-release@" in verify_section or "softprops/action-gh-release@" not in publish_section:
        raise AssertionError("GitHub Release 动作不在唯一 publish job")
    required_publish_checks = (
        "scripts\\verify_release_handoff.ps1",
        "needs.verify_release.outputs.zip_sha256",
        "needs.verify_release.outputs.pylock_sha256",
        "needs.verify_release.outputs.manifest_sha256",
        "needs.verify_release.outputs.sums_sha256",
    )
    if any(check not in publish_section for check in required_publish_checks):
        raise AssertionError("publish_release 缺少 artifact 或远端 peeled tag 复核")
    required_handoff_checks = (
        "git ls-remote --tags origin",
        "SHA256SUMS.txt",
        "release-manifest.json",
        "remotePeeledSha",
        "跨 job artifact SHA-256 不一致",
    )
    if any(check not in handoff_script for check in required_handoff_checks):
        raise AssertionError("handoff 校验脚本缺少外部哈希、manifest 或远端 peeled tag 复核")
    if "softprops/action-gh-release" in preflight or "contents: write" in preflight:
        raise AssertionError("无标签预检包含发布权限或发布动作")


def main() -> int:
    """运行不下载依赖、不构建产物的安全边界自测。"""
    assert_path_guards()
    assert_zip_guards()
    assert_runtime_metadata_determinism()
    assert_workflow_contract()
    print(
        json.dumps(
            {
                "status": "ok",
                "checks": [
                    "path_guards",
                    "zip_guards",
                    "runtime_metadata_determinism",
                    "workflow_permissions",
                    "artifact_handoff",
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
