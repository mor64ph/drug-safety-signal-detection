"""
Expectedness: is this reaction already in the drug's official label?

Everything else in this project is computed from FAERS, which means every
diagnostic shares FAERS's weaknesses. The label is an independent source: the
regulator-approved statement of what a drug is known to do.

That distinction carries most of the interpretive weight in real
pharmacovigilance. A drug reported disproportionately for a reaction already
printed on its label is, in the ordinary case, the reporting system working --
clinicians report what they have been told to watch for, which is the notoriety
effect operating exactly as designed. The same score for a reaction that appears
nowhere in the label is a different kind of object. Only the second is a
candidate for something not yet known.

Source: https://api.fda.gov/drug/label.json (same key, same rate limits)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.client import _get_session, API_KEY

LABEL_URL = "https://api.fda.gov/drug/label.json"
_CACHE = Path(__file__).resolve().parent.parent / "data" / "raw" / "labels.json"

# Sections describing harm. Indications and dosage are deliberately excluded:
# a reaction matching the indication text would otherwise read as "expected".
_HARM_SECTIONS = (
    "boxed_warning",
    "warnings_and_cautions",
    "warnings",
    "adverse_reactions",
    "adverse_reactions_table",
    "contraindications",
    "precautions",
    "postmarketing_experience",
)

_STOPWORDS = {
    "and", "or", "of", "the", "in", "to", "for", "with", "a", "an",
    "increased", "decreased", "abnormal", "disorder", "nos", "unspecified",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


def _load_cache() -> dict:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(cache), encoding="utf-8")


def label_query(variants: list[str] | None = None, epc: str | None = None) -> str:
    """
    Build a search for the LABEL endpoint, which does not share the event schema.

    Event queries are rooted at the report: patient.drug.openfda.generic_name.
    Labels are the drug itself, so the same field is plain openfda.generic_name.
    Passing an event path here returns HTTP 404 rather than an error about the
    field, so the mistake reads as "this drug has no label" for every drug at
    once. Brand names live in openfda.brand_name, and activesubstance becomes
    openfda.substance_name.
    """
    if epc:
        return f'openfda.pharm_class_epc:"{epc}"'
    clauses: list[str] = []
    for v in variants or []:
        v = v.strip()
        if not v:
            continue
        clauses.append(f'openfda.generic_name:"{v}"')
        clauses.append(f'openfda.substance_name:"{v}"')
        clauses.append(f'openfda.brand_name:"{v}"')
    return "(" + " OR ".join(clauses) + ")" if clauses else ""


def fetch_label(drug_search: str, limit: int = 3) -> dict:
    """
    Harm-related label text for a drug. One API call.

    Several labels are merged because a molecule has many manufacturers and
    generic labels vary in completeness; taking only the first risks calling a
    reaction unlabelled because one filer wrote a shorter document.
    """
    params = {"search": drug_search, "limit": limit}
    if API_KEY:
        params["api_key"] = API_KEY
    try:
        r = _get_session().get(LABEL_URL, params=params, timeout=30)
        if r.status_code == 404:
            return {"text": "", "boxed": "", "found": False}
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {"text": "", "boxed": "", "found": False}

    parts: list[str] = []
    boxed: list[str] = []
    for result in data.get("results", []) or []:
        for section in _HARM_SECTIONS:
            value = result.get(section)
            if not value:
                continue
            text = " ".join(value) if isinstance(value, list) else str(value)
            parts.append(text)
            if section == "boxed_warning":
                boxed.append(text)

    return {
        "text": _norm(" ".join(parts)),
        "boxed": _norm(" ".join(boxed)),
        "found": bool(parts),
    }


def is_labelled(reaction_pt: str, label: dict) -> tuple[bool, bool]:
    """
    Return (appears_in_label, appears_in_boxed_warning).

    Matching is approximate and cannot be otherwise. Labels are prose written
    for clinicians; MedDRA terms are a controlled vocabulary. A label saying
    "delayed gastric emptying" and a term reading IMPAIRED GASTRIC EMPTYING mean
    the same thing and share no exact string, so a full-phrase test alone would
    under-report. Requiring every distinctive word instead tolerates reordering
    and intervening text.

    Errs toward reporting "not found", so the flag should be read as "this
    reaction was not located in the label", never as proof it is absent.
    """
    text = label.get("text") or ""
    if not text:
        return False, False

    pt = _norm(reaction_pt)
    boxed = label.get("boxed") or ""

    if pt and pt in text:
        return True, bool(boxed and pt in boxed)

    tokens = [t for t in pt.split() if t and t not in _STOPWORDS and len(t) > 3]
    if not tokens:
        return False, False
    in_label = all(t in text for t in tokens)
    in_boxed = bool(boxed) and all(t in boxed for t in tokens)
    return in_label, in_boxed


def attach_expectedness(pairs_df, drug_searches: dict[str, str]):
    """
    Add `labelled` and `boxed_warning` columns to a scored pairs table.

    Cost: one call per drug, cached to disk thereafter.
    """
    import pandas as pd

    df = pairs_df.copy()
    if "labelled" not in df.columns:
        df["labelled"] = None
        df["boxed_warning"] = None
        df["label_found"] = None

    cache = _load_cache()
    fetched = 0
    attempted = 0
    resolved = 0

    for drug, search in drug_searches.items():
        rows = df.index[df["drug"] == drug]
        if len(rows) == 0:
            continue

        if drug in cache:
            label = cache[drug]
        else:
            label = fetch_label(search)
            cache[drug] = label
            fetched += 1

        attempted += 1
        if not label.get("found"):
            df.loc[rows, "label_found"] = False
            continue
        resolved += 1

        df.loc[rows, "label_found"] = True
        for idx in rows:
            lab, box = is_labelled(str(df.at[idx, "reaction_pt"]), label)
            df.at[idx, "labelled"] = lab
            df.at[idx, "boxed_warning"] = box

    if fetched:
        _save_cache(cache)

    # Individual drugs legitimately lack a label. Nearly all of them failing
    # means the queries are wrong, not that the drugs are unlabelled -- which is
    # exactly how the event-schema mistake hid: it reported "no label found" for
    # 33,852 rows and looked like a result.
    if attempted and resolved / attempted < 0.5:
        raise RuntimeError(
            f"only {resolved}/{attempted} drugs resolved a label. That is a "
            f"query problem, not a finding -- check that label_query() is being "
            f"used rather than an event-endpoint search string."
        )

    for col in ("labelled", "boxed_warning", "label_found"):
        df[col] = df[col].astype("boolean")
    return df
