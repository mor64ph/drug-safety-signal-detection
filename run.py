"""
reportscope pipeline entrypoint.

Usage:
  python run.py --score     # Score drugs, check labels, attach bias flags
  python run.py --rescore   # As above, rebuilding every drug from scratch
  python run.py --validate  # Run the known-answer control harness
  python run.py --serve     # Start the Flask app
  python run.py --fetch     # Download a record-level corpus (only M8 needs this)

--score resumes: drugs already present in data/results/ are skipped, so an
interrupted run loses nothing and can simply be re-run.

API key:
  Put OPENFDA_API_KEY=<key> in a .env file beside this script, or set it in the
  environment. Free from https://open.fda.gov/apis/authentication/ and raises
  the ceiling from 1,000 requests/day to 120,000. A full run needs roughly
  20,000, so the key is effectively required for the label and bias passes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The Windows console defaults to cp1252, which mangles the multiplication
# sign in control names. Reconfigure before anything prints.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Add project root to sys.path so 'src' imports work
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_RESULTS_DIR = _PROJECT_ROOT / "data" / "results"
_FLAT_DIR = _PROJECT_ROOT / "data" / "flat"

# Reactions per drug given bias diagnostics. 120 exceeds the largest per-drug
# table (106), so every row is covered. Roughly 34,000 requests, which needs an
# API key; without one, lower it and accept partial coverage.
BIAS_TOP_N = 120

# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step_fetch() -> None:
    """M2: Fetch and cache raw FAERS data for GLP-1 and statin classes."""
    from src.fetch import fetch_class
    from src.flatten import flatten
    import pandas as pd
    import json

    print("=" * 60)
    print("STEP 1: Fetching GLP-1 reports")
    print("=" * 60)
    glp1_search = 'patient.drug.openfda.pharm_class_epc:"GLP-1 Receptor Agonist [EPC]"'
    glp1_records = list(fetch_class(glp1_search))

    print("\n" + "=" * 60)
    print("STEP 2: Fetching statin reports")
    print("=" * 60)
    statin_search = 'patient.drug.openfda.pharm_class_epc:"HMG-CoA Reductase Inhibitor [EPC]"'
    statin_records = list(fetch_class(statin_search))

    all_records = glp1_records + statin_records
    print(f"\n[run] Total records fetched: {len(all_records):,}")
    print(f"  GLP-1: {len(glp1_records):,}")
    print(f"  Statin: {len(statin_records):,}")

    # Flatten immediately and save raw parquet for subsequent steps
    print("\n[run] Flattening records...")
    df = flatten(iter(all_records))

    _FLAT_DIR.mkdir(parents=True, exist_ok=True)
    flat_path = _FLAT_DIR / "flat_raw.parquet"
    df.to_parquet(flat_path, index=False)
    print(f"[run] Flat data saved to {flat_path} ({len(df):,} rows)")


def step_score() -> None:
    """M5: Build the exact population-level scored table via the count endpoint."""
    import json
    import pandas as pd
    from src.client import q_class, q_molecule, redact
    from src.normalise import _load_stoplist, _load_targets
    from src.score import score_via_counts, load_dme

    stoplist = set(_load_stoplist())
    targets = _load_targets()
    dme = load_dme()
    from src.client import API_KEY
    if API_KEY:
        print("[run] using openFDA API key (120,000 requests/day)")
    else:
        print("[run] NO API KEY: capped at 1,000 requests/day. A free key from")
        print("      https://open.fda.gov/apis/authentication/ raises this to")
        print("      120,000. Set OPENFDA_API_KEY. Progress is saved either way.")
    print(f"[run] {len(dme)} designated medical events checked per drug")

    from src.labels import label_query

    jobs: list[tuple[str, str]] = []
    # The label endpoint has its own schema, so each drug needs a second query
    # built from the same names. Reusing the event query returns 404 for every
    # drug and looks like "no label exists".
    label_jobs: dict[str, str] = {}
    for class_key, spec in targets.items():
        epc = spec.get("pharm_class_epc")
        if epc:
            jobs.append((class_key, q_class(epc)))
            label_jobs[class_key] = label_query(epc=epc)
        for molecule, variants in (spec.get("molecules") or {}).items():
            names = variants or [molecule]
            jobs.append((molecule, q_molecule(names)))
            label_jobs[molecule] = label_query(variants=names)

    out_path = _RESULTS_DIR / "scored_pairs.parquet"

    # The daily quota is 1,000 calls and a full run can approach it, so finished
    # drugs are kept and skipped on the next run rather than recomputed.
    done: set[str] = set()
    frames: list = []
    if out_path.exists() and "--rescore" not in sys.argv:
        try:
            prev = pd.read_parquet(out_path)
            done = set(prev["drug"].unique())
            frames.append(prev)
            print(f"[run] resuming: {len(done)} drug(s) already scored "
                  f"(pass --rescore to rebuild)")
        except Exception:
            done, frames = set(), []

    pending = [(lbl, s) for lbl, s in jobs if lbl not in done]
    print(f"[run] scoring {len(pending)} of {len(jobs)} drugs\n")

    for i, (label, search) in enumerate(pending, 1):
        try:
            # 60 rather than 100: each novel reaction costs a background lookup
            # against a 1,000/day quota, and the interface surfaces ~25.
            df = score_via_counts(search, label, max_terms=60,
                                  stoplist=stoplist, dme_terms=dme)
        except Exception as exc:
            print(f"\n[run] stopped at {label}: {redact(str(exc))}")
            print("[run] progress is saved; re-run --score to continue.")
            break
        n_dme = int(df["dme"].sum()) if not df.empty and "dme" in df else 0
        n_str = int((df["tier"] == "strong").sum()) if not df.empty else 0
        print(f"  [{i:>3}/{len(pending)}] {label:<20} {len(df):>4} pairs  "
              f"{n_str:>3} strong  {n_dme:>3} serious-event")
        if not df.empty:
            frames.append(df)

    if not frames:
        print("[run] No pairs scored. Check the search strings.")
        sys.exit(1)

    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["drug", "reaction_pt"], keep="last")
    out = out.sort_values(["drug", "IC025"], ascending=[True, False]).reset_index(drop=True)

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)

    # Expectedness first: it is one call per drug and independent of FAERS,
    # so it is the cheapest and most interpretive thing we can attach.
    print("\n[run] Checking each reaction against the official product label...")
    from src.labels import attach_expectedness
    try:
        out = attach_expectedness(out, label_jobs)
        known = int(out["labelled"].fillna(False).sum())
        boxed = int(out["boxed_warning"].fillna(False).sum())
        nolabel = int((~out["label_found"].fillna(False)).sum())
        print(f"  {known:,} pairs already in the label, {boxed:,} in a boxed "
              f"warning, {nolabel:,} rows where no label was found")
    except Exception as exc:
        print(f"[run] label check stopped early: {redact(str(exc))}")
    out.to_parquet(out_path, index=False)

    print("\n[run] Attaching bias diagnostics to top pairs...")
    from src.bias import attach_bias_flags
    try:
        # Every row, not the top 25. An undiagnosed row is styled almost like a
        # diagnosed one, and indication confounding hides precisely there --
        # atorvastatin x TYPE 2 DIABETES at ROR 21.5 is the kind of row a
        # frightened reader misreads. Partial coverage is defensible for a
        # research tool and not for a public one.
        out = attach_bias_flags(out, dict(jobs), top_n=BIAS_TOP_N, checkpoint=out_path)
    except Exception as exc:
        print(f"[run] bias diagnostics stopped early: {redact(str(exc))}")
    n_ind = int(out["indication_overlap"].fillna(False).sum())
    n_burst = int(out["notoriety_spike"].fillna(False).sum())
    print(f"  flagged: {n_ind} indication-confounded, {n_burst} notoriety spikes")

    out.to_parquet(out_path, index=False)
    print(f"\n[run] {len(out):,} pairs across {out['drug'].nunique()} drugs "
          f"-> {out_path}")
    print(f"[run] Signal pairs: {int(out['signal'].sum()):,}")


def step_interactions() -> None:
    """M8: score curated drug pairs against an independence baseline."""
    import json
    import pandas as pd
    from src.client import q_class, q_molecule
    from src.normalise import _load_targets
    from src.score import _load_bg_cache
    from src.interactions import score_pair, summarise

    scored_path = _RESULTS_DIR / "scored_pairs.parquet"
    if not scored_path.exists():
        print("[run] No scored table. Run --score first.")
        sys.exit(1)
    scored = pd.read_parquet(scored_path)
    bg = _load_bg_cache()

    searches: dict[str, str] = {}
    for class_key, spec in _load_targets().items():
        epc = spec.get("pharm_class_epc")
        if epc:
            searches[class_key] = q_class(epc)
        for molecule, variants in (spec.get("molecules") or {}).items():
            searches[molecule] = q_molecule(variants or [molecule])

    cfg = json.loads(
        (_PROJECT_ROOT / "config" / "interaction_pairs.json").read_text(encoding="utf-8")
    )
    frames = []
    for spec in cfg["pairs"]:
        a, b, expect = spec["a"], spec["b"], spec["expect"]
        if a not in searches or b not in searches:
            print(f"  {a} + {b}: not in the drug list, skipped")
            continue
        df = score_pair(a, searches[a], b, searches[b], scored, bg, top_n=20)
        hit = df[df["reaction_pt"] == expect] if not df.empty else pd.DataFrame()
        note = ""
        if not hit.empty:
            r = hit.iloc[0]
            note = (f"  <- {expect}: n={r.n_triple:,} omega025={r.omega025:+.2f}"
                    f" {'SIGNAL' if r.interaction else 'below threshold'}")
        elif not df.empty:
            note = f"  <- {expect} not among the top triples"
        print(f"  {a} + {b}: {len(df)} triple(s){note}")
        if not df.empty:
            frames.append(df)

    if not frames:
        print("\n[run] " + summarise(pd.DataFrame()))
        return
    out = pd.concat(frames, ignore_index=True)
    out_path = _RESULTS_DIR / "interactions.parquet"
    out.to_parquet(out_path, index=False)
    print(f"\n[run] {len(out):,} triples -> {out_path}")
    print("[run] " + summarise(out))


def step_validate() -> None:
    """M6: Run positive/negative control validation harness."""
    import pandas as pd
    from src.validate import run_controls

    # Load scored results if available
    scored_path = _RESULTS_DIR / "scored_pairs.parquet"
    scored_df = None
    if scored_path.exists():
        try:
            scored_df = pd.read_parquet(scored_path)
        except Exception:
            pass

    result = run_controls(scored_df=scored_df)
    if result["passed"]:
        print("\n[validate] ALL CORE CONTROLS PASSED")
    else:
        print(f"\n[validate] SOME CONTROLS FAILED ({result['n_pass']}/{result['n_total']})")
        sys.exit(1)


def step_serve() -> None:
    """M9: Start the Flask application."""
    from src.app import app
    print("[run] Starting reportscope Flask app on http://127.0.0.1:5000")
    print("[run] Press Ctrl+C to stop.")
    app.run(debug=False, host="127.0.0.1", port=5000)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="reportscope -- FAERS adverse event signal detection pipeline"
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch and cache raw FAERS data (M2)",
    )
    parser.add_argument(
        "--score",
        action="store_true",
        help="Flatten, normalise, and score pairs (M3+M4+M5)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run validation harness (M6)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start Flask app (M9)",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Rebuild every drug instead of resuming from the saved table",
    )
    parser.add_argument(
        "--interactions",
        action="store_true",
        help="Score curated drug pairs for interaction signals (M8)",
    )
    args = parser.parse_args()

    run_all = not (args.fetch or args.score or args.validate
                   or args.serve or args.interactions)

    if args.interactions:
        step_interactions()

    if args.fetch or run_all:
        step_fetch()

    if args.score or run_all:
        step_score()

    if args.validate or run_all:
        step_validate()

    if args.serve or run_all:
        step_serve()


if __name__ == "__main__":
    main()
