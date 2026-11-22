"""Local UI preview with FAKE fixture data (no Supabase needed). Test-only tooling.

    .venv\\Scripts\\python -m tests.preview_server
    open http://127.0.0.1:8000  — log in as approver@preview.local / preview-approver
                                    or viewer@preview.local / preview-viewer
"""

import os

import uvicorn

os.environ.setdefault("SESSION_SECRET", "preview-only-secret-" + "x" * 32)
os.environ.setdefault("DEMO_APPROVER_EMAIL", "approver@preview.local")
os.environ.setdefault("DEMO_APPROVER_PASSWORD", "preview-approver")
os.environ.setdefault("DEMO_VIEWER_EMAIL", "viewer@preview.local")
os.environ.setdefault("DEMO_VIEWER_PASSWORD", "preview-viewer")

from app import app  # noqa: E402
from tests import fixtures  # noqa: E402
from tests.fakedb import FakeDb  # noqa: E402

db = FakeDb(fixtures.tables())
# A request still running (checks under way), to show the live stepper and polling.
running = fixtures._request("FIXTURE-RUNNING", "CANDIDATES_CHECKED", "pending", fixtures._result(
    "FIXTURE-RUNNING", "CANDIDATES_CHECKED", "pending", duration_s=41,
    summary="FAKE: 4 candidate suppliers found; background checks in progress."))
db.insert("requests", running)
for row in fixtures._candidates("FIXTURE-RUNNING")[:2]:
    db.insert("candidates", row)
app.state.db_factory = lambda: db

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
