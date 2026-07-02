#!/usr/bin/env python3
"""
Mock of the Apps Script web app (Code.gs) for LOCAL end-to-end testing of the CLI.
Implements the same POST contract: ping / submit (idempotent via client_key) /
status / cancel / send_now. No email is ever sent — everything is in-memory.

  python3 mock_server.py [port]          # default 8787
then:
  AUTO_EMAIL_CONFIG=/tmp/aes-test-config.json \
  python3 gmail_pipeline.py init --url http://127.0.0.1:8787/exec --secret testsecret-123
"""
import json
import random
import re
import string
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = "testsecret-123"
BATCHES = {}
KEYS = {}  # client_key -> {batch_id, drafted, sendAtMs}
EMAIL_RX = re.compile(r"^[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]{2,}$")


def rid():
    return "b" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def counts_of(items):
    c = {"pending": 0, "sent": 0, "failed": 0, "sent_assumed": 0, "cancelled": 0}
    for it in items:
        c[it["state"]] = c.get(it["state"], 0) + 1
    return c


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"ok": True, "service": "mock"})

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._send({"ok": False, "error": "bad JSON body"})
        if req.get("secret") != SECRET:
            return self._send({"ok": False, "error": "auth failed"})
        action = req.get("action")

        if action == "ping":
            return self._send({"ok": True, "pong": True, "version": 4, "account": "mock@example.com",
                               "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

        if action == "submit":
            emails = req.get("emails") or []
            if not emails:
                return self._send({"ok": False, "error": "no emails"})
            if len(emails) > 30:
                return self._send({"ok": False, "error": "chunk too big (max 30 per POST)"})
            ms = req.get("send_at_ms")
            if not ms or ms < time.time() * 1000 - 60000:
                return self._send({"ok": False, "error": "send_at_ms missing or in the past"})
            key = str(req.get("client_key") or "")
            if len(key) < 8:
                return self._send({"ok": False, "error": "client_key (>=8 chars) required for idempotency"})
            for i, em in enumerate(emails):  # server-side validation parity
                if not isinstance(em.get("to"), str) or not EMAIL_RX.fullmatch(em["to"].strip()):
                    return self._send({"ok": False, "error": f"invalid to-address at index {i}"})
                subj = em.get("subject")
                if not isinstance(subj, str) or not subj.strip() or len(subj) > 200 or re.search(r"[\r\n]", subj):
                    return self._send({"ok": False, "error": f"invalid subject at index {i}"})
            atts = req.get("attachments") or []  # v4 parity: decode-validate, applied per chunk
            if len(atts) > 10:
                return self._send({"ok": False, "error": "too many attachments (max 10)"})
            total = 0
            for i, at in enumerate(atts):
                if not isinstance(at.get("data"), str) or not at["data"]:
                    return self._send({"ok": False, "error": f"attachment {i} missing base64 data"})
                try:
                    import base64 as _b64
                    total += len(_b64.b64decode(at["data"], validate=True))
                except Exception:
                    return self._send({"ok": False, "error": f"attachment {i} is not valid base64"})
                if total > 22 * 1024 * 1024:
                    return self._send({"ok": False, "error": "attachments exceed 22MB decoded total"})
            if key in KEYS:  # idempotent replay
                k = KEYS[key]
                return self._send({"ok": True, "deduped": True, "batch_id": k["batch_id"],
                                   "drafted": k["drafted"], "draft_failures": [],
                                   "send_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                time.gmtime(k["sendAtMs"] / 1000))})
            # contract parity: a local part starting with "faildraft" simulates a
            # per-email draft-creation failure (reported in draft_failures, not drafted)
            ok_emails = [em for em in emails if not em["to"].strip().lower().startswith("faildraft")]
            failures = [{"to": em["to"].strip(), "error": "mock: simulated draft failure"}
                        for em in emails if em["to"].strip().lower().startswith("faildraft")]
            items = [{"to": em["to"].strip(), "draftId": "d" + rid(), "state": "pending", "attempts": 0}
                     for em in ok_emails]
            bid = rid()
            BATCHES[bid] = {"id": bid, "sendAtMs": ms, "label": req.get("label", ""),
                            "state": "pending", "items": items, "created": time.time() * 1000}
            KEYS[key] = {"batch_id": bid, "drafted": len(items), "sendAtMs": ms}
            return self._send({"ok": True, "batch_id": bid, "drafted": len(items),
                               "draft_failures": failures, "attached": len(atts),
                               "send_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))})

        if action == "status":
            out = []
            for b in BATCHES.values():
                if req.get("batch_id") and b["id"] != req["batch_id"]:
                    continue
                e = {"batch_id": b["id"], "label": b["label"], "state": b["state"],
                     "send_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(b["sendAtMs"] / 1000)),
                     "counts": counts_of(b["items"])}
                if req.get("verbose"):
                    e["items"] = b["items"]
                out.append(e)
            return self._send({"ok": True, "account": "mock@example.com", "batches": out})

        if action == "cancel":
            b = BATCHES.get(req.get("batch_id"))
            if not b:
                return self._send({"ok": False, "error": "no such batch"})
            n = 0
            for it in b["items"]:
                if it["state"] == "pending":
                    it["state"] = "cancelled"
                    n += 1
            res = {"ok": True, "batch_id": b["id"], "cancelled": n,
                   "trashed": n if req.get("trash_drafts") else 0}
            if n:
                b["state"] = "cancelled"
            else:
                res["note"] = "nothing was pending (already sent / failed / cancelled) — state unchanged"
            return self._send(res)

        if action == "send_now":
            b = BATCHES.get(req.get("batch_id"))
            if not b:
                return self._send({"ok": False, "error": "no such batch"})
            if b["state"] == "cancelled":
                return self._send({"ok": False, "error": "batch is cancelled"})
            for it in b["items"]:
                if it["state"] == "pending":
                    it["state"] = "sent"
                    it["sentAt"] = time.time() * 1000
            b["state"] = "done"
            return self._send({"ok": True, "batch_id": b["id"], "state": b["state"],
                               "counts": counts_of(b["items"])})

        return self._send({"ok": False, "error": f"unknown action: {action}"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"mock Apps Script on http://127.0.0.1:{port}/exec  (secret: {SECRET})")
    HTTPServer(("127.0.0.1", port), H).serve_forever()
