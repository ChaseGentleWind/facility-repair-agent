"""Session 序列化测试（验证 round-trip 一致性）"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agent.state import AgentState, Session, TicketDraft


def test_ticket_draft_serialization():
    """测试 TicketDraft 序列化和反序列化"""
    draft = TicketDraft(
        description="空调不制冷",
        estate="园区A",
        building="T1",
        floor="3楼",
        area="办公区",
        room="301",
        visit_time="6月2日 14时30分",
        image_urls=["http://example.com/img1.jpg", "http://example.com/img2.jpg"],
        normalized_description="空调制冷效果不佳",
        fault_type_code="100",
        fault_type_name="空调报修类",
        repair_priority_rag="HIGH",
        repair_type="公司报修",
    )

    # 序列化
    data = draft.serialize()

    # 反序列化
    restored = TicketDraft.deserialize(data)

    # 断言所有字段相等
    assert restored.description == draft.description
    assert restored.estate == draft.estate
    assert restored.building == draft.building
    assert restored.floor == draft.floor
    assert restored.area == draft.area
    assert restored.room == draft.room
    assert restored.visit_time == draft.visit_time
    assert restored.image_urls == draft.image_urls
    assert restored.normalized_description == draft.normalized_description
    assert restored.fault_type_code == draft.fault_type_code
    assert restored.fault_type_name == draft.fault_type_name
    assert restored.repair_priority_rag == draft.repair_priority_rag
    assert restored.repair_type == draft.repair_type


def test_ticket_draft_partial_serialization():
    """测试 TicketDraft 部分字段序列化"""
    draft = TicketDraft(
        description="水龙头漏水",
        estate="园区B",
    )

    data = draft.serialize()
    restored = TicketDraft.deserialize(data)

    assert restored.description == "水龙头漏水"
    assert restored.estate == "园区B"
    assert restored.building is None
    assert restored.floor is None
    assert restored.image_urls == []


def test_session_basic_serialization():
    """测试 Session 基本序列化"""
    now = datetime.now()
    session = Session(
        session_id="sess_test",
        client_id="client_001",
        state=AgentState.COLLECTING,
        history=[
            {"role": "user", "content": "空调坏了"},
            {"role": "assistant", "content": "请问您在哪个园区？"},
        ],
        draft=TicketDraft(description="空调故障"),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )

    # 序列化
    data = session.serialize()

    # 反序列化
    restored = Session.deserialize(data)

    # 断言核心字段
    assert restored.session_id == session.session_id
    assert restored.client_id == session.client_id
    assert restored.state == session.state
    assert restored.history == session.history
    assert restored.draft.description == session.draft.description
    assert restored.created_at == session.created_at
    assert restored.expires_at == session.expires_at


def test_session_complex_serialization():
    """测试 Session 复杂状态序列化（包含所有可选字段）"""
    now = datetime.now()
    session = Session(
        session_id="sess_complex",
        client_id="client_002",
        state=AgentState.PREVIEW_READY,
        history=[
            {"role": "user", "content": "空调不制冷"},
            {"role": "assistant", "content": "请问您在哪个园区？"},
            {"role": "user", "content": "园区A T1 3楼"},
        ],
        draft=TicketDraft(
            description="空调不制冷",
            estate="园区A",
            building="T1",
            floor="3楼",
            area="办公区",
            room="301",
            visit_time="6月2日 14时30分",
            image_urls=["http://example.com/img.jpg"],
            normalized_description="空调制冷效果不佳",
            fault_type_code="100",
            fault_type_name="空调报修类",
            repair_priority_rag="HIGH",
            repair_type="公司报修",
        ),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
        stall_count=2,
        last_missing=["visit_time"],
        image_description="空调外机有明显漏水痕迹",
        user_confirmed_description_priority=True,
        pending_clarification={
            "question": "请问具体是哪个时间段不制冷？",
            "asked_in_state": "COLLECTING",
        },
        ticket={
            "ticket_id": "1234567890",
            "repair_no": 1726200,
            "order_status": "PREVIEW",
            "location": {"estate": "园区A", "building": "T1", "floor": "3楼"},
        },
    )

    # 序列化
    data = session.serialize()

    # 反序列化
    restored = Session.deserialize(data)

    # 断言所有字段
    assert restored.session_id == session.session_id
    assert restored.client_id == session.client_id
    assert restored.state == session.state
    assert restored.history == session.history
    assert restored.stall_count == session.stall_count
    assert restored.last_missing == session.last_missing
    assert restored.image_description == session.image_description
    assert restored.user_confirmed_description_priority == session.user_confirmed_description_priority
    assert restored.pending_clarification == session.pending_clarification
    assert restored.ticket == session.ticket

    # 断言 draft 字段
    assert restored.draft.description == session.draft.description
    assert restored.draft.estate == session.draft.estate
    assert restored.draft.building == session.draft.building
    assert restored.draft.floor == session.draft.floor
    assert restored.draft.area == session.draft.area
    assert restored.draft.room == session.draft.room
    assert restored.draft.visit_time == session.draft.visit_time
    assert restored.draft.image_urls == session.draft.image_urls
    assert restored.draft.normalized_description == session.draft.normalized_description
    assert restored.draft.fault_type_code == session.draft.fault_type_code
    assert restored.draft.fault_type_name == session.draft.fault_type_name
    assert restored.draft.repair_priority_rag == session.draft.repair_priority_rag
    assert restored.draft.repair_type == session.draft.repair_type

    # 断言 datetime 精度（ISO 格式 round-trip）
    assert restored.created_at.replace(microsecond=0) == session.created_at.replace(microsecond=0)
    assert restored.expires_at.replace(microsecond=0) == session.expires_at.replace(microsecond=0)


def test_session_enum_serialization():
    """测试 AgentState 枚举序列化"""
    states = [
        AgentState.GREETING,
        AgentState.COLLECTING,
        AgentState.WAITING_IMAGE,
        AgentState.CONFIRMING,
        AgentState.PREVIEW_READY,
        AgentState.SUBMITTED,
        AgentState.COMPLETED,
        AgentState.ESCALATED,
    ]

    for state in states:
        now = datetime.now()
        session = Session(
            session_id="sess_enum",
            client_id="client_003",
            state=state,
            history=[],
            draft=TicketDraft(),
            created_at=now,
            expires_at=now + timedelta(seconds=1800),
        )

        data = session.serialize()
        restored = Session.deserialize(data)

        assert restored.state == state


def test_session_empty_draft_serialization():
    """测试空 draft 序列化"""
    now = datetime.now()
    session = Session(
        session_id="sess_empty",
        client_id="client_004",
        state=AgentState.GREETING,
        history=[],
        draft=TicketDraft(),
        created_at=now,
        expires_at=now + timedelta(seconds=1800),
    )

    data = session.serialize()
    restored = Session.deserialize(data)

    assert restored.draft.description is None
    assert restored.draft.estate is None
    assert restored.draft.image_urls == []


def test_ticket_draft_deserialize_missing_keys():
    """测试反序列化时缺失字段有默认值"""
    data = {
        "description": "空调坏了",
        "estate": "园区A",
        # 其他字段全部缺失
    }

    draft = TicketDraft.deserialize(data)

    assert draft.description == "空调坏了"
    assert draft.estate == "园区A"
    assert draft.building is None
    assert draft.floor is None
    assert draft.area is None
    assert draft.room is None
    assert draft.visit_time is None
    assert draft.image_urls == []
    assert draft.normalized_description is None
    assert draft.fault_type_code is None
    assert draft.fault_type_name is None
    assert draft.repair_priority_rag is None
    assert draft.repair_type is None
