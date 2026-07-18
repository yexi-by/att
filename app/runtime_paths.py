"""运行目录解析工具。

本模块负责统一判断当前程序应该把配置、数据库、日志和随包资源放在哪里。
开发态默认使用源码根目录；发布态默认使用可执行文件所在目录；用户也可以通过
`ATT_MZ_HOME` 显式指定完整应用目录。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_HOME_ENV_NAME = "ATT_MZ_HOME"


def source_root() -> Path:
    """返回源码布局下的项目根目录。"""
    return Path(__file__).resolve().parents[1]


def resolve_app_home() -> Path:
    """解析应用运行目录；发行版入口必须显式设置 ``ATT_MZ_HOME``。"""
    env_value = os.environ.get(APP_HOME_ENV_NAME)
    if env_value is not None and env_value.strip():
        return Path(env_value).expanduser().resolve()
    return source_root()


def resolve_app_path(*parts: str) -> Path:
    """在应用运行目录下拼接路径。"""
    return resolve_app_home().joinpath(*parts).resolve()


def resolve_app_home_path(path_text: str | Path) -> Path:
    """把绝对路径原样解析，把相对路径解析到应用运行目录下。"""
    path = Path(path_text)
    if path.is_absolute():
        return path.resolve()
    return (resolve_app_home() / path).resolve()


__all__: list[str] = [
    "APP_HOME_ENV_NAME",
    "resolve_app_home",
    "resolve_app_home_path",
    "resolve_app_path",
    "source_root",
]
