"""事件定义与规范化序列化。

所有状态变更都以事件形式追加到事件日志。每条事件带链式哈希：
hash = sha256(canonical({seq, ts, event, actor, idem, payload, result, prev_hash}))
任何对历史事件的篡改都会破坏链条，启动校验时即可发现。
"""
from __future__ import annotations

from hashlib import sha256
import json


GENESIS_HASH = "0" * 64


def canonical(obj) -> str:
    """规范化 JSON：键排序、无空白、UTF-8 不转义，保证摘要是稳定的。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(obj) -> str:
    return sha256(canonical(obj).encode("utf-8")).hexdigest()


def make_event(
    seq: int,
    ts: str,
    event: str,
    actor: str,
    payload: dict,
    prev_hash: str,
    idem: str | None = None,
    result: dict | None = None,
) -> dict:
    """构造一条带链式哈希的事件记录（不可变 dict，只增不改）。"""
    body = {
        "seq": seq,
        "ts": ts,
        "event": event,
        "actor": actor,
        "idem": idem,
        "payload": payload,
        "result": result or {},
        "prev_hash": prev_hash,
    }
    body["hash"] = digest_of(body)
    return body


def verify_chain(events: list[dict]) -> None:
    """校验事件哈希链。发现篡改时抛出 ValueError。"""
    prev = GENESIS_HASH
    for ev in events:
        if ev.get("prev_hash") != prev:
            raise ValueError(
                f"事件链断裂: seq={ev.get('seq')} prev_hash 不匹配，日志可能被篡改"
            )
        body = {k: v for k, v in ev.items() if k != "hash"}
        if digest_of(body) != ev.get("hash"):
            raise ValueError(f"事件哈希不匹配: seq={ev.get('seq')}，日志可能被篡改")
        prev = ev["hash"]


# ---------------------------------------------------------------------------
# 事件类型常量
# ---------------------------------------------------------------------------

# 命令类事件（由操作者触发）
EV_PROPOSAL_SUBMITTED = "proposal_submitted"
EV_PROPOSAL_WITHDRAWN = "proposal_withdrawn"
EV_DEADLINE_REVISED = "deadline_revised"
EV_TOPICS_MERGED = "topics_merged"
EV_TOPIC_DEFERRED = "topic_deferred"
EV_TOPIC_TRANSFERRED = "topic_transferred"
EV_APPROVAL_REQUESTED = "approval_requested"
EV_APPROVAL_DECIDED = "approval_decided"
EV_AGENDA_PUBLISHED = "agenda_published"

# 系统类事件（由领域规则自动产生，用于解释与审计）
EV_CONFLICT_FLAGGED = "conflict_flagged"
EV_TOPIC_STATUS_CHANGED = "topic_status_changed"

ALL_EVENTS = frozenset({
    EV_PROPOSAL_SUBMITTED,
    EV_PROPOSAL_WITHDRAWN,
    EV_DEADLINE_REVISED,
    EV_TOPICS_MERGED,
    EV_TOPIC_DEFERRED,
    EV_TOPIC_TRANSFERRED,
    EV_APPROVAL_REQUESTED,
    EV_APPROVAL_DECIDED,
    EV_AGENDA_PUBLISHED,
    EV_CONFLICT_FLAGGED,
    EV_TOPIC_STATUS_CHANGED,
})
