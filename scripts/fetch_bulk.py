"""
Download the small openFDA bulk datasets.

    python scripts/fetch_bulk.py                 # whatever is missing
    python scripts/fetch_bulk.py --refresh       # all of them again
    python scripts/fetch_bulk.py drugsfda ndc    # just these

Writes data/raw/bulk/<name>.json.zip. That directory is gitignored: these are
build inputs, and the committed artifacts are the small tables derived from
them.

URLs are read from https://api.fda.gov/download.json rather than written down
here. Partition counts change with every export -- drug/event is 1,767 files
today -- so a hardcoded URL is a time bomb that fails as a 404 months later.

Deliberately limited to the datasets that arrive as a single file. drug/event
is 114 GB across 1,767 partitions and drug/label is 1.8 GB; neither belongs on
a laptop, and nothing here needs FAERS at record level -- every figure the app
shows is built from the count endpoint by `run.py --score`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.client import _get_session  # noqa: E402

MANIFEST = "https://api.fda.gov/download.json"
OUT = ROOT / "data" / "raw" / "bulk"

# name -> (category, endpoint). The name is what the build scripts ask for.
DATASETS = {
    # approval_date: the drug-age correction that is otherwise impossible
    "drugsfda": ("drug", "drugsfda"),
    # reason_for_recall, classification: active recalls
    "enforcement": ("drug", "enforcement"),
    # shortage_reason, availability
    "shortages": ("drug", "shortages"),
    # marketing_end_date: withdrawn drugs, the cerivastatin blind spot
    "ndc": ("drug", "ndc"),
    # te_code: therapeutic equivalence
    "orangebook": ("drug", "orangebook"),
    # ingredient normalisation
    "unii": ("other", "unii"),
    # CAERS: adverse events for dietary supplements, which FAERS barely covers
    "food-event": ("food", "event"),
}

MAX_SINGLE_MB = 200


def main() -> int:
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")] or list(DATASETS)
    refresh = "--refresh" in sys.argv

    unknown = [w for w in wanted if w not in DATASETS]
    if unknown:
        raise SystemExit(f"unknown dataset(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(DATASETS)}")

    session = _get_session()
    print(f"reading {MANIFEST}")
    manifest = session.get(MANIFEST, timeout=60).json()["results"]

    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0

    for name in wanted:
        category, endpoint = DATASETS[name]
        meta = manifest.get(category, {}).get(endpoint)
        if not meta:
            print(f"  {name}: MISSING from the manifest ({category}/{endpoint})")
            failures += 1
            continue

        parts = meta.get("partitions") or []
        if len(parts) != 1:
            print(f"  {name}: {len(parts)} partitions -- this script only handles "
                  f"single-file datasets. Skipped.")
            failures += 1
            continue

        part = parts[0]
        size_mb = float(part.get("size_mb") or 0)
        if size_mb > MAX_SINGLE_MB:
            print(f"  {name}: {size_mb:,.0f} MB exceeds the {MAX_SINGLE_MB} MB "
                  f"guard. Skipped.")
            failures += 1
            continue

        dest = OUT / f"{name}.json.zip"
        if dest.exists() and not refresh:
            have = dest.stat().st_size / 1e6
            # The manifest rounds to 2dp, so a percentage alone is too tight on
            # a small file: shortages is 0.4456 MB against a declared 0.42,
            # which is 6% out and was reported as a mismatch on a download that
            # was perfectly fine. Whichever tolerance is larger.
            ok = abs(have - size_mb) <= max(0.05 * size_mb, 0.2)
            print(f"  {name:<12} cached {have:>7.1f} MB"
                  f"{'' if ok else f'  SIZE MISMATCH (manifest says {size_mb:.1f}) -- use --refresh'}")
            if not ok:
                failures += 1
            continue

        print(f"  {name:<12} downloading {size_mb:>7.1f} MB  "
              f"(export {meta.get('export_date', '?')})")
        with session.get(part["file"], timeout=600, stream=True) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(".part")
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            # Rename only on success, so an interrupted download never leaves a
            # truncated file that looks cached on the next run.
            tmp.replace(dest)
        print(f"  {name:<12} wrote   {dest.stat().st_size / 1e6:>7.1f} MB")

    print(f"\n{len(wanted) - failures}/{len(wanted)} ready in "
          f"{OUT.relative_to(ROOT)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
