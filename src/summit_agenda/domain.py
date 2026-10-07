"""峰会议题协同 —— 领域模型。

本模块只包含纯领域定义：枚举、错误、规范化函数与不可变值对象。
不依赖任何外部服务，也不依赖本包的其他模块（除常量外）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
import re
import unicodedata


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class DomainError(Exception):
    """领域错误。code 用于 API 映射 HTTP 状态码与 CLI 退出码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# 保密级别与角色
# ---------------------------------------------------------------------------

class Secrecy(IntEnum):
    """材料保密级别，数值越大越敏感。"""

    PUBLIC = 0        # 公开
    INTERNAL = 1      # 内部
    CONFIDENTIAL = 2  # 机密
    SECRET = 3        # 绝密

    @classmethod
    def parse(cls, value: "str | int | Secrecy") -> "Secrecy":
        if isinstance(value, Secrecy):
            return value
        if isinstance(value, int):
            return cls(value)
        key = str(value).strip().lower()
        aliases = {
            "public": cls.PUBLIC, "公开": cls.PUBLIC,
            "internal": cls.INTERNAL, "内部": cls.INTERNAL,
            "confidential": cls.CONFIDENTIAL, "机密": cls.CONFIDENTIAL,
            "secret": cls.SECRET, "绝密": cls.SECRET,
        }
        if key in aliases:
            return aliases[key]
        if key.isdigit():
            return cls(int(key))
        raise DomainError("invalid_secrecy", f"未知保密级别: {value!r}")


class Role(str, Enum):
    """系统角色。命令权限与摘要可见性都以此为准。"""

    SUBMITTER = "submitter"      # 机构提交人
    COORDINATOR = "coordinator"  # 会务协调员（归并/延期/移交/发起审批/发布议程）
    APPROVER = "approver"        # 审批人
    AUDITOR = "auditor"          # 审计员（只读）

    @classmethod
    def parse(cls, value: "str | Role") -> "Role":
        if isinstance(value, Role):
            return value
        key = str(value).strip().lower()
        aliases = {
            "submitter": cls.SUBMITTER, "提交人": cls.SUBMITTER,
            "coordinator": cls.COORDINATOR, "协调员": cls.COORDINATOR, "会务": cls.COORDINATOR,
            "approver": cls.APPROVER, "审批人": cls.APPROVER,
            "auditor": cls.AUDITOR, "审计": cls.AUDITOR,
        }
        if key in aliases:
            return aliases[key]
        raise DomainError("invalid_role", f"未知角色: {value!r}")


#: 各角色对应的保密 clearance：只能看到不超过该级别的材料摘要。
ROLE_CLEARANCE: dict[Role, Secrecy] = {
    Role.SUBMITTER: Secrecy.INTERNAL,
    Role.COORDINATOR: Secrecy.CONFIDENTIAL,
    Role.APPROVER: Secrecy.SECRET,
    Role.AUDITOR: Secrecy.INTERNAL,
}


# ---------------------------------------------------------------------------
# 议题状态机
# ---------------------------------------------------------------------------

class TopicStatus(str, Enum):
    OPEN = "open"                          # 已受理，待归并/审批
    PENDING_APPROVAL = "pending_approval"  # 已发起审批，等待审批人决定
    APPROVED = "approved"                  # 审批通过，可进入议程
    REJECTED = "rejected"                  # 审批拒绝
    MERGED = "merged"                      # 已归并到其他议题
    DEFERRED = "deferred"                  # 已延期
    WITHDRAWN = "withdrawn"                # 全部提案被撤回


#: 允许发起审批的状态
APPROVABLE_STATES = frozenset({TopicStatus.OPEN, TopicStatus.DEFERRED})
#: 议题已终结（不可再变更）的状态
TERMINAL_STATES = frozenset({TopicStatus.MERGED, TopicStatus.REJECTED, TopicStatus.WITHDRAWN})


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """议题标题规范化：去空白、全半角归一、小写，用于判重。"""
    text = unicodedata.normalize("NFKC", str(title))
    return _WS_RE.sub("", text).lower()


def topic_key_of(title: str) -> str:
    """议题判重键。相同 key 的提案视为同一议题。"""
    key = normalize_title(title)
    if not key:
        raise DomainError("invalid_title", "议题标题不能为空")
    return key


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_deadline(value: str) -> str:
    """截止时间一律使用 ISO 日期（YYYY-MM-DD）。"""
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise DomainError("invalid_deadline", f"截止时间须为 YYYY-MM-DD 格式: {value!r}")
    # 校验日期真实存在
    from datetime import date

    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise DomainError("invalid_deadline", f"非法日期: {value!r}") from exc
    return text


# ---------------------------------------------------------------------------
# 不可变值对象（对外读模型）
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Actor:
    """操作者身份。命令与查询都显式携带，便于审计与鉴权。"""

    actor_id: str
    role: Role
    org: str = ""

    @classmethod
    def of(cls, actor_id: str, role: "str | Role", org: str = "") -> "Actor":
        if not actor_id:
            raise DomainError("invalid_actor", "缺少操作者标识")
        return cls(actor_id=str(actor_id), role=Role.parse(role), org=str(org or ""))


@dataclass(frozen=True, slots=True)
class RevisionLink:
    """截止时间修订链中的一环。digest 由上一环 digest 链式推出，篡改可检测。"""

    revision_no: int
    proposal_id: str
    previous_deadline: str
    new_deadline: str
    reason: str
    actor: str
    ts: str
    prev_digest: str
    digest: str


@dataclass(frozen=True, slots=True)
class AgendaView:
    """议程版本的只读视图。议程一旦发布即不可改写。"""

    version: int
    digest: str
    prev_digest: str
    published_at: str
    published_by: str
    entries: tuple[dict, ...] = field(default_factory=tuple)
