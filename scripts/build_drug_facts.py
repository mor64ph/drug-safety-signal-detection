"""
Build data/results/drug_facts.json from the Drugs@FDA bulk file.

    python scripts/fetch_bulk.py drugsfda
    python scripts/build_drug_facts.py

What this is for. Reporting volume is shaped by how long a drug has been on the
market -- the Weber effect: reports peak in the first years after launch and
decline afterwards, independently of the drug. This project documents that as a
known confounder and has so far had no way to correct for it, because nothing
in FAERS says when a drug was approved. Drugs@FDA does, in 9 MB.

The number this produces is `first_approval`: the earliest original approval of
the molecule in the United States, across every application containing it. So
cerivastatin, withdrawn in 2001, carries 1997 and is visibly not comparable on
reporting volume with something approved last year.

Matching runs on products[].active_ingredients[].name, not on
openfda.generic_name. Measured on the file: 28,970 of 29,325 records carry
products, and only 12,509 -- 43% -- carry a populated openfda block, so
matching the openfda path alone would silently miss most of the file. That is
the same shape as the bug that once made every drug look unlabelled.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

BULK = ROOT / "data" / "raw" / "bulk" / "drugsfda.json.zip"
OUT = ROOT / "data" / "results" / "drug_facts.json"
TARGETS = ROOT / "config" / "targets.json"
SCORED = ROOT / "data" / "results" / "scored_pairs.parquet"

# The extract these figures are set against, so "years marketed" is stable and
# does not drift every time the script is run. Mirrors DATA_AS_OF in src/app.py.
AS_OF = date(2026, 7, 30)

# Known first US approvals, for a known-answer test. Every one of these was
# checked against the public approval record, not recalled from memory of the
# data. A tolerance of a year absorbs the difference between an original NDA
# approval date and the date a specific formulation was approved.
KNOWN = {
    "atorvastatin": 1996,     # Lipitor
    "cerivastatin": 1997,     # Baycol, withdrawn 2001
    "rosuvastatin": 2003,     # Crestor
    "simvastatin": 1991,      # Zocor
    "sitagliptin": 2006,      # Januvia
    "empagliflozin": 2014,    # Jardiance
    "semaglutide": 2017,      # Ozempic
    "liraglutide": 2010,      # Victoza
    "metformin": 1994,        # Glucophage, US
    "celecoxib": 1998,        # Celebrex
    "clopidogrel": 1997,      # Plavix
    "montelukast": 1998,      # Singulair
}
TOLERANCE_YEARS = 1

# Expected to resolve to nothing, and not a defect. Drugs@FDA records US
# approvals only, and these three have none: carbimazole is not marketed in the
# US, which uses methimazole; gliclazide is not FDA-approved; vildagliptin is
# European. They are in the catalogue because FAERS receives reports about them
# from outside the US. Absence here means "no US approval on record", which is
# a fact worth keeping rather than a gap worth filling.
NO_US_APPROVAL = {"carbimazole", "gliclazide", "vildagliptin"}


def catalogue() -> dict[str, dict]:
    """The 361 scored drugs, each with its variants and EPC class."""
    drugs = sorted(pd.read_parquet(SCORED, columns=["drug"])["drug"].unique())
    config = json.loads(TARGETS.read_text(encoding="utf-8"))

    molecules: dict[str, dict] = {}
    classes: dict[str, dict] = {}
    for key, cls in config["classes"].items():
        members = list((cls.get("molecules") or {}).keys())
        classes[key] = {"variants": [], "members": members}
        for name, variants in (cls.get("molecules") or {}).items():
            molecules[name] = {"variants": variants, "members": []}

    out: dict[str, dict] = {}
    for drug in drugs:
        if drug in molecules:
            out[drug] = {"kind": "molecule", **molecules[drug]}
        elif drug in classes:
            out[drug] = {"kind": "class", **classes[drug]}
        else:
            raise SystemExit(f"{drug!r} is scored but absent from targets.json")
    return out


def _ingredient_tokens(record: dict) -> set[str]:
    """Every ingredient and name this application can be recognised by."""
    names: set[str] = set()
    for product in record.get("products") or []:
        for ing in product.get("active_ingredients") or []:
            if ing.get("name"):
                names.add(str(ing["name"]).lower())
        if product.get("brand_name"):
            names.add(str(product["brand_name"]).lower())
    openfda = record.get("openfda") or {}
    for key in ("generic_name", "substance_name", "brand_name"):
        for value in openfda.get(key) or []:
            names.add(str(value).lower())
    return names


def _original_approval(record: dict) -> str | None:
    """The earliest approved ORIG submission date, as YYYYMMDD.

    ORIG rather than any approval: SUPPL submissions are later changes to an
    existing application -- a new strength, a manufacturing change -- and the
    sample record carried a SUPPL from 1991 on an application whose original
    approval was in the 1970s. Taking the earliest approval of any type would
    read supplements as launches.
    """
    dates = [
        s.get("submission_status_date")
        for s in record.get("submissions") or []
        if s.get("submission_type") == "ORIG"
        and s.get("submission_status") == "AP"
        and s.get("submission_status_date")
    ]
    return min(dates) if dates else None


def main() -> int:
    if not BULK.exists():
        raise SystemExit(f"{BULK.relative_to(ROOT)} is missing. Run: "
                         f"python scripts/fetch_bulk.py drugsfda")

    with zipfile.ZipFile(BULK) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            records = json.load(fh)["results"]
    print(f"{len(records):,} Drugs@FDA applications")

    entries = catalogue()
    molecules = {d: m for d, m in entries.items() if m["kind"] == "molecule"}

    # Word-boundary match per molecule. "ATORVASTATIN CALCIUM" has to match
    # atorvastatin, so this is a containment test rather than equality -- but
    # bounded, or 'insulin' would also match 'insulin-like growth factor'.
    #
    # Every variant, not just the catalogue key. Drugs@FDA carries US-adopted
    # names, and this catalogue keys several molecules on the INN: matching the
    # key alone left ciclosporin, ethinylestradiol and rifampicin unresolved
    # when the variant lists already held cyclosporine, ethinyl estradiol and
    # rifampin. Same length floor as the interaction matcher, so a short
    # variant cannot fire inside an unrelated ingredient name.
    patterns: dict[str, re.Pattern] = {}
    for name, meta in molecules.items():
        forms = {name.replace("_", " ")}
        forms.update(v.strip().lower() for v in meta.get("variants") or [])
        forms = {f for f in forms if len(f) >= 5}
        patterns[name] = re.compile(
            "|".join(r"\b" + re.escape(f) + r"\b"
                     for f in sorted(forms, key=len, reverse=True)))

    earliest: dict[str, str] = {}
    counts: dict[str, int] = {}
    for record in records:
        approval = _original_approval(record)
        if not approval:
            continue
        haystack = " | ".join(_ingredient_tokens(record))
        if not haystack:
            continue
        for name, pattern in patterns.items():
            if pattern.search(haystack):
                counts[name] = counts.get(name, 0) + 1
                if name not in earliest or approval < earliest[name]:
                    earliest[name] = approval

    facts: dict[str, dict] = {}
    for name, stamp in earliest.items():
        try:
            approved = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        except (ValueError, IndexError):
            continue
        facts[name] = {
            "first_approval": approved.isoformat(),
            "years_marketed": round((AS_OF - approved).days / 365.25, 1),
            "applications": counts.get(name, 0),
        }

    # Classes inherit the earliest approval among their members: the date the
    # class itself first reached the US market.
    for name, meta in entries.items():
        if meta["kind"] != "class":
            continue
        member_dates = [facts[m]["first_approval"]
                        for m in meta["members"] if m in facts]
        if not member_dates:
            continue
        first = min(member_dates)
        approved = date.fromisoformat(first)
        facts[name] = {
            "first_approval": first,
            "years_marketed": round((AS_OF - approved).days / 365.25, 1),
            "applications": sum(facts[m]["applications"]
                                for m in meta["members"] if m in facts),
            "from_class_members": True,
        }

    print(f"resolved an approval date for {len(facts)}/{len(entries)} "
          f"catalogue drugs ({len(facts) / len(entries):.0%})")

    unresolved = {d for d in entries if d not in facts}
    unexpected = unresolved - NO_US_APPROVAL
    if unexpected:
        print(f"  unresolved and NOT on the known no-US-approval list: "
              f"{sorted(unexpected)}")
    if unresolved & NO_US_APPROVAL:
        print(f"  unresolved as expected, no US approval: "
              f"{sorted(unresolved & NO_US_APPROVAL)}")

    # --- known-answer test -------------------------------------------------
    print("\ncontrols (first US approval):")
    wrong = 0
    for name, expected in KNOWN.items():
        got = facts.get(name, {}).get("first_approval")
        if not got:
            print(f"  MISS  {name:<15} expected ~{expected}, not resolved")
            wrong += 1
            continue
        year = int(got[:4])
        ok = abs(year - expected) <= TOLERANCE_YEARS
        wrong += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<15} {got}  "
              f"(expected ~{expected})")
    print(f"  {len(KNOWN) - wrong}/{len(KNOWN)} within "
          f"{TOLERANCE_YEARS} year")

    if len(facts) / len(entries) < 0.5:
        print("\n  WARNING: under half the catalogue resolved. Check the "
              "matching before shipping -- a missing date reads downstream as "
              "'age unknown', not as an error.")
        return 1
    if wrong > 2:
        print("\n  WARNING: too many controls wrong. The submission filter or "
              "the ingredient match is off.")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(facts, indent=1, sort_keys=True), encoding="utf-8")
    print(f"\nwrote {OUT.relative_to(ROOT)} "
          f"({OUT.stat().st_size / 1e3:.0f} KB)")

    ages = sorted(v["years_marketed"] for v in facts.values())
    print(f"  years marketed: min {ages[0]}, median "
          f"{ages[len(ages) // 2]}, max {ages[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
