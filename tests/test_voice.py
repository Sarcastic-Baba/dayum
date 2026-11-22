"""The Vapi bridge: payload shapes, fallbacks, speech cleanup, and the intake flow.
The LLM is replaced by a fake here, so these tests make no network calls."""

import importlib
import os
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from engine import voice
from engine.db import SQLiteDB

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["storage"] = {"backend": "sqlite", "sqlite_path": str(tmp_path / "v.db")}
    cfg["voice"]["log_path"] = str(tmp_path / "voice.log")
    cfg["log_path"] = str(tmp_path / "run.log")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("BOXSOURCE_CONFIG", str(path))
    monkeypatch.setenv("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY") or "test-key")
    monkeypatch.delenv("VOICE_TIMEOUT_S", raising=False)
    import voice_server
    vs = importlib.reload(voice_server)
    voice.SESSIONS.clear()
    return vs, TestClient(vs.app), cfg


def payload(query=None, args=None, call_id="call-test-1", typ="tool-calls"):
    fn_args = args if args is not None else {"query": query}
    return {"message": {"type": typ, "call": {"id": call_id}, "toolCallList": [
        {"id": "call_abc", "type": "function",
         "function": {"name": "ask_backend_agent", "arguments": fn_args}}]}}


def result_of(resp):
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == ["results"] and body["results"][0]["toolCallId"] == "call_abc"
    return body["results"][0]["result"]


def test_health(server):
    _, client, _ = server
    assert client.get("/health").json() == {"ok": True}


def test_non_tool_call_events_are_ignored_with_200(server):
    _, client, _ = server
    r = client.post("/vapi/tool", json={"message": {"type": "status-update", "status": "ended"}})
    assert r.status_code == 200 and r.json() == {}
    r = client.post("/vapi/tool", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json() == {}


def test_arguments_as_object_or_json_string(server, monkeypatch):
    vs, client, _ = server
    monkeypatch.setattr(vs.voice, "answer", lambda cfg, q, call_id: f"heard {q}")
    assert result_of(client.post("/vapi/tool", json=payload("hello"))) == "heard hello"
    assert result_of(client.post("/vapi/tool", json=payload(args='{"query": "hi there"}'))) == "heard hi there"


def test_missing_or_empty_query_gets_a_spoken_fallback(server):
    _, client, _ = server
    for args in ({}, {"query": "   "}, "", "{not json"):
        assert result_of(client.post("/vapi/tool", json=payload(args=args))) == voice.SAY_AGAIN


def test_timeout_and_errors_return_the_spoken_fallback(server, monkeypatch):
    vs, client, _ = server
    monkeypatch.setattr(vs, "TIMEOUT_S", 0.05)
    monkeypatch.setattr(vs.voice, "answer", lambda cfg, q, call_id: time.sleep(0.5) or "late")
    assert result_of(client.post("/vapi/tool", json=payload("hello"))) == vs.FALLBACK

    def boom(cfg, q, call_id):
        raise RuntimeError("agent down")

    monkeypatch.setattr(vs, "TIMEOUT_S", 8)
    monkeypatch.setattr(vs.voice, "answer", boom)
    assert result_of(client.post("/vapi/tool", json=payload("hello"))) == vs.FALLBACK


def test_to_speech_is_plain_and_short():
    text = ("**Best quote:** ₹37.20/box from [Alpha](https://alpha.example) 📦\n"
            "- MOQ 10,000\n- Lead time 12 days. Third sentence. Fourth sentence. Fifth.")
    out = voice.to_speech(text)
    assert "*" not in out and "http" not in out and "📦" not in out and "\n" not in out
    assert "37.20 rupees" in out and out.count(".") <= 4


def test_word_numbers_reads_digits_and_words():
    assert {50000.0, 5.0, 18.0, 12.0} <= voice.word_numbers("50,000 5 ply, 18 by 12 by 12")
    assert 50000.0 in voice.word_numbers("fifty thousand boxes")
    assert 200000.0 in voice.word_numbers("2 lakh a month")
    assert 25000.0 in voice.word_numbers("twenty five thousand")
    assert 50500.0 in voice.word_numbers("fifty thousand five hundred")
    # Regression: digits followed by a number word are two numbers ("50,000 five ply").
    assert {50000.0, 5.0} <= voice.word_numbers("We need 50,000 five ply boxes")


class FakeLLM:
    """Always 'extracts' the full spec — including values the caller never said — so the
    tests prove the guard drops them and asks instead."""

    def __call__(self, prompt, schema, models, timeout_s, on_usage=None):
        usage = {"tokens_in": 100, "tokens_out": 20}
        if on_usage:
            on_usage(usage)
        return schema(item="corrugated box", quantity_per_month=50000, delivery_location="Gurgaon",
                      ply=5, size_in=[18, 12, 12]), usage


def test_intake_asks_followups_then_registers_once(server, monkeypatch):
    vs, client, cfg = server
    monkeypatch.setattr(voice.llm, "extract", FakeLLM())
    q = {r["field"]: r["ask"] for r in cfg["voice"]["required"]}
    say = lambda text: result_of(client.post("/vapi/tool", json=payload(text)))  # noqa: E731

    assert say("I need corrugated boxes") == q["quantity_per_month"]  # 50,000 was never said
    assert say("fifty thousand a month") == q["delivery_location"]
    assert say("to our Gurgaon warehouse") == q["ply"]
    assert say("5 ply") == q["size_in"]
    back = say("18 by 12 by 12 inches")
    assert back.startswith("Let me confirm: 50,000 5-ply corrugated boxes a month, 18 by 12 by 12")
    assert back.endswith("Shall I register it?")
    assert say("yes please") == cfg["voice"]["registered"]
    assert say("yes") == cfg["voice"]["already_registered"]  # a retry can't register twice

    db = SQLiteDB(cfg["storage"]["sqlite_path"])
    rid = voice.request_id_for("call-test-1")
    reqs = db.select("requests")
    assert [r["request_id"] for r in reqs] == [rid] and rid.startswith("VOICE-calltest-")
    raw = reqs[0]["input_json"]
    assert raw["quantity_per_month"] == 50000 and raw["spec"] == {"ply": 5, "size_in": [18, 12, 12]}
    assert raw["call_id"] == "call-test-1"
    assert reqs[0]["state"] == "RECEIVED"  # the sourcing agent takes over from here
    assert db.select("costs", {"request_id": rid})  # recorded after the row existed


def test_registered_is_only_said_for_a_row_this_call_owns(server, monkeypatch):
    """Regression: call ids sharing a prefix collided on one id, and the second caller was
    told 'registered' although nothing was written."""
    vs, client, cfg = server
    monkeypatch.setattr(voice.llm, "extract", FakeLLM())
    full = "50,000 5 ply corrugated boxes a month, 18 by 12 by 12 inches, to Gurgaon"
    for call in ("curl-call-003", "curl-call-005"):
        assert result_of(client.post("/vapi/tool", json=payload(full, call_id=call))).startswith("Let me confirm")
        assert result_of(client.post("/vapi/tool", json=payload("yes", call_id=call))) == cfg["voice"]["registered"]
    db = SQLiteDB(cfg["storage"]["sqlite_path"])
    owners = sorted(r["input_json"]["call_id"] for r in db.select("requests"))
    assert owners == ["curl-call-003", "curl-call-005"]  # two calls, two requests

    # A stale row from another call already holds this call's id: take a suffixed id.
    voice.SESSIONS.clear()
    clash = voice.request_id_for("curl-call-007")
    db.insert("requests", {"request_id": clash, "input_json": {"call_id": "someone-else"}})
    client.post("/vapi/tool", json=payload(full, call_id="curl-call-007"))
    client.post("/vapi/tool", json=payload("yes", call_id="curl-call-007"))
    mine = [r for r in db.select("requests") if r["input_json"].get("call_id") == "curl-call-007"]
    assert [r["request_id"] for r in mine] == [f"{clash}-2"]


def test_status_or_approval_questions_are_redirected(server, monkeypatch):
    vs, client, cfg = server
    monkeypatch.setattr(voice.llm, "extract", FakeLLM())
    for text in ("what's the status of my order?", "please approve the purchase order"):
        r = client.post("/vapi/tool", json=payload(text, call_id="call-other"))
        assert result_of(r) == cfg["voice"]["redirect"]
