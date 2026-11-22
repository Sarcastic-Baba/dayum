"""In-memory stand-in for dashboard.db.SupabaseDb. Test-only.

Understands the PostgREST filters the platform uses (eq., in.()), primary keys,
identity ids, and the approvals guard trigger from the contract migration."""

import copy
import fnmatch
import itertools
from datetime import datetime, timezone

from dashboard.db import Conflict, Rejected

PRIMARY_KEYS = {
    "requests": ("request_id",),
    "candidates": ("request_id", "supplier_id"),
    "checks": ("request_id", "supplier_id", "check_name"),
    "outbox": ("idem_key",),
    "quotes": ("message_id",),
    "approvals": ("request_id", "action", "supplier_id"),
}
IDENTITY = {"evidence", "steps", "approvals", "costs", "logs", "quotes"}


def _now():
    return datetime.now(timezone.utc).isoformat()


class FakeDb:
    def __init__(self, tables: dict | None = None):
        self.tables: dict[str, list[dict]] = {}
        self._ids = itertools.count(1)
        for name, rows in (tables or {}).items():
            for row in rows:
                self.insert(name, row)

    def select(self, table, filters=None, columns="*", order=None, limit=None):
        rows = [r for r in self.tables.get(table, []) if _matches(r, filters or {})]
        if order:
            col, _, direction = order.partition(".")
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=direction == "desc")
        if limit is not None:
            rows = rows[:limit]
        return copy.deepcopy(rows)

    def insert(self, table, row):
        row = copy.deepcopy(row)
        if table in IDENTITY and "id" not in row:
            row["id"] = next(self._ids)
        if table == "requests":
            row.setdefault("created_at", _now())
            row.setdefault("updated_at", row["created_at"])
            row.setdefault("state", "RECEIVED")
            row.setdefault("status", "pending")
        if table == "approvals":
            row.setdefault("status", "pending")
        key = PRIMARY_KEYS.get(table)
        existing = self.tables.setdefault(table, [])
        if key and any(all(r.get(k) == row.get(k) for k in key) for r in existing):
            raise Conflict(f"duplicate key in {table}")
        existing.append(row)
        return copy.deepcopy(row)

    def update(self, table, filters, values):
        updated = []
        for row in self.tables.get(table, []):
            if not _matches(row, filters):
                continue
            if table == "approvals" and row["status"] != "pending":
                raise Rejected(f"approval {row['id']} already decided ({row['status']})")
            row.update(copy.deepcopy(values))
            updated.append(copy.deepcopy(row))
        return updated


def _matches(row, filters):
    for col, expr in filters.items():
        op, _, value = expr.partition(".")
        if op == "eq" and str(row.get(col)) != value:
            return False
        if op == "like" and not fnmatch.fnmatchcase(str(row.get(col)), value):
            return False
        if op == "in":
            options = [v.strip().strip('"') for v in value.strip("()").split(",")]
            if str(row.get(col)) not in options:
                return False
    return True
