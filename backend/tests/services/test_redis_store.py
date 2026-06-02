"""Redis 存储后端测试（使用 fakeredis）"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fakeredis import aioredis

from app.agent.state import AgentState, Session, TicketDraft
from app.services.session_store import RedisSessionStore
from app.services.ticket_counter import RedisTicketCounter


@pytest.fixture
async def redis_client():
    """创建 fakeredis 客户端"""
    client = aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def redis_store(redis_client):
    """创建 Redis session store"""
    return RedisSessionStore(
        redis=redis_client,
        prefix="test:session:",
        lock_prefix="test:lock:",
    )


@pytest.fixture
def redis_counter(redis_client):
    """创建 Redis ticket counter"""
    return RedisTicketCounter(
        redis=redis_client,
        key="test:repair_no",
        seed=1000,
    )


@pytest.mark.asyncio
async def test_session_create_and_get(redis_store):
    """测试会话创建和读取"""
    now = datetime.now()
    session = Session(
        session_id="sess_test123",
        client_id="client_001",
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )

    await redis_store.create(session)
    retrieved = await redis_store.get("sess_test123")

    assert retrieved is not None
    assert retrieved.session_id == "sess_test123"
    assert retrieved.client_id == "client_001"
    assert retrieved.state == AgentState.GREETING


@pytest.mark.asyncio
async def test_session_save_and_update(redis_store):
    """测试会话保存和更新"""
    now = datetime.now()
    session = Session(
        session_id="sess_test456",
        client_id="client_002",
        state=AgentState.COLLECTING,
        history=[{"role": "user", "content": "test"}],
        draft=TicketDraft(description="空调故障"),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )

    await redis_store.create(session)

    # 修改状态
    session.state = AgentState.CONFIRMING
    session.draft.estate = "园区A"
    await redis_store.save(session)

    retrieved = await redis_store.get("sess_test456")
    assert retrieved.state == AgentState.CONFIRMING
    assert retrieved.draft.estate == "园区A"


@pytest.mark.asyncio
async def test_session_ttl_refresh(redis_store):
    """测试 TTL 刷新"""
    now = datetime.now()
    session = Session(
        session_id="sess_test789",
        client_id="client_003",
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )

    await redis_store.create(session)
    await redis_store.refresh_ttl("sess_test789")

    # 验证可以读取（TTL 被刷新）
    retrieved = await redis_store.get("sess_test789")
    assert retrieved is not None


@pytest.mark.asyncio
async def test_session_delete(redis_store):
    """测试会话删除"""
    now = datetime.now()
    session = Session(
        session_id="sess_del",
        client_id="client_004",
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )

    await redis_store.create(session)
    await redis_store.delete("sess_del")

    retrieved = await redis_store.get("sess_del")
    assert retrieved is None


@pytest.mark.asyncio
async def test_lock_exclusive_access(redis_store):
    """测试锁互斥（两个任务同时 acquire，只有一个拿到）"""
    now = datetime.now()
    session = Session(
        session_id="sess_lock",
        client_id="client_005",
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )
    await redis_store.create(session)

    lock_results = []

    async def try_lock(name: str):
        async with redis_store.acquire_lock("sess_lock", ttl_s=5) as lock:
            if lock is not None:
                lock_results.append(f"{name}_acquired")
                await asyncio.sleep(0.1)  # 模拟持锁操作
            else:
                lock_results.append(f"{name}_failed")

    # 并发尝试获取锁
    await asyncio.gather(
        try_lock("task1"),
        try_lock("task2"),
    )

    # 断言只有一个拿到锁
    acquired = [r for r in lock_results if "acquired" in r]
    failed = [r for r in lock_results if "failed" in r]
    assert len(acquired) == 1
    assert len(failed) == 1


@pytest.mark.asyncio
async def test_lock_release(redis_store):
    """测试锁释放后可以再次获取"""
    now = datetime.now()
    session = Session(
        session_id="sess_lock2",
        client_id="client_006",
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )
    await redis_store.create(session)

    # 第一次获取锁
    async with redis_store.acquire_lock("sess_lock2", ttl_s=5) as lock1:
        assert lock1 is not None

    # 锁释放后再次获取
    async with redis_store.acquire_lock("sess_lock2", ttl_s=5) as lock2:
        assert lock2 is not None


@pytest.mark.asyncio
async def test_counter_increment(redis_counter):
    """测试计数器递增"""
    n1 = await redis_counter.next()
    n2 = await redis_counter.next()
    n3 = await redis_counter.next()

    # 断言递增且不重复
    assert n1 == 1000
    assert n2 == 1001
    assert n3 == 1002


@pytest.mark.asyncio
async def test_counter_concurrent_unique(redis_counter):
    """测试并发场景下计数器不重复"""
    results = await asyncio.gather(*[redis_counter.next() for _ in range(100)])

    # 断言 100 个号全部唯一
    assert len(results) == 100
    assert len(set(results)) == 100

    # 断言是连续的
    assert min(results) == 1000
    assert max(results) == 1099


@pytest.mark.asyncio
async def test_counter_seed_persistence(redis_client):
    """测试计数器种子只初始化一次"""
    counter1 = RedisTicketCounter(redis=redis_client, key="test:seed", seed=5000)
    n1 = await counter1.next()
    assert n1 == 5000

    # 创建新实例，种子不会覆盖
    counter2 = RedisTicketCounter(redis=redis_client, key="test:seed", seed=9999)
    n2 = await counter2.next()
    assert n2 == 5001  # 继续递增，不是 9999
