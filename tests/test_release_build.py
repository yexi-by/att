from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import (
    encode_path_markers,
    filter_trusted_upstream_path_labels,
    find_path_marker_labels,
    normalize_wheel_archive,
    reproducible_build_environment,
)
from scripts.release_safety_selftest import assert_runtime_metadata_determinism

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_install_metadata_is_deterministic() -> None:
    """不同安装时刻不得改变发行运行时的最终字节。"""
    assert_runtime_metadata_determinism()


def test_skill_protocol_check_uses_utf8_on_legacy_windows_code_page() -> None:
    """Skill 漂移检查不得因 Windows 旧代码页无法输出中文而失败。"""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run(
        [sys.executable, "scripts/generate_skill_protocol.py", "--check"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "Skill 协议生成物与 canonical 源一致" in result.stdout.decode("utf-8")


def test_path_leak_diagnostics_report_labels_without_path_values() -> None:
    """路径泄漏诊断只报告来源和编码，不回显真实路径。"""
    markers = {
        "repository": "C:\\private\\repository",
        "user_profile": "C:\\Users\\private-user",
    }
    encoded_markers = encode_path_markers(markers)
    payload = b"prefix c:\\private\\repository\\file.py suffix"
    payload += "C:\\Users\\private-user\\.cargo".encode("utf-16-le")

    labels = find_path_marker_labels(payload, encoded_markers)

    assert labels == ("repository:utf-8", "user_profile:utf-16-le")
    assert all(value not in repr(labels) for value in markers.values())


def test_locked_upstream_native_only_ignores_toolchain_path_labels() -> None:
    """锁定上游扩展可保留原始工具链路径，但工作区路径仍必须失败。"""
    relative_path = "runtime/Lib/site-packages/example/native.pyd"
    payload = b"locked upstream binary"
    trusted_hashes = {relative_path: hashlib.sha256(payload).hexdigest()}
    labels = ("cargo_home:utf-8", "github_workspace:utf-8", "user_profile:utf-16-le")

    filtered = filter_trusted_upstream_path_labels(
        relative_path,
        payload,
        labels,
        trusted_hashes=trusted_hashes,
    )

    assert filtered == ("github_workspace:utf-8",)
    assert (
        filter_trusted_upstream_path_labels(
            relative_path,
            payload + b"tampered",
            labels,
            trusted_hashes=trusted_hashes,
        )
        == labels
    )
    assert (
        filter_trusted_upstream_path_labels(
            "runtime/Lib/site-packages/app/_native.pyd",
            payload,
            labels,
            trusted_hashes=trusted_hashes,
        )
        == labels
    )


def test_reproducible_environment_owns_rust_and_msvc_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """正式构建不得继承可覆盖路径净化规则的外部编译参数。"""
    user_profile = tmp_path / "runner"
    cargo_home = user_profile / ".cargo"
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    monkeypatch.setenv("HOME", str(user_profile))
    monkeypatch.setenv("CARGO_HOME", str(cargo_home))
    monkeypatch.setenv("RUSTFLAGS", "poison-rustflags")
    monkeypatch.setenv("CARGO_ENCODED_RUSTFLAGS", "poison-encoded")
    monkeypatch.setenv("CFLAGS", "poison-cflags")
    monkeypatch.setenv("CFLAGS_x86_64-pc-windows-msvc", "poison-target-cflags")
    environment = reproducible_build_environment(tmp_path / "cargo-target")

    assert "RUSTFLAGS" not in environment
    rust_flags = environment["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
    assert "poison-encoded" not in rust_flags
    assert "debuginfo=0" in rust_flags
    assert "link-arg=/PDBALTPATH:%_PDB%" in rust_flags
    remap_flags = [flag for flag in rust_flags if flag.startswith("--remap-path-prefix=")]
    assert remap_flags
    assert any(str(cargo_home).replace("\\", "/") in flag for flag in remap_flags)
    user_profile_index = max(index for index, flag in enumerate(remap_flags) if flag.endswith("=.user-profile"))
    cargo_home_index = max(index for index, flag in enumerate(remap_flags) if flag.endswith("=.cargo-home"))
    assert cargo_home_index > user_profile_index
    c_flags = environment["CFLAGS_x86_64-pc-windows-msvc"]
    assert "poison" not in c_flags
    assert "/d1trimfile:" in c_flags


def test_maturin_sbom_is_disabled_for_reproducible_release_wheel() -> None:
    """项目 wheel 不得生成包含 checkout file URL 的默认 Rust SBOM。"""
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["tool"]["maturin"]["sbom"] == {"rust": False, "auditwheel": False}


def test_release_checkout_text_is_fixed_to_lf() -> None:
    """Windows fresh checkout 的打包输入必须固定为 LF。"""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert "* text=auto eol=lf" in attributes


def _write_test_wheel(
    path: Path,
    *,
    names: tuple[str, ...],
    timestamp: tuple[int, int, int, int, int, int],
    mode: int,
) -> None:
    """写入带可控 ZIP 漂移的最小测试 wheel。"""
    payloads = {
        "app/__init__.py": b'__version__ = "0.1.15"\n',
        "att_mz-0.1.15.dist-info/RECORD": b"app/__init__.py,,\natt_mz-0.1.15.dist-info/RECORD,,\n",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            info = zipfile.ZipInfo(name)
            info.filename = name
            info.orig_filename = name
            info.date_time = timestamp
            info.create_system = 3
            info.external_attr = mode << 16
            info.extra = b"\x0a\x00\x00\x00"
            archive.writestr(info, payloads.get(name, b""))


def test_normalize_wheel_archive_is_reproducible_and_idempotent(tmp_path: Path) -> None:
    """成员数据相同时，顺序和 ZIP 元数据不得影响 wheel 字节。"""
    names = ("app/__init__.py", "att_mz-0.1.15.dist-info/RECORD")
    left = tmp_path / "left.whl"
    right = tmp_path / "right.whl"
    _write_test_wheel(left, names=names, timestamp=(2025, 1, 2, 3, 4, 6), mode=0o600)
    _write_test_wheel(right, names=tuple(reversed(names)), timestamp=(2026, 7, 20, 12, 0, 0), mode=0o666)

    normalize_wheel_archive(left)
    normalize_wheel_archive(right)

    expected = left.read_bytes()
    assert right.read_bytes() == expected
    normalize_wheel_archive(left)
    assert left.read_bytes() == expected
    with zipfile.ZipFile(left) as archive:
        assert archive.namelist() == sorted(names)
        assert all(member.date_time == (2026, 1, 1, 0, 0, 0) for member in archive.infolist())


@pytest.mark.parametrize("member_name", ("../evil.py", "/evil.py", "C:/evil.py", "app\\evil.py", "app//evil.py"))
def test_normalize_wheel_archive_rejects_unsafe_member_names(tmp_path: Path, member_name: str) -> None:
    """wheel 规范化不得接受可越界或平台歧义的成员名。"""
    wheel = tmp_path / "unsafe.whl"
    _write_test_wheel(wheel, names=(member_name,), timestamp=(2026, 1, 1, 0, 0, 0), mode=0o644)

    with pytest.raises(RuntimeError, match="不安全成员名"):
        normalize_wheel_archive(wheel)


def test_normalize_wheel_archive_rejects_casefold_collisions(tmp_path: Path) -> None:
    """Windows 下会落到同一路径的 wheel 成员必须拒绝。"""
    wheel = tmp_path / "duplicate.whl"
    _write_test_wheel(
        wheel,
        names=("app/module.py", "APP/MODULE.PY"),
        timestamp=(2026, 1, 1, 0, 0, 0),
        mode=0o644,
    )

    with pytest.raises(RuntimeError, match="重复或大小写冲突"):
        normalize_wheel_archive(wheel)
