"""
M3: JSON → DataFrame flattener.

Produces one row per (report, suspect drug, reaction).
Handles missing fields defensively — never crashes, records nulls.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd


# ---------------------------------------------------------------------------
# Age normalisation constants
# ---------------------------------------------------------------------------
# patientonsetageunit: 800=Decade, 801=Year, 802=Month, 803=Week, 804=Day, 805=Hour
_AGE_UNIT_TO_YEARS: dict[str | int, float] = {
    800: 10.0,
    801: 1.0,
    802: 1 / 12,
    803: 1 / 52.18,
    804: 1 / 365.25,
    805: 1 / 8766.0,
    "800": 10.0,
    "801": 1.0,
    "802": 1 / 12,
    "803": 1 / 52.18,
    "804": 1 / 365.25,
    "805": 1 / 8766.0,
}


def _safe_str(val: object, fallback: str = "") -> str:
    if val is None:
        return fallback
    return str(val).strip()


def _first(lst: list | None, fallback: str = "") -> str:
    if not lst:
        return fallback
    return _safe_str(lst[0], fallback)


def _resolve_drug_name(drug: dict) -> str:
    """
    Resolve the best generic name for a drug record.

    Priority:
      1. openfda.generic_name[0]  (most reliable, but has qualifiers)
      2. activesubstance.activesubstancename
      3. medicinalproduct

    Returns lowercase.
    """
    openfda = drug.get("openfda") or {}
    generic_names = openfda.get("generic_name") or []
    if generic_names:
        return _first(generic_names).lower()

    active = drug.get("activesubstance") or {}
    name = _safe_str(active.get("activesubstancename"))
    if name:
        return name.lower()

    return _safe_str(drug.get("medicinalproduct")).lower()


def _normalise_age(report: dict) -> float | None:
    """Normalise patient onset age to years; returns None if absent/invalid."""
    patient = report.get("patient") or {}
    age_val = patient.get("patientonsetage")
    age_unit = patient.get("patientonsetageunit")
    if age_val is None:
        return None
    try:
        age_f = float(age_val)
    except (ValueError, TypeError):
        return None
    multiplier = _AGE_UNIT_TO_YEARS.get(age_unit, 1.0)  # default assume years
    return round(age_f * multiplier, 2)


# ---------------------------------------------------------------------------
# Main flattener
# ---------------------------------------------------------------------------

def flatten(records_iter: Iterable[dict]) -> pd.DataFrame:
    """
    Flatten an iterable of raw API report dicts into a tidy DataFrame.

    One row per (report × suspect_drug × reaction_pt).
    drug_characterization=1 (Suspect) rows are the primary analysis rows;
    the column is retained so callers can filter differently.

    Returns:
        pd.DataFrame with columns defined in MODULE SPEC M3.
    """
    rows: list[dict] = []

    for report in records_iter:
        report_id = _safe_str(report.get("safetyreportid"))
        receiptdate = _safe_str(report.get("receiptdate"))
        serious = _safe_str(report.get("serious"))
        serious_death = _safe_str(report.get("seriousnessdeath"))

        patient = report.get("patient") or {}
        age_years = _normalise_age(report)
        patient_sex = _safe_str(patient.get("patientsex"))

        # Reactions
        reactions: list[dict] = patient.get("reaction") or []
        reaction_pts: list[str] = []
        reaction_outcomes: list[str] = []
        for rxn in reactions:
            pt = _safe_str(rxn.get("reactionmeddrapt"))
            if pt:
                reaction_pts.append(pt.upper())  # normalise to UPPER for stoplist
            reaction_outcomes.append(_safe_str(rxn.get("reactionoutcome")))

        n_reactions = len(reactions)

        # Drugs
        drugs: list[dict] = patient.get("drug") or []
        n_suspect_drugs = sum(
            1 for d in drugs
            if _safe_str(d.get("drugcharacterization")) == "1"
        )

        for drug in drugs:
            char = _safe_str(drug.get("drugcharacterization"))
            drug_name = _resolve_drug_name(drug)
            drug_brand = _safe_str(drug.get("medicinalproduct"))

            # drugindication: may be a list or a string depending on report
            raw_indication = drug.get("drugindication")
            if isinstance(raw_indication, list):
                drugindication = "|".join(_safe_str(x) for x in raw_indication if x)
            else:
                drugindication = _safe_str(raw_indication)

            # Emit one row per reaction
            for i, rpt in enumerate(reaction_pts):
                outcome = reaction_outcomes[i] if i < len(reaction_outcomes) else ""
                rows.append(
                    {
                        "safetyreportid": report_id or None,
                        "receiptdate": receiptdate or None,
                        "serious": serious or None,
                        "seriousnessdeath": serious_death or None,
                        "drug_name": drug_name or None,
                        "drug_brand": drug_brand or None,
                        "drug_characterization": char or None,
                        "drugindication": drugindication or None,
                        "reaction_pt": rpt or None,
                        "reaction_outcome": outcome or None,
                        "patient_age_years": age_years,
                        "patient_sex": patient_sex or None,
                        "n_suspect_drugs": n_suspect_drugs,
                        "n_reactions": n_reactions,
                    }
                )

    if not rows:
        return pd.DataFrame(
            columns=[
                "safetyreportid", "receiptdate", "serious", "seriousnessdeath",
                "drug_name", "drug_brand", "drug_characterization",
                "drugindication", "reaction_pt", "reaction_outcome",
                "patient_age_years", "patient_sex",
                "n_suspect_drugs", "n_reactions",
            ]
        )

    df = pd.DataFrame(rows)
    _print_quality_report(df)
    return df


# ---------------------------------------------------------------------------
# Data quality report
# ---------------------------------------------------------------------------

def _print_quality_report(df: pd.DataFrame) -> None:
    """Print null rate per column to stdout."""
    n = len(df)
    print(f"\n[flatten] Data quality report — {n:,} rows total:")
    print(f"{'Column':<28} {'Null%':>7}  {'Non-null':>10}")
    print("-" * 50)
    for col in df.columns:
        nulls = df[col].isna().sum()
        pct = 100 * nulls / n if n > 0 else 0.0
        non_null = n - nulls
        print(f"  {col:<26} {pct:>6.1f}%  {non_null:>10,}")
    print()
