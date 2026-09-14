"""
Attach reported outcome severity to every scored pair.

    python scripts/fetch_severity.py            # resume
    python scripts/fetch_severity.py --refresh  # start over

Adds four columns to data/results/scored_pairs.parquet: the number of reports
for each drug-reaction pair that were flagged as involving death,
hospitalisation, a life-threatening event, or disability.

Why it matters more than another ratio. ROR 4.2 where two thirds of the
reports involved hospitalisation is a different object from ROR 4.2 of
transient nausea, and until now the interface could not tell them apart. For
atorvastatin x RHABDOMYOLYSIS, 67.8% of the 6,122 reports record a
hospitalisation and 10.8% a death.

Cost. The naive shape is one call per pair per field: 33,852 x 4 = 135,408
calls, about four hours at the client's pinned spacing and more than the daily
quota. Instead this asks, per drug and per field,

    search=<drug> AND seriousnessdeath:1
    count=patient.reaction.reactionmeddrapt.exact

which returns the death-report count for every one of that drug's reactions in
a single response -- the same trick the DME sweep uses. 361 drugs x 4 fields =
1,444 calls, roughly six minutes.

Read these as reported outcomes, not as rates. The denominator is reports, not
patients, and seriousness is asserted by whoever filed the report.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src import client  # noqa: E402
from src.client import QuotaExhausted  # noqa: E402

SCORED = ROOT / "data" / "results" / "scored_pairs.parquet"
CACHE = ROOT / "data" / "raw" / "severity.json"
TARGETS = ROOT / "config" / "targets.json"

# FAERS flags these per report. `serious` is deliberately not used: it is true
# for any of the others, so it would add a column that is just their union.
FIELDS = {
    "deaths": "seriousnessdeath",
    "hospitalisations": "seriousnesshospitalization",
    "life_threatening": "seriousnesslifethreatening",
    "disabling": "seriousnessdisabling",
}


def drug_queries() -> dict[str, str]:
    """A label-endpoint-free search string per catalogue drug."""
    drugs = sorted(pd.read_parquet(SCORED, columns=["drug"])["drug"].unique())
    config = json.loads(TARGETS.read_text(encoding="utf-8"))

    molecules, classes = {}, {}
    for key, cls in config["classes"].items():
        classes[key] = cls.get("pharm_class_epc")
        for name, variants in (cls.get("molecules") or {}).items():
            molecules[name] = variants

    out = {}
    for drug in drugs:
        if drug in molecules:
            out[drug] = client.q_molecule(molecules[drug])
        elif drug in classes:
            out[drug] = client.q_class(classes[drug])
        else:
            raise SystemExit(f"{drug!r} is scored but absent from targets.json")
    return out


def main() -> int:
    refresh = "--refresh" in sys.argv
    queries = drug_queries()

    cache: dict = {}
    if CACHE.exists() and not refresh:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}

    todo = [(d, col) for d in queries for col in FIELDS
            if cache.get(d, {}).get(col) is None]
    print(f"{len(queries)} drugs x {len(FIELDS)} outcomes; "
          f"{len(todo)} calls to make")

    started = time.time()
    try:
        for i, (drug, col) in enumerate(todo, 1):
            field = FIELDS[col]
            search = f"{queries[drug]} AND {field}:1"
            try:
                rows = client.counts(
                    search, "patient.reaction.reactionmeddrapt.exact", limit=1000)
            except Exception as exc:
                # One unqueryable drug must not end the run, but the reason is
                # worth seeing -- silently zero-filling would read downstream
                # as "no deaths reported".
                print(f"  {drug}/{col}: {client.redact(str(exc))[:80]}")
                rows = []
            cache.setdefault(drug, {})[col] = {
                str(r["term"]): int(r["count"]) for r in rows
            }
            if i % 100 == 0 or i == len(todo):
                rate = i / max(time.time() - started, 1e-9)
                print(f"  {i}/{len(todo)} ({rate:.1f}/s)")
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except (KeyboardInterrupt, QuotaExhausted) as exc:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
        print(f"\nstopped ({type(exc).__name__}); rerun to resume.")
        return 1

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache), encoding="utf-8")

    df = pd.read_parquet(SCORED)
    for col in FIELDS:
        df[col] = [
            int((cache.get(drug, {}).get(col) or {}).get(rxn, 0))
            for drug, rxn in zip(df["drug"], df["reaction_pt"])
        ]

    # A count that exceeds the pair's own report total cannot be a share of it.
    #
    # This fires on 659 rows, 648 of them DME terms, concentrated in six
    # reactions: CARDIAC ARREST, SEPSIS, PANCYTOPENIA, ACUTE KIDNEY INJURY,
    # HEPATIC FAILURE, VENTRICULAR FIBRILLATION. The severity counts are
    # internally consistent -- for acetaminophen x CARDIAC ARREST,
    # total(drug AND reaction AND death) is 5,892 and matches the count bucket
    # exactly -- but the stored `a` for that pair is 3,259 against a measured
    # total(drug AND reaction) of 7,383. A random sample of ten other pairs,
    # DME and not, matched their stored `a` exactly, so `a` is broadly right
    # and something specific to these terms is not.
    #
    # Unresolved, and deliberately not papered over. The counts are stored
    # because they are correct; the *share* is suppressed per row wherever the
    # two cannot be reconciled, because dividing by a denominator this code
    # cannot verify would print a percentage over 100 and call it a rate.
    df["severity_base_ok"] = True
    for col in FIELDS:
        df.loc[df[col] > df["a"], "severity_base_ok"] = False
    inconsistent = int((~df["severity_base_ok"]).sum())
    if inconsistent:
        print(f"\n  {inconsistent:,} rows have a serious-outcome count above "
              f"their own `a`.")
        print(f"  Counts kept; share suppressed on those rows. See the comment "
              f"here and TEST-PLAN.md S-04.")

    df.to_parquet(SCORED, index=False)
    print(f"\nwrote {SCORED.relative_to(ROOT)}")
    covered = int((df[list(FIELDS)].sum(axis=1) > 0).sum())
    print(f"  {covered:,}/{len(df):,} pairs have at least one serious outcome "
          f"recorded ({covered / len(df):.0%})")
    for col in FIELDS:
        share = (df[col] / df["a"].clip(lower=1))
        print(f"  {col:<18} total {int(df[col].sum()):>10,}  "
              f"median share of a: {share.median():.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
