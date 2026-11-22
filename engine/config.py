"""config.yaml is reloaded at the start of every worker cycle, so a rule change (the hour-3
requirement change, the on-stage live fix) takes effect without a code change."""

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "config.yaml"


def load_config(path=None):
    path = Path(path or os.environ.get("BOXSOURCE_CONFIG") or DEFAULT_PATH).resolve()
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_dir"] = str(path.parent)
    return cfg


def resolve(cfg, p):
    """Relative paths in config are relative to the config file, not the current directory,
    so the worker, the CLI and the voice bridge always share one database and outbox."""
    if not p:
        return p
    p = Path(p)
    return str(p if p.is_absolute() else Path(cfg.get("_dir", ".")) / p)
