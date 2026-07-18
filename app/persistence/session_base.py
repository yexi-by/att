"""数据库会话 mixin 的最小状态契约。"""

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import cast

import aiosqlite


class SessionMixinBase:
    """声明数据库会话子能力需要的公共状态。"""

    # mixin 只声明会话状态契约；真实连接由 TargetGameSession.__init__ 在入口处写入。
    connection: aiosqlite.Connection = cast(aiosqlite.Connection, object())
    active_translation_run_id: str | None = None

    @property
    def db_path(self) -> Path:
        """返回当前会话绑定的数据库路径。"""
        raise NotImplementedError

    @property
    def game_id(self) -> str:
        """返回当前会话绑定的稳定游戏标识。"""
        raise NotImplementedError

    async def reconcile_translation_run_recovery(self) -> bool:
        """协调同数据库目录中的翻译终态一次性恢复日志。"""
        raise NotImplementedError

    @property
    def has_persistent_mutation_lease(self) -> bool:
        """返回会话是否在整个命令期间持有目标游戏修改租约。"""
        raise NotImplementedError

    def translation_run_write_operation(self) -> AbstractAsyncContextManager[None]:
        """返回串行化翻译运行写操作的异步上下文。"""
        raise NotImplementedError
