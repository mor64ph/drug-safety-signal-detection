"""
M4: Entity resolution, groupings, stoplist filtering, drug class tagging.

Takes a raw flattened DataFrame and returns a cleaned version with:
  - Stoplist rows removed
  - reaction_group column added (from groupings.json)
  - drug_class column added (glp1, statin, other)
  - molecule column added (canonical molecule name within class)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


# ---------------------------------------------------------------------------
# Config loaders (cached)
# ---------------------------------------------------------------------------

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


def _load_groupings() -> dict[str, list[str]]:
    path = _CONFIG_DIR / "groupings.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_targets_raw() -> dict:
    path = _CONFIG_DIR / "targets.json"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_targets() -> dict[str, dict]:
    """The drug classes. Keys are class names; "areas" and notes are excluded."""
    return _load_targets_raw().get("classes", {})


def _load_areas() -> dict[str, dict]:
    """Therapeutic areas, each listing the classes that form its comparator group."""
    return _load_targets_raw().get("areas", {})


# ---------------------------------------------------------------------------
# Molecule resolution builder
# ---------------------------------------------------------------------------

def _build_molecule_map(targets: dict) -> dict[str, tuple[str, str]]:
    """
    Returns a mapping from lowercased variant name → (drug_class, canonical_molecule).

    Handles partial substring matching via the returned dict. Exact matches
    take precedence.
    """
    mapping: dict[str, tuple[str, str]] = {}
    for drug_class, class_data in targets.items():
        molecules = class_data.get("molecules", {})
        for canonical, variants in molecules.items():
            for variant in variants:
                key = variant.strip().lower()
                mapping[key] = (drug_class, canonical)
            # Also map canonical itself
            mapping[canonical.strip().lower()] = (drug_class, canonical)
    return mapping


def _resolve_drug_class_molecule(
    drug_name: str | None,
    exact_map: dict[str, tuple[str, str]],
) -> tuple[str, str | None]:
    """
    Resolve drug_name to (drug_class, molecule).

    Tries exact match first, then substring match for generic_name variants
    (e.g. "ORAL SEMAGLUTIDE 14 MG" contains "oral semaglutide").
    """
    if not drug_name:
        return "other", None

    name_lower = drug_name.lower().strip()

    # Exact match
    if name_lower in exact_map:
        cls, mol = exact_map[name_lower]
        return cls, mol

    # Substring match: find all variants that appear in the drug_name
    best: tuple[str, str] | None = None
    best_len = 0
    for variant, (cls, mol) in exact_map.items():
        if variant in name_lower and len(variant) > best_len:
            best = (cls, mol)
            best_len = len(variant)

    if best:
        return best
    return "other", None


# ---------------------------------------------------------------------------
# Groupings lookup
# ---------------------------------------------------------------------------

def _build_pt_to_group(groupings: dict[str, list[str]]) -> dict[str, str]:
    """Return a mapping from PT (uppercase) → group name."""
    mapping: dict[str, str] = {}
    for group_name, pts in groupings.items():
        for pt in pts:
            mapping[pt.upper()] = group_name
    return mapping


# ---------------------------------------------------------------------------
# Main normalise function
# ---------------------------------------------------------------------------

def normalise(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Clean and enrich the flattened DataFrame.

    Steps:
      1. Drop stoplist rows (reaction_pt in stoplist)
      2. Add reaction_group column
      3. Add drug_class and molecule columns
      4. Filter to suspect drugs only (drug_characterization == '1') unless
         caller has already filtered

    Returns:
        (normalised_df, summary_dict)
    """
    if df.empty:
        return df.copy(), {"rows_in": 0, "rows_out": 0, "dropped_stoplist": 0}

    rows_in = len(df)
    stoplist = _load_stoplist()
    groupings = _load_groupings()
    targets = _load_targets()

    pt_to_group = _build_pt_to_group(groupings)
    exact_map = _build_molecule_map(targets)

    # 1. Stoplist filtering
    reaction_upper = df["reaction_pt"].fillna("").str.upper()
    mask_stop = reaction_upper.isin(stoplist)
    dropped_stoplist = mask_stop.sum()
    df = df[~mask_stop].copy()

    # 2. reaction_group
    df["reaction_group"] = (
        df["reaction_pt"]
        .fillna("")
        .str.upper()
        .map(pt_to_group)
    )
    df["reaction_group"] = df["reaction_group"].where(df["reaction_group"].notna(), None)

    # 3. drug_class and molecule
    resolved = df["drug_name"].apply(
        lambda name: _resolve_drug_class_molecule(name, exact_map)
    )
    df["drug_class"] = resolved.apply(lambda x: x[0])
    df["molecule"] = resolved.apply(lambda x: x[1])

    rows_out = len(df)

    summary = {
        "rows_in": rows_in,
        "rows_out": rows_out,
        "dropped_stoplist": int(dropped_stoplist),
        "glp1_rows": int((df["drug_class"] == "glp1").sum()),
        "statin_rows": int((df["drug_class"] == "statin").sum()),
        "other_rows": int((df["drug_class"] == "other").sum()),
        "unique_reactions": int(df["reaction_pt"].nunique()),
        "unique_molecules": int(df["molecule"].dropna().nunique()),
    }

    print(f"\n[normalise] Summary:")
    for k, v in summary.items():
        print(f"  {k}: {v:,}" if isinstance(v, int) else f"  {k}: {v}")
    print()

    return df, summary
