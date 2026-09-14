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

BULK_DIR = ROOT / "data" / "raw" / "bulk"
BULK = BULK_DIR / "drugsfda.json.zip"
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


def _load_bulk(name: str) -> list[dict]:
    path = BULK_DIR / f"{name}.json.zip"
    if not path.exists():
        raise SystemExit(f"{path.relative_to(ROOT)} is missing. Run: "
                         f"python scripts/fetch_bulk.py {name}")
    with zipfile.ZipFile(path) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            return json.load(fh)["results"]


def _matchers(molecules: dict[str, dict]) -> dict[str, re.Pattern]:
    """One pattern per molecule, covering its key and every variant.

    Same construction as the approval match, and for the same reason: this
    catalogue keys several molecules on the INN while the bulk files use the
    US-adopted name.
    """
    out: dict[str, re.Pattern] = {}
    for name, meta in molecules.items():
        forms = {name.replace("_", " ")}
        forms.update(v.strip().lower() for v in meta.get("variants") or [])
        forms = {f for f in forms if len(f) >= 5}
        out[name] = re.compile(
            "|".join(r"\b" + re.escape(f) + r"\b"
                     for f in sorted(forms, key=len, reverse=True)))
    return out


def add_marketing(facts: dict, patterns: dict[str, re.Pattern]) -> None:
    """Whether each molecule is still on the US market, from the NDC directory.

    Absence from NDC is the withdrawal signal, not packaging.marketing_end_date.
    Only 4,098 of 137,841 NDC products carry an end date at all, and the ones
    that do are mostly in the *future* -- a package listing expiry, not a
    withdrawal. Cerivastatin, pulled from the world market in 2001, is simply
    not in the directory, which is the shape this actually takes.
    """
    records = _load_bulk("ndc")
    hits: dict[str, int] = {}
    for record in records:
        names = {str(record.get("generic_name") or "").lower(),
                 str(record.get("brand_name") or "").lower()}
        for ing in record.get("active_ingredients") or []:
            if ing.get("name"):
                names.add(str(ing["name"]).lower())
        haystack = " | ".join(n for n in names if n)
        if not haystack:
            continue
        for name, pattern in patterns.items():
            if pattern.search(haystack):
                hits[name] = hits.get(name, 0) + 1

    for name in patterns:
        entry = facts.setdefault(name, {})
        entry["ndc_products"] = hits.get(name, 0)
        entry["marketed_now"] = hits.get(name, 0) > 0
    print(f"  NDC: {sum(1 for v in hits.values() if v)} molecules currently "
          f"listed, from {len(records):,} products")


def add_recalls(facts: dict, patterns: dict[str, re.Pattern]) -> None:
    """Open recalls per molecule, from the enforcement reports.

    Matched on product_description, because only 3,261 of 17,938 records carry
    a populated openfda block -- 18%. Free text is worse than a structured
    field and it is what there is; the count is therefore a floor, and the
    wording is kept so a reader can judge it.
    """
    records = _load_bulk("enforcement")
    for name in patterns:
        facts.setdefault(name, {})["recalls_open"] = 0

    open_hits: dict[str, list[dict]] = {}
    for record in records:
        if record.get("status") != "Ongoing":
            continue
        openfda = record.get("openfda") or {}
        parts = [str(record.get("product_description") or "")]
        for key in ("generic_name", "brand_name", "substance_name"):
            parts.extend(str(v) for v in openfda.get(key) or [])
        haystack = " | ".join(parts).lower()
        if not haystack.strip():
            continue
        for name, pattern in patterns.items():
            if pattern.search(haystack):
                open_hits.setdefault(name, []).append({
                    "classification": record.get("classification"),
                    "reason": (record.get("reason_for_recall") or "")[:200],
                    "initiated": record.get("recall_initiation_date"),
                })

    for name, items in open_hits.items():
        entry = facts.setdefault(name, {})
        entry["recalls_open"] = len(items)
        # Class I is "reasonable probability of serious harm or death", so it
        # is the one worth surfacing on its own rather than as a count.
        entry["recalls_class_i"] = sum(
            1 for i in items if i["classification"] == "Class I")
        entry["recall_latest"] = max(items, key=lambda i: i["initiated"] or "")
    print(f"  recalls: {len(open_hits)} molecules with an open recall, from "
          f"{sum(1 for r in records if r.get('status') == 'Ongoing'):,} ongoing")


def add_shortages(facts: dict, patterns: dict[str, re.Pattern]) -> None:
    """Current shortages and planned discontinuations."""
    records = _load_bulk("shortages")
    found: dict[str, dict] = {}
    for record in records:
        status = record.get("status")
        if status not in ("Current", "To Be Discontinued"):
            continue
        haystack = " | ".join([
            str(record.get("generic_name") or ""),
            str(record.get("presentation") or ""),
        ]).lower()
        for name, pattern in patterns.items():
            if pattern.search(haystack):
                # "Current" outranks a planned discontinuation for display.
                existing = found.get(name)
                if existing and existing["status"] == "Current":
                    continue
                found[name] = {
                    "status": status,
                    "since": record.get("initial_posting_date"),
                    "reason": (record.get("related_info") or "")[:160],
                }
    for name, info in found.items():
        facts.setdefault(name, {})["shortage"] = info
    print(f"  shortages: {len(found)} molecules affected, from "
          f"{len(records):,} entries")


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

    # The remaining three sources describe the product rather than the
    # molecule's history, so they are added per molecule only. A pharmacologic
    # class is not something that can be recalled or run short.
    matchers = _matchers(molecules)
    print("\nsupply and safety status:")
    add_marketing(facts, matchers)
    add_recalls(facts, matchers)
    add_shortages(facts, matchers)

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

    # Withdrawal control. Cerivastatin was pulled from the world market in
    # 2001 and must not read as currently marketed; atorvastatin must. This is
    # the pair that makes the NDC-absence test meaningful rather than assumed.
    print("\nwithdrawal control (NDC listing):")
    for name, expect_marketed in (("cerivastatin", False), ("atorvastatin", True)):
        got = facts.get(name, {}).get("marketed_now")
        ok = got is expect_marketed
        wrong += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<14} marketed_now={got} "
              f"(expected {expect_marketed}), "
              f"{facts.get(name, {}).get('ndc_products', 0)} NDC products")

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

    # Not every entry has an approval date: the supply passes add a record for
    # molecules that Drugs@FDA has no US approval for, which is itself a fact
    # worth keeping rather than a reason to drop the row.
    ages = sorted(v["years_marketed"] for v in facts.values()
                  if "years_marketed" in v)
    print(f"  {len(ages)} with an approval date -- years marketed: "
          f"min {ages[0]}, median {ages[len(ages) // 2]}, max {ages[-1]}")
    print(f"  {sum(1 for v in facts.values() if v.get('marketed_now'))} "
          f"currently listed in NDC, "
          f"{sum(1 for v in facts.values() if v.get('marketed_now') is False)} "
          f"not")
    print(f"  {sum(1 for v in facts.values() if v.get('recalls_open'))} with an "
          f"open recall, "
          f"{sum(1 for v in facts.values() if v.get('recalls_class_i'))} of "
          f"them Class I")
    print(f"  {sum(1 for v in facts.values() if v.get('shortage'))} in "
          f"shortage or being discontinued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
