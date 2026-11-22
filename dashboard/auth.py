"""Demo login (chaitanya/AGENTS.md → Demo login): two accounts from env, signed session cookie.

DEMO_APPROVER_EMAIL / DEMO_APPROVER_PASSWORD  can approve or reject irreversible actions
DEMO_VIEWER_EMAIL   / DEMO_VIEWER_PASSWORD    read-only (for judges to click around)
SESSION_SECRET  32+ random characters; signs the session cookie.
API_TOKEN       optional bearer token for creating/reading requests from scripts (never approvals).
"""

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

COOKIE = "boxsource_session"
SESSION_TTL_S = 12 * 3600


@dataclass(frozen=True)
class User:
    email: str
    role: str  # "approver" | "viewer"

    @property
    def can_approve(self) -> bool:
        return self.role == "approver"


def _accounts() -> dict[str, tuple[str, str]]:
    """email (lower-case) -> (password, role). Accounts with a blank email or password are disabled."""
    accounts = {}
    for role in ("viewer", "approver"):  # approver wins if both share an email
        email = os.getenv(f"DEMO_{role.upper()}_EMAIL", "").strip().lower()
        password = os.getenv(f"DEMO_{role.upper()}_PASSWORD", "")
        if email and password:
            accounts[email] = (password, role)
    return accounts


def check_login(email: str, password: str) -> User | None:
    email = (email or "").strip().lower()
    stored = _accounts().get(email)
    # Compare digests so timing does not reveal password length or which emails exist.
    expected = stored[0] if stored else os.urandom(16).hex()
    ok = hmac.compare_digest(hashlib.sha256(password.encode()).digest(),
                             hashlib.sha256(expected.encode()).digest())
    return User(email, stored[1]) if ok and stored else None


def _secret() -> bytes:
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET must be set (32+ random characters)")
    return secret.encode()


def make_session(user: User) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": user.email, "exp": int(time.time()) + SESSION_TTL_S}).encode()).decode()
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_session(cookie: str | None) -> User | None:
    """The user for a valid, unexpired cookie. The role is re-read from env on every
    request, so removing or demoting an account takes effect immediately."""
    if not cookie or "." not in cookie:
        return None
    payload, sig = cookie.rsplit(".", 1)
    try:
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    except RuntimeError:
        return None
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except ValueError:
        return None
    account = _accounts().get(data.get("u", ""))
    if data.get("exp", 0) < time.time() or not account:
        return None
    return User(data["u"], account[1])


def api_token_ok(authorization: str | None) -> bool:
    token = os.getenv("API_TOKEN", "")
    return bool(token) and hmac.compare_digest(authorization or "", f"Bearer {token}")
