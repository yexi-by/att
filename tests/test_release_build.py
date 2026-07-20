from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
