"""
Recompute the Bayesian columns on the existing scored table.

    python scripts/rescore_bayesian.py            # report only
    python scripts/rescore_bayesian.py --apply    # write the parquet back

IC, IC025, IC975, tier and signal are all functions of a, b, c and d, and all
four are already stored -- so swapping the frequentist bound for BCPNN needs no
API calls and no refetch. This exists so the change can be inspected before it
lands, and so the switch is reproducible rather than a one-off edit.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.score import bcpnn, tier  # noqa: E402

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
