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
from functools import cache
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


# ---------------------------------------------------------------------------
# Interactions
#
# A different question from expectedness, answered by a different part of the
# same document.
#
# This is the only honest route to an interaction feature here. The statistical
# route was built and abandoned: the Omega measure scored 0/12 against curated
# pairs with known interactions -- warfarin+NSAID x GI HAEMORRHAGE at -1.19,
# opioid+benzodiazepine x RESPIRATORY DEPRESSION at -2.94 despite a boxed
# warning on both classes -- while promoting confounded pairs like
# warfarin+NSAID x SEPSIS at +3.71. The measure inverts, so it stays withheld.
#
# FAERS cannot be made to answer it either. drugcharacterization does record
# 1=Suspect, 2=Concomitant, 3=Interacting, but those are the reporter's own
# assertion rather than a derived fact, only 0.47% of the 27,080,643 drug
# entries are ever marked Interacting, and the field cannot be tied back to a
# named drug through the API: "ibuprofen AND NAUSEA" returns 19,517 reports,
# and adding drugcharacterization:1 keeps 19,487 of them, because the match is
# at report level and 99.8% of reports contain some suspect drug. The three
# categories sum to 184% of the reports.
#
# The label sidesteps all of it. A pharmacokinetic interaction -- an antibiotic
# slowing the clearance of something taken daily -- is known pharmacology,
# printed in the approved label before anyone is harmed. Reading it is a
# lookup, not an inference, so there is no measure to validate and nothing to
# get backwards.
# ---------------------------------------------------------------------------

_INTERACTION_SECTIONS = (
    "drug_interactions",
    "drug_and_or_laboratory_test_interactions",
    # Real content, not decoration: atorvastatin's interaction table runs to
    # 12,134 characters against 7,453 of prose. Tags are stripped on the way
    # in, so a quotation from it is at least readable.
    "drug_interactions_table",
    # The most serious combination warnings are not in the interactions
    # section at all. Measured: morphine's interaction sections contain zero
    # mentions of "benzodiazepine", because opioid + benzodiazepine
    # respiratory depression -- boxed on both classes -- is in the boxed
    # warning. Omitting it lost the single most safety-critical pair in the
    # whole test set.
    "boxed_warning",
)

# How labels actually name a class, keyed by the formal EPC string.
_SYNONYMS_FILE = (Path(__file__).resolve().parent.parent
                  / "config" / "class_synonyms.json")
@cache
def _class_synonyms() -> dict[str, list[str]]:
    try:
        raw = json.loads(_SYNONYMS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# Shorter than this and a term matches inside unrelated words. It also drops
# the handful of catalogue entries whose name is a bare word.
_MIN_TERM = 5


def _flatten(value) -> str:
    text = " ".join(value) if isinstance(value, list) else str(value or "")
    return _WS.sub(" ", _TAGS.sub(" ", text)).strip()


def fetch_interactions(drug_search: str, limit: int = 3) -> dict:
    """
    The label's interaction sections. One API call.

    Several labels are merged for the same reason expectedness merges them: a
    molecule has many filers and coverage is uneven. Measured on four drugs,
    the first label returned carried an interactions section for warfarin
    (6,477 chars) and atorvastatin (7,453) but **none at all** for ibuprofen,
    and for ciprofloxacin it returned the ophthalmic label, whose 469
    characters say only that no interaction studies were done. Reading one
    label would therefore report "no interactions" for a drug that has them.
    """
    params = {"search": drug_search, "limit": limit}
    if API_KEY:
        params["api_key"] = API_KEY
    try:
        r = _get_session().get(LABEL_URL, params=params, timeout=30)
        if r.status_code == 404:
            return {"text": "", "found": False}
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {"text": "", "found": False}

    parts: list[str] = []
    for result in data.get("results", []) or []:
        for section in _INTERACTION_SECTIONS:
            chunk = _flatten(result.get(section))
            if chunk:
                parts.append(chunk)

    return {"text": _WS.sub(" ", " ".join(parts)).strip(), "found": bool(parts)}


def interaction_terms(variants: list[str], epc: str | None) -> list[str]:
    """
    What to look for in another drug's interaction text, longest first.

    Both the names and the class, because labels are written both ways: one
    names "clarithromycin", another warns about "CYP3A4 inhibitors" or
    "NSAIDs" and never lists a member. Longest first so the quotation shown
    comes from the most specific match available.
    """
    terms = {v.strip() for v in variants or [] if len(v.strip()) >= _MIN_TERM}
    if epc:
        # "Sodium-Glucose Cotransporter 2 Inhibitor [EPC]" -> without the tag.
        plain = epc.replace("[EPC]", "").strip()
        if len(plain) >= _MIN_TERM:
            terms.add(plain)
        # And the abbreviation the label is likely to use instead. The formal
        # phrase alone missed lisinopril + ibuprofen: that label says "NSAID"
        # three times and "nonsteroidal" never.
        for synonym in _class_synonyms().get(epc, ()):
            if len(synonym) >= _MIN_TERM:
                terms.add(synonym)
    return sorted(terms, key=len, reverse=True)


def quote_around(text: str, start: int, end: int, limit: int = 400) -> str:
    """The sentence containing a match, clipped.

    Clipped because some of these sections are a single 7,000-character
    paragraph with no sentence break the regex can find.
    """
    left = text.rfind(". ", 0, start) + 2
    right = text.find(". ", end)
    quote = text[left: right + 1 if right != -1 else len(text)].strip()
    if len(quote) > limit:
        quote = quote[: limit - 3].rstrip() + "..."
    return quote


def build_term_index(entries: dict[str, dict]) -> tuple[dict[str, set], "re.Pattern"]:
    """A term -> owning drugs map, and one regex matching every term at once.

    A term can belong to several drugs, so this is a set: every molecule in a
    class shares that class's EPC string, and the class itself is also a
    catalogue entry, so "HMG-CoA Reductase Inhibitor" is owned by atorvastatin,
    simvastatin, the rest of the statins, and by 'statin' too.

    One combined pattern rather than one per pair. The first version of this
    compiled a fresh regex inside the pair loop -- 361 x 360 pairs times a few
    terms each, around 390,000 compilations -- and it had not finished after
    several minutes. Scanning each text once against a single alternation is
    361 searches instead.
    """
    owners: dict[str, set] = {}
    for drug, meta in entries.items():
        for term in interaction_terms(meta.get("variants") or [],
                                      meta.get("epc")):
            owners.setdefault(term.lower(), set()).add(drug)

    # Longest first so the alternation prefers the most specific term when two
    # overlap -- "insulin glargine" before "insulin".
    ordered = sorted(owners, key=len, reverse=True)
    # The trailing s? is load-bearing. A plain \b after the term cannot match
    # a plural: \b requires a non-word character, and the "s" of "nonsteroidal
    # anti-inflammatory drugs" is a word character, so the EPC phrase
    # "Nonsteroidal Anti-inflammatory Drug" failed against every label that
    # writes it in the plural -- which is all of them. That single missing
    # character cost methotrexate + ibuprofen.
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(t) for t in ordered) + r")s?\b", re.I)
    return owners, pattern


def mentions_in(text: str, owners: dict[str, set], pattern: "re.Pattern",
                exclude: str) -> dict[str, dict]:
    """Which drugs this text names, with the sentence naming each.

    Where a drug is matched by more than one of its terms, the longest wins,
    so the quotation shown is the most specific one available -- a label that
    says both "clarithromycin" and "CYP3A4 inhibitors" is quoted on the former.
    """
    if not text:
        return {}

    best: dict[str, tuple[int, str, int, int]] = {}
    for match in pattern.finditer(text):
        term = match.group(1).lower()
        for owner in owners.get(term, ()):
            if owner == exclude:
                continue
            current = best.get(owner)
            if current is None or len(term) > current[0]:
                best[owner] = (len(term), match.group(1),
                               match.start(), match.end())

    return {owner: {"term": term,
                    "quote": quote_around(text, start, end)}
            for owner, (_, term, start, end) in best.items()}


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
