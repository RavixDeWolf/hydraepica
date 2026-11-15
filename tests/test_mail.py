"""Mail plugin: your own mailbox over IMAP/SMTP, plain text only, confirms on send/delete, every action audited."""
import _harness_env  # noqa: F401  throwaway config + state (must be first)
import json
import os
import sys
import unittest
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "harness", "stage"))
import test_stage_server as t  # noqa: E402  (sets up the stage app against temp state)
import mail  # noqa: E402

S = {"imap_host": "imap.example.com", "smtp_host": "smtp.example.com", "user": "me@example.com", "password": "MAIL_TEST_PW",
     "users": [t.OWNER]}


def raw(subject="Hi", body="hello there", html=None, frm="Ann <ann@example.com>"):
    m = EmailMessage(); m["From"] = frm; m["To"] = "me@example.com"; m["Subject"] = subject
    m["Date"] = "Sun, 15 Nov 2026 04:00:00 -0600"; m["Message-ID"] = "<abc@example.com>"
    if html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


class FakeImap:
    def __init__(self, msgs):
        self.msgs, self.calls, self.closed = dict(msgs), [], False

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly)); return "OK", [b"1"]

    def uid(self, cmd, *a):
        self.calls.append((cmd,) + a)
        if cmd == "search":
            return "OK", [b" ".join(k.encode() for k in sorted(self.msgs, key=int))]
        if cmd == "fetch":
            out = []
            for u in a[0].decode().split(",") if isinstance(a[0], bytes) else [a[0]]:
                if u in self.msgs:
                    out.append((f"{u} (UID {u} FLAGS ())".encode(), self.msgs[u]))
            return "OK", out
        if cmd == "move":
            return "OK", [b""]
        return "OK", [b""]

    def expunge(self): self.calls.append(("expunge",))
    def close(self): self.closed = True
    def logout(self): pass


class Unit(unittest.TestCase):
    def test_list_newest_first_and_headers(self):
        f = FakeImap({"1": raw("old"), "2": raw("new")})
        out = mail.list_messages(S, connect=lambda s: f)
        self.assertEqual([m["subject"] for m in out], ["new", "old"])
        self.assertEqual(out[0]["from"], "Ann <ann@example.com>")
        self.assertTrue(f.calls[0][2])                       # listing opens the folder read-only
        self.assertTrue(f.closed)

    def test_html_mail_is_reduced_to_text(self):
        f = FakeImap({"7": raw(html="<html><head><style>x{}</style></head><body><p>Hello <b>you</b></p>"
                                    "<script>alert(1)</script><a href='http://x'>link</a></body></html>")})
        m = mail.read_message(S, "7", connect=lambda s: f)
        self.assertIn("Hello you", m["text"])
        self.assertNotIn("<", m["text"]); self.assertNotIn("alert", m["text"])

    def test_bad_ids_and_folders_refused(self):
        for bad in ("1 OR 2", "../x", ""):
            with self.assertRaises(mail.MailError):
                mail.read_message(S, bad, connect=lambda s: FakeImap({}))
        with self.assertRaises(mail.MailError):
            mail.list_messages(S, folder='INBOX" DELETE', connect=lambda s: FakeImap({}))

    def test_delete_moves_to_trash(self):
        f = FakeImap({"3": raw()})
        self.assertEqual(mail.delete_message(S, "3", connect=lambda s: f), {"uid": "3", "moved_to": "Trash"})

    def test_build_checks_addresses_and_headers(self):
        msg, rcpt = mail.build(S, "Bob <bob@example.com>", "Re: hi", "text", in_reply_to="<abc@example.com>")
        self.assertEqual(rcpt, ["bob@example.com"]); self.assertEqual(msg["In-Reply-To"], "<abc@example.com>")
        for to, subj, body in (("not-an-address", "s", "b"), ("bob@example.com", "two\nlines", "b"),
                               ("bob@example.com", "s", "   ")):
            with self.assertRaises(mail.MailError):
                mail.build(S, to, subj, body)

    def test_send_uses_the_deliverer(self):
        got = []
        out = mail.send(S, "bob@example.com", "s", "b", deliver=lambda s, m: got.append(m))
        self.assertTrue(out["sent"]); self.assertEqual(got[0]["To"], "bob@example.com")

    def test_off_until_configured_and_allow_list(self):
        self.assertFalse(mail.enabled({})); self.assertTrue(mail.allowed(t.OWNER, S))
        self.assertFalse(mail.allowed(t.USER_C, S))


class Api(unittest.TestCase):
    def setUp(self):
        self._o = (mail.settings, mail.imap_connect, mail.smtp_send)
        mail.settings = lambda: S
        self.f = FakeImap({"5": raw("Invoice")})
        mail.list_messages.__defaults__ = ("INBOX", 30, lambda s: self.f)
        mail.read_message.__defaults__ = ("INBOX", lambda s: self.f)
        mail.delete_message.__defaults__ = ("INBOX", lambda s: self.f)
        self.sent = []
        mail.send.__defaults__ = ("", "", lambda s, m: self.sent.append(m))
        if t.s.MAIL_AUDIT.exists():
            t.s.MAIL_AUDIT.unlink()

    def tearDown(self):
        mail.settings, mail.imap_connect, mail.smtp_send = self._o
        mail.list_messages.__defaults__ = ("INBOX", 30, mail.imap_connect)
        mail.read_message.__defaults__ = ("INBOX", mail.imap_connect)
        mail.delete_message.__defaults__ = ("INBOX", mail.imap_connect)
        mail.send.__defaults__ = ("", "", mail.smtp_send)

    def audit(self):
        return [json.loads(l) for l in open(t.s.MAIL_AUDIT)]

    def test_owner_reads_others_refused(self):
        self.assertTrue(t.c.get("/api/me", headers=t.H(t.OWNER)).json()["mail"])
        self.assertFalse(t.c.get("/api/me", headers=t.H(t.USER_C)).json()["mail"])
        r = t.c.get("/api/mail", headers=t.H(t.OWNER)); self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["messages"][0]["subject"], "Invoice")
        self.assertEqual(t.c.get("/api/mail/msg/5", headers=t.H(t.OWNER)).json()["subject"], "Invoice")
        self.assertEqual(t.c.get("/api/mail", headers=t.H(t.USER_C)).status_code, 403)
        self.assertEqual(t.c.get("/api/mail").status_code, 401)
        self.assertEqual([a["action"] for a in self.audit()], ["list", "read"])

    def test_no_url_tickets_for_mail(self):
        tk = t.c.get("/api/ticket", headers=t.H(t.OWNER)).json().get("t")
        if tk:
            self.assertEqual(t.c.get(f"/api/mail?t={tk}").status_code, 401)

    def test_send_needs_confirm_and_fresh_signin(self):
        body = {"to": "bob@example.com", "subject": "s", "text": "hello"}
        self.assertEqual(t.c.post("/api/mail/send", json=body, headers=t.H(t.OWNER)).status_code, 400)
        self.assertEqual(t.c.post("/api/mail/send", json=dict(body, confirm=True), headers=t.H(t.OWNER, age=1000)).status_code, 401)
        r = t.c.post("/api/mail/send", json=dict(body, confirm=True), headers=t.H(t.OWNER))
        self.assertEqual(r.status_code, 200, r.text); self.assertEqual(len(self.sent), 1)
        a = self.audit()[-1]
        self.assertEqual((a["action"], a["ok"], a["to"]), ("send", True, "bob@example.com"))
        self.assertNotIn("hello", json.dumps(self.audit()))           # never the body

    def test_delete_needs_confirm(self):
        self.assertEqual(t.c.post("/api/mail/delete", json={"uid": "5"}, headers=t.H(t.OWNER)).status_code, 400)
        r = t.c.post("/api/mail/delete", json={"uid": "5", "confirm": True}, headers=t.H(t.OWNER))
        self.assertEqual(r.json()["moved_to"], "Trash")

    def test_server_failure_is_said_and_audited(self):
        def down(s):
            raise OSError("connection refused")
        mail.list_messages.__defaults__ = ("INBOX", 30, down)
        r = t.c.get("/api/mail", headers=t.H(t.OWNER))
        self.assertEqual(r.status_code, 502); self.assertIn("connection refused", r.json()["detail"])
        self.assertEqual(self.audit()[-1]["ok"], False)
