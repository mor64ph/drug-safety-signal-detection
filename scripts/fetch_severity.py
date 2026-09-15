"""
Attach reported outcome severity to every scored pair.

    python scripts/fetch_severity.py            # resume
    python scripts/fetch_severity.py --refresh  # start over

Adds four columns to data/results/scored_pairs.parquet: how many of a
drug-reaction pair's reports were flagged as involving death, hospitalisation,
a life-threatening event, or disability.

Why it matters more than another ratio. ROR 4.2 where two thirds of the reports
involved hospitalisation is a different object from ROR 4.2 of transient
nausea, and until these columns existed the interface could not tell them
apart. For atorvastatin x RHABDOMYOLYSIS, 4,152 of 6,123 reports record a
hospitalisation and 660 a death.

## Why this is chunked, and why the obvious version was wrong

The first version asked, once per drug and outcome,

    search=<drug> AND seriousnessdeath:1
    count=patient.reaction.reactionmeddrapt.exact

and read each pair's count out of the response. 1,444 calls, fast, and
**quietly wrong**. That response is capped at 1,000 buckets, and a heavily
reported drug has far more than 1,000 distinct reactions among its
death-flagged reports. Any term below the cut-off came back absent and was
stored as 0 -- indistinguishable from "no deaths were reported".

Measured, after it had already shipped: atorvastatin x TYPE 2 DIABETES
MELLITUS stored 0 hospitalisations against a true 792, and 0 deaths against a
true 219; atorvastatin x FOURNIER^S GANGRENE stored 0 against a true 255.
Rhabdomyolysis, ranking high enough to survive the cap, was correct. So the
column was right where the reaction was common and silently zero where it was
not, which is the worst possible shape for a severity figure.

The fix is the same trick the DME sweep uses. Restricting the search to reports
containing at least one of a dozen named reactions makes those reactions
dominate the buckets, so none of them can be pushed past the cap:

    search=<drug> AND (term1 OR ... OR term12) AND seriousnessdeath:1
    count=patient.reaction.reactionmeddrapt.exact

2,992 chunks x 4 outcomes = 11,968 calls, about 50 minutes. Exact for every
pair rather than fast and wrong for the tail.

**A bucket is only accepted from the chunk that contains its term.** This is
defect D-09 exactly: a chunk's response buckets every reaction in the matched
reports, not only the twelve searched for, so a term appearing as a
co-occurrence in another chunk would otherwise overwrite its own correct count
with a restricted, smaller one.

Read the result as reported outcomes, not as rates. The denominator is reports,
not patients, and seriousness is asserted by whoever filed the report.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src import client  # noqa: E402
from src.client import QuotaExhausted, F_REACTION, q_reaction  # noqa: E402

SCORED = ROOT / "data" / "results" / "scored_pairs.parquet"
CACHE = ROOT / "data" / "raw" / "severity.json"
TARGETS = ROOT / "config" / "targets.json"

CHUNK = 12

# The count endpoint returns 1,000 buckets with a key, 100 without. Asking for
# more than the ceiling is silently clamped, which is how the first rebuild
# came out wrong -- src.client.counts used to clamp to 100 unconditionally.
BUCKET_LIMIT = 1000

# FAERS flags these per report. `serious` is deliberately unused: it is true for
# any of the others, so it would add a column that is only their union.
FIELDS = {
    "deaths": "seriousnessdeath",
    "hospitalisations": "seriousnesshospitalization",
    "life_threatening": "seriousnesslifethreatening",
    "disabling": "seriousnessdisabling",
}


def drug_queries(drugs) -> dict[str, str]:
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
    df = pd.read_parquet(SCORED)
    by_drug = {d: sorted(g["reaction_pt"].astype(str).unique())
               for d, g in df.groupby("drug")}
    queries = drug_queries(list(by_drug))

    cache: dict = {}
    if CACHE.exists() and not refresh:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    # The old cache is keyed the same way but holds capped values, so a resume
    # would inherit the defect. Versioned to force one clean rebuild.
    if cache.get("_schema") != 3:
        cache = {"_schema": 3}

    jobs = []
    for drug, terms in by_drug.items():
        for i in range(0, len(terms), CHUNK):
            for col in FIELDS:
                jobs.append((drug, i, col))
    todo = [j for j in jobs
            if cache.get(j[0], {}).get(f"{j[1]}:{j[2]}") is None]
    print(f"{len(by_drug)} drugs, {len(df):,} pairs, {len(jobs):,} chunk-calls; "
          f"{len(todo):,} to make")

    stats = {"fallback": 0, "unresolved": 0}
    started = time.time()
    try:
        for n, (drug, offset, col) in enumerate(todo, 1):
            terms = by_drug[drug][offset:offset + CHUNK]
            owned = {t.upper() for t in terms}
            clause = "(" + " OR ".join(q_reaction(t) for t in terms) + ")"
            search = f"{queries[drug]} AND {clause} AND {FIELDS[col]}:1"
            got: dict[str, int] = {}
            truncated = False
            try:
                rows = client.counts(search, f"{F_REACTION}.exact",
                                     limit=BUCKET_LIMIT)
                # A response that fills the cap was cut off, so a term missing
                # from it may simply be below the cut rather than absent from
                # the data. Only a short response proves a real zero.
                truncated = len(rows) >= BUCKET_LIMIT
                for r in rows:
                    term = str(r["term"]).upper()
                    # D-09: only the owning chunk's buckets are trustworthy.
                    if term in owned:
                        got[term] = int(r["count"])
            except Exception as exc:
                print(f"  {drug}/{offset}/{col}: "
                      f"{client.redact(str(exc))[:70]}")
                truncated = True   # nothing was learned; do not record zeros

            # Resolve the ambiguous ones individually. These are by definition
            # rare reactions -- they did not make the top 1,000 of their own
            # restricted population -- so there is usually at most one per
            # chunk, and a direct count is exact.
            if truncated:
                for t in terms:
                    if t.upper() in got:
                        continue
                    try:
                        got[t.upper()] = client.total(
                            f"{queries[drug]} AND {q_reaction(t)} "
                            f"AND {FIELDS[col]}:1")
                        stats["fallback"] += 1
                    except Exception:
                        stats["unresolved"] += 1

            cache.setdefault(drug, {})[f"{offset}:{col}"] = {
                t.upper(): got.get(t.upper(), 0) for t in terms
            }
            if n % 400 == 0 or n == len(todo):
                rate = n / max(time.time() - started, 1e-9)
                eta = (len(todo) - n) / max(rate, 1e-9) / 60
                print(f"  {n:,}/{len(todo):,} ({rate:.1f}/s, ~{eta:.0f} min "
                      f"left, {stats['fallback']:,} direct lookups)")
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except (KeyboardInterrupt, QuotaExhausted) as exc:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
        print(f"\nstopped ({type(exc).__name__}); rerun to resume.")
        return 1

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache), encoding="utf-8")

    # Flatten: drug -> outcome -> term -> count
    flat: dict[str, dict[str, dict[str, int]]] = {}
    for drug, blocks in cache.items():
        if drug.startswith("_"):
            continue
        for key, mapping in blocks.items():
            _, col = key.split(":", 1)
            flat.setdefault(drug, {}).setdefault(col, {}).update(mapping)

    for col in FIELDS:
        df[col] = [
            int(((flat.get(d) or {}).get(col) or {}).get(str(r).upper(), 0))
            for d, r in zip(df["drug"], df["reaction_pt"])
        ]

    df["severity_base_ok"] = True
    for col in FIELDS:
        df.loc[df[col] > df["a"], "severity_base_ok"] = False
    bad = int((~df["severity_base_ok"]).sum())
    if bad:
        print(f"\n  {bad:,} rows have an outcome count above their own `a`; "
              f"share suppressed on those (see TEST-PLAN S-04)")

    df.to_parquet(SCORED, index=False)
    print(f"\nwrote {SCORED.relative_to(ROOT)}")
    covered = int((df[list(FIELDS)].sum(axis=1) > 0).sum())
    print(f"  {covered:,}/{len(df):,} pairs with at least one serious outcome "
          f"({covered / len(df):.0%})")
    print(f"  {stats['fallback']:,} counts resolved by a direct lookup after a "
          f"truncated response; {stats['unresolved']:,} unresolved")

    # The three that exposed the capped version. They are the regression test.
    print("\ncontrols (atorvastatin):")
    for rxn, col, expect in (("TYPE 2 DIABETES MELLITUS", "hospitalisations", 792),
                             ("TYPE 2 DIABETES MELLITUS", "deaths", 219),
                             ("FOURNIER^S GANGRENE", "hospitalisations", 255),
                             ("RHABDOMYOLYSIS", "hospitalisations", 4152)):
        row = df[(df["drug"] == "atorvastatin") & (df["reaction_pt"] == rxn)]
        got = int(row[col].iloc[0]) if len(row) else -1
        ok = abs(got - expect) <= max(2, expect * 0.01)
        print(f"  {'ok  ' if ok else 'FAIL'} {rxn[:26]:<28}{col:<18}"
              f"got {got:>7,}  expected ~{expect:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
