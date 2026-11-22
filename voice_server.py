"""Vapi bridge: exposes the sourcing agent's voice intake to a phone assistant as one tool,
`ask_backend_agent(query)`. Demo-grade on purpose: no auth, no extra database.

  POST /vapi/tool   Vapi webhook. tool-calls -> {"results": [{"toolCallId", "result"}]};
                    any other event -> {}. Always HTTP 200: Vapi must never see a 500.
  GET  /health      {"ok": true}

Run:  python cli.py voice      (port 8000), then  ngrok http 8000.   See VOICE_DEMO.md.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request

from engine import voice
from engine.config import load_config

load_dotenv(Path(__file__).resolve().parent / ".env")
CFG = load_config()
voice.check_env(CFG)  # fail fast at startup with a clear message if a key is missing
TIMEOUT_S = float(os.environ.get("VOICE_TIMEOUT_S") or CFG["voice"]["timeout_s"])
FALLBACK = "Sorry, I couldn't get that right now, could you try again?"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")
app = FastAPI(title="BoxSource voice bridge", docs_url=None, redoc_url=None)


@app.get("/health")
def health():
    return {"ok": True}


def tool_calls(message):
    """Vapi has used a few names for this list across versions."""
    calls = (message.get("toolCallList") or message.get("toolCalls")
             or [t.get("toolCall") for t in message.get("toolWithToolCallList") or []
                 if isinstance(t, dict)])
    return [c for c in calls if isinstance(c, dict)]


def query_of(call):
    args = (call.get("function") or {}).get("arguments") or {}
    if isinstance(args, str):  # arguments may arrive as a JSON string
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = {}
    q = args.get("query") if isinstance(args, dict) else None
    return q.strip() if isinstance(q, str) else ""


async def answer_one(call_id, query):
    t0 = time.monotonic()
    try:
        text = await asyncio.wait_for(asyncio.to_thread(voice.answer, CFG, query, call_id),
                                      TIMEOUT_S)
        outcome = "ok"
    except asyncio.TimeoutError:
        text, outcome = FALLBACK, f"timeout after {TIMEOUT_S}s"
    except Exception as e:  # noqa: BLE001 — the caller hears a friendly line, never an error
        text, outcome = FALLBACK, f"error {type(e).__name__}: {e}"
    ms = round((time.monotonic() - t0) * 1000)
    voice.log_turn(CFG, call_id, query, text, ms, outcome)
    log.info("call=%s %dms %s | query=%r | answer=%r", call_id, ms, outcome, query, text)
    return text


@app.post("/vapi/tool")
async def vapi_tool(request: Request):
    try:
        body = await request.json()
        message = body.get("message") or {}
        if message.get("type") != "tool-calls":
            return {}  # status-update, end-of-call-report, ...: acknowledge and ignore
        call_id = (message.get("call") or {}).get("id")
        results = []
        for call in tool_calls(message):
            results.append({"toolCallId": call.get("id"),
                            "result": await answer_one(call_id, query_of(call))})
        return {"results": results}
    except Exception as e:  # noqa: BLE001 — malformed body: still a 200, never a 500
        log.warning("ignored malformed request: %s", e)
        return {}
