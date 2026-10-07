"""截止时间修订链：链式结构、摘要衔接、幂等。"""
import unittest

from summit_agenda import DomainError
from summit_agenda.events import GENESIS_HASH, digest_of
from helpers import COORD, SUB_A, SUB_B, idem, make_service, submit


class RevisionChainTests(unittest.TestCase):
    def test_revisions_form_linked_chain(self):
        svc = make_service()
        r = submit(svc, SUB_A, deadline="2026-11-15")
        pid = r["proposal_id"]
        svc.revise_deadline(SUB_A, pid, new_deadline="2026-11-30",
                            reason="筹备组延后", idem=idem())
        svc.revise_deadline(SUB_A, pid, new_deadline="2026-12-10",
                            reason="再延后", idem=idem())
        chain = svc.revision_chain(COORD, pid)
        self.assertTrue(chain["chain_valid"])
        self.assertEqual(chain["current_deadline"], "2026-12-10")
        revs = chain["revisions"]
        self.assertEqual([r_["revision_no"] for r_ in revs], [1, 2])
        self.assertEqual(revs[0]["prev_digest"], GENESIS_HASH)
        self.assertEqual(revs[1]["prev_digest"], revs[0]["digest"])
        self.assertEqual(revs[0]["previous_deadline"], "2026-11-15")
        self.assertEqual(revs[0]["new_deadline"], "2026-11-30")
        self.assertEqual(revs[1]["previous_deadline"], "2026-11-30")
        # 摘要可独立重算
        body = {
            "proposal_id": pid, "revision_no": 1,
            "previous_deadline": "2026-11-15", "new_deadline": "2026-11-30",
            "reason": "筹备组延后", "prev_digest": GENESIS_HASH,
        }
        self.assertEqual(revs[0]["digest"], digest_of(body))

    def test_revise_to_same_deadline_is_noop(self):
        svc = make_service()
        r = submit(svc, SUB_A, deadline="2026-11-15")
        out = svc.revise_deadline(SUB_A, r["proposal_id"],
                                  new_deadline="2026-11-15",
                                  reason="无变化", idem=idem())
        self.assertTrue(out["unchanged"])
        self.assertEqual(svc.revision_chain(COORD, r["proposal_id"])["revisions"], [])

    def test_revise_idem_key_replay(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        key = idem()
        o1 = svc.revise_deadline(SUB_A, r["proposal_id"], new_deadline="2026-12-01",
                                 reason="r", idem=key)
        o2 = svc.revise_deadline(SUB_A, r["proposal_id"], new_deadline="2026-12-01",
                                 reason="r", idem=key)
        self.assertTrue(o2["deduplicated"])
        self.assertEqual(o1["revision_no"], o2["revision_no"])
        self.assertEqual(
            len(svc.revision_chain(COORD, r["proposal_id"])["revisions"]), 1)

    def test_submitter_cannot_revise_other_org(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        with self.assertRaises(DomainError) as ctx:
            svc.revise_deadline(SUB_B, r["proposal_id"],
                                new_deadline="2026-12-01", reason="r", idem=idem())
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_revise_withdrawn_proposal_rejected(self):
        svc = make_service()
        r = submit(svc, SUB_A)
        svc.withdraw_proposal(SUB_A, r["proposal_id"], idem=idem())
        with self.assertRaises(DomainError) as ctx:
            svc.revise_deadline(SUB_A, r["proposal_id"],
                                new_deadline="2026-12-01", reason="r", idem=idem())
        self.assertEqual(ctx.exception.code, "invalid_state")


if __name__ == "__main__":
    unittest.main()
