"""应用核心：命令处理、事件溯源投影、查询与解释。

设计要点：
- 一切状态变更先落事件日志（含幂等键与命令结果），再应用到内存投影；
  重启后重放日志即可恢复，包括未完成的审批。
- 提交/撤回幂等：幂等键命中直接返回首个事件记录的结果；同一机构对同一
  议题的重复提交、对已撤回提案的再次撤回，都是无副作用的幂等返回。
- 议程版本只增不改：每次发布生成带链式摘要的新版本，历史版本可重放、可校验。
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading

from .domain import (
    Actor,
    APPROVABLE_STATES,
    DomainError,
    RevisionLink,
    Role,
    Secrecy,
    TERMINAL_STATES,
    TopicStatus,
    topic_key_of,
    validate_deadline,
)
from .events import (
    EV_AGENDA_PUBLISHED,
    EV_APPROVAL_DECIDED,
    EV_APPROVAL_REQUESTED,
    EV_CONFLICT_FLAGGED,
    EV_DEADLINE_REVISED,
    EV_PROPOSAL_SUBMITTED,
    EV_PROPOSAL_WITHDRAWN,
    EV_TOPIC_DEFERRED,
    EV_TOPIC_STATUS_CHANGED,
    EV_TOPIC_TRANSFERRED,
    EV_TOPICS_MERGED,
    GENESIS_HASH,
    digest_of,
)
from .rbac import redact_summary, require_command
from .store import EventStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 投影：由事件流重建的内存状态
# ---------------------------------------------------------------------------

class Projection:
    def __init__(self) -> None:
        self.proposals: dict[str, dict] = {}
        self.topics: dict[str, dict] = {}
        self.topic_keys: dict[str, str] = {}       # topic_key -> topic_id
        self.approvals: dict[str, dict] = {}
        self.agendas: list[dict] = []
        self.idempotency: dict[str, dict] = {}     # idem -> {"seq", "result"}
        self.revisions: dict[str, list[dict]] = {} # proposal_id -> [RevisionLink dict]
        self.conflicts: list[dict] = []
        self.counters: dict[str, int] = {"P": 0, "T": 0, "AP": 0}

    # -- id 生成（重放时由事件中的 id 回填，保证重启后编号连续） --

    def bump(self, prefix: str, value: int) -> None:
        self.counters[prefix] = max(self.counters[prefix], value)

    def next_id(self, prefix: str) -> str:
        self.counters[prefix] += 1
        return f"{prefix}-{self.counters[prefix]}"


def _split_id(raw: str) -> tuple[str, int] | None:
    head, _, tail = raw.rpartition("-")
    if head and tail.isdigit():
        return head, int(tail)
    return None


def _apply(proj: Projection, ev: dict) -> None:
    """把一条事件应用到投影。必须确定性：同样的日志重放出同样的状态。"""
    p = ev["payload"]
    kind = ev["event"]

    if ev.get("idem"):
        proj.idempotency[ev["idem"]] = {"seq": ev["seq"], "result": ev.get("result", {})}

    if kind == EV_PROPOSAL_SUBMITTED:
        pid = p["proposal_id"]
        tid = p["topic_id"]
        proj.proposals[pid] = {
            "proposal_id": pid,
            "topic_id": tid,
            "topic_key": p["topic_key"],
            "title": p["title"],
            "summary": p["summary"],
            "org": p["org"],
            "workgroup": p["workgroup"],
            "owner": p["owner"],
            "deadline": p["deadline"],
            "secrecy": p["secrecy"],
            "depends_on": list(p.get("depends_on", [])),
            "materials": list(p.get("materials", [])),
            "withdrawn": False,
            "created_seq": ev["seq"],
            "created_ts": ev["ts"],
        }
        for raw in (pid, tid):
            parsed = _split_id(raw)
            if parsed:
                proj.bump(parsed[0], parsed[1])
        topic = proj.topics.get(tid)
        if topic is None:
            topic = {
                "topic_id": tid,
                "topic_key": p["topic_key"],
                "title": p["title"],
                "status": TopicStatus.OPEN.value,
                "org": p["org"],
                "workgroup": p["workgroup"],
                "proposal_ids": [],
                "merged_into": None,
                "deferred_until": None,
                "created_seq": ev["seq"],
            }
            proj.topics[tid] = topic
            proj.topic_keys[p["topic_key"]] = tid
        if pid not in topic["proposal_ids"]:
            topic["proposal_ids"].append(pid)

    elif kind == EV_PROPOSAL_WITHDRAWN:
        prop = proj.proposals[p["proposal_id"]]
        prop["withdrawn"] = True

    elif kind == EV_DEADLINE_REVISED:
        prop = proj.proposals[p["proposal_id"]]
        prop["deadline"] = p["new_deadline"]
        proj.revisions.setdefault(p["proposal_id"], []).append({
            "revision_no": p["revision_no"],
            "proposal_id": p["proposal_id"],
            "previous_deadline": p["previous_deadline"],
            "new_deadline": p["new_deadline"],
            "reason": p["reason"],
            "actor": ev["actor"],
            "ts": ev["ts"],
            "prev_digest": p["prev_digest"],
            "digest": p["digest"],
        })

    elif kind == EV_CONFLICT_FLAGGED:
        proj.conflicts.append({
            "seq": ev["seq"], "ts": ev["ts"],
            "kind": p["kind"], "topic_id": p.get("topic_id"),
            "detail": p["detail"], "related": list(p.get("related", [])),
        })

    elif kind == EV_TOPICS_MERGED:
        survivor = proj.topics[p["survivor"]]
        for mid in p["merged"]:
            topic = proj.topics[mid]
            topic["status"] = TopicStatus.MERGED.value
            topic["merged_into"] = p["survivor"]
            for pid in list(topic["proposal_ids"]):
                proj.proposals[pid]["topic_id"] = p["survivor"]
                if pid not in survivor["proposal_ids"]:
                    survivor["proposal_ids"].append(pid)
            topic["proposal_ids"] = []
            # 被归并议题的判重键指向存活议题，后续提交直接归并
            proj.topic_keys[topic["topic_key"]] = p["survivor"]

    elif kind == EV_TOPIC_DEFERRED:
        topic = proj.topics[p["topic_id"]]
        topic["status"] = TopicStatus.DEFERRED.value
        topic["deferred_until"] = p["until"]

    elif kind == EV_TOPIC_TRANSFERRED:
        topic = proj.topics[p["topic_id"]]
        topic["workgroup"] = p["to_workgroup"]
        topic["org"] = p["to_org"]

    elif kind == EV_TOPIC_STATUS_CHANGED:
        topic = proj.topics[p["topic_id"]]
        topic["status"] = p["to"]
        if p["to"] != TopicStatus.DEFERRED.value:
            topic["deferred_until"] = None

    elif kind == EV_APPROVAL_REQUESTED:
        aid = p["approval_id"]
        parsed = _split_id(aid)
        if parsed:
            proj.bump(parsed[0], parsed[1])
        proj.approvals[aid] = {
            "approval_id": aid,
            "topic_id": p["topic_id"],
            "status": "pending",
            "requested_by": ev["actor"],
            "requested_ts": ev["ts"],
            "decided_by": None,
            "decision": None,
            "decision_reason": None,
            "decided_ts": None,
        }
        proj.topics[p["topic_id"]]["status"] = TopicStatus.PENDING_APPROVAL.value
        proj.topics[p["topic_id"]]["deferred_until"] = None

    elif kind == EV_APPROVAL_DECIDED:
        ap = proj.approvals[p["approval_id"]]
        ap["status"] = p["decision"]
        ap["decided_by"] = ev["actor"]
        ap["decision"] = p["decision"]
        ap["decision_reason"] = p["reason"]
        ap["decided_ts"] = ev["ts"]
        topic = proj.topics[ap["topic_id"]]
        topic["status"] = (
            TopicStatus.APPROVED.value if p["decision"] == "approved"
            else TopicStatus.REJECTED.value
        )

    elif kind == EV_AGENDA_PUBLISHED:
        proj.agendas.append({
            "version": p["version"],
            "digest": p["digest"],
            "prev_digest": p["prev_digest"],
            "published_at": ev["ts"],
            "published_by": ev["actor"],
            "entries": list(p["entries"]),
            "seq": ev["seq"],
        })


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class SummitService:
    """峰会议题协同服务。所有命令与查询的入口。"""

    def __init__(self, store: EventStore, clock=_utc_now):
        self._store = store
        self._clock = clock
        self._lock = threading.RLock()
        self._proj = Projection()
        self._events: list[dict] = []
        for ev in store.events():
            _apply(self._proj, ev)
            self._events.append(ev)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, data_dir: str, clock=_utc_now) -> "SummitService":
        """打开（或创建）数据目录并恢复全部状态。"""
        return cls(EventStore(f"{data_dir}/events.jsonl"), clock=clock)

    def _append(self, event: str, actor: Actor | str, payload: dict,
                idem: str | None = None, result: dict | None = None) -> dict:
        actor_id = actor.actor_id if isinstance(actor, Actor) else str(actor)
        ev = self._store.append(event, actor_id, payload, ts=self._clock(),
                                idem=idem, result=result)
        _apply(self._proj, ev)
        self._events.append(ev)
        return ev

    def _idem_lookup(self, idem: str | None) -> dict | None:
        if not idem:
            return None
        hit = self._proj.idempotency.get(idem)
        if hit is None:
            return None
        return {**hit["result"], "deduplicated": True}

    def _get_proposal(self, proposal_id: str) -> dict:
        prop = self._proj.proposals.get(proposal_id)
        if prop is None:
            raise DomainError("not_found", f"提案不存在: {proposal_id}")
        return prop

    def _get_topic(self, topic_id: str) -> dict:
        topic = self._proj.topics.get(topic_id)
        if topic is None:
            raise DomainError("not_found", f"议题不存在: {topic_id}")
        return topic

    @staticmethod
    def _active_proposals(topic: dict, proj: Projection) -> list[dict]:
        return [
            proj.proposals[pid] for pid in topic["proposal_ids"]
            if not proj.proposals[pid]["withdrawn"]
        ]

    def _resolve_dependency(self, ref: str) -> str | None:
        """把前置引用（议题号或标题）解析为议题号；无法解析返回 None。"""
        if ref in self._proj.topics:
            return ref
        try:
            key = topic_key_of(ref)
        except DomainError:
            return None
        return self._proj.topic_keys.get(key)

    def _topic_dependencies(self, topic: dict) -> list[tuple[str, str | None]]:
        """议题全部有效前置：[(原始引用, 解析后的议题号或 None)]。"""
        refs: list[str] = []
        for pid in topic["proposal_ids"]:
            prop = self._proj.proposals[pid]
            if prop["withdrawn"]:
                continue
            for ref in prop["depends_on"]:
                if ref not in refs:
                    refs.append(ref)
        return [(ref, self._resolve_dependency(ref)) for ref in refs]

    def _raw_dep_refs(self, topic: dict) -> list[str]:
        refs: list[str] = []
        for pid in topic["proposal_ids"]:
            prop = self._proj.proposals[pid]
            if not prop["withdrawn"]:
                refs.extend(prop["depends_on"])
        return refs

    def _current_graph(self, key_overlay: dict[str, str] | None = None,
                       edge_overlay: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
        """当前依赖图（跳过已归并/已关闭议题）。overlay 用于预演尚未落库的变更。"""
        key_overlay = key_overlay or {}
        edge_overlay = edge_overlay or {}

        def resolve(ref: str) -> str | None:
            if ref in key_overlay.values():
                return ref
            try:
                key = topic_key_of(ref)
            except DomainError:
                return None
            if key in key_overlay:
                return key_overlay[key]
            return self._resolve_dependency(ref)

        graph: dict[str, list[str]] = {}
        for tid, topic in self._proj.topics.items():
            if topic["status"] in (TopicStatus.MERGED.value, TopicStatus.WITHDRAWN.value):
                continue
            deps = [d for d in (resolve(r) for r in self._raw_dep_refs(topic)) if d]
            deps.extend(edge_overlay.get(tid, []))
            graph[tid] = deps
        for tid, deps in edge_overlay.items():
            if tid not in graph:
                graph[tid] = list(deps)
        return graph

    @staticmethod
    def _detect_cycle(graph: dict[str, list[str]]) -> bool:
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}

        def dfs(u: str) -> bool:
            color[u] = GRAY
            for v in graph.get(u, []):
                c = color.get(v, WHITE)
                if c == GRAY:
                    return True
                if c == WHITE and dfs(v):
                    return True
            color[u] = BLACK
            return False

        return any(color.get(u, WHITE) == WHITE and dfs(u) for u in list(graph))

    def _cycle_after_submit(self, tid: str, topic_key: str, new_deps: list[str]) -> bool:
        """预演本次提交落库后的完整依赖图，判断是否会成环（含自依赖）。"""
        resolved: list[str] = []
        for ref in new_deps:
            if ref == tid:
                return True
            try:
                key = topic_key_of(ref)
            except DomainError:
                continue
            if key == topic_key:
                return True  # 自依赖
            dep = self._resolve_dependency(ref)
            if dep:
                resolved.append(dep)
        graph = self._current_graph(
            key_overlay={topic_key: tid},
            edge_overlay={tid: resolved},
        )
        return self._detect_cycle(graph)

    def _would_cycle(self, topic_id: str) -> bool:
        """当前图中 topic_id 是否能沿前置关系回到自身（供冲突检查）。"""
        graph = self._current_graph()
        stack = list(graph.get(topic_id, []))
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur == topic_id:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(graph.get(cur, []))
        return False

    # ------------------------------------------------------------------
    # 命令：提交 / 撤回 / 修订
    # ------------------------------------------------------------------

    def submit_proposal(
        self,
        actor: Actor,
        *,
        idem: str,
        title: str,
        summary: str,
        org: str,
        workgroup: str,
        owner: str,
        deadline: str,
        secrecy: str = "public",
        depends_on: list[str] | None = None,
        materials: list[str] | None = None,
    ) -> dict:
        """提交议题提案。幂等：同一 idem 或同机构同议题重复提交不产生副作用。"""
        with self._lock:
            require_command(actor, "submit")
            if not idem:
                raise DomainError("invalid_idem", "提交必须携带幂等键 idem")
            if actor.role is Role.SUBMITTER:
                if not actor.org:
                    raise DomainError("forbidden", "提交人必须声明所属机构")
                if actor.org != org:
                    raise DomainError("forbidden", "提交人只能为本机构提交提案")

            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit

            deadline = validate_deadline(deadline)
            secrecy_v = Secrecy.parse(secrecy)
            topic_key = topic_key_of(title)
            depends_on = [str(d).strip() for d in (depends_on or []) if str(d).strip()]
            materials = [str(m) for m in (materials or [])]

            # 自然幂等：同机构对同一议题已有有效提案（且议题未终结）→ 返回原提案
            for prop in self._proj.proposals.values():
                if (prop["org"] == org and prop["topic_key"] == topic_key
                        and not prop["withdrawn"]):
                    host = self._proj.topics[prop["topic_id"]]
                    if host["status"] in (TopicStatus.WITHDRAWN.value,
                                          TopicStatus.REJECTED.value):
                        continue
                    return {
                        "proposal_id": prop["proposal_id"],
                        "topic_id": prop["topic_id"],
                        "title": prop["title"],
                        "duplicated": False,
                        "deduplicated": True,
                        "detail": "同一机构已提交过该议题，返回原提案",
                    }

            # 归并/关闭后的判重键修正
            existing_tid = self._proj.topic_keys.get(topic_key)
            if existing_tid:
                existing = self._proj.topics[existing_tid]
                if existing["status"] == TopicStatus.MERGED.value and existing["merged_into"]:
                    existing_tid = existing["merged_into"]
                    existing = self._proj.topics[existing_tid]
                if existing["status"] in (TopicStatus.WITHDRAWN.value,
                                          TopicStatus.REJECTED.value):
                    existing_tid = None  # 原议题已关闭/被拒绝，同题重新立项

            if existing_tid:
                tid = existing_tid
                duplicated = True
            else:
                tid = self._proj.next_id("T")
                duplicated = False
            # 依赖环检查：预演本次提交落库后的完整依赖图
            if depends_on and self._cycle_after_submit(tid, topic_key, depends_on):
                raise DomainError("dependency_cycle", "前置关系会构成依赖环，已拒绝")
            pid = self._proj.next_id("P")

            payload = {
                "proposal_id": pid, "topic_id": tid, "topic_key": topic_key,
                "title": str(title).strip(), "summary": str(summary),
                "org": org, "workgroup": workgroup, "owner": owner,
                "deadline": deadline, "secrecy": secrecy_v.name.lower(),
                "depends_on": depends_on, "materials": materials,
            }
            result = {
                "proposal_id": pid, "topic_id": tid,
                "title": payload["title"],
                "duplicated": duplicated, "deduplicated": False,
            }
            self._append(EV_PROPOSAL_SUBMITTED, actor, payload, idem=idem, result=result)

            # 冲突检测：同一议题被不同工作组重复承诺
            if duplicated:
                topic = self._proj.topics[tid]
                groups = {
                    self._proj.proposals[pid2]["workgroup"]
                    for pid2 in topic["proposal_ids"]
                    if not self._proj.proposals[pid2]["withdrawn"]
                }
                if len(groups) > 1 and not any(
                    c["kind"] == "duplicate_commit" and c["topic_id"] == tid
                    and pid in c["related"]
                    for c in self._proj.conflicts
                ):
                    self._append(EV_CONFLICT_FLAGGED, "system", {
                        "kind": "duplicate_commit", "topic_id": tid,
                        "detail": f"议题《{topic['title']}》被多个工作组重复承诺: "
                                  + "、".join(sorted(groups)),
                        "related": [pid],
                    })
            return result

    def withdraw_proposal(self, actor: Actor, proposal_id: str, *,
                          idem: str, reason: str = "") -> dict:
        """撤回提案。幂等：重复撤回返回相同结果，不产生新事件。"""
        with self._lock:
            require_command(actor, "withdraw")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit
            prop = self._get_proposal(proposal_id)
            if actor.role is Role.SUBMITTER and actor.org != prop["org"]:
                raise DomainError("forbidden", "只能撤回本机构的提案")
            if prop["withdrawn"]:
                return {"proposal_id": proposal_id, "withdrawn": True,
                        "already_withdrawn": True, "deduplicated": False}

            result = {"proposal_id": proposal_id, "withdrawn": True,
                      "already_withdrawn": False}
            self._append(EV_PROPOSAL_WITHDRAWN, actor,
                         {"proposal_id": proposal_id, "topic_id": prop["topic_id"],
                          "reason": str(reason)},
                         idem=idem, result=result)

            # 议题下所有提案都撤回 → 议题关闭
            topic = self._proj.topics[prop["topic_id"]]
            if not self._active_proposals(topic, self._proj) and topic["status"] not in (
                TopicStatus.WITHDRAWN.value, TopicStatus.MERGED.value,
            ):
                self._append(EV_TOPIC_STATUS_CHANGED, "system", {
                    "topic_id": topic["topic_id"],
                    "from": topic["status"],
                    "to": TopicStatus.WITHDRAWN.value,
                    "reason": "全部提案已撤回",
                })
            return result

    def revise_deadline(self, actor: Actor, proposal_id: str, *,
                        new_deadline: str, reason: str, idem: str) -> dict:
        """修订截止时间。每次修订在修订链上追加一环，链式摘要可校验。"""
        with self._lock:
            require_command(actor, "revise_deadline")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit
            prop = self._get_proposal(proposal_id)
            if actor.role is Role.SUBMITTER and actor.org != prop["org"]:
                raise DomainError("forbidden", "只能修订本机构提案的截止时间")
            if prop["withdrawn"]:
                raise DomainError("invalid_state", "提案已撤回，不能修订截止时间")
            topic = self._proj.topics[prop["topic_id"]]
            if topic["status"] in {s.value for s in TERMINAL_STATES}:
                raise DomainError("invalid_state", f"议题已终结（{topic['status']}），不能修订")

            new_deadline = validate_deadline(new_deadline)
            old_deadline = prop["deadline"]
            if new_deadline == old_deadline:
                chain = self._proj.revisions.get(proposal_id, [])
                return {"proposal_id": proposal_id, "deadline": old_deadline,
                        "revision_no": len(chain), "unchanged": True,
                        "deduplicated": False}

            chain = self._proj.revisions.get(proposal_id, [])
            revision_no = len(chain) + 1
            prev_digest = chain[-1]["digest"] if chain else GENESIS_HASH
            link_body = {
                "proposal_id": proposal_id, "revision_no": revision_no,
                "previous_deadline": old_deadline, "new_deadline": new_deadline,
                "reason": str(reason), "prev_digest": prev_digest,
            }
            digest = digest_of(link_body)
            payload = {**link_body, "topic_id": prop["topic_id"], "digest": digest}
            result = {"proposal_id": proposal_id, "deadline": new_deadline,
                      "revision_no": revision_no, "digest": digest,
                      "unchanged": False}
            self._append(EV_DEADLINE_REVISED, actor, payload, idem=idem, result=result)
            return result

    # ------------------------------------------------------------------
    # 命令：归并 / 延期 / 移交 / 审批 / 发布
    # ------------------------------------------------------------------

    def merge_topics(self, actor: Actor, *, survivor_id: str,
                     merged_ids: list[str], reason: str, idem: str) -> dict:
        """把若干议题归并到存活议题。被归并议题的提案移交存活议题。"""
        with self._lock:
            require_command(actor, "merge")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit
            survivor = self._get_topic(survivor_id)
            if survivor["status"] in {s.value for s in TERMINAL_STATES}:
                raise DomainError("invalid_state", f"存活议题已终结: {survivor_id}")
            merged_ids = list(dict.fromkeys(merged_ids))
            if survivor_id in merged_ids:
                raise DomainError("invalid_merge", "存活议题不能同时出现在被归并列表中")
            if not merged_ids:
                raise DomainError("invalid_merge", "被归并列表不能为空")
            for mid in merged_ids:
                topic = self._get_topic(mid)
                if topic["status"] in {s.value for s in TERMINAL_STATES}:
                    raise DomainError("invalid_state", f"议题已终结，不能归并: {mid}")

            payload = {"survivor": survivor_id, "merged": merged_ids,
                       "reason": str(reason)}
            result = {"survivor": survivor_id, "merged": merged_ids}
            self._append(EV_TOPICS_MERGED, actor, payload, idem=idem, result=result)
            return result

    def defer_topic(self, actor: Actor, topic_id: str, *,
                    until: str, reason: str, idem: str) -> dict:
        """延期议题到指定日期。"""
        with self._lock:
            require_command(actor, "defer")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit
            topic = self._get_topic(topic_id)
            if topic["status"] not in (TopicStatus.OPEN.value, TopicStatus.DEFERRED.value):
                raise DomainError("invalid_state",
                                  f"当前状态（{topic['status']}）不能延期")
            until = validate_deadline(until)
            payload = {"topic_id": topic_id, "until": until, "reason": str(reason)}
            result = {"topic_id": topic_id, "status": TopicStatus.DEFERRED.value,
                      "until": until}
            self._append(EV_TOPIC_DEFERRED, actor, payload, idem=idem, result=result)
            return result

    def transfer_topic(self, actor: Actor, topic_id: str, *,
                       to_workgroup: str, to_org: str | None = None,
                       reason: str = "", idem: str) -> dict:
        """把议题移交给另一个工作组（可跨机构）。"""
        with self._lock:
            require_command(actor, "transfer")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit
            topic = self._get_topic(topic_id)
            if topic["status"] in {s.value for s in TERMINAL_STATES}:
                raise DomainError("invalid_state", f"议题已终结（{topic['status']}），不能移交")
            payload = {
                "topic_id": topic_id,
                "from_workgroup": topic["workgroup"], "to_workgroup": to_workgroup,
                "from_org": topic["org"], "to_org": to_org or topic["org"],
                "reason": str(reason),
            }
            result = {"topic_id": topic_id, "workgroup": to_workgroup,
                      "org": payload["to_org"]}
            self._append(EV_TOPIC_TRANSFERRED, actor, payload, idem=idem, result=result)
            return result

    def request_approval(self, actor: Actor, topic_id: str, *, idem: str) -> dict:
        """发起审批。前置议题未全部通过时拒绝发起。"""
        with self._lock:
            require_command(actor, "request_approval")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit
            topic = self._get_topic(topic_id)
            if topic["status"] not in {s.value for s in APPROVABLE_STATES}:
                raise DomainError("invalid_state",
                                  f"当前状态（{topic['status']}）不能发起审批")
            for ap in self._proj.approvals.values():
                if ap["topic_id"] == topic_id and ap["status"] == "pending":
                    raise DomainError("invalid_state",
                                      f"议题已有待审批单 {ap['approval_id']}")

            unmet = []
            for ref, dep in self._topic_dependencies(topic):
                if dep is None:
                    unmet.append(f"{ref}(未知议题)")
                    continue
                dep_topic = self._proj.topics[dep]
                if dep_topic["status"] != TopicStatus.APPROVED.value:
                    unmet.append(f"{dep}({dep_topic['status']})")
            if unmet:
                raise DomainError(
                    "unmet_dependency",
                    "前置议题尚未全部审批通过: " + "、".join(unmet))

            aid = self._proj.next_id("AP")
            payload = {"approval_id": aid, "topic_id": topic_id}
            result = {"approval_id": aid, "topic_id": topic_id, "status": "pending"}
            self._append(EV_APPROVAL_REQUESTED, actor, payload, idem=idem, result=result)
            return result

    def decide_approval(self, actor: Actor, approval_id: str, *,
                        decision: str, reason: str = "", idem: str) -> dict:
        """审批人决定。已通过/已拒绝的审批单不可再改。"""
        with self._lock:
            require_command(actor, "decide")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit
            ap = self._proj.approvals.get(approval_id)
            if ap is None:
                raise DomainError("not_found", f"审批单不存在: {approval_id}")
            if ap["status"] != "pending":
                raise DomainError(
                    "already_decided",
                    f"审批单 {approval_id} 已{ap['status']}，不可更改")
            decision = str(decision).strip().lower()
            if decision not in ("approved", "rejected"):
                raise DomainError("invalid_decision", "decision 须为 approved 或 rejected")
            payload = {"approval_id": approval_id, "topic_id": ap["topic_id"],
                       "decision": decision, "reason": str(reason)}
            result = {"approval_id": approval_id, "topic_id": ap["topic_id"],
                      "decision": decision}
            self._append(EV_APPROVAL_DECIDED, actor, payload, idem=idem, result=result)
            return result

    def publish_agenda(self, actor: Actor, *, idem: str) -> dict:
        """发布议程版本：当前全部已通过议题的不可改写快照。

        版本摘要链式衔接上一版本；内容与上一版本完全相同时幂等返回，不产生新版本。
        """
        with self._lock:
            require_command(actor, "publish")
            hit = self._idem_lookup(idem)
            if hit is not None:
                return hit

            entries = []
            for tid in sorted(self._proj.topics):
                topic = self._proj.topics[tid]
                if topic["status"] != TopicStatus.APPROVED.value:
                    continue
                active = self._active_proposals(topic, self._proj)
                if not active:
                    continue
                primary = min(active, key=lambda p: p["created_seq"])
                entries.append({
                    "topic_id": tid,
                    "title": topic["title"],
                    "org": topic["org"],
                    "workgroup": topic["workgroup"],
                    "owner": primary["owner"],
                    "deadline": primary["deadline"],
                    "secrecy": Secrecy(max(Secrecy.parse(p["secrecy"]) for p in active)).name.lower(),
                    "proposal_ids": sorted(p["proposal_id"] for p in active),
                })

            last = self._proj.agendas[-1] if self._proj.agendas else None
            if last and last["entries"] == entries:
                return {"version": last["version"], "digest": last["digest"],
                        "unchanged": True, "deduplicated": False}

            version = (last["version"] + 1) if last else 1
            prev_digest = last["digest"] if last else GENESIS_HASH
            digest = digest_of({"version": version, "entries": entries,
                                "prev_digest": prev_digest})
            payload = {"version": version, "entries": entries,
                       "prev_digest": prev_digest, "digest": digest}
            result = {"version": version, "digest": digest,
                      "entries": len(entries), "unchanged": False}
            self._append(EV_AGENDA_PUBLISHED, actor, payload, idem=idem, result=result)
            return result

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _proposal_view(self, prop: dict) -> dict:
        return {
            "proposal_id": prop["proposal_id"],
            "topic_id": prop["topic_id"],
            "title": prop["title"],
            "summary": prop["summary"],
            "org": prop["org"],
            "workgroup": prop["workgroup"],
            "owner": prop["owner"],
            "deadline": prop["deadline"],
            "secrecy": prop["secrecy"],
            "depends_on": list(prop["depends_on"]),
            "materials": list(prop["materials"]),
            "withdrawn": prop["withdrawn"],
        }

    def list_proposals(self, actor: Actor, *, org: str | None = None) -> list[dict]:
        """提案列表，按操作者角色对材料摘要脱敏。"""
        views = []
        for prop in sorted(self._proj.proposals.values(),
                           key=lambda p: p["created_seq"]):
            if org and prop["org"] != org:
                continue
            views.append(redact_summary(self._proposal_view(prop), actor))
        return views

    def get_topic(self, actor: Actor, topic_id: str) -> dict:
        topic = self._get_topic(topic_id)
        proposals = [
            redact_summary(self._proposal_view(self._proj.proposals[pid]), actor)
            for pid in topic["proposal_ids"]
        ]
        return {
            "topic_id": topic_id,
            "title": topic["title"],
            "status": topic["status"],
            "org": topic["org"],
            "workgroup": topic["workgroup"],
            "merged_into": topic["merged_into"],
            "deferred_until": topic["deferred_until"],
            "dependencies": [
                {"ref": ref, "resolved": resolved}
                for ref, resolved in self._topic_dependencies(topic)
            ],
            "proposals": proposals,
        }

    def list_topics(self, actor: Actor, *, status: str | None = None) -> list[dict]:
        out = []
        for tid in sorted(self._proj.topics):
            topic = self._proj.topics[tid]
            if status and topic["status"] != status:
                continue
            out.append(self.get_topic(actor, tid))
        return out

    def pending_approvals(self, actor: Actor) -> list[dict]:
        """未完成的审批。重启后由事件重放恢复，不丢失。"""
        return [dict(ap) for ap in sorted(
            self._proj.approvals.values(), key=lambda a: a["requested_ts"])
            if ap["status"] == "pending"]

    def list_approvals(self, actor: Actor) -> list[dict]:
        return [dict(ap) for ap in sorted(
            self._proj.approvals.values(), key=lambda a: a["requested_ts"])]

    def revision_chain(self, actor: Actor, proposal_id: str) -> dict:
        """提案截止时间的修订链，附链式校验结果。"""
        self._get_proposal(proposal_id)
        chain = [RevisionLink(**link) for link in
                 self._proj.revisions.get(proposal_id, [])]
        valid = True
        prev = GENESIS_HASH
        for link in chain:
            body = {
                "proposal_id": link.proposal_id, "revision_no": link.revision_no,
                "previous_deadline": link.previous_deadline,
                "new_deadline": link.new_deadline, "reason": link.reason,
                "prev_digest": link.prev_digest,
            }
            if link.prev_digest != prev or digest_of(body) != link.digest:
                valid = False
                break
            prev = link.digest
        return {
            "proposal_id": proposal_id,
            "current_deadline": self._proj.proposals[proposal_id]["deadline"],
            "revisions": [{
                "revision_no": link.revision_no,
                "previous_deadline": link.previous_deadline,
                "new_deadline": link.new_deadline,
                "reason": link.reason,
                "actor": link.actor,
                "ts": link.ts,
                "prev_digest": link.prev_digest,
                "digest": link.digest,
            } for link in chain],
            "chain_valid": valid,
        }

    def check_conflicts(self, actor: Actor) -> list[dict]:
        """全量冲突检查：重复承诺、依赖环、前置未通过、截止时序倒置。"""
        findings: list[dict] = []
        # 1) 重复承诺（同一议题多个工作组的有效提案）
        for tid, topic in sorted(self._proj.topics.items()):
            if topic["status"] in (TopicStatus.MERGED.value, TopicStatus.WITHDRAWN.value):
                continue
            active = self._active_proposals(topic, self._proj)
            groups = {p["workgroup"] for p in active}
            if len(groups) > 1:
                findings.append({
                    "kind": "duplicate_commit", "topic_id": tid,
                    "detail": f"议题《{topic['title']}》被多个工作组重复承诺: "
                              + "、".join(sorted(groups)),
                    "related": [p["proposal_id"] for p in active],
                })
        # 2) 依赖环 / 未知前置 / 前置未通过 / 截止时序
        for tid, topic in sorted(self._proj.topics.items()):
            if topic["status"] in (TopicStatus.MERGED.value, TopicStatus.WITHDRAWN.value,
                                   TopicStatus.REJECTED.value):
                continue
            for ref, dep in self._topic_dependencies(topic):
                if dep is None:
                    findings.append({
                        "kind": "unknown_dependency", "topic_id": tid,
                        "detail": f"前置议题无法解析: {ref}", "related": [],
                    })
                    continue
                dep_topic = self._proj.topics[dep]
                if dep_topic["status"] != TopicStatus.APPROVED.value:
                    findings.append({
                        "kind": "unmet_dependency", "topic_id": tid,
                        "detail": f"前置议题 {dep}《{dep_topic['title']}》"
                                  f"尚未通过（{dep_topic['status']}）",
                        "related": [dep],
                    })
                # 截止时序：本议题截止早于前置议题截止 → 时序倒置
                active = self._active_proposals(topic, self._proj)
                dep_active = self._active_proposals(dep_topic, self._proj)
                if active and dep_active:
                    my_deadline = min(p["deadline"] for p in active)
                    dep_deadline = min(p["deadline"] for p in dep_active)
                    if my_deadline < dep_deadline:
                        findings.append({
                            "kind": "deadline_inversion", "topic_id": tid,
                            "detail": f"本议题截止 {my_deadline} 早于前置议题 "
                                      f"{dep} 的截止 {dep_deadline}",
                            "related": [dep],
                        })
        # 3) 依赖环（全局）
        for tid in sorted(self._proj.topics):
            topic = self._proj.topics[tid]
            if topic["status"] in (TopicStatus.MERGED.value, TopicStatus.WITHDRAWN.value):
                continue
            if self._would_cycle(tid):
                findings.append({
                    "kind": "dependency_cycle", "topic_id": tid,
                    "detail": f"议题 {tid} 处于依赖环中", "related": [],
                })
        return findings

    # ------------------------------------------------------------------
    # 议程版本：发布历史、重放、校验
    # ------------------------------------------------------------------

    def list_agendas(self, actor: Actor) -> list[dict]:
        return [{
            "version": a["version"], "digest": a["digest"],
            "prev_digest": a["prev_digest"], "published_at": a["published_at"],
            "published_by": a["published_by"], "entries": len(a["entries"]),
        } for a in self._proj.agendas]

    def replay_agenda(self, actor: Actor, version: int) -> dict:
        """重放指定议程版本：内容直接来自发布事件，并附链式校验结果。"""
        agenda = next((a for a in self._proj.agendas if a["version"] == version), None)
        if agenda is None:
            raise DomainError("not_found", f"议程版本不存在: v{version}")
        # 校验从创世到该版本的议程摘要链
        prev = GENESIS_HASH
        for a in self._proj.agendas:
            expect = digest_of({"version": a["version"], "entries": a["entries"],
                                "prev_digest": a["prev_digest"]})
            if a["prev_digest"] != prev or expect != a["digest"]:
                raise DomainError("chain_broken",
                                  f"议程摘要链在 v{a['version']} 处校验失败")
            prev = a["digest"]
            if a["version"] == version:
                break
        return {
            "version": agenda["version"],
            "digest": agenda["digest"],
            "prev_digest": agenda["prev_digest"],
            "published_at": agenda["published_at"],
            "published_by": agenda["published_by"],
            "entries": [redact_summary({**e, "summary": None, "materials": []},
                                       actor) for e in agenda["entries"]],
            "chain_valid": True,
        }

    def verify(self, actor: Actor) -> dict:
        """校验事件日志哈希链 + 全部议程摘要链 + 全部修订链。"""
        self._store.verify()
        prev = GENESIS_HASH
        for a in self._proj.agendas:
            expect = digest_of({"version": a["version"], "entries": a["entries"],
                                "prev_digest": a["prev_digest"]})
            if a["prev_digest"] != prev or expect != a["digest"]:
                raise DomainError("chain_broken", f"议程摘要链在 v{a['version']} 处断裂")
            prev = a["digest"]
        for pid in self._proj.revisions:
            if not self.revision_chain(actor, pid)["chain_valid"]:
                raise DomainError("chain_broken", f"修订链校验失败: {pid}")
        return {"ok": True, "events": len(self._events),
                "agendas": len(self._proj.agendas),
                "pending_approvals": len(self.pending_approvals(actor))}

    # ------------------------------------------------------------------
    # 解释：为什么被合并 / 延期 / 拒绝 / 移交
    # ------------------------------------------------------------------

    def _events_for_topic(self, topic_id: str) -> list[dict]:
        topic = self._proj.topics.get(topic_id)
        if topic is None:
            raise DomainError("not_found", f"议题不存在: {topic_id}")
        # 议题的提案（含历史上被归并走的）+ 归并前的提案
        pids = set(topic["proposal_ids"])
        for prop in self._proj.proposals.values():
            if prop["topic_id"] == topic_id:
                pids.add(prop["proposal_id"])
        # 被归并到本议题的议题，其历史事件也属于本议题的解释链
        merged_into_this = {t["topic_id"] for t in self._proj.topics.values()
                            if t["merged_into"] == topic_id}
        related_topics = {topic_id} | merged_into_this
        for mt in merged_into_this:
            for prop in self._proj.proposals.values():
                if prop["proposal_id"] in self._proj.topics[mt]["proposal_ids"]:
                    pids.add(prop["proposal_id"])

        out = []
        for ev in self._events:
            p = ev["payload"]
            hit = False
            if p.get("topic_id") in related_topics:
                hit = True
            elif p.get("proposal_id") in pids:
                hit = True
            elif ev["event"] == EV_TOPICS_MERGED and (
                p.get("survivor") in related_topics
                or any(m in related_topics for m in p.get("merged", []))
            ):
                hit = True
            elif ev["event"] == EV_AGENDA_PUBLISHED and any(
                e["topic_id"] == topic_id for e in p.get("entries", [])
            ):
                hit = True
            elif ev["event"] == EV_APPROVAL_DECIDED and p.get("topic_id") in related_topics:
                hit = True
            if hit:
                out.append(ev)
        return out

    def explain_topic(self, actor: Actor, topic_id: str) -> dict:
        """按时间线解释议题的处置过程，并给出结论性说明。"""
        topic = self._get_topic(topic_id)
        trail = []
        for ev in self._events_for_topic(topic_id):
            trail.append({
                "seq": ev["seq"], "ts": ev["ts"], "kind": ev["event"],
                "actor": ev["actor"], "detail": _render_event(ev, self._proj),
            })
        verdict = self._verdict(topic)
        return {
            "topic_id": topic_id,
            "title": topic["title"],
            "status": topic["status"],
            "merged_into": topic["merged_into"],
            "verdict": verdict,
            "trail": trail,
        }

    def _verdict(self, topic: dict) -> str:
        tid = topic["topic_id"]
        status = topic["status"]
        events = self._events_for_topic(tid)

        def last(kind: str):
            for ev in reversed(events):
                if ev["event"] == kind:
                    return ev
            return None

        if status == TopicStatus.MERGED.value:
            ev = last(EV_TOPICS_MERGED)
            if ev:
                return (f"该议题于 {ev['ts']} 由 {ev['actor']} 归并至 "
                        f"{ev['payload']['survivor']}，原因：{ev['payload']['reason']}")
            return f"该议题已归并至 {topic['merged_into']}"
        if status == TopicStatus.DEFERRED.value:
            ev = last(EV_TOPIC_DEFERRED)
            if ev:
                return (f"该议题于 {ev['ts']} 由 {ev['actor']} 延期至 "
                        f"{ev['payload']['until']}，原因：{ev['payload']['reason']}")
        if status == TopicStatus.REJECTED.value:
            ev = last(EV_APPROVAL_DECIDED)
            if ev:
                return (f"该议题于 {ev['ts']} 被审批人 {ev['actor']} 拒绝，"
                        f"原因：{ev['payload']['reason']}")
        if status == TopicStatus.APPROVED.value:
            ev = last(EV_APPROVAL_DECIDED)
            pub = last(EV_AGENDA_PUBLISHED)
            base = f"该议题于 {ev['ts']} 经审批人 {ev['actor']} 通过" if ev else "该议题已通过审批"
            if pub:
                base += f"，并纳入议程版本 v{pub['payload']['version']}"
            return base
        if status == TopicStatus.WITHDRAWN.value:
            return "该议题的全部提案已撤回，议题关闭"
        if status == TopicStatus.PENDING_APPROVAL.value:
            ev = last(EV_APPROVAL_REQUESTED)
            if ev:
                return f"该议题于 {ev['ts']} 由 {ev['actor']} 发起审批，等待审批人决定"
        transfer = last(EV_TOPIC_TRANSFERRED)
        if transfer:
            p = transfer["payload"]
            return (f"该议题在处理中，最近于 {transfer['ts']} 由 "
                    f"{p['from_workgroup']} 移交 {p['to_workgroup']}，原因：{p['reason']}")
        return "该议题已受理，待归并与审批"


def _render_event(ev: dict, proj: Projection) -> str:
    """把事件渲染成一行中文说明，供解释时间线使用。"""
    p = ev["payload"]
    kind = ev["event"]
    if kind == EV_PROPOSAL_SUBMITTED:
        return (f"{p['org']}/{p['workgroup']} 提交提案 {p['proposal_id']}"
                f"《{p['title']}》（责任人 {p['owner']}，截止 {p['deadline']}，"
                f"保密级别 {p['secrecy']}）")
    if kind == EV_PROPOSAL_WITHDRAWN:
        return f"提案 {p['proposal_id']} 被撤回，原因：{p.get('reason', '')}"
    if kind == EV_DEADLINE_REVISED:
        return (f"提案 {p['proposal_id']} 截止时间第 {p['revision_no']} 次修订："
                f"{p['previous_deadline']} → {p['new_deadline']}，原因：{p['reason']}")
    if kind == EV_CONFLICT_FLAGGED:
        return f"冲突告警[{p['kind']}]：{p['detail']}"
    if kind == EV_TOPICS_MERGED:
        return (f"议题归并：{', '.join(p['merged'])} 并入 {p['survivor']}，"
                f"原因：{p['reason']}")
    if kind == EV_TOPIC_DEFERRED:
        return f"议题延期至 {p['until']}，原因：{p['reason']}"
    if kind == EV_TOPIC_TRANSFERRED:
        return (f"议题由 {p['from_workgroup']}({p['from_org']}) 移交 "
                f"{p['to_workgroup']}({p['to_org']})，原因：{p['reason']}")
    if kind == EV_TOPIC_STATUS_CHANGED:
        return f"议题状态 {p['from']} → {p['to']}：{p.get('reason', '')}"
    if kind == EV_APPROVAL_REQUESTED:
        return f"发起审批 {p['approval_id']}"
    if kind == EV_APPROVAL_DECIDED:
        text = "通过" if p["decision"] == "approved" else "拒绝"
        return f"审批 {p['approval_id']} {text}，原因：{p.get('reason', '')}"
    if kind == EV_AGENDA_PUBLISHED:
        return f"纳入议程版本 v{p['version']}（摘要 {p['digest'][:12]}…）"
    return kind
