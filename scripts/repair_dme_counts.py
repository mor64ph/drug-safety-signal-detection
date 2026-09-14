"""
Repair cell `a` for DME pairs undercounted by defect D-09.

    python scripts/repair_dme_counts.py            # report only
    python scripts/repair_dme_counts.py --apply     # rewrite the parquet

D-09: dme_counts() accepted a term's bucket from any chunk, so a DME term that
co-occurred with another chunk's terms was overwritten by that chunk's
restricted, smaller count. Whichever chunk ran last won. The fix is in
src/score.py; this repairs the data already stored.

Why a full rescore is not needed. The two margins are correct whatever `a` was:
a+b is the drug's report total and a+c is the reaction's, and neither came from
the DME sweep. So given a corrected a', every other cell follows arithmetically

    b' = (a+b) - a'      c' = (a+c) - a'      d' = N - a' - b' - c'

and the metrics are recomputed from those. One DME sweep per drug -- about
1,805 calls -- instead of rebuilding the whole table.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import client  # noqa: E402
from src.client import QuotaExhausted  # noqa: E402
from src.score import bcpnn, dme_counts, tier  # noqa: E402

SCORED = ROOT / "data" / "results" / "scored_pairs.parquet"
CACHE = ROOT / "data" / "raw" / "dme_repair.json"
TARGETS = ROOT / "config" / "targets.json"
DME = ROOT / "config" / "dme.txt"


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
    apply = "--apply" in sys.argv
    df = pd.read_parquet(SCORED)
    terms = [l.strip() for l in DME.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    drugs = sorted(df["drug"].unique())
    queries = drug_queries(drugs)

    cache: dict = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}

    todo = [d for d in drugs if d not in cache]
    print(f"{len(drugs)} drugs, {len(todo)} to re-sweep "
          f"({len(terms)} DME terms, chunks of 12)")

    started = time.time()
    try:
        for i, drug in enumerate(todo, 1):
            cache[drug] = dme_counts(queries[drug], terms)
            if i % 25 == 0 or i == len(todo):
                rate = i / max(time.time() - started, 1e-9)
                print(f"  {i}/{len(todo)} ({rate:.2f} drugs/s)")
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except (KeyboardInterrupt, QuotaExhausted) as exc:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
        print(f"\nstopped ({type(exc).__name__}); rerun to resume.")
        return 1
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache), encoding="utf-8")

    # --- fallback for DME pairs the sweep did not return ------------------
    #
    # A term can be missing from its own chunk's response: with a small count
    # on a heavily-reported drug it falls below the bucket cap, crowded out by
    # co-occurring reactions. 45 of 14,736 DME pairs end up here, and their
    # stored `a` is whatever the clobbering left behind -- felodipine x SEPSIS
    # stored 12 while the severity sweep found 44 deaths on that pair, which is
    # impossible. One direct total() each closes them.
    resolved = {(d, str(r).upper())
                for d in cache for r in (cache.get(d) or {})}
    gaps = [(d, str(r).upper())
            for d, r, is_dme in zip(df["drug"], df["reaction_pt"], df["dme"])
            if is_dme and (d, str(r).upper()) not in resolved]
    if gaps:
        print(f"\n{len(gaps)} DME pairs absent from the sweep; fetching each "
              f"directly")
        from src.client import q_reaction
        for drug, term in gaps:
            try:
                n = client.total(f"{queries[drug]} AND {q_reaction(term)}")
            except Exception as exc:
                print(f"  {drug}/{term}: {client.redact(str(exc))[:60]}")
                continue
            cache.setdefault(drug, {})[term] = int(n)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")

    # --- compare stored `a` against the re-swept value ---------------------
    corrected = np.array([
        (cache.get(d) or {}).get(str(r).upper())
        for d, r in zip(df["drug"], df["reaction_pt"])
    ], dtype=object)

    have = np.array([v is not None for v in corrected])
    new_a = np.where(have, np.array([v if v is not None else 0
                                     for v in corrected], dtype=float),
                     df["a"].to_numpy(float))
    old_a = df["a"].to_numpy(float)

    changed = have & (new_a != old_a)
    under = have & (new_a > old_a)
    over = have & (new_a < old_a)
    print(f"\n{int(have.sum()):,} pairs are DME terms with a re-swept count")
    print(f"  {int(changed.sum()):,} differ from the stored value")
    print(f"    {int(under.sum()):,} were undercounted (the D-09 direction)")
    print(f"    {int(over.sum()):,} were overcounted")
    if changed.any():
        ratio = new_a[changed] / np.clip(old_a[changed], 1, None)
        print(f"  correction factor: median {np.median(ratio):.2f}, "
              f"max {np.max(ratio):.2f}")
        worst = np.argsort(-(new_a - old_a))[:6]
        print("  largest corrections:")
        for k in worst:
            if not changed[k]:
                continue
            print(f"    {df['drug'].iloc[k]:<16}"
                  f"{df['reaction_pt'].iloc[k][:26]:<28}"
                  f"{int(old_a[k]):>8,} -> {int(new_a[k]):>8,}")

    if not apply:
        print("\nreport only. pass --apply to rewrite the parquet.")
        return 0

    # --- recompute every cell and metric from the true margins -------------
    ab = old_a + df["b"].to_numpy(float)     # drug total, independent of `a`
    ac = old_a + df["c"].to_numpy(float)     # reaction total, likewise
    N = df["N"].to_numpy(float)

    a = new_a
    b = ab - a
    c = ac - a
    d = N - a - b - c

    with np.errstate(divide="ignore", invalid="ignore"):
        ror = (a * d) / (b * c)
        se = np.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
        ror_lo = np.exp(np.log(ror) - 1.96 * se)
        ror_hi = np.exp(np.log(ror) + 1.96 * se)
        prr = (a / ab) / (c / (c + d))
        chi2 = (a * d - b * c) ** 2 * N / (ab * (c + d) * ac * (b + d))
    ic, ic025, ic975 = bcpnn(a, b, c, d)

    bad = (b <= 0) | (c <= 0) | (d <= 0)
    for arr in (ror, ror_lo, ror_hi, prr, chi2):
        arr[bad] = np.nan

    df["a"] = a.astype(int)
    df["b"] = b.astype(int)
    df["c"] = c.astype(int)
    df["d"] = d.astype(int)
    df["ROR"] = np.round(ror, 4)
    df["ROR_lower"] = np.round(ror_lo, 4)
    df["ROR_upper"] = np.round(ror_hi, 4)
    df["PRR"] = np.round(prr, 4)
    df["chi2"] = np.round(chi2, 4)
    df["IC"] = np.round(ic, 4)
    df["IC025"] = np.round(ic025, 4)
    df["IC975"] = np.round(ic975, 4)
    df["signal"] = (a >= 3) & np.isfinite(ror_lo) & (ror_lo > 1.0)
    df["tier"] = [tier(v) if n >= 3 else "none"
                  for v, n in zip(df["IC025"], df["a"])]

    # The invariant that exposed D-09 in the first place.
    sev = [c for c in ("deaths", "hospitalisations", "life_threatening",
                       "disabling") if c in df.columns]
    if sev:
        ok = np.ones(len(df), dtype=bool)
        for col in sev:
            ok &= df[col].to_numpy(float) <= df["a"].to_numpy(float)
        df["severity_base_ok"] = ok
        print(f"\nseverity invariant: {int((~ok).sum()):,} rows still violate "
              f"it (was 877)")

    df.to_parquet(SCORED, index=False)
    print(f"wrote {SCORED.relative_to(ROOT)}")

    from src.validate import run_controls
    print("\nre-running the known-answer controls:")
    result = run_controls(scored_df=df)
    print(f"  {result['n_pass']}/{result['n_total']} passed")
    return 0 if result["n_pass"] == result["n_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
