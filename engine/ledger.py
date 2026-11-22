"""Cost and time ledger: every paid call is a `costs` row; every step is one structured log
line (in `logs` and in a local run.log). Request duration = sum of active step time, so
time a human spends approving does not count against the limit."""

import json
from pathlib import Path

from .config import resolve
from .steps import now_iso


def record_cost(db, request_id, step, call_type, units, inr):
    db.insert("costs", {"request_id": request_id, "step": step, "call_type": call_type,
                        "units": float(units), "inr": round(float(inr), 4)})


def total_cost(db, request_id):
    return round(sum(r["inr"] for r in db.select("costs", {"request_id": request_id})), 4)


def llm_cost_inr(cfg, tokens_in, tokens_out):
    p = cfg["pricing"]
    return (tokens_in / 1000 * p["llm_input_per_1k_tokens"]
            + tokens_out / 1000 * p["llm_output_per_1k_tokens"])


def log_step(db, cfg, request_id, step, status, duration_ms=0, cost_inr=0.0,
             supplier_id=None, level="info", detail=None):
    line = {"request_id": request_id, "step": step, "supplier_id": supplier_id,
            "status": status, "duration_ms": int(duration_ms), "cost_inr": round(cost_inr, 4)}
    if detail:
        line["detail"] = detail
    db.insert("logs", {"request_id": request_id, "level": level, "message": json.dumps(line)})
    path = resolve(cfg, cfg.get("log_path"))
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now_iso(), "level": level, **line}) + "\n")


def active_seconds(db, request_id):
    total_ms = 0
    for r in db.select("logs", {"request_id": request_id}):
        try:
            total_ms += json.loads(r["message"]).get("duration_ms", 0)
        except (ValueError, AttributeError):
            pass
    return round(total_ms / 1000, 1)
