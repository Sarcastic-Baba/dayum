"""config.yaml is reloaded at the start of every worker cycle, so a rule change (the hour-3
requirement change, the on-stage live fix) takes effect without a code change."""

import os

import yaml

DEFAULT_PATH = "config.yaml"


def load_config(path=None):
    path = path or os.environ.get("BOXSOURCE_CONFIG", DEFAULT_PATH)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
