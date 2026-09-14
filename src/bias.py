"""
M7: Bias diagnostics.

Attaches a "why you should doubt this" profile to each surfaced pair:

  - trend / burstiness   is the accrual steady, or one spike?
  - indication_overlap   is the "reaction" actually the reason for the drug?
  - active comparator    does the signal survive a fair comparison group?

All three are measurements rather than caveats. A disclaimer tells the reader
a bias might exist; these tell them how large it is for this specific pair.

Everything here is computed from the API, matching the population-level
scoring path in score.score_via_counts().
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from src.client import (
    call, counts, total as api_total, q_class, q_reaction, redact,
    F_INDICATION,
)
from src.score import _ror

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"

_TRIVIAL = {
    "AND", "OR", "WITH", "OF", "A", "AN", "THE", "IN", "TO", "FOR", "USED",
    "PRODUCT", "UNKNOWN", "INDICATION", "PROPHYLAXIS", "THERAPY", "NOS",
}


def _load_targets() -> dict:
    path = _CONFIG_DIR / "targets.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_comparators() -> dict[str, str]:
    """
    Map every drug name to the query for its comparison group.

    The group is the other classes treating the same thing, with the drug's own
    class removed. Leaving it in would compare a drug against itself and drive
    the ratio toward 1 regardless of the drug, which looks reassuring and means
    nothing.
    """
    raw = _load_targets()
    areas = raw.get("areas") or {}
    classes = raw.get("classes") or {}

    out: dict[str, str] = {}
    for class_key, spec in classes.items():
        area = areas.get(spec.get("area") or "")
        if not area:
            continue
        own = spec.get("pharm_class_epc")
        peers = [e for e in (area.get("classes") or []) if e != own]
        if not peers:
            continue
        query = "(" + " OR ".join(q_class(e) for e in peers) + ")"
        for name in [class_key, *(spec.get("molecules") or {})]:
            out[name] = query
    return out


# ---------------------------------------------------------------------------
# Indication confounding
# ---------------------------------------------------------------------------

def drug_indications(drug_search: str, limit: int = 40) -> dict[str, int]:
    """
    Top recorded indications for a drug, case-folded and merged. One API call.

    drugindication.exact is case-SENSITIVE, so the same concept arrives as
    several buckets: GLP-1 returns "TYPE 2 DIABETES MELLITUS" (54,836) and
    "Type 2 diabetes mellitus" (52,081) separately. Reading only the first
    would understate the true count by roughly half.
    """
    merged: dict[str, int] = defaultdict(int)
    for bucket in counts(drug_search, f"{F_INDICATION}.exact", limit=limit):
        term = str(bucket.get("term", "")).strip().upper()
        if term:
            merged[term] += int(bucket.get("count", 0))
    return dict(sorted(merged.items(), key=lambda kv: -kv[1]))


def indication_overlap(reaction_pt: str, indications: dict[str, int]) -> bool:
    """
    True when the "reaction" looks like the reason the drug was prescribed.

    Statins surface HYPERLIPIDAEMIA at ROR 69.6 and GLP-1 agonists surface
    diabetes complications. Neither is a drug effect: the term is disproportionate
    because only people with that condition receive the drug. This is confounding
    by indication, and it is the single most common way a disproportionality
    result misleads.
    """
    words = set(re.findall(r"[A-Z]+", reaction_pt.upper())) - _TRIVIAL
    if not words:
        return False
    for ind in indications:
        ind_words = set(re.findall(r"[A-Z]+", ind)) - _TRIVIAL
        if ind_words and words & ind_words:
            # Require a real overlap, not one incidental word.
            if len(words & ind_words) / len(words) >= 0.5:
                return True
    return False


# ---------------------------------------------------------------------------
# Reporting trend / notoriety
# ---------------------------------------------------------------------------

def pair_trend(pair_search: str) -> dict[str, int]:
    """
    Reports per month for a pair. One API call.

    count=receiptdate returns every distinct day present (~1,200 buckets) and
    is not subject to the 100-bucket cap, so a full history costs one request.
    """
    data = call({"search": pair_search, "count": "receiptdate"})
    monthly: dict[str, int] = defaultdict(int)
    for bucket in data.get("results", []) or []:
        day = str(bucket.get("time", ""))
        if len(day) >= 6:
            monthly[f"{day[:4]}-{day[4:6]}"] += int(bucket.get("count", 0))
    return dict(sorted(monthly.items()))


def burstiness(monthly: dict[str, int]) -> float:
    """
    Share of all reports falling in the single busiest month.

    Steady accrual tracks real prescribing. A tall spike usually tracks a news
    cycle, a published study, or a litigation advert -- reporting behaviour
    rather than a change in the drug.
    """
    if not monthly:
        return 0.0
    n = sum(monthly.values())
    return max(monthly.values()) / n if n else 0.0


# ---------------------------------------------------------------------------
# Active comparator
# ---------------------------------------------------------------------------

def active_comparator_ror(
    comparator_search: str,
    reaction_pt: str,
    comparator_total: int,
    reaction_total: int,
    grand_total: int = 20_692_690,
) -> float | None:
    """
    ROR recomputed against a clinically similar class instead of all of FAERS.

    Comparing a diabetes drug against every drug in the database partly measures
    diabetes. Comparing it against other diabetes drugs removes that. When the
    signal shrinks sharply the original number was substantially about the
    disease; the smaller number is the more honest one.
    """
    a = api_total(f"{comparator_search} AND {q_reaction(reaction_pt)}")
    if a == 0:
        return None
    b = max(comparator_total - a, 0)
    c = max(reaction_total - a, 0)
    d = max(grand_total - a - b - c, 0)
    if b == 0 or c == 0 or d == 0:
        return None
    ror, _lo, _hi = _ror(a, b, c, d)
    return None if pd.isna(ror) else round(float(ror), 4)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def attach_bias_flags(
    pairs_df: pd.DataFrame,
    drug_searches: dict[str, str],
    top_n: int = 12,
    checkpoint: "Path | None" = None,
) -> pd.DataFrame:
    """
    Add bias diagnostics to the highest-ranked pairs of each drug.

    Diagnostics are computed for the top `top_n` pairs per drug because each
    costs API calls and only ranked pairs are ever displayed. Lower-ranked rows
    keep their scores and carry null flags.

    Cost: 1 call per drug for indications, +2 per diagnosed pair.
    """
    if pairs_df.empty:
        return pairs_df

    targets = _load_targets()
    df = pairs_df.copy()
    for col in ("burstiness", "notoriety_spike", "indication_overlap",
                "active_comparator_ror", "active_comparator_shrinkage", "trend"):
        if col not in df.columns:
            df[col] = None
    # Diagnostics cost API calls, so only ranked pairs get them. Recording which
    # rows were examined keeps "we checked and found nothing" distinct from
    # "we never looked" -- an undiagnosed row rendered as a blank cell would
    # otherwise read as a clean bill of health.
    if "diagnosed" not in df.columns:
        df["diagnosed"] = False
    df["diagnosed"] = df["diagnosed"].fillna(False).astype(bool)

    comparator_of = build_comparators()
    comp_totals: dict[str, int] = {}
    bg_totals: dict[str, int] = {}
    # Drugs of the same class share a comparator and overlap heavily in their
    # top reactions, so this cache removes most of the cost of widening coverage.
    comp_pair: dict[tuple[str, str], float | None] = {}

    pending = [d for d in drug_searches if (df["drug"] == d).any()]
    total_drugs = len(pending)
    print(f"  [bias] {total_drugs} drug(s), top {top_n} reactions each")

    for n, drug in enumerate(pending, 1):
        search = drug_searches[drug]
        rows = df[df["drug"] == drug].head(top_n)
        rows = rows[~rows["diagnosed"]]
        if rows.empty:
            continue

        # This phase runs for tens of minutes. Without progress it is impossible
        # to tell a slow run from a hung one.
        if n % 20 == 0 or n == total_drugs:
            done = int(df["diagnosed"].sum())
            print(f"  [bias] {n}/{total_drugs} drugs, {done:,} rows diagnosed")
            if checkpoint:
                try:
                    df.to_parquet(checkpoint, index=False)
                except Exception:
                    pass

        try:
            indications = drug_indications(search)
        except Exception as exc:
            # Out of quota or network trouble. Keep what is already diagnosed
            # rather than losing the whole pass.
            print(f"  [bias] stopped at {drug}: {redact(str(exc))}")
            break

        failed = False
        for idx, row in rows.iterrows():
            pt = row["reaction_pt"]
            try:
                monthly = pair_trend(f"{search} AND {q_reaction(pt)}")
            except Exception as exc:
                print(f"  [bias] stopped at {drug} x {pt}: {redact(str(exc))}")
                failed = True
                break
            burst = burstiness(monthly)

            df.at[idx, "diagnosed"] = True
            df.at[idx, "trend"] = json.dumps(monthly)
            df.at[idx, "burstiness"] = round(burst, 4)
            df.at[idx, "notoriety_spike"] = bool(burst > 0.25)
            df.at[idx, "indication_overlap"] = bool(
                indication_overlap(pt, indications)
            )

            comp_search = comparator_of.get(drug)
            if not comp_search:
                continue
            if comp_search not in comp_totals:
                comp_totals[comp_search] = api_total(comp_search)
            if pt not in bg_totals:
                bg_totals[pt] = int(row["a"]) + int(row["c"])

            key = (comp_search, pt)
            if key not in comp_pair:
                comp_pair[key] = active_comparator_ror(
                    comp_search, pt, comp_totals[comp_search], bg_totals[pt]
                )
            ac = comp_pair[key]
            if ac is None:
                continue
            df.at[idx, "active_comparator_ror"] = ac
            ror = row.get("ROR")
            if ror:
                df.at[idx, "active_comparator_shrinkage"] = round(
                    (float(ror) - ac) / float(ror), 4
                )

        if failed:
            break

    for col in ("burstiness", "active_comparator_ror", "active_comparator_shrinkage"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("notoriety_spike", "indication_overlap"):
        df[col] = df[col].astype("boolean")
    return df


def sparkline(trend_json: str | None) -> list[dict[str, Any]]:
    """Convert a stored trend blob into [{month, count}] for charting."""
    if not trend_json:
        return []
    try:
        monthly = json.loads(trend_json)
    except (json.JSONDecodeError, TypeError):
        return []
    return [{"month": k, "count": v} for k, v in sorted(monthly.items())]
