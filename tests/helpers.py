"""测试公共辅助。"""
from __future__ import annotations

import itertools

from summit_agenda import Actor, EventStore, SummitService

FIXED_TS = "2026-10-07T09:00:00+08:00"

COORD = Actor.of("li-hui", "coordinator")
APPROVER = Actor.of("chen-shen", "approver")
AUDITOR = Actor.of("sun-jian", "auditor")
SUB_A = Actor.of("wang-ke", "submitter", org="机构A")
SUB_B = Actor.of("zhao-min", "submitter", org="机构B")

_counter = itertools.count(1)


def make_service() -> SummitService:
    """内存事件库 + 固定时钟的干净服务。"""
    return SummitService(EventStore(":memory:"), clock=lambda: FIXED_TS)


def idem(prefix: str = "k") -> str:
    return f"{prefix}-{next(_counter)}"


def submit(service, actor=SUB_A, title="数字丝路合作", org="机构A",
           workgroup="甲组", owner="张三", deadline="2026-11-15",
           secrecy="internal", depends_on=None, summary="摘要", materials=None):
    return service.submit_proposal(
        actor, idem=idem("sub"), title=title, summary=summary, org=org,
        workgroup=workgroup, owner=owner, deadline=deadline, secrecy=secrecy,
        depends_on=depends_on or [], materials=materials or [])
