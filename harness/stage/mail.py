"""mail.py - the stage's Mail tab: your own mailbox over IMAP + SMTP. Optional plugin, off until [mail] is set.

    [mail]
    imap_host = "imap.example.com"     # port 993, TLS
    smtp_host = "smtp.example.com"     # port 465 (TLS) or 587 (STARTTLS): smtp_port
    user = "me@example.com"
    password = "vault:MAIL_PASSWORD"   # vault.py set MAIL_PASSWORD (or the stage's Settings); an env var name works too
    from = "Me <me@example.com>"       # optional, defaults to user
    users = [111111111]                # Telegram ids allowed to open it; default: the owner only

Rules (the stage server enforces the sign-in ones):
  - only the allow-listed people, signed in with Telegram initData sent in a header and checked server-side (HMAC);
  - reading needs a normal sign-in; SEND and DELETE need one less than 15 minutes old and an explicit confirm;
  - messages are shown as plain text (HTML mail is reduced to its text), never rendered as a page;
  - every action is one line in <state>/stage/mail_audit.jsonl (who, what, which message; never bodies or passwords),
    and a failure is said to the user, never hidden.
"""
import email
import email.header
import email.policy
import email.utils
import html
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

import config

LIST_MAX = 50
BODY_MAX = 200_000
SEND_MAX = 100_000
ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


class MailError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def settings():
    return config.section("mail") or {}


def enabled(s=None):
    s = settings() if s is None else s
    return bool(s.get("imap_host") and s.get("user") and s.get("password"))


def allowed(uid, s=None):
    s = settings() if s is None else s
    ids = {int(x) for x in (s.get("users") or [config.OWNER_ID])}
    return enabled(s) and int(uid) in ids


def audit(path, who, action, **kw):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "who": who, "action": action, **kw}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _password(s):
    pw = config.secret(s.get("password"))
    if not pw:
        raise MailError(500, f"mail password not found ({s.get('password')})")
    return pw


# ---- IMAP ------------------------------------------------------------------------------------------------------
def imap_connect(s):
    m = imaplib.IMAP4_SSL(s["imap_host"], int(s.get("imap_port", 993)), ssl_context=ssl.create_default_context(),
                          timeout=20)
    m.login(s["user"], _password(s))
    return m


def _ok(typ, what):
    if typ != "OK":
        raise MailError(502, f"mail server refused {what}")


def _hdr(v):
    try:
        return str(email.header.make_header(email.header.decode_header(v or "")))
    except Exception:
        return str(v or "")


def list_messages(s, folder="INBOX", n=30, connect=imap_connect):
    n = max(1, min(int(n), LIST_MAX))
    m = connect(s)
    try:
        _ok(m.select(_folder(folder), readonly=True)[0], "opening the folder")
        typ, data = m.uid("search", None, "ALL")
        _ok(typ, "the search")
        uids = (data[0] or b"").split()[-n:][::-1]
        out = []
        if uids:
            typ, data = m.uid("fetch", b",".join(uids),
                              "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            _ok(typ, "fetching headers")
            for part in data:
                if not isinstance(part, tuple):
                    continue
                meta = part[0].decode(errors="replace")
                uid = re.search(r"UID (\d+)", meta)
                h = email.message_from_bytes(part[1], policy=email.policy.default)
                out.append({"uid": uid.group(1) if uid else "", "from": _hdr(h.get("From")),
                            "subject": _hdr(h.get("Subject")) or "(no subject)", "date": _hdr(h.get("Date")),
                            "seen": "\\Seen" in meta})
            order = {u.decode(): i for i, u in enumerate(uids)}
            out.sort(key=lambda r: order.get(r["uid"], 1e9))
        return out
    finally:
        _close(m)


def text_of(msg):
    """The readable text of a message: text/plain, else HTML reduced to text. Never markup. Pure."""
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()[:BODY_MAX]
    part = msg.get_body(preferencelist=("html",))
    if part is None:
        return ""
    h = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", "", part.get_content())
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()[:BODY_MAX]


def read_message(s, uid, folder="INBOX", connect=imap_connect):
    uid = _uid(uid)
    m = connect(s)
    try:
        _ok(m.select(_folder(folder))[0], "opening the folder")
        typ, data = m.uid("fetch", uid, "(RFC822)")
        _ok(typ, "fetching the message")
        raw = next((p[1] for p in data if isinstance(p, tuple)), None)
        if raw is None:
            raise MailError(404, "no such message")
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        files = [p.get_filename() or "(unnamed)" for p in msg.iter_attachments()]
        return {"uid": uid, "from": _hdr(msg.get("From")), "to": _hdr(msg.get("To")), "cc": _hdr(msg.get("Cc")),
                "subject": _hdr(msg.get("Subject")), "date": _hdr(msg.get("Date")),
                "message_id": str(msg.get("Message-ID") or ""), "text": text_of(msg), "attachments": files}
    finally:
        _close(m)


def delete_message(s, uid, folder="INBOX", connect=imap_connect):
    """Moves it to the server's Trash when there is one (MOVE), else flags it \\Deleted and expunges."""
    uid = _uid(uid)
    trash = s.get("trash", "Trash")
    m = connect(s)
    try:
        _ok(m.select(_folder(folder))[0], "opening the folder")
        if trash and folder != trash:
            typ, _ = m.uid("move", uid, _folder(trash))
            if typ == "OK":
                return {"uid": uid, "moved_to": trash}
        _ok(m.uid("store", uid, "+FLAGS", "(\\Deleted)")[0], "the delete")
        m.expunge()
        return {"uid": uid, "deleted": True}
    finally:
        _close(m)


def _uid(uid):
    if not re.fullmatch(r"\d{1,12}", str(uid)):
        raise MailError(400, "bad message id")
    return str(uid)


def _folder(f):
    f = str(f or "INBOX")
    if not re.fullmatch(r"[\w .\-/\[\]]{1,80}", f):
        raise MailError(400, "bad folder name")
    return '"' + f + '"'


def _close(m):
    for step in ("close", "logout"):
        try:
            getattr(m, step)()
        except Exception:
            pass


# ---- SMTP ------------------------------------------------------------------------------------------------------
def build(s, to, subject, body, cc="", in_reply_to=""):
    """-> EmailMessage, after checking every address. Pure."""
    rcpt = [a for _, a in getaddresses([to or "", cc or ""]) if a]
    if not rcpt or not all(ADDR_RE.match(a) for a in rcpt):
        raise MailError(400, "check the addresses")
    if len(rcpt) > 20:
        raise MailError(400, "at most 20 recipients")
    if not body.strip() or len(body) > SEND_MAX:
        raise MailError(400, f"the message needs text (at most {SEND_MAX} characters)")
    if "\n" in subject or "\r" in subject:
        raise MailError(400, "the subject is one line")
    msg = EmailMessage()
    msg["From"] = s.get("from") or s["user"]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    if in_reply_to and re.fullmatch(r"<[^<>\s]{3,300}>", in_reply_to):
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=parseaddr(s["user"])[1].split("@")[-1] or None)
    msg.set_content(body)
    return msg, rcpt


def smtp_send(s, msg):
    host, port = s.get("smtp_host"), int(s.get("smtp_port", 465))
    if not host:
        raise MailError(500, "no smtp_host in [mail]")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as c:
            c.login(s["user"], _password(s))
            c.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as c:
            c.starttls(context=ctx)
            c.login(s["user"], _password(s))
            c.send_message(msg)


def send(s, to, subject, body, cc="", in_reply_to="", deliver=smtp_send):
    msg, rcpt = build(s, to, subject, body, cc, in_reply_to)
    deliver(s, msg)
    return {"sent": True, "to": rcpt, "message_id": msg["Message-ID"]}
