"""Kill the worker at every action boundary, restart it, and assert that each email is sent
exactly once and each quote is ingested exactly once (PRD §13).

Windows has no SIGKILL, so most kills use the deterministic crash hook
BOXSOURCE_CRASH_AT=<boundary> (os._exit — no cleanup, no flush, like a hard kill). One test
also hard-kills a running worker with Popen.kill() at an arbitrary moment.

All supplier data here is test fixture data (mock data lives only in tests/).
"""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from engine import emails
from engine.actions import send_key
from engine.db import SQLiteDB

ROOT = Path(__file__).resolve().parents[1]
RID = "SRC-T01"
SPEC = {"request_id": RID, "item": "corrugated box",
        "spec": {"ply": 5, "size_in": [18, 12, 12], "material": "kraft"},
        "quantity_per_month": 50000, "delivery_location": "Gurgaon, Haryana",
        "needed_by": "2026-12-15", "budget_inr_per_unit": None}
SUPPLIERS = [("sup_01", "Alpha Packaging (fixture)", 36.0, 30),
             ("sup_02", "Beta Cartons (fixture)", None, 55)]
REPLIES = {"sup_01": "Our price is Rs. 37.20 per box ex-GST for 5 ply.",
           "sup_02": "Price: ₹39.50/box (excluding GST), 5-ply."}
CHECKS = ("real_active", "manufacturer", "duplicate", "spec_match", "price", "distance",
          "fraud_risk")

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
    cfg["storage"] = {"backend": "sqlite", "sqlite_path": str(tmp_path / "test.db")}
    cfg["email"].update(transport="file", outbox_dir=str(tmp_path / "outbox"),
                        inbox_dir=str(tmp_path / "inbox"))
    cfg["log_path"] = str(tmp_path / "run.log")
    cfg["quote_wait_s"] = 3600
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    e = {**os.environ, "BOXSOURCE_CONFIG": str(path), "PYTHONIOENCODING": "utf-8"}
    e.pop("BOXSOURCE_CRASH_AT", None)
    seed(cfg)
    return cfg, e


def seed(cfg):
    """A request that has already passed discovery and the checks."""
    db = SQLiteDB(cfg["storage"]["sqlite_path"])
    db.insert("requests", {"request_id": RID, "input_json": SPEC, "spec_json": SPEC,
                           "state": "CANDIDATES_CHECKED"})
    for sid, name, listed, dist in SUPPLIERS:
        db.insert("candidates", {"request_id": RID, "supplier_id": sid, "name": name,
                                 "website": f"https://{sid}.example.invalid",
                                 "classification": "manufacturer",
                                 "data_json": {"listed_price_inr_per_unit": listed,
                                               "distance_km": dist}})
        for check in CHECKS:
            db.insert("checks", {"request_id": RID, "supplier_id": sid, "check_name": check,
                                 "result": "pass", "explanation": "fixture"})


def worker(e, crash_at=None):
    e = dict(e)
    if crash_at:
        e["BOXSOURCE_CRASH_AT"] = crash_at
    return subprocess.run([sys.executable, "cli.py", "run", "--until-idle", "--interval", "0",
                           "--max-cycles", "20"],
                          cwd=ROOT, env=e, capture_output=True, text=True, timeout=120)


def cli(e, *args):
    p = subprocess.run([sys.executable, "cli.py", *args], cwd=ROOT, env=e,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def write_replies(cfg):
    """The team replying as the sellers, threaded onto the RFQs that went out."""
    inbox = Path(cfg["email"]["inbox_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    for sid, line in REPLIES.items():
        raw = textwrap.dedent(f"""\
            From: seller+{sid}@example.invalid
            To: rfq-test+{sid}@example.invalid
            Subject: Re: Request for quotation [{RID}/{sid}] - corrugated box
            Message-ID: <reply-{sid}@example.invalid>
            In-Reply-To: {emails.message_id(send_key(RID, "send_rfq", sid))}
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


def state(cfg):
    return SQLiteDB(cfg["storage"]["sqlite_path"]).select("requests", {"request_id": RID})[0]


def drive(cfg, e, crash_at=None):
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
        assert state(cfg)["state"] == expect, (expect, state(cfg))

    phase("WAIT_APPROVAL_RFQ")
    cli(e, "approve", RID, "--action", "send_rfq", "--approver", "Test Approver")
    phase("WAIT_QUOTES")
    write_replies(cfg)
    phase("WAIT_APPROVAL_PO")
    cli(e, "approve", RID, "--action", "send_po", "--approver", "Test Approver")
    phase("DONE")
    return fired


def assert_exactly_once(cfg):
    db = SQLiteDB(cfg["storage"]["sqlite_path"])
    outbox = Path(cfg["email"]["outbox_dir"])
    emls, tmps = sorted(outbox.glob("*.eml")), sorted(outbox.glob("*.tmp"))
    assert len(emls) == 3 and not tmps, (emls, tmps)          # 2 RFQs + 1 PO, nothing partial
    assert len(db.select("outbox")) == 3
    assert {o["idem_key"] for o in db.select("outbox")} == {
        send_key(RID, "send_rfq", "sup_01"), send_key(RID, "send_rfq", "sup_02"),
        send_key(RID, "send_po", "sup_01")}
    quotes = db.select("quotes", {"request_id": RID})
    assert len(quotes) == 2 and all(q["parsed_ok"] for q in quotes)
    result = state(cfg)["result_json"]
    assert result["recommendation"]["primary"] == "sup_01"     # 37.20 < 39.50 ex-GST
    assert all(a["status"] == "done" for a in result["actions"])


def test_full_run_without_crash(env):
    cfg, e = env
    drive(cfg, e)
    assert_exactly_once(cfg)


@pytest.mark.parametrize("crash_at", CRASH_POINTS)
def test_kill_and_restart_at_each_boundary(env, crash_at):
    cfg, e = env
    assert drive(cfg, e, crash_at), f"crash point {crash_at} was never reached"
    assert_exactly_once(cfg)


def test_hard_kill_of_a_running_worker(env):
    """Popen.kill() (TerminateProcess on Windows) at an arbitrary moment, then restart."""
    cfg, e = env
    worker(e)
    cli(e, "approve", RID, "--action", "send_rfq", "--approver", "Test Approver")
    looping = subprocess.Popen([sys.executable, "cli.py", "run", "--interval", "0.05"],
                               cwd=ROOT, env=e, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    looping.kill()
    looping.wait(timeout=30)
    assert worker(e).returncode == 0
    write_replies(cfg)
    assert worker(e).returncode == 0
    cli(e, "approve", RID, "--action", "send_po", "--approver", "Test Approver")
    assert worker(e).returncode == 0
    assert state(cfg)["state"] == "DONE"
    assert_exactly_once(cfg)
