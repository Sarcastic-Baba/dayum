"""Kill the worker at every action boundary, restart it, and assert that each email is sent
exactly once and each quote is ingested exactly once (PRD §13).

Windows has no SIGKILL, so most kills use the deterministic crash hook
BOXSOURCE_CRASH_AT=<boundary> (os._exit — no cleanup, no flush, like a hard kill). One test
also hard-kills a running worker with Popen.kill() at an arbitrary moment.

Runs on a throwaway SQLite database by default. BOXSOURCE_TEST_BACKEND=supabase runs the same
assertions against the live Supabase project: each test gets a unique request id, the worker
only touches that id (BOXSOURCE_ONLY_PREFIX), and the rows are deleted afterwards.
All supplier data is fixture data (tests/fixture_data.py).
"""

import os
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv

from engine import emails
from engine.actions import send_key
from engine.config import load_config
from engine.db import open_db
from fixture_data import cleanup, seed

ROOT = Path(__file__).resolve().parents[1]
BACKEND = os.environ.get("BOXSOURCE_TEST_BACKEND", "sqlite")
if BACKEND == "supabase":
    load_dotenv(ROOT / ".env")
REPLIES = {"sup_01": "Our price is Rs. 37.20 per box ex-GST for 5 ply.",
           "sup_02": "Price: ₹39.50/box (excluding GST), 5-ply."}

CRASH_POINTS = [
    "after_state:WAIT_APPROVAL_RFQ",
    "send_rfq:before_send", "send_rfq:after_send", "send_rfq:after_outbox",
    "after_state:RFQS_SENT",
    "quote:before_insert", "quote:after_insert",
    "after_state:QUOTES_RECEIVED",
    "send_po:before_send", "send_po:after_send", "send_po:after_outbox",
    "after_state:PO_SENT",
]


@pytest.fixture
def env(tmp_path):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["storage"] = ({"backend": "supabase"} if BACKEND == "supabase"
                      else {"backend": "sqlite", "sqlite_path": str(tmp_path / "test.db")})
    cfg["email"].update(transport="file", outbox_dir=str(tmp_path / "outbox"),
                        inbox_dir=str(tmp_path / "inbox"))
    cfg["log_path"] = str(tmp_path / "run.log")
    cfg["quote_wait_s"] = 3600
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    rid = f"T-{uuid.uuid4().hex[:8]}"
    e = {**os.environ, "BOXSOURCE_CONFIG": str(path), "PYTHONIOENCODING": "utf-8",
         "BOXSOURCE_ONLY_PREFIX": rid}
    e.pop("BOXSOURCE_CRASH_AT", None)
    cfg = load_config(path)
    db = open_db(cfg)
    seed(db, rid)
    yield cfg, e, rid, db
    if BACKEND == "supabase":
        cleanup(db, rid)


def worker(e, crash_at=None):
    e = dict(e)
    if crash_at:
        e["BOXSOURCE_CRASH_AT"] = crash_at
    return subprocess.run([sys.executable, "cli.py", "run", "--until-idle", "--interval", "0",
                           "--max-cycles", "20"],
                          cwd=ROOT, env=e, capture_output=True, text=True, timeout=600)


def cli(e, *args):
    p = subprocess.run([sys.executable, "cli.py", *args], cwd=ROOT, env=e,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def write_replies(cfg, rid):
    """The team replying as the sellers, threaded onto the RFQs that went out."""
    inbox = Path(cfg["email"]["inbox_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    for sid, line in REPLIES.items():
        raw = textwrap.dedent(f"""\
            From: seller+{sid}@example.invalid
            To: rfq-test+{sid}@example.invalid
            Subject: Re: Request for quotation [{rid}/{sid}] - corrugated box
            Message-ID: <reply-{rid}-{sid}@example.invalid>
            In-Reply-To: {emails.message_id(send_key(rid, "send_rfq", sid))}
            Date: Sun, 22 Nov 2026 16:05:00 +0530
            Content-Type: text/plain; charset=utf-8

            Hello,
            {line}
            MOQ: 10,000 boxes
            Lead time: 12 days
            Regards

            On Sun, 22 Nov 2026, BoxSource wrote:
            > Price per box in INR, stating whether it is ex-GST or including GST
            """)
        (inbox / f"reply-{sid}.eml").write_text(raw, encoding="utf-8")


def state(db, rid):
    return db.select("requests", {"request_id": rid})[0]


def drive(cfg, e, rid, db, crash_at=None):
    """Run the request to DONE. The crash hook stays armed until it fires once; after a
    crash the worker is simply restarted, exactly as the team would on stage."""
    fired = False

    def phase(expect):
        nonlocal fired
        if crash_at and not fired:
            p = worker(e, crash_at)
            if p.returncode != 0:
                fired = True
        p = worker(e)
        assert p.returncode == 0, p.stdout + p.stderr
        assert state(db, rid)["state"] == expect, (expect, state(db, rid))

    phase("WAIT_APPROVAL_RFQ")
    cli(e, "approve", rid, "--action", "send_rfq", "--approver", "Test Approver")
    phase("WAIT_QUOTES")
    write_replies(cfg, rid)
    phase("WAIT_APPROVAL_PO")
    cli(e, "approve", rid, "--action", "send_po", "--approver", "Test Approver")
    phase("DONE")
    return fired


def assert_exactly_once(cfg, rid, db):
    outbox = Path(cfg["email"]["outbox_dir"])
    emls, tmps = sorted(outbox.glob("*.eml")), sorted(outbox.glob("*.tmp"))
    assert len(emls) == 3 and not tmps, (emls, tmps)          # 2 RFQs + 1 PO, nothing partial
    keys = {send_key(rid, "send_rfq", "sup_01"), send_key(rid, "send_rfq", "sup_02"),
            send_key(rid, "send_po", "sup_01")}
    assert {o["idem_key"] for o in db.select("outbox", {"idem_key": ("in", sorted(keys))})} == keys
    quotes = db.select("quotes", {"request_id": rid})
    assert len(quotes) == 2 and all(q["parsed_ok"] for q in quotes)
    result = state(db, rid)["result_json"]
    assert result["recommendation"]["primary"] == "sup_01"     # 37.20 < 39.50 ex-GST
    assert all(a["status"] == "done" for a in result["actions"])
    previews = db.select("approvals", {"request_id": rid})
    assert previews and all(a["email_body"] for a in previews)  # the approver saw the text


def test_full_run_without_crash(env):
    cfg, e, rid, db = env
    drive(cfg, e, rid, db)
    assert_exactly_once(cfg, rid, db)


@pytest.mark.parametrize("crash_at", CRASH_POINTS)
def test_kill_and_restart_at_each_boundary(env, crash_at):
    cfg, e, rid, db = env
    assert drive(cfg, e, rid, db, crash_at), f"crash point {crash_at} was never reached"
    assert_exactly_once(cfg, rid, db)


def test_hard_kill_of_a_running_worker(env):
    """Popen.kill() (TerminateProcess on Windows) at an arbitrary moment, then restart."""
    cfg, e, rid, db = env
    worker(e)
    cli(e, "approve", rid, "--action", "send_rfq", "--approver", "Test Approver")
    looping = subprocess.Popen([sys.executable, "cli.py", "run", "--interval", "0.05"],
                               cwd=ROOT, env=e, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    looping.kill()
    looping.wait(timeout=30)
    assert worker(e).returncode == 0
    write_replies(cfg, rid)
    assert worker(e).returncode == 0
    cli(e, "approve", rid, "--action", "send_po", "--approver", "Test Approver")
    assert worker(e).returncode == 0
    assert state(db, rid)["state"] == "DONE"
    assert_exactly_once(cfg, rid, db)


def test_approval_decisions_are_final_and_freeze_the_draft(env):
    """The schema's guard: a decided approval can't change, and the email the approver saw
    can't be altered in the same update as the decision."""
    cfg, e, rid, db = env
    worker(e)  # drafts the RFQs and creates the pending approvals
    a = db.select("approvals", {"request_id": rid, "supplier_id": "sup_01"})[0]
    with pytest.raises(Exception):
        db.update("approvals", {"id": a["id"]}, {"status": "approved", "approver": "x",
                                                 "decided_at": "2026-11-22T00:00:00+00:00",
                                                 "email_body": "something else"})
    cli(e, "approve", rid, "--action", "send_rfq", "--approver", "Test Approver")
    with pytest.raises(Exception):
        db.update("approvals", {"id": a["id"]}, {"status": "rejected", "reason": "changed mind"})
