"""运行目录解析测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import runtime_paths
from app.application.font_replacement import resolve_replacement_font_path
from app.observability import resolve_log_file_path
from app.persistence import GameRegistry, build_db_path, resolve_default_db_directory
from app.utils.config_loader_utils import resolve_setting_path


def test_app_home_uses_environment_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """应用运行目录优先使用显式环境变量。"""
    monkeypatch.setenv(runtime_paths.APP_HOME_ENV_NAME, str(tmp_path))

    assert runtime_paths.resolve_app_home() == tmp_path.resolve()
    assert runtime_paths.resolve_app_home_path("setting.toml") == (tmp_path / "setting.toml").resolve()


def test_app_home_defaults_to_source_root_without_launcher_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开发态未传入口环境变量时使用源码根目录。"""
    monkeypatch.delenv(runtime_paths.APP_HOME_ENV_NAME, raising=False)

    assert runtime_paths.resolve_app_home() == runtime_paths.source_root()


def test_default_project_paths_use_app_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """配置、数据库和日志默认都落在应用运行目录下。"""
    monkeypatch.setenv(runtime_paths.APP_HOME_ENV_NAME, str(tmp_path))

    assert resolve_setting_path() == (tmp_path / "setting.toml").resolve()
    assert resolve_default_db_directory() == (tmp_path / "data" / "db").resolve()
    assert GameRegistry().db_directory == (tmp_path / "data" / "db").resolve()
    assert build_db_path("测试游戏") == (tmp_path / "data" / "db" / "测试游戏.db").resolve()
    assert resolve_log_file_path() == (tmp_path / "logs" / "app.log").resolve()


def test_relative_replacement_font_uses_app_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """候选覆盖字体的相对路径按应用运行目录解析。"""
    font_path = tmp_path / "fonts" / "Test.ttf"
    font_path.parent.mkdir()
    _ = font_path.write_bytes(b"font")
    monkeypatch.setenv(runtime_paths.APP_HOME_ENV_NAME, str(tmp_path))

    assert resolve_replacement_font_path("fonts/Test.ttf") == font_path.resolve()
