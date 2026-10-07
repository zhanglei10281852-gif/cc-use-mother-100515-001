"""HTTP 接口层：基于标准库 http.server，无外部依赖。

认证方式（演示级）：请求头携带
  X-Actor-Id: 操作者标识
  X-Actor-Role: submitter | coordinator | approver | auditor
  X-Actor-Org: 所属机构
服务端按角色强制鉴权，材料摘要按保密级别脱敏。
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import urlparse, parse_qs, unquote

from .domain import Actor, DomainError
from .service import SummitService


_ERROR_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "invalid_state": 409,
    "already_decided": 409,
    "unmet_dependency": 409,
    "dependency_cycle": 409,
    "invalid_merge": 400,
    "invalid_decision": 400,
    "invalid_idem": 400,
    "invalid_deadline": 400,
    "invalid_secrecy": 400,
    "invalid_role": 400,
    "invalid_actor": 400,
    "invalid_title": 400,
    "invalid_command": 400,
    "chain_broken": 500,
    "store_corrupt": 500,
}


def _status_for(code: str) -> int:
    return _ERROR_STATUS.get(code, 400)


class _Handler(BaseHTTPRequestHandler):
    service: SummitService = None  # 由 create_server 注入
    server_version = "SummitAgenda/1.0"

    # --------------------------------------------------------------
    # 基础工具
    # --------------------------------------------------------------

    def _actor(self) -> Actor:
        # 头部值须为 latin-1，中文机构名按 percent-encoding 传输
        headers = self.headers
        return Actor.of(
            unquote(headers.get("X-Actor-Id", "")),
            headers.get("X-Actor-Role", "auditor"),
            unquote(headers.get("X-Actor-Org", "")),
        )

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError("invalid_json", f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise DomainError("invalid_json", "请求体须为 JSON 对象")
        return data

    def _send(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    # --------------------------------------------------------------
    # 路由
    # --------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if method == "GET" and parts == ["health"]:  # 健康检查无需身份
                self._send(200, {"ok": True})
                return
            actor = self._actor()
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            body = self._json_body() if method == "POST" else {}
            result = self._route(method, parts, query, body, actor)
            status = 201 if method == "POST" else 200
            self._send(status, result)
        except DomainError as exc:
            self._send(_status_for(exc.code), exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接直接断开
            self._send(500, {"error": "internal", "message": str(exc)})

    def _route(self, method: str, parts: list[str], query: dict,
               body: dict, actor: Actor):
        svc = self.service
        # ---- 查询 ----
        if method == "GET" and parts == ["proposals"]:
            return svc.list_proposals(actor, org=query.get("org"))
        if method == "GET" and parts == ["topics"]:
            return svc.list_topics(actor, status=query.get("status"))
        if method == "GET" and len(parts) == 2 and parts[0] == "topics":
            return svc.get_topic(actor, parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "topics" and parts[2] == "explain":
            return svc.explain_topic(actor, parts[1])
        if method == "GET" and parts == ["approvals"]:
            if query.get("status") == "pending":
                return svc.pending_approvals(actor)
            return svc.list_approvals(actor)
        if method == "GET" and parts == ["agendas"]:
            return svc.list_agendas(actor)
        if method == "GET" and len(parts) == 3 and parts[0] == "agendas" and parts[2] == "replay":
            return svc.replay_agenda(actor, int(parts[1]))
        if method == "GET" and parts == ["conflicts"]:
            return svc.check_conflicts(actor)
        if method == "GET" and len(parts) == 3 and parts[0] == "proposals" and parts[2] == "revisions":
            return svc.revision_chain(actor, parts[1])
        if method == "GET" and parts == ["verify"]:
            return svc.verify(actor)
        # ---- 命令 ----
        if method == "POST" and parts == ["proposals"]:
            return svc.submit_proposal(
                actor,
                idem=body.get("idem", ""),
                title=body.get("title", ""),
                summary=body.get("summary", ""),
                org=body.get("org", ""),
                workgroup=body.get("workgroup", ""),
                owner=body.get("owner", ""),
                deadline=body.get("deadline", ""),
                secrecy=body.get("secrecy", "public"),
                depends_on=body.get("depends_on") or [],
                materials=body.get("materials") or [],
            )
        if method == "POST" and len(parts) == 3 and parts[0] == "proposals" and parts[2] == "withdraw":
            return svc.withdraw_proposal(actor, parts[1],
                                         idem=body.get("idem", ""),
                                         reason=body.get("reason", ""))
        if method == "POST" and len(parts) == 3 and parts[0] == "proposals" and parts[2] == "deadline":
            return svc.revise_deadline(actor, parts[1],
                                       new_deadline=body.get("new_deadline", ""),
                                       reason=body.get("reason", ""),
                                       idem=body.get("idem", ""))
        if method == "POST" and parts == ["topics", "merge"]:
            return svc.merge_topics(actor,
                                    survivor_id=body.get("survivor", ""),
                                    merged_ids=body.get("merged", []),
                                    reason=body.get("reason", ""),
                                    idem=body.get("idem", ""))
        if method == "POST" and len(parts) == 3 and parts[0] == "topics" and parts[2] == "defer":
            return svc.defer_topic(actor, parts[1],
                                   until=body.get("until", ""),
                                   reason=body.get("reason", ""),
                                   idem=body.get("idem", ""))
        if method == "POST" and len(parts) == 3 and parts[0] == "topics" and parts[2] == "transfer":
            return svc.transfer_topic(actor, parts[1],
                                      to_workgroup=body.get("to_workgroup", ""),
                                      to_org=body.get("to_org"),
                                      reason=body.get("reason", ""),
                                      idem=body.get("idem", ""))
        if method == "POST" and len(parts) == 3 and parts[0] == "topics" and parts[2] == "request-approval":
            return svc.request_approval(actor, parts[1], idem=body.get("idem", ""))
        if method == "POST" and len(parts) == 3 and parts[0] == "approvals" and parts[2] == "decide":
            return svc.decide_approval(actor, parts[1],
                                       decision=body.get("decision", ""),
                                       reason=body.get("reason", ""),
                                       idem=body.get("idem", ""))
        if method == "POST" and parts == ["agendas", "publish"]:
            return svc.publish_agenda(actor, idem=body.get("idem", ""))
        raise DomainError("not_found", f"路由不存在: {method} /{'/'.join(parts)}")


def create_server(service: SummitService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """构建 HTTP 服务。pending 审批等状态已在 SummitService 构造时由事件日志恢复。"""
    handler = type("BoundHandler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    return server
