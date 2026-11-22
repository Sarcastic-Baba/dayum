"""Minimal Supabase (PostgREST) client. Server-side only: it uses the secret key.

Filters use PostgREST syntax, e.g. {"request_id": "eq.SRC-1", "status": "eq.pending"}.
"""

import os

import httpx


class DbError(Exception):
    """The database could not be reached or rejected the call."""


class Conflict(DbError):
    """A unique or primary key already exists (HTTP 409)."""


class Rejected(DbError):
    """A constraint or trigger refused the write (e.g. approval already decided)."""


class SupabaseDb:
    def __init__(self, url: str, key: str, timeout: float = 10):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key}
        # Legacy service-role JWTs need Authorization; new sb_secret_ keys use apikey.
        if not key.startswith("sb_secret_"):
            self.headers["Authorization"] = f"Bearer {key}"
        # One pooled, thread-safe client: reusing TLS connections cuts each call from
        # ~1 s (new handshake) to one round trip.
        self.http = httpx.Client(timeout=timeout, limits=httpx.Limits(max_connections=12))

    def select(self, table: str, filters: dict | None = None,
               columns: str = "*", order: str | None = None, limit: int | None = None) -> list[dict]:
        params = {"select": columns, **(filters or {})}
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        return self._call("GET", table, params=params)

    def insert(self, table: str, row: dict) -> dict:
        rows = self._call("POST", table, json=row, prefer="return=representation")
        return rows[0]

    def update(self, table: str, filters: dict, values: dict) -> list[dict]:
        """Returns the updated rows; an empty list means no row matched the filters."""
        return self._call("PATCH", table, params=filters, json=values, prefer="return=representation")

    def _call(self, method, table, params=None, json=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            response = self.http.request(method, f"{self.base}/{table}", params=params, json=json,
                                         headers=headers)
        except httpx.HTTPError as exc:
            raise DbError(f"database unreachable: {type(exc).__name__}") from None
        if response.status_code == 409:
            raise Conflict(_message(response))
        if response.status_code in (400, 422) or (
                response.status_code >= 400 and _code(response) == "P0001"):
            raise Rejected(_message(response))
        if response.status_code >= 400:
            raise DbError(f"database error {response.status_code}: {_message(response)}")
        return response.json() if response.content else []


def _code(response) -> str:
    try:
        return response.json().get("code", "")
    except ValueError:
        return ""


def _message(response) -> str:
    try:
        return response.json().get("message", response.text)
    except ValueError:
        return response.text


_shared: dict[tuple[str, str], SupabaseDb] = {}


def from_env() -> SupabaseDb:
    """One shared client per (url, key), so warm requests reuse open connections."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SECRET_KEY", "")
    if not url.startswith("https://") or not key:
        raise DbError("database is not configured (SUPABASE_URL / SUPABASE_SECRET_KEY)")
    if (url, key) not in _shared:
        _shared[(url, key)] = SupabaseDb(url, key)
    return _shared[(url, key)]
