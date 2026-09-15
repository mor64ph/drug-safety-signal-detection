"""
M12: the clinician worksheet.

Somebody types the medicines they take and the symptoms they have, and takes the
output to a prescriber. It is preparation for a conversation, and the one thing
it must never do is answer the question the reader arrived with.

Why, in numbers from this extract. FATIGUE is reported with 360 of the 361 drugs
here, NAUSEA with 360, DIARRHOEA with 356, DIZZINESS with 346, RASH with 339.
Take a realistic case -- five medicines, four symptoms -- and 18 of the 20
combinations produce a scored row. A page that ranked those rows by ROR would
name a culprit every single time, for any drug and any symptom anybody typed,
and the action it would prompt is stopping a prescribed medicine. Doing that on
the strength of a reporting ratio is more dangerous than the symptom.

So the design is the inverse of the rest of the site:

  * Nothing is ranked. Drugs appear in the order they were entered, symptoms in
    the order they were entered. Sorting by ROR or IC025 -- by anything that
    implies a culprit -- is the specific failure mode, not a feature that was
    left out.
  * Each symptom states how unspecific it is before any drug is mentioned,
    computed live from the table.
  * Each cell leads with the official label, because "your atorvastatin label
    does mention myalgia" is a checkable fact a prescriber can act on. The tier
    and the report count follow, smaller. The strength badge is deliberately not
    used here: filled, it is the loudest thing in a row, and ten rows of it
    rank-order the reader's medicines by eye whatever the sort order says.
  * Nothing entered is stored. The entries arrive by POST so they stay out of
    the URL, the browser history and the server's access log; they are not
    written to the database, to the search history or to the analytics counters.
    A symptom list attached to an identifiable account is health data, and this
    feature has no use for it after the page renders.

Anonymous by design -- someone worried about a medicine should not have to make
an account first -- so the only route protection is the app-wide CSRF check and
rate limiter.
"""

from __future__ import annotations

import json
import logging
from functools import cache
from pathlib import Path

import pandas as pd
from flask import Blueprint, render_template, request

log = logging.getLogger("rxsignal.worksheet")

_INTERACTION_PAIRS = (Path(__file__).resolve().parent.parent
                      / "data" / "results" / "label_interaction_pairs.json")

bp = Blueprint("worksheet", __name__)

# A real polypharmacy patient and a real consultation. Past this the printed
# page stops being something anyone reads in an appointment.
MAX_MEDICINES = 12
MAX_SYMPTOMS = 10

# Above this the ratio mostly reflects something the comparable drugs share --
# the illness, the route of administration, the reporting environment -- rather
# than the molecule, and that is worth saying in words on a page like this.
SHRINKAGE_PLAIN = 0.5

# Offered in the datalist. Chosen because they are the terms people actually
# arrive with, which is also why the page has to keep saying how unspecific
# they are.
COMMON_SYMPTOMS = (
    "FATIGUE", "NAUSEA", "DIARRHOEA", "DIZZINESS", "RASH", "HEADACHE",
    "MYALGIA", "ARTHRALGIA", "VOMITING", "CONSTIPATION", "ABDOMINAL PAIN",
    "PRURITUS", "COUGH", "INSOMNIA", "ANXIETY", "DEPRESSION", "TREMOR",
    "PALPITATIONS", "DYSPNOEA", "OEDEMA PERIPHERAL", "WEIGHT DECREASED",
    "WEIGHT INCREASED", "MUSCLE SPASMS", "ASTHENIA", "SOMNOLENCE",
    "DRY MOUTH", "HYPERHIDROSIS", "MEMORY IMPAIRMENT", "PARAESTHESIA",
    "VISION BLURRED", "ALOPECIA", "MALAISE",
)


def _app():
    """src.app registers this blueprint, so its helpers load at call time."""
    from src import app as app_module

    return app_module


@cache
def _interaction_index() -> dict:
    """Precomputed label interactions: {drug: {other drug: {term, quote}}}.

    Built offline by scripts/fetch_label_interactions.py. Loaded once and held,
    like the scored table -- nothing on the request path calls an API.

    Missing file is survivable and silent to the reader: the section simply
    does not appear. It is logged once, because "no interactions found" and
    "the index never got built" look identical on the page and only one of
    them is a fact about the medicines.
    """
    try:
        index = json.loads(_INTERACTION_PAIRS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "no label interaction index (%s): the worksheet will show no "
            "interaction section at all. Run "
            "scripts/fetch_label_interactions.py.", exc)
        return {}
    log.info("label interactions: %s drugs name at least one other",
             f"{len(index):,}")
    return index


def _interactions(names: list[str]) -> list[dict]:
    """Pairs on this list whose official labels name each other.

    One entry per unordered pair, carrying whichever directions were found.
    The two directions are not redundant -- a statin label naming
    clarithromycin and a clarithromycin label naming statins are two documents
    agreeing, and either alone is worth showing -- but they are one finding to
    a reader, not two.

    This says nothing about which medicine caused anything. It reports that a
    regulator-approved document tells the prescriber to take care combining
    them, which is a checkable fact and the reason this belongs on a page
    someone carries into an appointment.
    """
    index = _interaction_index()
    if not index:
        return []

    found: dict[tuple[str, str], dict] = {}
    for a in names:
        mentions = index.get(a) or {}
        for b in names:
            if a == b:
                continue
            hit = mentions.get(b)
            if not hit:
                continue
            key = tuple(sorted((a, b)))
            entry = found.setdefault(key, {"a": key[0], "b": key[1],
                                           "mentions": []})
            entry["mentions"].append(
                {"source": a, "names": b,
                 "term": hit.get("term", ""), "quote": hit.get("quote", "")})

    # Entered order, so the page does not imply a ranking here either.
    position = {name: i for i, name in enumerate(names)}
    return sorted(found.values(),
                  key=lambda e: (position.get(e["a"], 99),
                                 position.get(e["b"], 99)))


def _entries(raw: str, limit: int, label: str) -> tuple[list[str], list[str]]:
    """Split a free-text list on commas or newlines, de-duplicated, capped."""
    out: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").replace("\n", ",").replace(";", ",").split(","):
        item = part.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item[:64])

    notes: list[str] = []
    if len(out) > limit:
        dropped = out[limit:]
        out = out[:limit]
        notes.append(
            f"This page takes {limit} {label} at a time, so "
            f"{', '.join(dropped)} {'was' if len(dropped) == 1 else 'were'} "
            "left out."
        )
    return out, notes


def _resolve_medicines(raw: str) -> tuple[list[str], list[str]]:
    """Names the results table contains, in the order they were entered."""
    from src.user_features import resolve_drug

    entered, notes = _entries(raw, MAX_MEDICINES, "medicines")
    names: list[str] = []
    for item in entered:
        name, err = resolve_drug(item)
        if err:
            notes.append(err)
            continue
        if name in names:
            notes.append(
                f"'{item}' is the same active ingredient as one already listed "
                f"({name}), so it appears once."
            )
            continue
        names.append(name)
    return names, notes


# MedDRA has on the order of 26,000 preferred terms. This extract scores 879 of
# them, so "not a term used in this database" is a statement about coverage and
# has to be said as one -- a reader who is told their symptom is unknown will
# otherwise hear that it does not happen.
MEDDRA_TERM_COUNT = "roughly 26,000"


def _resolve_symptoms(raw: str) -> tuple[list[dict], list[str]]:
    """
    Match typed symptoms to scored MedDRA terms, in the order they were entered.

    _match_reactions already folds the caret form openFDA stores possessives in,
    so someone typing "fournier's gangrene" lands on FOURNIER^S GANGRENE.
    """
    app_module = _app()
    entered, notes = _entries(raw, MAX_SYMPTOMS, "symptoms")
    total_terms = len(app_module._reaction_index())

    out: list[dict] = []
    chosen: set[str] = set()
    for item in entered:
        query = app_module._clean_query(item)
        if not query:
            notes.append(
                f"'{item}' could not be read as a symptom — letters, numbers, "
                "spaces and the usual separators only."
            )
            continue

        matches = app_module._match_reactions(query)
        if not matches:
            notes.append(
                f"'{query}' is not a term used in this database. That does not "
                "mean it cannot happen, or that it is not a side effect of "
                f"anything you take: MedDRA has {MEDDRA_TERM_COUNT} terms, this "
                f"extract scores {total_terms} of them, and only those can be "
                "looked up. Try the clinical word — pruritus rather than "
                "itching, myalgia rather than muscle ache."
            )
            continue
        if len(matches) > 1:
            shown = ", ".join(m["term"] for m in matches[:8])
            notes.append(
                f"'{query}' matches {len(matches)} terms in this database, so it "
                f"was left out rather than guessed at. Enter one of them: {shown}"
                + ("…" if len(matches) > 8 else "")
            )
            continue

        match = matches[0]
        if match["stored"] in chosen:
            continue
        chosen.add(match["stored"])
        out.append(match)

    return out, notes


def _plain_notes(drug: str, term: str, row) -> list[str]:
    """The reasons to doubt a pair, written for someone reading it about themselves."""
    notes: list[str] = []

    if bool(row.get("indication_overlap")):
        notes.append(
            f"{term} is close to a condition {drug} is prescribed for, so reports "
            "pairing the two may be describing the illness rather than the "
            "medicine."
        )

    shrink = row.get("active_comparator_shrinkage")
    if shrink is not None and not pd.isna(shrink) and float(shrink) >= SHRINKAGE_PLAIN:
        notes.append(
            "Compared against medicines used for the same things, instead of "
            f"against the whole database, this pattern is {float(shrink):.0%} "
            f"weaker. Most of it is not specific to {drug}."
        )

    if bool(row.get("notoriety_spike")):
        notes.append(
            "More than a quarter of the reports behind this arrived in a single "
            "month, which usually follows a news story, a published study or a "
            "law-firm campaign rather than any change in the medicine."
        )

    return notes


def _label_fact(drug: str, term: str, pair: dict) -> tuple[str, str]:
    """
    The leading sentence of a cell: what the official product label says.

    This is the part a prescriber can check and act on. A ratio is not, which is
    why it comes second and smaller.
    """
    if pair["boxed"]:
        return "boxed", (
            f"The {drug} label carries {term} in its boxed warning — the "
            "strongest warning a label can carry."
        )
    if pair["labelled"] is True:
        return "listed", f"The {drug} label does mention {term}."
    if pair["labelled"] is False:
        return "absent", (
            f"{term} was not found in the {drug} label text. Label matching is "
            "approximate, so this is not proof the label leaves it out."
        )
    return "unchecked", (
        f"No label text could be retrieved for {drug}, so its label could not be "
        f"checked for {term}."
    )


def _sheet(names: list[str], symptoms: list[dict]) -> list[dict]:
    """
    One block per symptom, one row per medicine, both in the order entered.

    The pairs are formatted through _format_pairs so the figures on this page
    are the same figures the search page shows, and the raw row is kept beside
    it for the two diagnostics that have to be rendered as sentences here.
    """
    app_module = _app()
    df = app_module._load_results()
    total_drugs = df["drug"].nunique() if not df.empty else 0

    stored_terms = [s["stored"] for s in symptoms]
    subset = df[df["drug"].isin(names) & df["reaction_pt"].isin(stored_terms)] \
        if not df.empty else df

    formatted = {}
    raw_rows = {}
    if not subset.empty:
        for pair in app_module._format_pairs(subset):
            formatted[(pair["drug"], pair["reaction_pt"])] = pair
        for _, row in subset.iterrows():
            key = (str(row["drug"]), app_module._display_pt(str(row["reaction_pt"])))
            raw_rows[key] = row

    by_term = {s["stored"]: s for s in app_module._reaction_index()}

    blocks = []
    for symptom in symptoms:
        term = symptom["term"]
        indexed = by_term.get(symptom["stored"], symptom)
        reported_for = int(indexed.get("drugs", 0))

        rows = []
        for name in names:
            pair = formatted.get((name, term))
            if pair is None:
                rows.append({"drug": name, "pair": None})
                continue
            kind, sentence = _label_fact(name, term, pair)
            rows.append(
                {
                    "drug": name,
                    "pair": pair,
                    "label_kind": kind,
                    "label_fact": sentence,
                    "notes": _plain_notes(name, term, raw_rows.get((name, term), {})),
                }
            )

        blocks.append(
            {
                "term": term,
                "stored": symptom["stored"],
                "reported_for": reported_for,
                "total_drugs": int(total_drugs),
                "share": (reported_for / total_drugs) if total_drugs else 0.0,
                "reports": int(indexed.get("reports", 0)),
                "on_your_list": sum(1 for r in rows if r["pair"] is not None),
                "rows": rows,
            }
        )

    return blocks


@bp.route("/worksheet", methods=["GET", "POST"])
def worksheet():
    """
    The form on GET, the worksheet on POST.

    POST rather than a query string on purpose: a symptom list in a URL ends up
    in the browser history, in the Referer header and in every access log
    between here and the reader, and this page has no reason to put it there.
    """
    if request.method != "POST":
        return render_template(
            "worksheet.html",
            medicines_raw="",
            symptoms_raw="",
            names=[],
            blocks=[],
            notes=[],
            error=None,
            interactions=[],
            catalogue=_app()._catalogue(),
            symptom_options=COMMON_SYMPTOMS,
            max_medicines=MAX_MEDICINES,
            max_symptoms=MAX_SYMPTOMS,
            disclaimer=_app().DISCLAIMER,
        )

    medicines_raw = (request.form.get("medicines") or "")[:1024]
    symptoms_raw = (request.form.get("symptoms") or "")[:1024]

    names, med_notes = _resolve_medicines(medicines_raw)
    symptoms, symptom_notes = _resolve_symptoms(symptoms_raw)
    notes = med_notes + symptom_notes

    error = None
    if not medicines_raw.strip() or not symptoms_raw.strip():
        error = (
            "Enter at least one medicine and at least one symptom. The page needs "
            "both to have anything to put in front of a prescriber."
        )
    elif not names and not symptoms:
        error = "None of what you entered could be matched. The details are below."
    elif not names:
        error = (
            "None of those medicines is in the catalogue, so there is nothing to "
            "set against your symptoms."
        )
    elif not symptoms:
        error = (
            "None of those symptoms is a term this database scores, so there is "
            "nothing to look up. Take the list of medicines to your prescriber "
            "anyway — the symptom being absent here says nothing about it."
        )

    blocks = [] if error else _sheet(names, symptoms)

    # Independent of the symptoms: a labelled interaction is a property of the
    # combination, and stays worth showing even when nothing the reader typed
    # is a term this database scores.
    interactions = _interactions(names) if names else []

    # Deliberately nothing recorded: no analytics.record_search, no counter, no
    # row. The only trace this request leaves is the rendered page.
    return render_template(
        "worksheet.html",
        medicines_raw=medicines_raw,
        symptoms_raw=symptoms_raw,
        names=names,
        blocks=blocks,
        interactions=interactions,
        notes=notes,
        error=error,
        catalogue=_app()._catalogue(),
        symptom_options=COMMON_SYMPTOMS,
        max_medicines=MAX_MEDICINES,
        max_symptoms=MAX_SYMPTOMS,
        disclaimer=_app().DISCLAIMER,
    )
