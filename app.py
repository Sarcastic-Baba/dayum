"""BoxSource deployment foundation; sourcing workflow is not implemented yet."""

import hmac
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException

load_dotenv()
app = FastAPI(title="BoxSource", version="0.1.0", docs_url=None, redoc_url=None)


@app.get("/")
def index():
    return {"service": "BoxSource", "status": "setup", "health": "/api/health"}


@app.get("/api/health")
def health():
    """Liveness only: does not claim database readiness."""
    return {"status": "ok"}


@app.get("/api/ready")
async def ready(authorization: str | None = Header(default=None)):
    """Verify credentials and migration without exposing database details."""
    token = os.getenv("HEALTHCHECK_TOKEN", "")
    if not token or not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "")
    if not url.startswith("https://") or not key:
        raise HTTPException(status_code=503, detail="Database is not configured")
    headers = {"apikey": key}
    # Legacy service-role JWTs need Authorization; new secret keys use apikey.
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{url}/rest/v1/requests",
                params={"select": "request_id", "limit": "0"},
                headers=headers,
            )
            response.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Database readiness check failed") from None
    return {"status": "ready", "database": "connected"}
