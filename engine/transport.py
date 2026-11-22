"""Email transports. Both implement:
    send(key, raw)     deliver one message
    was_sent(key)      did a message with this idempotency key already go out?
    replies()          raw reply emails waiting in the test inbox (our own sends excluded)

FileTransport writes .eml files (local runs, tests). SmtpImapTransport sends via SMTP and
reads replies via IMAP from a team-controlled test inbox; it reconciles "was it sent?" by
searching for the X-BoxSource-Key header, so a crash between SMTP accepting the message
and the outbox row being written never causes a second send.
"""

import email
import imaplib
import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email import policy
from pathlib import Path

KEY_HEADER = "X-BoxSource-Key"


def _is_own(raw):
    msg = email.message_from_bytes(raw.encode("utf-8", "surrogateescape"), policy=policy.default)
    return msg.get(KEY_HEADER) is not None


class FileTransport:
    def __init__(self, outbox_dir, inbox_dir):
        self.outbox, self.inbox = Path(outbox_dir), Path(inbox_dir)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        return self.outbox / f"{key}.eml"

    def send(self, key, raw):
        tmp = self.outbox / f"{key}.tmp"
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, self._path(key))  # atomic: the file is either complete or absent

    def was_sent(self, key):
        return self._path(key).exists()

    def replies(self):
        return [raw for f in sorted(self.inbox.glob("*.eml"))
                if not _is_own(raw := f.read_text(encoding="utf-8", errors="surrogateescape"))]


class SmtpImapTransport:
    """Gmail-style test inbox: SMTP over SSL to send, IMAP over SSL to read."""

    def __init__(self, cfg):
        e = cfg["email"]
        self.smtp_host = os.environ["SMTP_HOST"]
        self.smtp_port = int(os.environ.get("SMTP_PORT", "465"))
        self.smtp_user, self.smtp_pass = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
        self.imap_host = os.environ["IMAP_HOST"]
        self.imap_user = os.environ.get("IMAP_USER", self.smtp_user)
        self.imap_pass = os.environ.get("IMAP_PASS", self.smtp_pass)
        self.sent_folder = e.get("sent_folder", '"[Gmail]/Sent Mail"')
        self.inbox_folder = e.get("inbox_folder", "INBOX")
        self.lookback_days = e.get("reply_lookback_days", 3)

    def send(self, key, raw):
        msg = email.message_from_string(raw, policy=policy.default)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=ctx, timeout=30) as s:
            s.login(self.smtp_user, self.smtp_pass)
            s.send_message(msg)

    def _imap(self):
        m = imaplib.IMAP4_SSL(self.imap_host)
        m.login(self.imap_user, self.imap_pass)
        return m

    def was_sent(self, key):
        m = self._imap()
        try:
            for folder in (self.sent_folder, self.inbox_folder):
                if m.select(folder, readonly=True)[0] != "OK":
                    continue
                typ, data = m.search(None, "HEADER", KEY_HEADER, key)
                if typ == "OK" and data and data[0].split():
                    return True
            return False
        finally:
            m.logout()

    def replies(self):
        since = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).strftime("%d-%b-%Y")
        m = self._imap()
        try:
            m.select(self.inbox_folder, readonly=True)  # readonly: never mutate \Seen flags
            typ, data = m.search(None, "SINCE", since)
            out = []
            for num in (data[0].split() if typ == "OK" and data else []):
                typ, parts = m.fetch(num, "(RFC822)")
                if typ == "OK" and parts and isinstance(parts[0], tuple):
                    raw = parts[0][1].decode("utf-8", errors="surrogateescape")  # lossless
                    if not _is_own(raw):
                        out.append(raw)
            return out
        finally:
            m.logout()


def open_transport(cfg):
    e = cfg["email"]
    if e["transport"] == "file":
        return FileTransport(e["outbox_dir"], e["inbox_dir"])
    if e["transport"] == "smtp":
        return SmtpImapTransport(cfg)
    raise ValueError(f"unknown email transport {e['transport']!r}")
