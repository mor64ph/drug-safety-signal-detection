"""
Recompute the derived statistics on the existing scored table.

    python scripts/rescore_bayesian.py            # report only
    python scripts/rescore_bayesian.py --apply    # write the parquet back

Every statistic here -- BCPNN's IC/IC025/IC975, MGPS's EBGM/EB05/EB95, the
tier and the signal gate -- is a function of a, b, c and d, all four of which
are already stored. So recomputing needs no API calls and no refetch. This
exists so a change can be inspected before it lands, and so the switch is
reproducible rather than a one-off edit.

Three measures now sit side by side, and they are not redundant:

  ROR     compares odds, with no prior at all.
  IC025   a log2 lower bound under a prior fixed in advance (BCPNN).
  EB05    a lower bound under a prior estimated from this very table (MGPS),
          and the number the FDA screens on, conventionally at 2.

Where they disagree, the disagreement is the finding. On cerivastatin's ALS row
-- 14 reports, the known litigation artifact -- BCPNN ranks it fifth for that
drug while MGPS still ranks it first, because MGPS shrinks toward the expected
count and that expectation is near zero for a drug with 366 reports in total.
Neither is wrong; they answer different questions. IC025 stays the ranking key
because it is the one validated against the 22 controls.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.score import bcpnn, fit_mgps, mgps, tier  # noqa: E402

SCORED = ROOT / "data" / "results" / "scored_pairs.parquet"


def main() -> int:
    apply = "--apply" in sys.argv
    df = pd.read_parquet(SCORED)
    print(f"{len(df):,} pairs")

    before_tier = df["tier"].copy()
    before_ic025 = df["IC025"].to_numpy(float)

    ic, ic025, ic975 = bcpnn(df["a"], df["b"], df["c"], df["d"])
    df["IC"] = np.round(ic, 4)
    df["IC025"] = np.round(ic025, 4)
    df["IC975"] = np.round(ic975, 4)
    df["tier"] = [tier(v) if n >= 3 else "none"
                  for v, n in zip(df["IC025"], df["a"])]

    # MGPS is a table-level fit, not a per-row calculation: its five
    # hyperparameters are estimated by maximum likelihood across every cell at
    # once, which is what makes its prior empirical rather than assumed. That
    # is why it lives here and not in the row loop of score_via_counts.
    expected_counts = ((df["a"] + df["b"]).to_numpy(float)
                       * (df["a"] + df["c"]).to_numpy(float)
                       / df["N"].to_numpy(float))
    params = fit_mgps(df["a"].to_numpy(float), expected_counts)
    print("MGPS hyperparameters, MLE over %s cells:" % f"{len(df):,}")
    print("  alpha1=%.4f beta1=%.4f alpha2=%.4f beta2=%.4f pi=%.4f" % params)
    ebgm, eb05, eb95, expected = mgps(df["a"], df["b"], df["c"], df["d"],
                                      params=params)
    df["expected"] = np.round(expected, 4)
    df["EBGM"] = np.round(ebgm, 4)
    df["EB05"] = np.round(eb05, 4)
    df["EB95"] = np.round(eb95, 4)
    violations = int(((df["EB05"] > df["EBGM"])
                      | (df["EBGM"] > df["EB95"])).sum())
    if violations:
        print("  WARNING: %d rows violate EB05 <= EBGM <= EB95" % violations)
        return 1
    print("  EB05 > 2, the FDA screening bar: %s pairs (%.1f%%)"
          % (f"{int((df['EB05'] > 2).sum()):,}",
             100 * (df["EB05"] > 2).mean()))

    print("\ntier counts:")
    b_counts = collections.Counter(before_tier)
    a_counts = collections.Counter(df["tier"])
    for name in ("strong", "moderate", "weak", "none"):
        print(f"  {name:<9} {b_counts.get(name, 0):>7,} -> {a_counts.get(name, 0):>7,} "
              f"({a_counts.get(name, 0) - b_counts.get(name, 0):+,})")

    moved = int((before_tier != df["tier"]).sum())
    print(f"  {moved:,} pairs changed tier")

    delta = np.abs(before_ic025 - df["IC025"].to_numpy(float))
    print(f"\nIC025 shift: mean {np.nanmean(delta):.4f}, "
          f"median {np.nanmedian(delta):.4f}, max {np.nanmax(delta):.4f}")

    width = df["IC975"].to_numpy(float) - df["IC025"].to_numpy(float)
    a = df["a"].to_numpy(float)
    print("credible interval width, by evidence:")
    for lo, hi, label in ((1, 9, "a<10"), (10, 99, "a=10-99"),
                          (100, 999, "a=100-999"), (1000, 10 ** 12, "a>=1000")):
        m = (a >= lo) & (a <= hi)
        if m.sum():
            print(f"  {label:<10} n={int(m.sum()):>6,}  median width "
                  f"{np.nanmedian(width[m]):.3f}")

    anchor = df[(df["drug"] == "statin") &
                (df["reaction_pt"] == "RHABDOMYOLYSIS")]
    if len(anchor):
        row = anchor.iloc[0]
        print(f"\nanchor statin x RHABDOMYOLYSIS: ROR {row['ROR']:.2f}, "
              f"IC {row['IC']:.4f}, IC025 {row['IC025']:.4f}, "
              f"IC975 {row['IC975']:.4f}")

    if not apply:
        print("\nreport only. pass --apply to write the parquet.")
        return 0

    df.to_parquet(SCORED, index=False)
    print(f"\nwrote {SCORED.relative_to(ROOT)}")

    # The control harness is the thing that decides whether this was safe.
    from src.validate import run_controls
    print("\nre-running the known-answer controls under BCPNN:")
    result = run_controls(scored_df=df)
    print(f"  {result['n_pass']}/{result['n_total']} passed")
    return 0 if result["n_pass"] == result["n_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
