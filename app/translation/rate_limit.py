"""翻译请求的进程内共享速率限制器。"""

import asyncio


class RpmRateLimiter:
    """按固定间隔发放物理 LLM 请求许可。"""

    def __init__(self, rpm: int) -> None:
        """创建每分钟最多允许 ``rpm`` 次请求的限制器。"""
        if isinstance(rpm, bool) or rpm <= 0:
            raise ValueError("RPM 必须是大于 0 的整数")
        self._interval_seconds: float = 60.0 / rpm
        self._lock: asyncio.Lock = asyncio.Lock()
        self._next_request_at: float | None = None

    async def acquire(self) -> None:
        """等待直到下一个物理请求可以发送。"""
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._next_request_at is not None:
                delay_seconds = self._next_request_at - now
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                    now = loop.time()
            self._next_request_at = now + self._interval_seconds


__all__: list[str] = ["RpmRateLimiter"]
