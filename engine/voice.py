"""Voice intake for the sourcing agent (PRD §22).

The voice agent only *registers* a request. It collects the requirement, asks the
product's follow-up questions for anything missing, reads the spec back, and on "yes"
registers a normal request row — exactly what the dashboard would create. The sourcing
agent (the worker) takes over from there. Progress, quotes and approvals live on the
dashboard, never on the call.

No guessing: every number and the place must appear in the caller's own words, or it is
asked for again. One phone call registers one request (VOICE-<call id>), so a retried
tool call can't duplicate it.
"""

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from . import ledger, llm
from .config import resolve
from .db import open_db
from .steps import now_iso

log = logging.getLogger("voice")
SESSIONS = {}  # call id -> {"words", "pending", "request_id", "tokens"} for one phone call

SAY_AGAIN = "Sorry, I didn't catch that. Could you say it again?"
TROUBLE = "Sorry, I'm having trouble understanding right now. Could you repeat the request?"
CAPABILITY = ("I can register a packaging sourcing request for you. What would you like to "
              "source?")

AFFIRM_RE = re.compile(r"^\W*(yes|yeah|yep|yup|sure|okay|ok|correct|that'?s right|go ahead|"
                       r"please do|do it|register|confirm\w*|sounds good|perfect)\b", re.I)
NEGATE_RE = re.compile(r"^\W*(no|nope|not|wrong|actually|change|wait)\b", re.I)
APPROVE_RE = re.compile(r"\b(approv\w*|place (the|an|my) order|send (the|my|an) (order|po|"
                        r"purchase order|rfqs?|quote requests?)|purchase order)\b", re.I)
STATUS_RE = re.compile(r"\b(status|update|progress|news|found|shortlist\w*|recommend\w*|"
                       r"best (supplier|option|quote|price)|cheapest|quotes?|repl(y|ies|ied)|"
                       r"results?|how('s| is| are) (my|the|it|things)|where are we)\b", re.I)
NEED_RE = re.compile(r"\b(need|want|require|looking for|source|sourcing|buy|"
                     r"purchase(?!\s+orders?))\b", re.I)


class Spoken(BaseModel):
    item: str | None = None
    ply: int | None = None
    size_in: list[float] | None = None
    material: str | None = None
    printing: str | None = None
    quantity_per_month: int | None = None
    delivery_location: str | None = None
    needed_by: str | None = None


PROMPT = ("Extract a packaging sourcing requirement from a phone caller's words.\n"
          "Rules: use only what the caller said. Leave a field null if they did not say it; never "
          "guess or fill a default. If they corrected themselves, use the latest value. "
          "item = the product name only, e.g. 'corrugated box' (no quantity, ply or size). "
          "quantity_per_month = units per month. size_in = [length, width, height] in inches. "
          "printing = any printing or branding they asked for. delivery_location = the city or area "
          "name exactly as the caller said it (e.g. 'Gurgaon', not 'our Gurgaon warehouse'). "
          "needed_by = YYYY-MM-DD only if a date was said.\nCaller's words: {words}")

# ---- no-guess guard: numbers and the place must be in the caller's own words -----------

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "x x twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "x"}
_SCALES = {"hundred": 100, "thousand": 1000, "k": 1000, "lakh": 100_000, "lakhs": 100_000,
           "lac": 100_000, "million": 1_000_000}
_ALIASES = {"gurugram": "gurgaon", "gurgaon": "gurugram"}


def word_numbers(text):
    """Every number the caller said, in digits or words: '50,000', 'fifty thousand', '2 lakh'."""
    found, cur, total, active, digits = set(), 0.0, 0.0, False, False

    def flush():
        nonlocal cur, total, active, digits
        if active:
            found.add(total + cur)
        cur, total, active, digits = 0.0, 0.0, False, False

    for tok in re.findall(r"\d+(?:\.\d+)?|[a-z]+", text.lower().replace(",", "")):
        if tok[0].isdigit():
            flush()
            cur, active, digits = float(tok), True, True
        elif tok in _UNITS or tok in _TENS:
            if digits:  # "50,000 five ply" is two numbers, not 50,005
                flush()
            cur += _UNITS.get(tok, _TENS.get(tok, 0))
            active = True
        elif tok in _SCALES and active:
            if _SCALES[tok] == 100:
                cur *= 100
            else:
                total, cur = total + cur * _SCALES[tok], 0.0
        elif not (tok == "and" and active):
            flush()
    flush()
    return found


def _place_said(place, words):
    w = words.lower()
    return any(tok in w or _ALIASES.get(tok, "\0") in w
               for tok in re.findall(r"[a-z]{4,}", place.lower()))


def guard(sp, words):
    """Drop any number or place the caller never said — it gets asked for instead."""
    nums, d = word_numbers(words), sp.model_dump()
    if d["quantity_per_month"] is not None and float(d["quantity_per_month"]) not in nums:
        d["quantity_per_month"] = None
    if d["ply"] is not None and float(d["ply"]) not in nums:
        d["ply"] = None
    if d["size_in"] and (len(d["size_in"]) != 3 or not all(float(x) in nums for x in d["size_in"])):
        d["size_in"] = None
    if d["delivery_location"] and not _place_said(d["delivery_location"], words):
        d["delivery_location"] = None
    return Spoken(**d)


# ---- speech ------------------------------------------------------------------------------

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def to_speech(text, max_sentences=3, max_chars=320):
    """Plain, short, speakable: no markdown, lists, code, URLs or emojis; at most 3 sentences."""
    t = str(text or "")
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    t = re.sub(r"^\s*([-*•·]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"(?:₹|\bINR\b|\bRs\.?)\s*([\d,]+(?:\.\d+)?)", r"\1 rupees", t)
    t = _EMOJI_RE.sub("", t)
    t = re.sub(r"[*_#>|~]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    out = " ".join(re.split(r"(?<=[.!?])\s+", t)[:max_sentences])
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return out


def _plural(item):
    i = item.strip()
    if i.endswith("s"):
        return i
    return i + "es" if i.endswith(("box", "x", "ch", "sh")) else i + "s"


def _num(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def _spoken_date(iso):
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {d:%B}"
    except (TypeError, ValueError):
        return iso


def next_question(cfg, sp):
    """The product's follow-up questions (config.yaml voice.required), asked in order."""
    for r in cfg["voice"]["required"]:
        if not getattr(sp, r["field"], None):
            return r["ask"]
    return None


def read_back(sp):
    ply = f"{sp.ply}-ply " if sp.ply else ""
    extras = [f"{' by '.join(_num(x) for x in sp.size_in)} inches" if sp.size_in else "",
              sp.material or "", f"printing: {sp.printing}" if sp.printing else ""]
    extra = "".join(f", {e}" for e in extras if e)
    by = f", needed by {_spoken_date(sp.needed_by)}" if sp.needed_by else ""
    return (f"Let me confirm: {sp.quantity_per_month:,} {ply}{_plural(sp.item)} a month{extra}, "
            f"delivered to {sp.delivery_location}{by}. Shall I register it?")


# ---- registering the request with the existing agent ---------------------------------------

def request_id_for(call_key):
    """Readable prefix plus a hash of the full call id: two calls can never share an id."""
    if call_key == "no-call-id":  # no stable identity to key on; just be unique
        return f"VOICE-{datetime.now(timezone.utc):%m%d%H%M%S}-{secrets.token_hex(2)}"
    short = re.sub(r"[^A-Za-z0-9]", "", call_key)[:8]
    return f"VOICE-{short}-{hashlib.sha256(call_key.encode()).hexdigest()[:6]}"


def register(cfg, db, s, call_key):
    """Hand the confirmed spec to the agent exactly as the dashboard would: a request row.
    "Registered" is only ever said for a row this call actually owns."""
    sp = s["pending"]
    spec = {k: v for k, v in {"ply": sp.ply, "size_in": sp.size_in, "material": sp.material,
                              "printing": sp.printing}.items() if v is not None}
    raw = {"item": sp.item, "spec": spec, "quantity_per_month": sp.quantity_per_month,
           "delivery_location": sp.delivery_location, "needed_by": sp.needed_by,
           "budget_inr_per_unit": None, "channel": "voice", "call_id": call_key,
           "caller_words": " ".join(s["words"])}
    base = rid = s["request_id"] or request_id_for(call_key)
    for n in range(2, 7):
        raw["request_id"] = rid
        won = db.insert("requests", {"request_id": rid, "input_json": raw, "created_by": "voice"},
                        conflict=["request_id"])
        if won:
            break
        owner = (db.select("requests", {"request_id": rid})[0]["input_json"] or {}).get("call_id")
        if owner == call_key and call_key != "no-call-id":
            break  # this call registered it already (a retried tool call)
        rid = f"{base}-{n}"  # another call owns that id: never claim its row
    else:
        return TROUBLE  # no free id found; don't tell the caller it worked
    tin, tout = s["tokens"]
    if won and (tin or tout):  # the request row exists now, so the cost row can reference it
        ledger.record_cost(db, rid, "voice_intake", "llm", tin + tout,
                           ledger.llm_cost_inr(cfg, tin, tout))
    s.update(request_id=rid, pending=None, tokens=[0, 0])
    if call_key == "no-call-id":
        SESSIONS.pop(call_key, None)  # the next anonymous caller starts a fresh session
    return cfg["voice"]["registered"]


def intake(cfg, s, q):
    s["words"].append(q)
    words = " ".join(s["words"])
    def add_usage(u):  # every model call is counted, including one that lost the race
        s["tokens"][0] += u["tokens_in"]
        s["tokens"][1] += u["tokens_out"]

    try:
        sp, _ = llm.extract(PROMPT.format(words=words), Spoken, cfg["voice"]["models"],
                            cfg["voice"]["llm_timeout_s"], on_usage=add_usage)
    except llm.LLMUnavailable as e:
        log.warning("extraction failed: %s", e)
        return TROUBLE
    sp = guard(sp, words)
    previous, s["pending"] = s["pending"], None
    if not (sp.item or sp.quantity_per_month or sp.delivery_location):
        return CAPABILITY
    question = next_question(cfg, sp)
    if question:
        return question
    s["pending"] = sp
    if previous == sp and NEGATE_RE.match(q):
        return "No problem. What should I change?"
    return read_back(sp)


def answer(cfg, query, call_id=None):
    """One spoken query -> one short spoken reply (the Vapi tool's result)."""
    q = (query or "").strip()
    if not q:
        return SAY_AGAIN
    key = call_id or "no-call-id"
    s = SESSIONS.setdefault(key, {"words": [], "pending": None, "request_id": None,
                                  "tokens": [0, 0]})
    v = cfg["voice"]
    short_yes = (bool(AFFIRM_RE.match(q) or APPROVE_RE.search(q))
                 and not re.search(r"\d", q) and len(q.split()) <= 8)
    if s["pending"] and short_yes and not NEGATE_RE.match(q):
        return to_speech(register(cfg, open_db(cfg), s, key))
    if s["request_id"]:
        return to_speech(v["already_registered"])
    has_number = bool(re.search(r"\d", q))
    if (APPROVE_RE.search(q) and not has_number) or (
            STATUS_RE.search(q) and not (NEED_RE.search(q) or has_number)):
        return to_speech(v["redirect"])  # registration only: progress lives on the dashboard
    return to_speech(intake(cfg, s, q))


# ---- startup and logging -----------------------------------------------------------------

def check_env(cfg):
    """Fail fast at startup, with a clear message, if a key the bridge needs is missing."""
    need = ["GEMINI_API_KEY"]
    if cfg["storage"]["backend"] == "supabase":
        need += ["SUPABASE_URL", "SUPABASE_SECRET_KEY"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"voice bridge: missing required env var(s): {', '.join(missing)}. "
                         "Add them to .env (see VOICE_DEMO.md).")


def log_turn(cfg, call_id, query, answer_text, ms, outcome):
    path = resolve(cfg, (cfg.get("voice") or {}).get("log_path"))
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now_iso(), "call_id": call_id, "query": query,
                            "answer": answer_text, "duration_ms": ms, "outcome": outcome},
                           ensure_ascii=False) + "\n")
