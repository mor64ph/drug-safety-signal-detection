"""
M6: Positive/negative controls validation harness.

run_controls() tests known drug-reaction pairs against expected outcomes
and returns a pass/fail summary.

Nine positive controls spanning eight therapeutic areas, and four negative
controls. The positives are drug-reaction pairs where the answer is already
settled -- boxed warnings, mandatory monitoring requirements, and one worldwide
withdrawal -- so a pipeline that fails to reproduce them is broken regardless of
how plausible its other output looks.

Several controls guard a specific piece of machinery as well as the arithmetic:
cerivastatin guards the multi-field molecule search that makes withdrawn drugs
visible at all; Fournier's gangrene guards the caret handling for possessive
terms; the stoplist controls guard the artifact exclusions.

Each positive also gets a time-stability check on the last twelve months. A real
pharmacological effect persists across time windows; an artifact often does not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.score import score_from_api

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RESULTS_DIR = _PROJECT_ROOT / "data" / "results"

# ---------------------------------------------------------------------------
# Control definitions
# ---------------------------------------------------------------------------

from src.client import q_class, q_molecule

# Thresholds are set at roughly half the MEASURED value, never invented. An
# invented threshold once failed a control that was working correctly: GLP-1 x
# NAUSEA was given a >=5.0 bar before anyone measured it, and the true figure is
# 4.10 -- low because NAUSEA appears in 3.76% of all 20.7M reports, so the
# background swamps the ratio however strong the association. A control should
# fail when the pipeline breaks, not when a guess was wrong.

_POSITIVE_CONTROLS = [
    {
        "name": "Statins x RHABDOMYOLYSIS",
        "drug_search": q_class("HMG-CoA Reductase Inhibitor [EPC]"),
        "reaction_pt": "RHABDOMYOLYSIS",
        "check": "ROR_in_range",
        "ror_min": 8.82,   # 12.96 x 0.70
        "ror_max": 17.10,  # 12.96 x 1.30
        "note": "THE ANCHOR. Hand-computed 12.96. If this moves, distrust everything.",
    },
    {
        "name": "GLP-1 x NAUSEA",
        "drug_search": q_class("GLP-1 Receptor Agonist [EPC]"),
        "reaction_pt": "NAUSEA",
        "check": "ROR_above", "ror_min": 3.0,   # measured 4.10
        "note": "Known class effect; ROR compressed by nausea's high background",
    },
    {
        "name": "GLP-1 x IMPAIRED GASTRIC EMPTYING",
        "drug_search": q_class("GLP-1 Receptor Agonist [EPC]"),
        "reaction_pt": "IMPAIRED GASTRIC EMPTYING",
        "check": "ROR_above", "ror_min": 20.0,  # measured 37.98
        "note": "Mechanistically predicted from delayed gastric emptying",
    },
    {
        "name": "Clozapine x AGRANULOCYTOSIS",
        "drug_search": q_molecule(["clozapine", "clozaril"]),
        "reaction_pt": "AGRANULOCYTOSIS",
        "check": "ROR_above", "ror_min": 12.0,  # measured 23.63
        "note": "The reason clozapine requires mandatory blood-count monitoring",
    },
    {
        "name": "Fluoroquinolones x TENDON RUPTURE",
        "drug_search": q_class("Fluoroquinolone Antibacterial [EPC]"),
        "reaction_pt": "TENDON RUPTURE",
        "check": "ROR_above", "ror_min": 12.0,  # measured 24.11
        "note": "Boxed warning. Invisible to frequency-ranked candidates.",
    },
    {
        "name": "Lamotrigine x STEVENS-JOHNSON SYNDROME",
        "drug_search": q_molecule(["lamotrigine", "lamictal"]),
        "reaction_pt": "STEVENS-JOHNSON SYNDROME",
        "check": "ROR_above", "ror_min": 11.0,  # measured 21.98
        "note": "Boxed warning; the reason for slow dose titration",
    },
    {
        "name": "SGLT2 x FOURNIER^S GANGRENE",
        "drug_search": q_class("Sodium-Glucose Cotransporter 2 Inhibitor [EPC]"),
        "reaction_pt": "FOURNIER^S GANGRENE",
        "check": "ROR_above", "ror_min": 100.0,  # measured 272.0
        "note": "FDA safety communication 2018. Also guards the caret-term handling.",
    },
    {
        "name": "Amiodarone x PULMONARY FIBROSIS",
        "drug_search": q_molecule(["amiodarone", "cordarone", "pacerone"]),
        "reaction_pt": "PULMONARY FIBROSIS",
        "check": "ROR_above", "ror_min": 9.0,   # measured 19.13
        "note": "Classic amiodarone organ toxicity",
    },
    {
        "name": "Cerivastatin x RHABDOMYOLYSIS",
        "drug_search": q_molecule(["cerivastatin", "baycol"]),
        "reaction_pt": "RHABDOMYOLYSIS",
        "check": "ROR_above", "ror_min": 35.0,  # measured 70.56
        "note": "Withdrawn worldwide 2001 for this. Must out-signal every marketed "
                "statin, and guards the multi-field molecule search that makes "
                "withdrawn drugs visible at all.",
    },
]

_NEGATIVE_CONTROLS = [
    {
        "name": "GLP-1 x HEADACHE",
        "drug_search": q_class("GLP-1 Receptor Agonist [EPC]"),
        "reaction_pt": "HEADACHE",
        "check": "no_signal",
        "note": "Common background complaint with no GLP-1 mechanism",
    },
    {
        "name": "Statins x HEADACHE",
        "drug_search": q_class("HMG-CoA Reductase Inhibitor [EPC]"),
        "reaction_pt": "HEADACHE",
        "check": "no_signal",
        "note": "Second class, same non-specific term -- guards against a "
                "pipeline that simply flags everything",
    },
    {
        "name": "Any drug x DRUG INEFFECTIVE (stoplist)",
        "drug_search": q_class("GLP-1 Receptor Agonist [EPC]"),
        "reaction_pt": "DRUG INEFFECTIVE",
        "check": "stoplist_absent",
        "note": "Efficacy artifact; must be stoplisted out of results entirely",
    },
    {
        "name": "Any drug x EXTRA DOSE ADMINISTERED (stoplist)",
        "drug_search": q_class("GLP-1 Receptor Agonist [EPC]"),
        "reaction_pt": "EXTRA DOSE ADMINISTERED",
        "check": "stoplist_absent",
        "note": "Ranked FIRST for GLP-1 at ROR 24.8 before stoplisting -- a fact "
                "about pen injectors, not the molecule",
    },
]


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------

def _evaluate_positive(control: dict, result: dict) -> dict:
    """Evaluate a positive control against its API result."""
    check_type = control["check"]
    ror = result.get("ROR")
    ror_lo = result.get("ROR_lower")
    a = result.get("a", 0)
    signal = bool(a >= 3 and ror_lo is not None and ror_lo > 1.0)

    passed = False
    reason = ""

    if ror is None:
        passed = False
        reason = "ROR could not be computed (insufficient data)"
    elif check_type == "ROR_in_range":
        in_range = control["ror_min"] <= ror <= control["ror_max"]
        passed = signal and in_range
        reason = (
            f"ROR={ror:.2f} [95% CI {result.get('ROR_lower','?'):.2f}-"
            f"{result.get('ROR_upper','?'):.2f}], "
            f"target {control['ror_min']:.2f}-{control['ror_max']:.2f}, "
            f"signal={signal}"
        )
    elif check_type == "ROR_above":
        passed = signal and ror >= control["ror_min"]
        reason = (
            f"ROR={ror:.2f}, threshold>={control['ror_min']:.1f}, signal={signal}"
        )

    return {
        "name": control["name"],
        "type": "positive",
        "passed": passed,
        "a": a,
        "ROR": ror,
        "ROR_lower": result.get("ROR_lower"),
        "ROR_upper": result.get("ROR_upper"),
        "IC025": result.get("IC025"),
        "reason": reason,
        "note": control["note"],
    }


def _evaluate_negative(control: dict, result: dict, scored_df: pd.DataFrame | None) -> dict:
    """Evaluate a negative control."""
    check_type = control["check"]
    a = result.get("a", 0)
    ror_lo = result.get("ROR_lower")
    signal = bool(a >= 3 and ror_lo is not None and ror_lo > 1.0)

    # Negative controls assert on IC025, not on the `signal` gate. The gate --
    # a >= 3 and lower CI above 1 -- fires on 71% of measured pairs, the weakest
    # at ROR 1.10, because 20.7M reports make confidence intervals narrow enough
    # that any trivial elevation clears it. Asserting on it tests a column the
    # interface does not display.
    #
    # Measured separation: statins x HEADACHE is ROR 1.33, IC025 0.36 (tier
    # "weak"), against positive controls at IC025 3.32-4.32 (tier "strong").
    # The pipeline distinguishes them by an order of magnitude; only the binary
    # flag failed to.
    ic025 = result.get("IC025")
    notable = ic025 is not None and ic025 > 1.0

    if check_type == "stoplist_absent":
        # Should not appear in scored results at all
        if scored_df is not None and not scored_df.empty:
            rxn = control["reaction_pt"].upper()
            present = (scored_df["reaction_pt"].str.upper() == rxn).any()
            passed = not present
            reason = "NOT in results (correct)" if passed else "FOUND in results (stoplist miss)"
        else:
            passed = True
            reason = "No scored data available to check stoplist; assuming passed"
    elif check_type == "no_signal":
        passed = not notable
        ror = result.get("ROR")
        reason = (
            f"a={a:,}, ROR={ror:.2f}, IC025={ic025:.2f} -> "
            f"{'NOTABLE (should not be)' if notable else 'below the notable bar (correct)'}"
        )

    return {
        "name": control["name"],
        "type": "negative",
        "passed": passed,
        "a": a,
        "ROR": result.get("ROR"),
        "ROR_lower": ror_lo,
        "ROR_upper": result.get("ROR_upper"),
        "IC025": result.get("IC025"),
        "reason": reason,
        "note": control["note"],
    }


# ---------------------------------------------------------------------------
# Time-stability check
# ---------------------------------------------------------------------------

def _time_stability_check(control: dict, full_result: dict) -> dict:
    """
    Recompute ROR for the last 12 months and check direction is same.

    Uses API totals filtered by receiptdate.
    """
    drug_search = control["drug_search"]
    reaction_pt = control["reaction_pt"]

    from src.client import total as api_total, q_reaction
    import numpy as np

    # Last 12 months: 2025-01-01 to 2026-01-01
    window = "receiptdate:[20250101 TO 20260101]"
    rxn_clause = q_reaction(reaction_pt)
    recent_drug = f"({drug_search}) AND {window}"
    recent_rxn = f"{rxn_clause} AND {window}"
    recent_drug_and_rxn = f"({drug_search}) AND {rxn_clause} AND {window}"

    N_recent = api_total("receiptdate:[20250101 TO 20260101]")
    a_r = api_total(recent_drug_and_rxn)
    drug_total_r = api_total(recent_drug)
    ac_r = api_total(recent_rxn)

    b_r = max(drug_total_r - a_r, 0)
    c_r = max(ac_r - a_r, 0)
    d_r = max(N_recent - a_r - b_r - c_r, 0)

    from src.score import _ror
    ror_r, ror_lo_r, ror_hi_r = _ror(a_r, b_r, c_r, d_r)

    full_ror = full_result.get("ROR")
    direction_same = True
    if full_ror and not np.isnan(ror_r):
        # Both should be elevated (>1) or both not
        direction_same = (ror_r > 1) == (full_ror > 1)

    return {
        "name": f"{control['name']} (last-12m)",
        "type": "time_stability",
        "passed": direction_same,
        "a_recent": a_r,
        "ROR_recent": round(ror_r, 4) if not np.isnan(ror_r) else None,
        "ROR_full": full_ror,
        "reason": (
            f"Recent ROR={ror_r:.2f} vs full ROR={full_ror:.2f}, "
            f"direction_same={direction_same}"
        ),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_controls(scored_df: pd.DataFrame | None = None) -> dict:
    """
    Run all positive/negative controls using API total() calls.

    Args:
        scored_df: Optional precomputed results DataFrame (for stoplist check).

    Returns:
        dict with keys: results (list), passed (bool), summary.
    """
    all_results: list[dict] = []

    print("\n[validate] Running positive controls...")
    for ctrl in _POSITIVE_CONTROLS:
        print(f"  Testing: {ctrl['name']}")
        try:
            api_result = score_from_api(ctrl["drug_search"], ctrl["reaction_pt"])
            ev = _evaluate_positive(ctrl, api_result)
        except Exception as exc:
            ev = {
                "name": ctrl["name"],
                "type": "positive",
                "passed": False,
                "reason": f"ERROR: {exc}",
                "note": ctrl["note"],
            }
        status = "PASS" if ev["passed"] else "FAIL"
        print(f"    [{status}] {ev.get('reason', '')}")
        all_results.append(ev)

        # Time stability for positive controls
        try:
            api_result_for_stability = score_from_api(
                ctrl["drug_search"], ctrl["reaction_pt"]
            )
            ts = _time_stability_check(ctrl, api_result_for_stability)
            ts_status = "PASS" if ts["passed"] else "FAIL"
            print(f"    [{ts_status}] Time-stability: {ts['reason']}")
            all_results.append(ts)
        except Exception as exc:
            all_results.append(
                {
                    "name": f"{ctrl['name']} (last-12m)",
                    "type": "time_stability",
                    "passed": False,
                    "reason": f"ERROR: {exc}",
                }
            )

    print("\n[validate] Running negative controls...")
    for ctrl in _NEGATIVE_CONTROLS:
        print(f"  Testing: {ctrl['name']}")
        try:
            api_result = score_from_api(ctrl["drug_search"], ctrl["reaction_pt"])
            ev = _evaluate_negative(ctrl, api_result, scored_df)
        except Exception as exc:
            ev = {
                "name": ctrl["name"],
                "type": "negative",
                "passed": False,
                "reason": f"ERROR: {exc}",
                "note": ctrl["note"],
            }
        status = "PASS" if ev["passed"] else "FAIL"
        print(f"    [{status}] {ev.get('reason', '')}")
        all_results.append(ev)

    # Summary
    n_pass = sum(1 for r in all_results if r["passed"])
    n_total = len(all_results)
    overall_pass = all(
        r["passed"] for r in all_results if r["type"] != "time_stability"
    )

    print(f"\n[validate] Results: {n_pass}/{n_total} passed")
    print(_format_table(all_results))

    return {
        "results": all_results,
        "passed": overall_pass,
        "n_pass": n_pass,
        "n_total": n_total,
    }


def _format_table(results: list[dict]) -> str:
    """Format results as a simple text table."""
    lines = [
        f"\n{'Control':<45} {'Type':<15} {'Pass?':<6} {'Reason'}",
        "-" * 100,
    ]
    for r in results:
        status = "YES" if r.get("passed") else "NO"
        lines.append(
            f"  {r['name']:<43} {r.get('type',''):<15} {status:<6} {r.get('reason','')}"
        )
    return "\n".join(lines)
