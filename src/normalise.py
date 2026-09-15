"""
Config loaders for the scoring pass.

This file used to be M4 of the record-level pipeline -- entity resolution,
reaction groupings, drug-class tagging -- operating on a flattened DataFrame of
individual reports. That whole approach was replaced by M5, which asks the
count endpoint for population-level totals directly and never holds a report.
`normalise()` and its five helpers had no caller left, so they are gone; what
remains is the two config files `run.py --score` reads on the way in.

Reaction groupings went with them, and deliberately: grouping preferred terms
before scoring destroys the signal it is meant to clarify, because a specific
term's disproportionality is diluted by every vague sibling folded in with it.
"""

from __future__ import annotations

import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


def _load_stoplist() -> frozenset[str]:
    path = _CONFIG_DIR / "stoplist.txt"
    if not path.exists():
        return frozenset()
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip().upper()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )


def _load_targets_raw() -> dict:
    path = _CONFIG_DIR / "targets.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_targets() -> dict[str, dict]:
    """The drug classes. Keys are class names; "areas" and notes are excluded."""
    return _load_targets_raw().get("classes", {})
