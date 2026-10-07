"""HTTP 接口端到端：提交→审批→发布→重放→解释，以及鉴权与脱敏。"""
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from summit_agenda import EventStore, SummitService
from summit_agenda.api import create_server
from helpers import FIXED_TS, idem


def _request(port, method, path, body=None, role="coordinator",
             actor="li-hui", org=""):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={
            "Content-Type": "application/json",
            # 头部只支持 latin-1，中文按 percent-encoding 传输
            "X-Actor-Id": urllib.parse.quote(actor),
            "X-Actor-Role": role,
            "X-Actor-Org": urllib.parse.quote(org),
        })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc = SummitService(EventStore(":memory:"), clock=lambda: FIXED_TS)
        cls.server = create_server(svc, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_full_flow_over_http(self):
        # 提交（两个工作组重复承诺同一议题）
        s1, r1 = _request(self.port, "POST", "/proposals", {
            "idem": idem(), "title": "数字丝路合作", "summary": "跨境数据方案",
            "org": "机构A", "workgroup": "甲组", "owner": "张三",
            "deadline": "2026-11-15", "secrecy": "confidential",
        }, role="submitter", actor="wang-ke", org="机构A")
        self.assertEqual(s1, 201)
        s2, r2 = _request(self.port, "POST", "/proposals", {
            "idem": idem(), "title": "数字丝路合作", "summary": "联合倡议草案",
            "org": "机构B", "workgroup": "乙组", "owner": "李四",
            "deadline": "2026-11-20", "secrecy": "secret",
        }, role="submitter", actor="zhao-min", org="机构B")
        self.assertEqual(s2, 201)
        self.assertEqual(r1["topic_id"], r2["topic_id"])
        tid = r1["topic_id"]

        # 冲突检查
        s, conflicts = _request(self.port, "GET", "/conflicts")
        self.assertEqual(s, 200)
        self.assertIn("duplicate_commit", [c["kind"] for c in conflicts])

        # 审批 → 发布 → 重放
        s, ap = _request(self.port, "POST", f"/topics/{tid}/request-approval",
                         {"idem": idem()})
        self.assertEqual(s, 201)
        s, _ = _request(self.port, "POST",
                        f"/approvals/{ap['approval_id']}/decide",
                        {"decision": "approved", "reason": "符合方向", "idem": idem()},
                        role="approver", actor="chen-shen")
        self.assertEqual(s, 201)
        s, pub = _request(self.port, "POST", "/agendas/publish", {"idem": idem()})
        self.assertEqual(s, 201)
        s, replay = _request(self.port, "GET", f"/agendas/{pub['version']}/replay")
        self.assertEqual(s, 200)
        self.assertTrue(replay["chain_valid"])
        self.assertEqual(replay["entries"][0]["topic_id"], tid)

        # 解释
        s, explain = _request(self.port, "GET", f"/topics/{tid}/explain")
        self.assertEqual(s, 200)
        self.assertIn("通过", explain["verdict"])
        self.assertTrue(any(t["kind"] == "conflict_flagged" for t in explain["trail"]))

    def test_rbac_over_http(self):
        # 无权限角色执行归并 → 403
        s, body = _request(self.port, "POST", "/topics/merge",
                           {"survivor": "T-1", "merged": ["T-2"],
                            "reason": "x", "idem": idem()},
                           role="submitter", actor="wang-ke", org="机构A")
        self.assertEqual(s, 403)
        self.assertEqual(body["error"], "forbidden")
        # 审计员看不到机密摘要
        s, views = _request(self.port, "GET", "/proposals",
                            role="auditor", actor="sun-jian")
        self.assertEqual(s, 200)
        confidential = [v for v in views if v["secrecy"] in ("confidential", "secret")]
        self.assertTrue(confidential)
        for v in confidential:
            self.assertTrue(v["redacted"])
            self.assertIsNone(v["summary"])

    def test_not_found(self):
        s, body = _request(self.port, "GET", "/topics/T-999")
        self.assertEqual(s, 404)
        self.assertEqual(body["error"], "not_found")

    def test_idempotent_submit_over_http(self):
        key = idem()
        payload = {"idem": key, "title": "幂等验证议题", "summary": "s",
                   "org": "机构A", "workgroup": "甲组", "owner": "张三",
                   "deadline": "2026-11-15"}
        s1, r1 = _request(self.port, "POST", "/proposals", payload,
                          role="submitter", actor="wang-ke", org="机构A")
        s2, r2 = _request(self.port, "POST", "/proposals", payload,
                          role="submitter", actor="wang-ke", org="机构A")
        self.assertEqual((s1, s2), (201, 201))
        self.assertEqual(r1["proposal_id"], r2["proposal_id"])
        self.assertTrue(r2["deduplicated"])


if __name__ == "__main__":
    unittest.main()
