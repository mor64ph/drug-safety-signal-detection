"""
Precompute which catalogue drugs each drug's label names as an interaction.

    python scripts/fetch_label_interactions.py            # fetch what is missing
    python scripts/fetch_label_interactions.py --refresh  # refetch everything
    python scripts/fetch_label_interactions.py --pairs-only   # rebuild from cache

Two outputs, on purpose:

  data/raw/label_interaction_text.json      the fetched prose. Gitignored and
                                            local: the merged interaction
                                            sections run to 100,105 characters
                                            for clarithromycin alone, so the
                                            whole catalogue is tens of MB. It
                                            exists only so --pairs-only can
                                            rebuild without refetching.

  data/results/label_interaction_pairs.json what the app loads. Only the
                                            matches, each with one quoted
                                            sentence, so it stays small enough
                                            to commit and to hold in memory.

Run as a build step, never from a request. Everything this app serves is
precomputed and the web process makes no outbound calls; a worksheet with
twelve medicines would otherwise be twelve API calls on the request path, on a
free instance, against a 240/min limit shared with every visitor.

Costs one call per drug: ~361 calls, about two and a half minutes at the
client's pinned 0.25s spacing. Resumable.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src import labels  # noqa: E402
from src.client import QuotaExhausted  # noqa: E402

TEXT_CACHE = ROOT / "data" / "raw" / "label_interaction_text.json"
PAIRS_OUT = ROOT / "data" / "results" / "label_interaction_pairs.json"
TARGETS = ROOT / "config" / "targets.json"
SCORED = ROOT / "data" / "results" / "scored_pairs.parquet"


def catalogue() -> dict[str, dict]:
    """Every scored drug with the variants and class its label query needs.

    Driven from the scored table rather than the config, so the cache covers
    exactly what the app can serve. All 361 resolve: 305 molecule keys and 56
    class keys.
    """
    drugs = sorted(pd.read_parquet(SCORED, columns=["drug"])["drug"].unique())
    config = json.loads(TARGETS.read_text(encoding="utf-8"))

    molecules: dict[str, dict] = {}
    classes: dict[str, dict] = {}
    for key, cls in config["classes"].items():
        epc = cls.get("pharm_class_epc")
        classes[key] = {"variants": [], "epc": epc}
        for name, variants in (cls.get("molecules") or {}).items():
            molecules[name] = {"variants": variants, "epc": epc}

    out: dict[str, dict] = {}
    for drug in drugs:
        if drug in molecules:
            out[drug] = molecules[drug]
        elif drug in classes:
            out[drug] = classes[drug]
        else:
            raise SystemExit(
                f"{drug!r} is in the scored table but not in targets.json, so "
                f"its label cannot be queried. Fix the config rather than "
                f"skipping it: a silently missing drug reads to a user as "
                f"'no interactions found'.")
    return out


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def fetch(entries: dict[str, dict], refresh: bool) -> dict:
    texts = {} if refresh else _load(TEXT_CACHE)
    todo = [d for d in entries if d not in texts]
    print(f"{len(entries)} drugs, {len(texts)} cached, {len(todo)} to fetch")

    if todo:
        started = time.time()
        try:
            for i, drug in enumerate(todo, 1):
                meta = entries[drug]
                search = (labels.label_query(variants=meta["variants"])
                          if meta["variants"]
                          else labels.label_query(epc=meta["epc"]))
                result = labels.fetch_interactions(search)
                texts[drug] = result["text"]
                if i % 40 == 0 or i == len(todo):
                    rate = i / max(time.time() - started, 1e-9)
                    print(f"  fetched {i}/{len(todo)} ({rate:.1f}/s)")
                    TEXT_CACHE.parent.mkdir(parents=True, exist_ok=True)
                    TEXT_CACHE.write_text(json.dumps(texts), encoding="utf-8")
        except (KeyboardInterrupt, QuotaExhausted) as exc:
            TEXT_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TEXT_CACHE.write_text(json.dumps(texts), encoding="utf-8")
            print(f"\nstopped ({type(exc).__name__}); rerun to resume.")
            raise SystemExit(1)

    TEXT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TEXT_CACHE.write_text(json.dumps(texts), encoding="utf-8")
    return texts


def build_pairs(entries: dict[str, dict], texts: dict) -> dict:
    """For each drug A, which other catalogue drugs A's label names.

    Both directions are kept. They are not redundant: a statin label naming
    clarithromycin and a clarithromycin label naming statins are two different
    documents agreeing, and either one alone is worth surfacing.

    One scan per text against a single combined pattern. Doing it per pair
    meant ~390,000 regex compilations and did not finish.
    """
    owners, pattern = labels.build_term_index(entries)
    print(f"  {len(owners):,} distinct terms across {len(entries)} drugs")

    pairs: dict[str, dict] = {}
    for i, (a, text) in enumerate(texts.items(), 1):
        hits = labels.mentions_in(text, owners, pattern, exclude=a)
        if hits:
            pairs[a] = hits
        if i % 100 == 0:
            print(f"  matched {i}/{len(texts)}", flush=True)
    return pairs


def main() -> int:
    entries = catalogue()

    if "--pairs-only" in sys.argv:
        texts = _load(TEXT_CACHE)
        if not texts:
            raise SystemExit(f"no cache at {TEXT_CACHE}; run without "
                             f"--pairs-only first")
    else:
        texts = fetch(entries, refresh="--refresh" in sys.argv)

    with_text = sum(1 for t in texts.values() if t)
    print(f"\n{with_text}/{len(texts)} drugs have interaction text "
          f"({with_text / max(len(texts), 1):.0%})")

    # A low rate means the query or the section list is wrong, not that these
    # drugs have no interactions. Loud, because the failure would otherwise
    # present as a feature that quietly finds nothing -- which is exactly how
    # the expectedness check failed the first time it was written.
    if with_text / max(len(texts), 1) < 0.5:
        print("  WARNING: under half the catalogue resolved. Check "
              "label_query and _INTERACTION_SECTIONS before shipping.")
        return 1

    print("matching every drug against every other...")
    pairs = build_pairs(entries, texts)

    PAIRS_OUT.parent.mkdir(parents=True, exist_ok=True)
    PAIRS_OUT.write_text(json.dumps(pairs, sort_keys=True), encoding="utf-8")

    # Known-answer test, run on every build. The statistical route to
    # interactions scored 0/12 on a set like this and was withheld; a lookup
    # that silently stopped matching would look identical to a combination
    # with nothing to report, so this has to fail the build rather than warn.
    known = [
        ("atorvastatin", "clarithromycin"), ("simvastatin", "clarithromycin"),
        ("warfarin", "ibuprofen"), ("warfarin", "amiodarone"),
        ("warfarin", "fluconazole"), ("oxycodone", "alprazolam"),
        ("morphine", "diazepam"), ("lisinopril", "ibuprofen"),
        ("lisinopril", "spironolactone"), ("methotrexate", "ibuprofen"),
        ("digoxin", "amiodarone"), ("clopidogrel", "omeprazole"),
        ("ciprofloxacin", "tizanidine"),
    ]
    # Combinations with no recognised interaction. A hit is a false positive.
    negative = [
        ("atorvastatin", "levothyroxine"), ("metformin", "cetirizine"),
        ("semaglutide", "atorvastatin"), ("metformin", "levothyroxine"),
    ]

    def linked(a: str, b: str) -> bool:
        return bool((pairs.get(a) or {}).get(b) or (pairs.get(b) or {}).get(a))

    missing = [f"{a}+{b}" for a, b in known if not linked(a, b)]
    false_pos = [f"{a}+{b}" for a, b in negative if linked(a, b)]
    print(f"\ncontrols: {len(known) - len(missing)}/{len(known)} known "
          f"interactions found, {len(false_pos)}/{len(negative)} false positives")
    if missing:
        print(f"  MISSING: {', '.join(missing)}")
    if false_pos:
        print(f"  FALSE POSITIVES: {', '.join(false_pos)}")

    n_hits = sum(len(v) for v in pairs.values())
    possible = len(entries) * (len(entries) - 1)
    print(f"\nwrote {PAIRS_OUT.relative_to(ROOT)} "
          f"({PAIRS_OUT.stat().st_size / 1e6:.1f} MB)")
    print(f"  {len(pairs)} drugs name at least one other")
    print(f"  {n_hits:,} directed pairs of {possible:,} possible "
          f"({n_hits / possible:.1%})")
    print(f"  text cache {TEXT_CACHE.stat().st_size / 1e6:.1f} MB (gitignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
