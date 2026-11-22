import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import fixtures  # noqa: E402
from tests.fakedb import FakeDb  # noqa: E402

APPROVER = ("approver@test.invalid", "approver-pass-123")
VIEWER = ("viewer@test.invalid", "viewer-pass-123")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("DEMO_APPROVER_EMAIL", APPROVER[0])
    monkeypatch.setenv("DEMO_APPROVER_PASSWORD", APPROVER[1])
    monkeypatch.setenv("DEMO_VIEWER_EMAIL", VIEWER[0])
    monkeypatch.setenv("DEMO_VIEWER_PASSWORD", VIEWER[1])
    monkeypatch.setenv("API_TOKEN", "test-api-token")


@pytest.fixture
def db():
    return FakeDb(fixtures.tables())


@pytest.fixture
def client(db):
    from app import app
    app.state.db_factory = lambda: db
    return TestClient(app, base_url="https://testserver")


def login(client, who):
    return client.post("/login", data={"email": who[0], "password": who[1], "next": "/"}, follow_redirects=False)


@pytest.fixture
def approver(client):
    assert login(client, APPROVER).status_code == 303
    return client


@pytest.fixture
def viewer(client):
    assert login(client, VIEWER).status_code == 303
    return client
