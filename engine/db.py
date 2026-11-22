"""Storage for the engine: one narrow interface, two backends.

Both backends expose the same five primitives:
    insert(table, row, conflict=None) -> bool  True if this call created the row. With
                                               `conflict` set, a duplicate on those columns is
                                               ignored (returns False) instead of raising.
    upsert(table, row, conflict)               insert, or update the other columns on conflict
    select(table, where=None, order=None)      -> list[dict]; order like ["-at", "id"]
    update(table, where, values)
    delete(table, where)
`where` maps column -> value (equality) or column -> (op, value), op in
eq | neq | in | not_in | is_null.

SQLiteDB mirrors supabase/migrations (same keys, CHECKs and foreign keys) plus the two
additions requested in PRD §14 (requests.result_json, quotes), so write-order bugs show up
locally, not only against Supabase. SupabaseDB talks to PostgREST with the service key.
"""

import json
import os
import sqlite3
from pathlib import Path

import httpx

JSON_COLS = {"input_json", "spec_json", "result_json", "data_json", "output_json", "source_json"}
_NOW = "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"

STATES = ("RECEIVED", "SPEC_PARSED", "CANDIDATES_FOUND", "CANDIDATES_CHECKED", "SHORTLISTED",
          "RECOMMENDED", "WAIT_APPROVAL_RFQ", "RFQS_SENT", "WAIT_QUOTES", "QUOTES_RECEIVED",
          "RANKED", "WAIT_APPROVAL_PO", "PO_SENT", "DONE", "HELD")
STATUSES = ("pending", "recommended", "awaiting_quotes", "no_verified_supplier", "held")
_IN = lambda values: ", ".join(f"'{v}'" for v in values)  # noqa: E731

# Mirrors the live Supabase schema (inspected 22 Nov 2026), including the platform's
# additions: requests.created_by, the approval email-preview columns, and the trigger that
# makes a decision final and freezes the draft the approver saw.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY,
  input_json TEXT NOT NULL,
  spec_json TEXT,
  state TEXT NOT NULL DEFAULT 'RECEIVED' CHECK (state IN ({_IN(STATES)})),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ({_IN(STATUSES)})),
  result_json TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL DEFAULT {_NOW},
  updated_at TEXT NOT NULL DEFAULT {_NOW}
);
CREATE TABLE IF NOT EXISTS candidates (
  request_id TEXT NOT NULL REFERENCES requests(request_id),
  supplier_id TEXT NOT NULL,
  name TEXT NOT NULL,
  website TEXT,
  address TEXT,
  classification TEXT NOT NULL DEFAULT 'cannot_verify'
    CHECK (classification IN ('manufacturer', 'trader', 'cannot_verify')),
  merged_into TEXT,
  data_json TEXT NOT NULL DEFAULT '{{}}',
  PRIMARY KEY (request_id, supplier_id),
  FOREIGN KEY (request_id, merged_into) REFERENCES candidates(request_id, supplier_id)
);
CREATE TABLE IF NOT EXISTS checks (
  request_id TEXT NOT NULL,
  supplier_id TEXT NOT NULL,
  check_name TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'cannot_verify')),
  explanation TEXT NOT NULL,
  missing TEXT,
  PRIMARY KEY (request_id, supplier_id, check_name),
  FOREIGN KEY (request_id, supplier_id) REFERENCES candidates(request_id, supplier_id)
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  supplier_id TEXT NOT NULL,
  check_name TEXT NOT NULL,
  fact TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_type TEXT NOT NULL,
  field_or_quote TEXT NOT NULL,
  record_date TEXT,
  retrieved_at TEXT NOT NULL DEFAULT {_NOW},
  FOREIGN KEY (request_id, supplier_id) REFERENCES candidates(request_id, supplier_id)
);
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES requests(request_id),
  step TEXT NOT NULL,
  idem_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'done', 'failed')),
  output_json TEXT,
  started_at TEXT NOT NULL DEFAULT {_NOW},
  done_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0)
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('send_rfq', 'send_po')),
  supplier_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  approver TEXT,
  reason TEXT,
  decided_at TEXT,
  idem_key TEXT UNIQUE,
  email_to TEXT,
  email_subject TEXT,
  email_body TEXT,
  created_at TEXT NOT NULL DEFAULT {_NOW},
  UNIQUE (request_id, action, supplier_id),
  FOREIGN KEY (request_id, supplier_id) REFERENCES candidates(request_id, supplier_id),
  CHECK (status = 'pending' OR (approver IS NOT NULL AND decided_at IS NOT NULL)),
  CHECK (status <> 'rejected' OR length(trim(reason)) > 0 AND reason IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS outbox (
  idem_key TEXT PRIMARY KEY REFERENCES steps(idem_key),
  approval_id INTEGER NOT NULL REFERENCES approvals(id),
  message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT {_NOW}
);
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  supplier_id TEXT NOT NULL,
  rfq_idem_key TEXT NOT NULL REFERENCES steps(idem_key),
  message_id TEXT NOT NULL UNIQUE,
  price_inr_per_unit REAL CHECK (price_inr_per_unit IS NULL OR price_inr_per_unit >= 0),
  currency TEXT,
  gst_basis TEXT CHECK (gst_basis IN ('ex-GST', 'incl-GST', 'unknown')),
  moq INTEGER CHECK (moq IS NULL OR moq >= 0),
  lead_time_days INTEGER CHECK (lead_time_days IS NULL OR lead_time_days >= 0),
  ply INTEGER,
  size_in TEXT,
  raw_eml TEXT NOT NULL,
  source_quote TEXT,
  parsed_ok INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL,
  FOREIGN KEY (request_id, supplier_id) REFERENCES candidates(request_id, supplier_id)
);
CREATE TABLE IF NOT EXISTS costs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES requests(request_id),
  step TEXT NOT NULL,
  call_type TEXT NOT NULL,
  units REAL NOT NULL CHECK (units >= 0),
  inr REAL NOT NULL CHECK (inr >= 0),
  at TEXT NOT NULL DEFAULT {_NOW}
);
CREATE TABLE IF NOT EXISTS pages (
  url TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT {_NOW},
  content_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (url, content_hash)
);
CREATE TABLE IF NOT EXISTS geo_cache (
  query TEXT PRIMARY KEY,
  lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon REAL NOT NULL CHECK (lon BETWEEN -180 AND 180),
  source_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT {_NOW}
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES requests(request_id),
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT {_NOW}
);
CREATE INDEX IF NOT EXISTS evidence_request_idx ON evidence(request_id, supplier_id);
CREATE INDEX IF NOT EXISTS steps_request_idx ON steps(request_id);
CREATE INDEX IF NOT EXISTS costs_request_idx ON costs(request_id);
CREATE INDEX IF NOT EXISTS logs_request_idx ON logs(request_id, at);
"""

TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS approvals_guard_update BEFORE UPDATE ON approvals
BEGIN
  SELECT RAISE(ABORT, 'approval already decided') WHERE OLD.status <> 'pending';
  SELECT RAISE(ABORT, 'approval draft changed during decision')
   WHERE NEW.status <> 'pending' AND (NEW.email_to IS NOT OLD.email_to
      OR NEW.email_subject IS NOT OLD.email_subject OR NEW.email_body IS NOT OLD.email_body);
END;
"""

# Columns added after the first local databases were created (ALTER keeps their data).
_UPGRADES = {"requests": {"created_by": "TEXT"},
             "approvals": {"idem_key": "TEXT", "email_to": "TEXT", "email_subject": "TEXT",
                           "email_body": "TEXT", "created_at": "TEXT"}}


def _scalar(v):
    """Dates (e.g. from YAML) become ISO strings; everything else passes through."""
    return v.isoformat() if hasattr(v, "isoformat") else v


def _split(cond):
    return cond if isinstance(cond, tuple) else ("eq", cond)


class SQLiteDB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit: every statement is its own durable transaction, so a kill at any
        # point leaves the database at a statement boundary.
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.executescript(SCHEMA)
        for table, cols in _UPGRADES.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS approvals_idem_key_uq ON approvals(idem_key)")
        self.conn.executescript(TRIGGERS)

    @staticmethod
    def _enc(row):
        return {k: (json.dumps(v, default=str) if k in JSON_COLS and v is not None else _scalar(v))
                for k, v in row.items()}

    @staticmethod
    def _dec(r):
        d = dict(r)
        for k in JSON_COLS & d.keys():
            if d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    @staticmethod
    def _where(where):
        clauses, args = [], []
        for col, cond in (where or {}).items():
            op, val = _split(cond)
            if op == "eq":
                clauses.append(f"{col} = ?")
                args.append(_scalar(val))
            elif op == "neq":
                clauses.append(f"{col} != ?")
                args.append(_scalar(val))
            elif op in ("in", "not_in"):
                marks = ",".join("?" * len(val))
                clauses.append(f"{col} {'NOT ' if op == 'not_in' else ''}IN ({marks})")
                args.extend(val)
            elif op == "is_null":
                clauses.append(f"{col} IS {'' if val else 'NOT '}NULL")
            else:
                raise ValueError(f"unknown op {op}")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def insert(self, table, row, conflict=None):
        row = self._enc(row)
        tail = f" ON CONFLICT ({','.join(conflict)}) DO NOTHING" if conflict else ""
        cur = self.conn.execute(
            f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))}){tail}",
            list(row.values()))
        return cur.rowcount == 1

    def insert_many(self, table, rows):
        """Plain insert of rows that share the same columns (one statement)."""
        if not rows:
            return
        rows = [self._enc(r) for r in rows]
        cols = list(rows[0])
        self.conn.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [[r[c] for c in cols] for r in rows])

    def upsert(self, table, row, conflict):
        row = self._enc(row)
        sets = ",".join(f"{c}=excluded.{c}" for c in row if c not in conflict)
        action = f"UPDATE SET {sets}" if sets else "NOTHING"
        self.conn.execute(
            f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))}) "
            f"ON CONFLICT ({','.join(conflict)}) DO {action}", list(row.values()))

    def select(self, table, where=None, order=None):
        w, args = self._where(where)
        o = ""
        if order:
            o = " ORDER BY " + ",".join(
                f"{c.lstrip('-')} {'DESC' if c.startswith('-') else 'ASC'}" for c in order)
        return [self._dec(r) for r in self.conn.execute(f"SELECT * FROM {table}{w}{o}", args)]

    def update(self, table, where, values):
        values = self._enc(values)
        w, args = self._where(where)
        self.conn.execute(f"UPDATE {table} SET {','.join(f'{c}=?' for c in values)}{w}",
                          list(values.values()) + args)

    def delete(self, table, where):
        w, args = self._where(where)
        self.conn.execute(f"DELETE FROM {table}{w}", args)


class SupabaseDB:
    """PostgREST backend. Same primitives; JSON columns travel as native JSON."""

    def __init__(self, url, key):
        headers = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_secret_"):  # legacy service-role JWTs also need Authorization
            headers["Authorization"] = f"Bearer {key}"
        self.http = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", headers=headers,
                                 timeout=20)

    @staticmethod
    def _enc(row):
        return {k: _scalar(v) for k, v in row.items()}

    @staticmethod
    def _fmt(v):
        v = _scalar(v)
        return str(v).lower() if isinstance(v, bool) else str(v)

    def _filters(self, where):
        params = {}
        for col, cond in (where or {}).items():
            op, val = _split(cond)
            if op in ("eq", "neq"):
                params[col] = f"{op}.{self._fmt(val)}"
            elif op in ("in", "not_in"):
                items = ",".join(f'"{self._fmt(v)}"' for v in val)
                params[col] = f"{'not.' if op == 'not_in' else ''}in.({items})"
            elif op == "is_null":
                params[col] = "is.null" if val else "not.is.null"
            else:
                raise ValueError(f"unknown op {op}")
        return params

    def _req(self, method, table, **kw):
        r = self.http.request(method, f"/{table}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {method} {table} -> {r.status_code}: {r.text[:300]}")
        return r

    def insert(self, table, row, conflict=None):
        prefer, params = "return=representation", {}
        if conflict:
            # PostgREST returns [] when the duplicate is ignored, [row] when inserted.
            prefer = "resolution=ignore-duplicates,return=representation"
            params["on_conflict"] = ",".join(conflict)
        r = self._req("POST", table, json=self._enc(row), params=params,
                      headers={"Prefer": prefer})
        return len(r.json()) == 1

    def insert_many(self, table, rows):
        """One round trip for many rows (PostgREST bulk insert; same columns in every row)."""
        if rows:
            self._req("POST", table, json=[self._enc(r) for r in rows],
                      headers={"Prefer": "return=minimal"})

    def upsert(self, table, row, conflict):
        self._req("POST", table, json=self._enc(row), params={"on_conflict": ",".join(conflict)},
                  headers={"Prefer": "resolution=merge-duplicates,return=minimal"})

    def select(self, table, where=None, order=None):
        params = self._filters(where)
        params["select"] = "*"
        if order:
            params["order"] = ",".join(
                f"{c.lstrip('-')}.{'desc' if c.startswith('-') else 'asc'}" for c in order)
        return self._req("GET", table, params=params).json()

    def update(self, table, where, values):
        self._req("PATCH", table, params=self._filters(where), json=self._enc(values),
                  headers={"Prefer": "return=minimal"})

    def delete(self, table, where):
        self._req("DELETE", table, params=self._filters(where),
                  headers={"Prefer": "return=minimal"})


def open_db(cfg):
    from .config import resolve

    backend = cfg["storage"]["backend"]
    if backend == "sqlite":
        return SQLiteDB(resolve(cfg, cfg["storage"]["sqlite_path"]))
    if backend == "supabase":
        return SupabaseDB(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    raise ValueError(f"unknown storage backend {backend!r}")
