"""BoxSource engine CLI — local runs and the demo. In production the dashboard submits
requests and records approvals in Supabase; the worker is the same either way.

  python cli.py submit request.json
  python cli.py run [--until-idle] [--interval 2]
  python cli.py approve REQ --action send_rfq [--supplier sup_01 ...] --approver "Name"
  python cli.py reject  REQ --action send_rfq --supplier sup_01 --approver "Name" --reason "..."
  python cli.py show REQ [--kickoff]
  python cli.py status
"""

import argparse
import json
import sys

from dotenv import load_dotenv

from engine import io_format, worker
from engine.config import load_config
from engine.db import open_db
from engine.steps import now_iso


def _decide(db, args, status):
    rows = db.select("approvals", {"request_id": args.request_id, "action": args.action,
                                   "status": "pending"})
    if args.supplier:
        rows = [r for r in rows if r["supplier_id"] in args.supplier]
    for r in rows:
        db.update("approvals", {"id": r["id"]}, {"status": status, "approver": args.approver,
                                                 "reason": args.reason, "decided_at": now_iso()})
    print(f"{status} {len(rows)} {args.action} approval(s)")


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(".env")
    p = argparse.ArgumentParser(prog="cli.py", description="BoxSource sourcing engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("submit").add_argument("file")
    r = sub.add_parser("run")
    r.add_argument("--until-idle", action="store_true")
    r.add_argument("--interval", type=float, default=2.0)
    r.add_argument("--max-cycles", type=int)
    for name in ("approve", "reject"):
        a = sub.add_parser(name)
        a.add_argument("request_id")
        a.add_argument("--action", required=True, choices=["send_rfq", "send_po"])
        a.add_argument("--supplier", nargs="*")
        a.add_argument("--approver", required=True)
        a.add_argument("--reason", required=(name == "reject"))
    sh = sub.add_parser("show")
    sh.add_argument("request_id")
    sh.add_argument("--kickoff", action="store_true")
    sub.add_parser("status")
    args = p.parse_args(argv)

    if args.cmd == "run":
        print(json.dumps(worker.run(until_idle=args.until_idle, interval=args.interval,
                                    max_cycles=args.max_cycles)))
        return 0
    cfg = load_config()
    db = open_db(cfg)
    if args.cmd == "submit":
        with open(args.file, encoding="utf-8") as f:
            raw = json.load(f)
        won = db.insert("requests", {"request_id": raw["request_id"], "input_json": raw},
                        conflict=["request_id"])
        print(("submitted " if won else "already exists: ") + raw["request_id"])
    elif args.cmd in ("approve", "reject"):
        _decide(db, args, "approved" if args.cmd == "approve" else "rejected")
    elif args.cmd == "show":
        rows = db.select("requests", {"request_id": args.request_id})
        if not rows:
            print("no such request", file=sys.stderr)
            return 1
        full = rows[0]["result_json"] or {}
        print(json.dumps(io_format.to_kickoff(full, cfg) if args.kickoff else full,
                         indent=2, ensure_ascii=False))
    elif args.cmd == "status":
        for row in db.select("requests", order=["created_at"]):
            res = row["result_json"] or {}
            print(f"{row['request_id']:12s} {row['state']:20s} {row['status']:22s} "
                  f"cost INR {res.get('cost_inr', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
