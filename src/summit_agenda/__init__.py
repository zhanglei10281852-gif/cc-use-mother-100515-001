"""峰会议题协同后端。

公开入口：
- SummitService / EventStore：领域服务与事件存储
- Actor / Role / Secrecy：身份与保密级别
- create_server：HTTP 接口
"""
from .domain import Actor, DomainError, Role, Secrecy, TopicStatus
from .service import SummitService
from .store import EventStore

__all__ = [
    "Actor",
    "DomainError",
    "EventStore",
    "Role",
    "Secrecy",
    "SummitService",
    "TopicStatus",
]
