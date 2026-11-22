"""Idempotent step runner, call cache, and the crash hook used by the exactly-once tests.

A step is identified by a deterministic idem_key. Its `steps` row is written *before* the
work starts (status 'started') and marked 'done' with the output afterwards, so a rerun
after a crash either returns the stored output or redoes work that never finished.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

MAX_ATTEMPTS = 3


class StepFailed(RuntimeError):
    """A step kept failing; the orchestrator holds the request instead of looping."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def idem_key(*parts):
    """Deterministic key — never random, never produced by the LLM."""
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def crash_point(name):
    """BOXSOURCE_CRASH_AT=<name> kills the process right here (no cleanup, like SIGKILL),
    so tests can stop the worker at each named boundary deterministically."""
    if os.environ.get("BOXSOURCE_CRASH_AT") == name:
        os._exit(1)


def get_step(db, key):
    rows = db.select("steps", {"idem_key": key})
    return rows[0] if rows else None


def begin_step(db, request_id, step, key):
    """Record intent before doing the work; returns the (possibly pre-existing) row."""
    row = get_step(db, key)
    if row is None:
        db.insert("steps", {"request_id": request_id, "step": step, "idem_key": key,
                            "status": "started"}, conflict=["idem_key"])
        return get_step(db, key)
    if row["status"] == "failed":
        raise StepFailed(f"{step} failed earlier after {row['attempts']} attempts")
    if row["attempts"] + 1 >= MAX_ATTEMPTS:
        db.update("steps", {"idem_key": key}, {"status": "failed"})
        raise StepFailed(f"{step} failed {MAX_ATTEMPTS} times")
    db.update("steps", {"idem_key": key}, {"attempts": row["attempts"] + 1})
    return row


def finish_step(db, key, output):
    db.update("steps", {"idem_key": key},
              {"status": "done", "output_json": output, "done_at": now_iso()})


def run_step(db, request_id, step, fn, key=None):
    """Run fn() to completion at most once per key; a rerun returns the stored output."""
    key = key or idem_key(request_id, "step", step)
    row = get_step(db, key)
    if row and row["status"] == "done":
        return row["output_json"]
    begin_step(db, request_id, step, key)
    out = fn()
    finish_step(db, key, out)
    return out


def cached(db, request_id, kind, payload, fn):
    """Cache a paid call (LLM, search, fetch) by (request, kind, input hash): restarts
    replay the stored result instead of paying again."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return run_step(db, request_id, f"cache:{kind}", fn,
                    key=idem_key(request_id, "cache", kind, digest))
