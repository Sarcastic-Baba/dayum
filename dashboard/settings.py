"""Read-only view of the engine's config.yaml for display (cost/time limits).
The engine owns the file; the dashboard never changes it."""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
_cache: dict = {"mtime": None, "limits": {}}


def limits() -> dict:
    """{'cost_inr_per_request': 25, 'time_s_per_request': 400} — {} if the file is missing.
    Re-read when the file changes, so a live config edit shows up without a redeploy locally."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache["mtime"]:
        try:
            data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            _cache["limits"] = dict(data.get("limits") or {})
        except (OSError, yaml.YAMLError):
            _cache["limits"] = {}
        _cache["mtime"] = mtime
    return _cache["limits"]
