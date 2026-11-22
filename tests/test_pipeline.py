"""Free-text requests from the dashboard (input_json.free_text) are parsed by the engine with
the voice extractor and its no-guess guard. The LLM is a fake here: no network calls."""

import yaml

import pytest

from engine import llm, orchestrator, sources


@pytest.fixture(autouse=True)
def offline_discovery(monkeypatch):
    """No network: discovery finds nothing, so these requests end no_verified_supplier."""
    monkeypatch.setattr(sources, "search_companies", lambda ctx, query, limit: [])
from engine.config import load_config
from engine.db import SQLiteDB
from engine.transport import FileTransport
from tests.fixture_data import SPEC


def _cfg(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    cfg["storage"] = {"backend": "sqlite", "sqlite_path": str(tmp_path / "p.db")}
    cfg["log_path"] = str(tmp_path / "run.log")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return load_config(path)


def test_free_text_request_is_parsed_without_guessing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    db = SQLiteDB(cfg["storage"]["sqlite_path"])

    def fake_extract(prompt, schema, models, timeout_s, on_usage=None):
        # "extracts" a quantity the text never states — the guard must drop it
        return schema(item="corrugated box", ply=5, quantity_per_month=99999,
                      delivery_location="Gurgaon"), {"tokens_in": 50, "tokens_out": 10}

    monkeypatch.setattr(llm, "extract", fake_extract)
    db.insert("requests", {"request_id": "WEB-T1", "created_by": "tester", "input_json": {
        "request_id": "WEB-T1", "item": None, "quantity_per_month": None, "delivery_location": None,
        "spec": {"ply": None, "size_in": [18, 12, 12]},          # a structured field that was filled in
        "free_text": "5 ply corrugated boxes for our Gurgaon warehouse"}})
    orchestrator.advance(db, cfg, FileTransport(tmp_path / "o", tmp_path / "i"), "WEB-T1")

    row = db.select("requests", {"request_id": "WEB-T1"})[0]
    spec = row["spec_json"]
    assert spec["item"] == "corrugated box" and spec["delivery_location"] == "Gurgaon"
    assert spec["spec"] == {"ply": 5, "size_in": [18, 12, 12]}  # form field kept, ply extracted
    assert spec["quantity_per_month"] is None                   # never said, so not invented
    assert "quantity_per_month not given — needs confirmation" in row["result_json"]["assumptions"]
    assert db.select("costs", {"request_id": "WEB-T1", "step": "parse_spec"})  # call is in the ledger
    assert row["result_json"]["limits"] == cfg["limits"]        # for the dashboard's progress bars
    assert row["state"] == "HELD" and row["status"] == "no_verified_supplier"  # nothing discovered


def test_structured_request_needs_no_llm(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    db = SQLiteDB(cfg["storage"]["sqlite_path"])

    def no_llm(*a, **k):
        raise AssertionError("a fully structured request must not call the LLM")

    monkeypatch.setattr(llm, "extract", no_llm)
    db.insert("requests", {"request_id": "WEB-T2", "input_json": {"request_id": "WEB-T2", **SPEC}})
    orchestrator.advance(db, cfg, FileTransport(tmp_path / "o", tmp_path / "i"), "WEB-T2")
    assert db.select("requests", {"request_id": "WEB-T2"})[0]["spec_json"]["quantity_per_month"] == 50000
