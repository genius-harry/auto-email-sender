"""End-to-end tests against the in-memory mock server. Stdlib only:

    python -m unittest discover -s tests -v

Email is irreversible, so the suite locks down the failure modes that matter:
time parsing (DST), validation, idempotent dedupe, flag-aware keys, attachment
confirmation, per-recipient draft failures, and the recontact guard.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "gmail_pipeline.py")
sys.path.insert(0, ROOT)
import gmail_pipeline as gp  # noqa: E402

BODY = ("Hi there,\n\nThis test body is deliberately long enough to clear the two "
        "hundred character validation floor, because the validator treats short "
        "bodies as a symptom of a failed template merge and refuses to submit them.\n\n"
        "Best,\nTests")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="aes-tests-")
        cls.port = free_port()
        cls.mock = subprocess.Popen([sys.executable, os.path.join(ROOT, "mock_server.py"), str(cls.port)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        cls.env = dict(os.environ, AUTO_EMAIL_CONFIG=os.path.join(cls.tmp, "config.json"))
        r = cls.cli("init", "--url", f"http://127.0.0.1:{cls.port}/exec", "--secret", "testsecret-123")
        assert r.returncode == 0, r.stdout + r.stderr

    @classmethod
    def tearDownClass(cls):
        cls.mock.kill()

    @classmethod
    def cli(cls, *args):
        return subprocess.run([sys.executable, CLI, *args], env=cls.env,
                              capture_output=True, text=True, cwd=cls.tmp)

    def batch(self, name, emails):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            json.dump(emails, f, ensure_ascii=False)
        return path

    # ---------- time parsing ----------

    def test_parse_send_at_is_dst_aware(self):
        from datetime import datetime, timezone
        summer = gp.parse_send_at("2026-07-10 09:00", "ET")   # July: EDT = UTC-4
        self.assertEqual(summer, int(datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc).timestamp() * 1000))
        winter = gp.parse_send_at("2026-01-09 09:00", "ET")   # January: EST = UTC-5
        self.assertEqual(winter, int(datetime(2026, 1, 9, 14, 0, tzinfo=timezone.utc).timestamp() * 1000))

    def test_parse_send_at_fixed_and_relative(self):
        fixed = gp.parse_send_at("2026-07-10 09:00", "EST")   # forced UTC-5 even in July
        summer = gp.parse_send_at("2026-07-10 09:00", "ET")
        self.assertEqual(fixed - summer, 3600 * 1000)
        rel = gp.parse_send_at("+10m")
        self.assertAlmostEqual(rel / 1000, time.time() + 600, delta=5)
        with self.assertRaises(ValueError):
            gp.parse_send_at("2026-07-10 09:00", "NOPE")

    # ---------- validation ----------

    def test_validator_catches_classic_mistakes(self):
        emails = [
            {"to": "a@example.com", "subject": "t", "body": "Hi {First Name},\n" + BODY},
            {"to": "a@example.com", "subject": "t", "body": BODY},          # duplicate
            {"to": "b@example.com", "subject": "t", "body": "Hello,\n" + BODY},  # empty greeting
            {"to": "c@example.com", "subject": "t", "body": "too short"},
        ]
        r = self.cli("validate", "--batch", self.batch("bad.json", emails))
        self.assertEqual(r.returncode, 1)
        for needle in ("unfilled placeholder", "duplicate", "chars"):
            self.assertIn(needle, r.stdout)

    # ---------- idempotency ----------

    def test_absolute_time_resubmit_is_deduped(self):
        b = self.batch("dedupe.json", [{"to": "x@example.com", "subject": "t", "body": BODY}])
        args = ("submit", "--batch", b, "--send-at", "2026-08-01 09:00", "--tz", "ET",
                "--no-tracker-check", "--label", "t-dedupe", "--yes")
        r1 = self.cli(*args)
        self.assertEqual(r1.returncode, 0, r1.stdout)
        r2 = self.cli(*args)
        self.assertIn("deduped", r2.stdout)

    def test_send_shaping_flags_change_the_key(self):
        b = self.batch("flags.json", [{"to": "y@example.com", "subject": "t", "body": BODY}])
        base = ("submit", "--batch", b, "--send-at", "2026-08-01 10:00", "--tz", "ET",
                "--no-tracker-check", "--label", "t-flags", "--yes")
        r1 = self.cli(*base)
        self.assertEqual(r1.returncode, 0, r1.stdout)
        r2 = self.cli(*base, "--plain")
        self.assertNotIn("deduped", r2.stdout)   # different flags => different batch

    # ---------- attachments ----------

    def test_attachments_confirmed_end_to_end(self):
        att = os.path.join(self.tmp, "a.csv")
        with open(att, "w") as f:
            f.write("k,v\n1,2\n")
        b = self.batch("att.json", [{"to": "z@example.com", "subject": "t", "body": BODY}])
        r = self.cli("submit", "--batch", b, "--send-at", "2026-08-01 11:00", "--tz", "ET",
                     "--no-tracker-check", "--label", "t-att", "--yes", "--attach", att)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("ATTACHMENT ERROR", r.stdout)
        receipts = [f for f in os.listdir(os.path.join(ROOT, "receipts")) if f.startswith("t-att-")]
        self.assertTrue(receipts)
        with open(os.path.join(ROOT, "receipts", sorted(receipts)[-1])) as f:
            self.assertEqual(json.load(f)[0]["attached"], 1)

    # ---------- per-recipient draft failures ----------

    def test_draft_failures_produce_retryable_file(self):
        b = self.batch("fail.json", [
            {"to": "faildraft@example.com", "subject": "t", "body": BODY},
            {"to": "ok@example.com", "subject": "t", "body": BODY},
        ])
        r = self.cli("submit", "--batch", b, "--send-at", "2026-08-01 12:00", "--tz", "ET",
                     "--no-tracker-check", "--label", "t-fail", "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED at draft creation", r.stdout)
        fails = [f for f in os.listdir(os.path.join(ROOT, "receipts")) if f.startswith("failed-t-fail-")]
        self.assertTrue(fails, "failed-recipients file missing")
        with open(os.path.join(ROOT, "receipts", sorted(fails)[-1])) as f:
            data = json.load(f)
        self.assertEqual([e["to"] for e in data], ["faildraft@example.com"])

    # ---------- recontact guard ----------

    def test_tracker_recontact_is_a_hard_error(self):
        tracker = os.path.join(self.tmp, "tracker.csv")
        with open(tracker, "w") as f:
            f.write("firm,email,status\nAcme,seen@example.com,sent\n")
        b = self.batch("guard.json", [{"to": "seen@example.com", "subject": "t", "body": BODY}])
        r = self.cli("submit", "--batch", b, "--send-at", "2026-08-01 13:00", "--tz", "ET",
                     "--tracker", tracker, "--label", "t-guard", "--yes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ALREADY in tracker", r.stdout)


if __name__ == "__main__":
    unittest.main()
