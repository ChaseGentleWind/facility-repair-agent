from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis


class TicketCounter(Protocol):
    """工单号计数器抽象接口"""
    async def next(self) -> int:
        """获取下一个工单号（全局递增）"""
        ...


# ── Memory 实现（复用现有 threading.Lock + counter）────────────────────────

class MemoryTicketCounter:
    """内存计数器实现（单进程，用于测试和本地开发）"""

    def __init__(self, seed: int = 1726198):
        self._counter = seed
        self._lock = threading.Lock()

    async def next(self) -> int:
        with self._lock:
            repair_no = self._counter
            self._counter += 1
            return repair_no


# ── Redis 实现 ────────────────────────────────────────────────────────────────

class RedisTicketCounter:
    """Redis 计数器实现（分布式，用于生产环境）"""

    def __init__(self, redis: AsyncRedis, key: str = "frap:repair_no:counter", seed: int = 1726198):
        self._redis = redis
        self._key = key
        self._seed = seed

    async def _ensure_initialized(self) -> None:
        """确保计数器已初始化（仅首次调用时设置种子值）"""
        await self._redis.setnx(self._key, self._seed)

    async def next(self) -> int:
        """原子递增并返回新值"""
        await self._ensure_initialized()
        return await self._redis.incr(self._key)


# ── 模块级单例 ────────────────────────────────────────────────────────────────

_counter_instance: TicketCounter | None = None


def init_ticket_counter(backend: str = "memory", redis_client: AsyncRedis | None = None, seed: int = 1726198) -> None:
    """初始化计数器后端（在 main.py lifespan 中调用）"""
    global _counter_instance
    if backend == "memory":
        _counter_instance = MemoryTicketCounter(seed=seed)
    elif backend == "redis":
        if redis_client is None:
            raise ValueError("redis_client is required for redis backend")
        _counter_instance = RedisTicketCounter(redis=redis_client, seed=seed)
    else:
        raise ValueError(f"Unsupported counter backend: {backend}")


def get_ticket_counter() -> TicketCounter:
    """获取当前计数器实例"""
    if _counter_instance is None:
        raise RuntimeError("TicketCounter not initialized. Call init_ticket_counter() first.")
    return _counter_instance
