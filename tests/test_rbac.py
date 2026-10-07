"""RBAC：命令权限与材料摘要的分级可见性。"""
import unittest

from summit_agenda import DomainError
from helpers import APPROVER, AUDITOR, COORD, SUB_A, SUB_B, idem, make_service, submit


def _seed(svc):
    submit(svc, SUB_A, title="公开议题", secrecy="public", summary="公开摘要")
    submit(svc, SUB_A, title="内部议题", secrecy="internal", summary="内部摘要")
    submit(svc, SUB_B, title="机密议题", org="机构B", workgroup="乙组",
           secrecy="confidential", summary="机密摘要")
    submit(svc, SUB_B, title="绝密议题", org="机构B", workgroup="乙组",
           secrecy="secret", summary="绝密摘要")


def _by_title(views, title):
    return next(v for v in views if v["title"] == title)


class RbacTests(unittest.TestCase):
    def test_summary_visibility_by_role(self):
        svc = make_service()
        _seed(svc)
        # 审批人 clearance=secret：全部可见
        views = svc.list_proposals(APPROVER)
        for title in ("公开议题", "内部议题", "机密议题", "绝密议题"):
            self.assertFalse(_by_title(views, title)["redacted"], title)
        # 协调员 clearance=confidential：看不到绝密
        views = svc.list_proposals(COORD)
        self.assertFalse(_by_title(views, "机密议题")["redacted"])
        secret = _by_title(views, "绝密议题")
        self.assertTrue(secret["redacted"])
        self.assertIsNone(secret["summary"])
        # 审计员 clearance=internal：看不到机密/绝密
        views = svc.list_proposals(AUDITOR)
        self.assertFalse(_by_title(views, "内部议题")["redacted"])
        self.assertTrue(_by_title(views, "机密议题")["redacted"])
        self.assertTrue(_by_title(views, "绝密议题")["redacted"])

    def test_submitter_scope_limited_to_own_org(self):
        svc = make_service()
        _seed(svc)
        views = svc.list_proposals(SUB_A)  # 机构A 提交人
        self.assertFalse(_by_title(views, "内部议题")["redacted"])   # 本机构内部件可见
        self.assertFalse(_by_title(views, "公开议题")["redacted"])
        # 其他机构的非公开件一律脱敏
        self.assertTrue(_by_title(views, "机密议题")["redacted"])
        views_b = svc.list_proposals(SUB_B)  # 机构B 提交人看机构A 的内部件
        self.assertTrue(_by_title(views_b, "内部议题")["redacted"])

    def test_redacted_view_keeps_metadata(self):
        svc = make_service()
        _seed(svc)
        views = svc.list_proposals(AUDITOR)
        secret = _by_title(views, "绝密议题")
        self.assertIsNone(secret["summary"])
        self.assertEqual(secret["materials"], [])
        # 元数据（标题/状态/责任人）保留，便于会务排查
        self.assertEqual(secret["owner"], "张三")
        self.assertEqual(secret["secrecy"], "secret")

    def test_command_permissions(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        tid = r["topic_id"]
        # 提交人不能归并/审批/发布
        with self.assertRaises(DomainError):
            svc.defer_topic(SUB_A, tid, until="2026-12-01", reason="x", idem=idem())
        with self.assertRaises(DomainError):
            svc.publish_agenda(SUB_A, idem=idem())
        # 审批人不能归并、不能发布
        with self.assertRaises(DomainError):
            svc.defer_topic(APPROVER, tid, until="2026-12-01", reason="x", idem=idem())
        with self.assertRaises(DomainError):
            svc.publish_agenda(APPROVER, idem=idem())
        # 协调员不能审批
        svc.request_approval(COORD, tid, idem=idem())
        ap = svc.pending_approvals(COORD)[0]
        with self.assertRaises(DomainError) as ctx:
            svc.decide_approval(COORD, ap["approval_id"], decision="approved", idem=idem())
        self.assertEqual(ctx.exception.code, "forbidden")
        # 审计员只读
        with self.assertRaises(DomainError):
            svc.submit_proposal(AUDITOR, idem=idem(), title="x", summary="s",
                                org="机构C", workgroup="丙组", owner="王五",
                                deadline="2026-11-01")


if __name__ == "__main__":
    unittest.main()
