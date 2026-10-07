"""归并/延期/移交/审批/发布 的工作流与解释。"""
import unittest

from summit_agenda import DomainError
from helpers import APPROVER, COORD, SUB_A, SUB_B, idem, make_service, submit


def _approve(svc, tid):
    ap = svc.request_approval(COORD, tid, idem=idem())
    svc.decide_approval(APPROVER, ap["approval_id"], decision="approved",
                        reason="符合方向", idem=idem())


class WorkflowTests(unittest.TestCase):
    def test_merge_moves_proposals_and_explains(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="数字丝路合作")
        r2 = submit(svc, SUB_B, title="数字丝绸之路倡议", org="机构B", workgroup="乙组")
        svc.merge_topics(COORD, survivor_id=r1["topic_id"],
                         merged_ids=[r2["topic_id"]],
                         reason="同一议题重复承诺", idem=idem())
        merged = svc.get_topic(COORD, r2["topic_id"])
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["merged_into"], r1["topic_id"])
        survivor = svc.get_topic(COORD, r1["topic_id"])
        self.assertEqual(len(survivor["proposals"]), 2)
        explain = svc.explain_topic(COORD, r2["topic_id"])
        self.assertIn("归并", explain["verdict"])
        self.assertIn("同一议题重复承诺", explain["verdict"])
        kinds = [t["kind"] for t in explain["trail"]]
        self.assertIn("topics_merged", kinds)

    def test_merge_terminal_topic_rejected(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="议题一")
        r2 = submit(svc, SUB_A, title="议题二")
        svc.merge_topics(COORD, survivor_id=r1["topic_id"],
                         merged_ids=[r2["topic_id"]], reason="r", idem=idem())
        with self.assertRaises(DomainError) as ctx:
            svc.merge_topics(COORD, survivor_id=r1["topic_id"],
                             merged_ids=[r2["topic_id"]], reason="r", idem=idem())
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_defer_and_explain(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        svc.defer_topic(COORD, r["topic_id"], until="2027-01-15",
                        reason="等待预算批复", idem=idem())
        topic = svc.get_topic(COORD, r["topic_id"])
        self.assertEqual(topic["status"], "deferred")
        self.assertEqual(topic["deferred_until"], "2027-01-15")
        explain = svc.explain_topic(COORD, r["topic_id"])
        self.assertIn("延期", explain["verdict"])
        self.assertIn("等待预算批复", explain["verdict"])

    def test_transfer_and_explain(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        svc.transfer_topic(COORD, r["topic_id"], to_workgroup="丙组",
                           to_org="机构C", reason="职责划转", idem=idem())
        topic = svc.get_topic(COORD, r["topic_id"])
        self.assertEqual(topic["workgroup"], "丙组")
        self.assertEqual(topic["org"], "机构C")
        explain = svc.explain_topic(COORD, r["topic_id"])
        self.assertIn("移交", explain["verdict"])
        self.assertIn("职责划转", explain["verdict"])

    def test_approval_flow_and_reject_explain(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="议题甲")
        r2 = submit(svc, SUB_A, title="议题乙")
        _approve(svc, r1["topic_id"])
        self.assertEqual(svc.get_topic(COORD, r1["topic_id"])["status"], "approved")
        # 拒绝路径
        ap = svc.request_approval(COORD, r2["topic_id"], idem=idem())
        svc.decide_approval(APPROVER, ap["approval_id"], decision="rejected",
                            reason="超出本届范围", idem=idem())
        self.assertEqual(svc.get_topic(COORD, r2["topic_id"])["status"], "rejected")
        explain = svc.explain_topic(COORD, r2["topic_id"])
        self.assertIn("拒绝", explain["verdict"])
        self.assertIn("超出本届范围", explain["verdict"])

    def test_double_decide_rejected(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        ap = svc.request_approval(COORD, r["topic_id"], idem=idem())
        svc.decide_approval(APPROVER, ap["approval_id"], decision="approved", idem=idem())
        with self.assertRaises(DomainError) as ctx:
            svc.decide_approval(APPROVER, ap["approval_id"], decision="rejected", idem=idem())
        self.assertEqual(ctx.exception.code, "already_decided")

    def test_unmet_dependency_blocks_approval(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="前置议题")
        r2 = submit(svc, SUB_A, title="后续议题", depends_on=[r1["topic_id"]])
        with self.assertRaises(DomainError) as ctx:
            svc.request_approval(COORD, r2["topic_id"], idem=idem())
        self.assertEqual(ctx.exception.code, "unmet_dependency")
        _approve(svc, r1["topic_id"])
        ap = svc.request_approval(COORD, r2["topic_id"], idem=idem())
        self.assertEqual(ap["status"], "pending")

    def test_deadline_inversion_reported(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="前置议题", deadline="2026-12-01")
        submit(svc, SUB_A, title="后续议题", deadline="2026-11-01",
               depends_on=[r1["topic_id"]])
        kinds = [c["kind"] for c in svc.check_conflicts(COORD)]
        self.assertIn("deadline_inversion", kinds)

    def test_publish_immutable_versions_and_idempotent_republish(self):
        svc = make_service()
        r1 = submit(svc, SUB_A, title="议题一")
        _approve(svc, r1["topic_id"])
        v1 = svc.publish_agenda(COORD, idem=idem())
        self.assertEqual(v1["version"], 1)
        # 内容未变 → 幂等返回，不产生新版本
        again = svc.publish_agenda(COORD, idem=idem())
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["version"], 1)
        self.assertEqual(len(svc.list_agendas(COORD)), 1)
        # 新议题通过后发布 v2，摘要链衔接
        r2 = submit(svc, SUB_A, title="议题二")
        _approve(svc, r2["topic_id"])
        v2 = svc.publish_agenda(COORD, idem=idem())
        self.assertEqual(v2["version"], 2)
        agendas = svc.list_agendas(COORD)
        self.assertEqual(agendas[1]["prev_digest"], agendas[0]["digest"])
        # v1 重放仍是当时内容（不可改写）
        replay1 = svc.replay_agenda(COORD, 1)
        self.assertEqual(len(replay1["entries"]), 1)
        self.assertEqual(replay1["entries"][0]["title"], "议题一")
        replay2 = svc.replay_agenda(COORD, 2)
        self.assertEqual(len(replay2["entries"]), 2)

    def test_approved_verdict_mentions_agenda_version(self):
        svc = make_service()
        r = submit(svc, SUB_A, title="议题一")
        _approve(svc, r["topic_id"])
        svc.publish_agenda(COORD, idem=idem())
        explain = svc.explain_topic(COORD, r["topic_id"])
        self.assertIn("通过", explain["verdict"])
        self.assertIn("v1", explain["verdict"])


if __name__ == "__main__":
    unittest.main()
