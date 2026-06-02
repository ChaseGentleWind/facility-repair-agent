from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, AsyncIterator, Protocol
from uuid import uuid4

from app.agent.state import AgentState, Session, TicketDraft
from app.config import settings

if TYPE_CHECKING:
    from redis.asyncio import Redis as AsyncRedis


class LockHandle(Protocol):
    """分布式锁句柄，用作 async context manager"""
    async def __aenter__(self) -> LockHandle: ...
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None: ...


class SessionStore(Protocol):
    """会话存储抽象接口"""
    async def create(self, session: Session) -> None:
        """创建新会话并持久化"""
        ...

    async def get(self, session_id: str) -> Session | None:
        """获取会话，不存在或已过期返回 None"""
        ...

    async def save(self, session: Session) -> None:
        """保存会话状态（覆盖）"""
        ...

    async def refresh_ttl(self, session_id: str) -> None:
        """刷新会话 TTL"""
        ...

    async def delete(self, session_id: str) -> None:
        """删除会话"""
        ...

    async def acquire_lock(self, session_id: str, ttl_s: int) -> LockHandle | None:
        """
        尝试获取会话锁（非阻塞）
        返回 None 表示锁被占用
        返回 LockHandle 可用作 async with，退出时自动释放
        """
        ...


# ── Memory 实现（复用现有 dict + asyncio.Lock）──────────────────────────────

class _MemoryLockHandle:
    def __init__(self, lock: asyncio.Lock):
        self._lock = lock

    async def __aenter__(self) -> LockHandle:
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self._lock.release()


class MemorySessionStore:
    """内存存储实现（单进程，用于测试和本地开发）"""

    def __init__(self):
        self._store: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def create(self, session: Session) -> None:
        self._store[session.session_id] = session
        self._locks[session.session_id] = asyncio.Lock()

    async def get(self, session_id: str) -> Session | None:
        session = self._store.get(session_id)
        if session is None:
            return None
        if session.expires_at < datetime.now():
            self._store.pop(session_id, None)
            self._locks.pop(session_id, None)
            return None
        return session

    async def save(self, session: Session) -> None:
        self._store[session.session_id] = session

    async def refresh_ttl(self, session_id: str) -> None:
        session = self._store.get(session_id)
        if session:
            session.expires_at = datetime.now() + timedelta(seconds=settings.session_ttl_seconds)

    async def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)
        self._locks.pop(session_id, None)

    @asynccontextmanager
    async def acquire_lock(self, session_id: str, ttl_s: int) -> AsyncIterator[LockHandle | None]:
        lock = self._locks.get(session_id)
        if lock is None:
            yield None
            return

        if lock.locked():
            yield None
            return

        async with _MemoryLockHandle(lock) as handle:
            yield handle


# ── Redis 实现 ────────────────────────────────────────────────────────────────

class _RedisLockHandle:
    def __init__(self, redis: AsyncRedis, lock_key: str):
        self._redis = redis
        self._lock_key = lock_key

    async def __aenter__(self) -> LockHandle:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._redis.delete(self._lock_key)


class RedisSessionStore:
    """Redis 存储实现（分布式，用于生产环境）"""

    def __init__(self, redis: AsyncRedis, prefix: str, lock_prefix: str):
        self._redis = redis
        self._prefix = prefix
        self._lock_prefix = lock_prefix

    def _session_key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def _lock_key(self, session_id: str) -> str:
        return f"{self._lock_prefix}{session_id}"

    async def create(self, session: Session) -> None:
        key = self._session_key(session.session_id)
        data = json.dumps(session.serialize(), ensure_ascii=False)
        ttl = int((session.expires_at - datetime.now()).total_seconds())
        await self._redis.setex(key, ttl, data)

    async def get(self, session_id: str) -> Session | None:
        key = self._session_key(session_id)
        data = await self._redis.get(key)
        if data is None:
            return None
        return Session.deserialize(json.loads(data))

    async def save(self, session: Session) -> None:
        key = self._session_key(session.session_id)
        data = json.dumps(session.serialize(), ensure_ascii=False)
        ttl = int((session.expires_at - datetime.now()).total_seconds())
        if ttl > 0:
            await self._redis.setex(key, ttl, data)

    async def refresh_ttl(self, session_id: str) -> None:
        key = self._session_key(session_id)
        await self._redis.expire(key, settings.session_ttl_seconds)

    async def delete(self, session_id: str) -> None:
        key = self._session_key(session_id)
        await self._redis.delete(key)

    @asynccontextmanager
    async def acquire_lock(self, session_id: str, ttl_s: int) -> AsyncIterator[LockHandle | None]:
        lock_key = self._lock_key(session_id)
        acquired = await self._redis.set(lock_key, "1", nx=True, ex=ttl_s)
        if not acquired:
            yield None
            return

        try:
            yield _RedisLockHandle(self._redis, lock_key)
        finally:
            # context manager 退出时 __aexit__ 已经释放了锁，这里不重复删除
            pass


# ── 模块级单例 ────────────────────────────────────────────────────────────────

_store_instance: SessionStore | None = None
_redis_client: AsyncRedis | None = None


def init_session_store(
    backend: str = "memory",
    redis_url: str | None = None,
    redis_client: AsyncRedis | None = None,
) -> None:
    """初始化存储后端（在 main.py lifespan 中调用）"""
    global _store_instance, _redis_client
    if backend == "memory":
        _store_instance = MemorySessionStore()
    elif backend == "redis":
        if redis_client is not None:
            _redis_client = redis_client
        elif redis_url is not None:
            from redis.asyncio import from_url
            _redis_client = from_url(redis_url, decode_responses=True)
        else:
            raise ValueError("redis_url or redis_client is required for redis backend")
        _store_instance = RedisSessionStore(
            redis=_redis_client,
            prefix=settings.redis_session_prefix,
            lock_prefix=settings.redis_lock_prefix,
        )
    else:
        raise ValueError(f"Unsupported storage backend: {backend}")


def get_session_store() -> SessionStore:
    """获取当前存储实例"""
    if _store_instance is None:
        raise RuntimeError("SessionStore not initialized. Call init_session_store() first.")
    return _store_instance


# ── 便捷函数（保持与旧 state.py 相同的签名）─────────────────────────────────

async def create_session(client_id: str) -> Session:
    now = datetime.now()
    session = Session(
        session_id=f"sess_{uuid4().hex[:12]}",
        client_id=client_id,
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
    )
    await get_session_store().create(session)
    return session


async def get_session(session_id: str) -> Session | None:
    return await get_session_store().get(session_id)


async def refresh_session(session: Session) -> None:
    await get_session_store().refresh_ttl(session.session_id)
