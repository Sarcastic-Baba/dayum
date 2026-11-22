"""The worker: drive every unfinished request through the state machine.

Runs on the laptop for the demo. Kill it at any moment and start it again — it resumes
from the database, and every action still happens exactly once.
"""

import os
import time

from .config import load_config
from .db import open_db
from .orchestrator import TERMINAL, WAITING, advance
from .transport import open_transport


def cycle():
    cfg = load_config()  # reloaded every cycle: a config change applies without a restart
    db, transport = open_db(cfg), open_transport(cfg)
    only = os.environ.get("BOXSOURCE_ONLY_PREFIX")  # e.g. tests on the shared database
    rows = db.select("requests", {"state": ("not_in", sorted(TERMINAL))}, order=["created_at"])
    return {r["request_id"]: advance(db, cfg, transport, r["request_id"])
            for r in rows if not only or r["request_id"].startswith(only)}


def run(until_idle=False, interval=2.0, max_cycles=None):
    """until_idle: return once every request is waiting or finished (used by tests/CLI)."""
    n = 0
    while True:
        states = cycle()
        n += 1
        idle = all(s in TERMINAL or s in WAITING for s in states.values())
        if (until_idle and idle) or (max_cycles and n >= max_cycles):
            return states
        time.sleep(interval)
