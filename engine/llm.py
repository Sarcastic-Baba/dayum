"""LLM wrapper — extraction only (PRD §3): strict JSON, validated by a Pydantic model.

Every model in the list is asked at once and the first valid answer wins, so latency is
the fastest model's and one overloaded (503) or rate-limited (429) model can't sink the
request. The time budget is enforced here, on the client side: Gemini rejects server
deadlines under 10 s. Every finished call reports its token usage through `on_usage` —
including a slower call that lost the race — so the cost ledger stays complete.
Raises LLMUnavailable when nothing valid arrives in time; callers turn that into a
question or cannot_verify, never a guess.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout

from google import genai
from google.genai import types

SERVER_MIN_DEADLINE_MS = 10_000
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")
_CLIENTS = {}


class LLMUnavailable(RuntimeError):
    pass


def _client(timeout_ms):
    """One client per key/timeout, so calls reuse the connection instead of a new TLS setup."""
    key = (os.environ["GEMINI_API_KEY"], timeout_ms)
    if key not in _CLIENTS:
        _CLIENTS[key] = genai.Client(api_key=key[0], http_options=types.HttpOptions(
            timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1)))
    return _CLIENTS[key]


def _call(client, model, prompt, schema):
    r = client.models.generate_content(model=model, contents=prompt, config=types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=schema, temperature=0))
    u = r.usage_metadata
    usage = {"model": r.model_version or model, "tokens_in": u.prompt_token_count or 0,
             "tokens_out": (u.candidates_token_count or 0)
             + (getattr(u, "thoughts_token_count", None) or 0)}
    return schema.model_validate_json(r.text), usage


def _report(on_usage):
    def done(fut):
        if fut.exception() is None:
            on_usage(fut.result()[1])
    return done


def extract(prompt, schema, models, timeout_s, on_usage=None):
    """-> (validated schema instance, usage of the winning call)."""
    client = _client(max(SERVER_MIN_DEADLINE_MS, int(timeout_s * 1000)))
    futures = []
    for model in models:
        fut = _POOL.submit(_call, client, model, prompt, schema)
        if on_usage:
            fut.add_done_callback(_report(on_usage))
        futures.append(fut)
    errors = []
    try:
        for fut in as_completed(futures, timeout=timeout_s):
            try:
                return fut.result()
            except Exception as e:  # noqa: BLE001 — wait for another model instead
                errors.append(f"{type(e).__name__}: {str(e)[:80]}")
    except FuturesTimeout:
        errors.append(f"no valid answer within {timeout_s}s")
    raise LLMUnavailable("; ".join(errors))
