from __future__ import annotations

from scripts.release_safety_selftest import assert_runtime_metadata_determinism


def test_runtime_install_metadata_is_deterministic() -> None:
    """不同安装时刻不得改变发行运行时的最终字节。"""
    assert_runtime_metadata_determinism()
