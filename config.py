"""Load and expose config.yaml as a module-level dict."""

from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

with open(_CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)
