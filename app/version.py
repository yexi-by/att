"""应用版本解析。"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "att-mz"
SOURCE_FALLBACK_VERSION = "0.1.15"


def application_version() -> str:
    """返回已安装包版本；源码未安装时使用与清单同步的开发回退值。"""
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return SOURCE_FALLBACK_VERSION


__all__ = ["application_version"]
