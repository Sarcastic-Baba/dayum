"""Seed DEMO-UI-1 — a clearly-marked fixture request for building the dashboard — into the
database configured in config.yaml, then let the real engine drive it to "Waiting for RFQ
approval": drafts, pending approvals with email previews, evidence, a not-eligible supplier,
and a full result_json. Delete it before the demo.

    python tests/seed_dashboard_demo.py            # create (safe to re-run)
    python tests/seed_dashboard_demo.py --delete   # remove it and everything under it
"""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from engine.config import load_config  # noqa: E402
from engine.db import open_db  # noqa: E402
from fixture_data import cleanup, seed  # noqa: E402

RID = "DEMO-UI-1"


def main():
    load_dotenv(ROOT / ".env")
    db = open_db(load_config())
    if "--delete" in sys.argv:
        cleanup(db, RID)
        print(f"deleted {RID}")
        return
    if not db.select("requests", {"request_id": RID}):
        seed(db, RID, evidence=True, not_eligible=True)
    subprocess.run([sys.executable, "cli.py", "run", "--until-idle", "--interval", "0",
                    "--max-cycles", "10"], cwd=ROOT, check=True, capture_output=True,
                   env={**os.environ, "BOXSOURCE_ONLY_PREFIX": RID})
    row = db.select("requests", {"request_id": RID})[0]
    approvals = db.select("approvals", {"request_id": RID})
    print(f"{RID}: state={row['state']} status={row['status']} "
          f"pending approvals={sum(a['status'] == 'pending' for a in approvals)}")


if __name__ == "__main__":
    main()
