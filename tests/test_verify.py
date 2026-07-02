"""Pure-logic tests for the verifier's SMTP-code classification (no network).

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import verify_emails as v  # noqa: E402


class VerdictTests(unittest.TestCase):
    def test_2xx_is_valid_unless_catch_all(self):
        self.assertEqual(v.verdict_for(250, False), "valid")
        self.assertEqual(v.verdict_for(251, False), "valid")
        self.assertEqual(v.verdict_for(250, True), "catch_all")

    def test_hard_bounces_are_invalid(self):
        for code in (550, 551, 553, 554, 521):
            self.assertEqual(v.verdict_for(code, None), "invalid", code)

    def test_552_is_not_a_mailbox_rejection(self):
        # 552 = mailbox full / over quota — the address exists, so not "invalid"
        self.assertEqual(v.verdict_for(552, None), "unknown")

    def test_greylist_and_no_response_are_unknown(self):
        self.assertEqual(v.verdict_for(451, None), "unknown")   # tempfail
        self.assertEqual(v.verdict_for(421, None), "unknown")   # service unavailable
        self.assertEqual(v.verdict_for(None, None), "unknown")  # never answered


class PartitionTests(unittest.TestCase):
    @staticmethod
    def _rec(verdict):
        return {"email": "x@example.com", "verify": {"verdict": verdict}}

    def test_only_proven_bad_records_are_dropped(self):
        recs = [self._rec(x) for x in ("valid", "catch_all", "unknown", "invalid", "no_mx")]
        keep, reject = v.partition_records(recs)
        self.assertEqual([r["verify"]["verdict"] for r in keep], ["valid", "catch_all", "unknown"])
        self.assertEqual([r["verify"]["verdict"] for r in reject], ["invalid", "no_mx"])

    def test_record_without_verify_object_is_kept(self):
        keep, reject = v.partition_records([{"email": "a@example.com"}])
        self.assertEqual(len(keep), 1)
        self.assertEqual(reject, [])


if __name__ == "__main__":
    unittest.main()
