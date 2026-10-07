"""提交与撤回的幂等性、重复承诺检测。"""
import unittest

from summit_agenda import DomainError
from helpers import SUB_A, SUB_B, COORD, idem, make_service, submit


class SubmitIdempotencyTests(unittest.TestCase):
    def test_same_idem_returns_same_result_without_side_effect(self):
        svc = make_service()
        key = idem()
        r1 = svc.submit_proposal(SUB_A, idem=key, title="数字丝路", summary="s",
                                 org="机构A", workgroup="甲组", owner="张三",
                                 deadline="2026-11-15", secrecy="internal")
        r2 = svc.submit_proposal(SUB_A, idem=key, title="数字丝路", summary="s",
                                 org="机构A", workgroup="甲组", owner="张三",
                                 deadline="2026-11-15", secrecy="internal")
        self.assertEqual(r1["proposal_id"], r2["proposal_id"])
        self.assertFalse(r1["deduplicated"])
        self.assertTrue(r2["deduplicated"])
        self.assertEqual(len(svc.list_proposals(COORD)), 1)

    def test_same_org_same_topic_is_naturally_idempotent(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="数字丝路")
        r2 = submit(svc, SUB_A, title="数字丝路")  # 不同幂等键、同机构同议题
        self.assertEqual(r1["proposal_id"], r2["proposal_id"])
        self.assertTrue(r2["deduplicated"])
        self.assertEqual(len(svc.list_proposals(COORD)), 1)

    def test_cross_workgroup_duplicate_flags_conflict(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="数字丝路", workgroup="甲组")
        r2 = submit(svc, SUB_B, title="数字丝路", org="机构B", workgroup="乙组")
        self.assertEqual(r1["topic_id"], r2["topic_id"])
        self.assertTrue(r2["duplicated"])
        self.assertNotEqual(r1["proposal_id"], r2["proposal_id"])
        kinds = [c["kind"] for c in svc.check_conflicts(COORD)]
        self.assertIn("duplicate_commit", kinds)

    def test_title_normalization(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="数字 丝路　合作")
        r2 = submit(svc, SUB_B, title="数字丝路合作", org="机构B", workgroup="乙组")
        self.assertEqual(r1["topic_id"], r2["topic_id"])

    def test_withdraw_is_idempotent(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        pid = r["proposal_id"]
        key = idem()
        w1 = svc.withdraw_proposal(SUB_A, pid, idem=key, reason="计划调整")
        w2 = svc.withdraw_proposal(SUB_A, pid, idem=key, reason="计划调整")
        w3 = svc.withdraw_proposal(SUB_A, pid, idem=idem(), reason="再次撤回")
        self.assertTrue(w1["withdrawn"])
        self.assertTrue(w2["deduplicated"])
        self.assertTrue(w3["already_withdrawn"])
        # 只有一次撤回事件落库
        events = [e for e in svc._store.events() if e["event"] == "proposal_withdrawn"]
        self.assertEqual(len(events), 1)

    def test_withdraw_last_proposal_closes_topic(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        svc.withdraw_proposal(SUB_A, r["proposal_id"], idem=idem())
        topic = svc.get_topic(COORD, r["topic_id"])
        self.assertEqual(topic["status"], "withdrawn")

    def test_resubmit_after_full_withdrawal_creates_new_topic(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="数字丝路")
        svc.withdraw_proposal(SUB_A, r1["proposal_id"], idem=idem())
        r2 = submit(svc, SUB_A, title="数字丝路")
        self.assertNotEqual(r1["topic_id"], r2["topic_id"])
        self.assertFalse(r2["deduplicated"])

    def test_resubmit_after_rejection_creates_new_topic(self):
        from helpers import APPROVER, COORD
        svc = make_service()
        r1 = submit(svc, SUB_A, title="数字丝路")
        ap = svc.request_approval(COORD, r1["topic_id"], idem=idem())
        svc.decide_approval(APPROVER, ap["approval_id"], decision="rejected",
                            reason="超出范围", idem=idem())
        r2 = submit(svc, SUB_A, title="数字丝路")
        self.assertNotEqual(r1["topic_id"], r2["topic_id"])
        self.assertFalse(r2["deduplicated"])
        self.assertEqual(svc.get_topic(COORD, r2["topic_id"])["status"], "open")

    def test_submitter_cannot_act_for_other_org(self):
        svc = make_service()
        with self.assertRaises(DomainError) as ctx:
            svc.submit_proposal(SUB_A, idem=idem(), title="x", summary="s",
                                org="机构B", workgroup="乙组", owner="李四",
                                deadline="2026-11-15")
        self.assertEqual(ctx.exception.code, "forbidden")
        r = submit(svc, SUB_A)
        with self.assertRaises(DomainError) as ctx:
            svc.withdraw_proposal(SUB_B, r["proposal_id"], idem=idem())
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_invalid_deadline_rejected(self):
        svc = make_service()
        with self.assertRaises(DomainError) as ctx:
            submit(svc, SUB_A, deadline="2026-13-40")
        self.assertEqual(ctx.exception.code, "invalid_deadline")

    def test_dependency_cycle_rejected(self):
        svc = make_service()
        # X 先挂着对 Y（按标题）的前置，Y 再依赖 X → 成环
        rx = submit(svc, SUB_A, title="议题X", depends_on=["议题Y"])
        with self.assertRaises(DomainError) as ctx:
            submit(svc, SUB_A, title="议题Y", depends_on=[rx["topic_id"]])
        self.assertEqual(ctx.exception.code, "dependency_cycle")

    def test_self_dependency_rejected(self):
        svc = make_service()
        with self.assertRaises(DomainError) as ctx:
            submit(svc, SUB_A, title="议题Z", depends_on=["议题Z"])
        self.assertEqual(ctx.exception.code, "dependency_cycle")


if __name__ == "__main__":
    unittest.main()
