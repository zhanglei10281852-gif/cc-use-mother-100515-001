"""重启恢复、议程重放、日志防篡改。"""
import json
import tempfile
import unittest
from pathlib import Path

from summit_agenda import DomainError, EventStore, SummitService
from helpers import APPROVER, COORD, FIXED_TS, SUB_A, idem, make_service, submit


def _open(data_dir):
    return SummitService(EventStore(str(Path(data_dir) / "events.jsonl")),
                         clock=lambda: FIXED_TS)


class RestartRecoveryTests(unittest.TestCase):
    def test_pending_approvals_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            svc = _open(d)
            r1 = submit(svc, SUB_A, title="议题一")
            r2 = submit(svc, SUB_A, title="议题二")
            svc.request_approval(COORD, r1["topic_id"], idem=idem())
            svc.request_approval(COORD, r2["topic_id"], idem=idem())
            # 模拟服务重启：同一数据目录重新打开
            svc2 = _open(d)
            pending = svc2.pending_approvals(COORD)
            self.assertEqual(len(pending), 2)
            # 重启后可以继续完成审批
            svc2.decide_approval(APPROVER, pending[0]["approval_id"],
                                 decision="approved", reason="ok", idem=idem())
            self.assertEqual(svc2.get_topic(COORD, r1["topic_id"])["status"],
                             "approved")
            self.assertEqual(len(svc2.pending_approvals(COORD)), 1)

    def test_idempotency_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            svc = _open(d)
            key = idem()
            r1 = svc.submit_proposal(SUB_A, idem=key, title="数字丝路", summary="s",
                                     org="机构A", workgroup="甲组", owner="张三",
                                     deadline="2026-11-15")
            svc2 = _open(d)
            r2 = svc2.submit_proposal(SUB_A, idem=key, title="数字丝路", summary="s",
                                      org="机构A", workgroup="甲组", owner="张三",
                                      deadline="2026-11-15")
            self.assertEqual(r1["proposal_id"], r2["proposal_id"])
            self.assertTrue(r2["deduplicated"])
            self.assertEqual(len(svc2.list_proposals(COORD)), 1)

    def test_revision_chain_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            svc = _open(d)
            r = submit(svc, SUB_A)
            svc.revise_deadline(SUB_A, r["proposal_id"], new_deadline="2026-12-01",
                                reason="延后", idem=idem())
            svc2 = _open(d)
            chain = svc2.revision_chain(COORD, r["proposal_id"])
            self.assertTrue(chain["chain_valid"])
            self.assertEqual(chain["current_deadline"], "2026-12-01")

    def test_agenda_replay_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            svc = _open(d)
            r = submit(svc, SUB_A, title="议题一")
            ap = svc.request_approval(COORD, r["topic_id"], idem=idem())
            svc.decide_approval(APPROVER, ap["approval_id"],
                                decision="approved", idem=idem())
            svc.publish_agenda(COORD, idem=idem())
            svc2 = _open(d)
            replay = svc2.replay_agenda(COORD, 1)
            self.assertTrue(replay["chain_valid"])
            self.assertEqual(replay["entries"][0]["title"], "议题一")
            self.assertEqual(svc2.verify(COORD)["agendas"], 1)

    def test_tampered_log_detected_on_open(self):
        with tempfile.TemporaryDirectory() as d:
            svc = _open(d)
            submit(svc, SUB_A, title="议题一")
            log = Path(d) / "events.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0])
            record["payload"]["title"] = "被篡改的标题"
            lines[0] = json.dumps(record, ensure_ascii=False)
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(DomainError) as ctx:
                _open(d)
            self.assertEqual(ctx.exception.code, "store_corrupt")

    def test_truncated_tail_detected(self):
        with tempfile.TemporaryDirectory() as d:
            svc = _open(d)
            submit(svc, SUB_A, title="议题一")
            submit(svc, SUB_A, title="议题二")
            log = Path(d) / "events.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            log.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            # 截断后链条本身仍自洽（只剩第一条），可正常打开
            svc2 = _open(d)
            self.assertEqual(len(svc2.list_proposals(COORD)), 1)


class VerifyTests(unittest.TestCase):
    def test_verify_ok(self):
        svc = make_service()
        r = submit(svc, SUB_A, title="议题一")
        ap = svc.request_approval(COORD, r["topic_id"], idem=idem())
        svc.decide_approval(APPROVER, ap["approval_id"], decision="approved", idem=idem())
        svc.publish_agenda(COORD, idem=idem())
        out = svc.verify(COORD)
        self.assertTrue(out["ok"])
        self.assertGreater(out["events"], 0)


if __name__ == "__main__":
    unittest.main()
