"""
M2: Date-partitioned fetcher with resume support.

Design:
  - probe(search) → total count (1 cheap call)
  - windows(search, start, end) → recursively bisect months > 24,000 reports
  - fetch_window(search, start, end) → paginate within window, cache each page
  - fetch_class(search_str, start, end) → full orchestration, yields records
  - Resumable: checks data/raw/ for existing cache before fetching

FAERS date format: YYYYMMDD (ISO 8601 without hyphens).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Generator, Iterator

from src.client import total, records, _HARD_SKIP_MAX, _LIMIT_MAX

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RAW_DIR = _PROJECT_ROOT / "data" / "raw"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(search: str, start: str, end: str, skip: int) -> str:
    """Deterministic filename for a cached page."""
    raw = f"{search}|{start}|{end}|{skip}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return h


def _cache_path(search: str, start: str, end: str, skip: int) -> Path:
    key = _cache_key(search, start, end, skip)
    return _RAW_DIR / f"{key}.json"


def _load_cached(path: Path) -> list[dict] | None:
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            path.unlink(missing_ok=True)
    return None


def _save_cache(path: Path, data: list[dict]) -> None:
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Date arithmetic helpers
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    """Parse YYYYMMDD string to date."""
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _fmt_date(d: date) -> str:
    """Format date to YYYYMMDD."""
    return d.strftime("%Y%m%d")


def _mid_date(start: str, end: str) -> str:
    """Return the midpoint date as YYYYMMDD."""
    d_start = _parse_date(start)
    d_end = _parse_date(end)
    mid = d_start + (d_end - d_start) / 2
    return _fmt_date(mid)


def _date_range_query(search: str, start: str, end: str) -> str:
    """Build a search string with date range filter."""
    return f"{search} AND receiptdate:[{start} TO {end}]"


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def probe(search: str) -> int:
    """Return total report count for a search string (1 cheap API call)."""
    return total(search)


def windows(
    search: str,
    start: str = "20130101",
    end: str = "20260101",
    _depth: int = 0,
) -> Generator[tuple[str, str], None, None]:
    """
    Generate non-overlapping (start, end) date windows such that each window
    contains <= 24,000 reports (safely under the 25,000 skip hard limit).

    Uses recursive bisection for dense windows.
    """
    max_depth = 20  # prevent infinite recursion on tiny intervals
    query = _date_range_query(search, start, end)
    n = probe(query)

    # Allow up to 24,000 (leave room for the skip=0 offset at 24,003)
    if n <= 24_000 or _depth >= max_depth:
        yield (start, end)
        return

    # Bisect
    mid = _mid_date(start, end)
    if mid == start or mid == end:
        # Dates too close to split further; yield as-is
        yield (start, end)
        return

    yield from windows(search, start, mid, _depth + 1)
    # end-exclusive: add 1 day to mid
    mid_plus1 = _fmt_date(_parse_date(mid) + timedelta(days=1))
    yield from windows(search, mid_plus1, end, _depth + 1)


def fetch_window(
    search: str,
    start: str,
    end: str,
) -> Iterator[dict]:
    """
    Paginate through all records in a date window.

    Caches each page to data/raw/.  On resume, skips already-cached pages.
    Stops when page is empty or skip would exceed _HARD_SKIP_MAX.
    """
    query = _date_range_query(search, start, end)
    skip = 0

    while skip <= _HARD_SKIP_MAX:
        cache = _cache_path(search, start, end, skip)
        page = _load_cached(cache)

        if page is None:
            page = records(query, limit=_LIMIT_MAX, skip=skip)
            _save_cache(cache, page)

        if not page:
            break

        yield from page

        if len(page) < _LIMIT_MAX:
            # Last page — no more records
            break

        skip += _LIMIT_MAX
        if skip > _HARD_SKIP_MAX:
            print(
                f"  [fetch] WARNING: window {start}-{end} hit skip limit "
                f"at skip={skip}. Some records may be missed. "
                "Consider narrowing the date range."
            )
            break


def fetch_class(
    search_str: str,
    start: str = "20130101",
    end: str = "20260101",
) -> Iterator[dict]:
    """
    Full orchestration: generate windows, paginate each, yield all records.

    Resumable: existing cache pages are used without re-fetching.

    Args:
        search_str: OpenFDA search query (without date filter; date is added here).
        start: Start date YYYYMMDD (default 2013-01-01, pre-GLP-1 era for baseline).
        end: End date YYYYMMDD (default 2026-01-01).

    Yields:
        Raw report dicts from the API.
    """
    print(f"[fetch] Probing total for: {search_str}")
    n_total = probe(search_str)
    print(f"[fetch] Total reports: {n_total:,}")

    fetched = 0
    for win_start, win_end in windows(search_str, start, end):
        win_query = _date_range_query(search_str, win_start, win_end)
        n_window = probe(win_query)
        print(f"  [fetch] Window {win_start}-{win_end}: {n_window:,} reports")

        for record in fetch_window(search_str, win_start, win_end):
            fetched += 1
            yield record

        if fetched % 5_000 == 0 and fetched > 0:
            print(f"  [fetch] Progress: {fetched:,} records yielded so far")

    print(f"[fetch] Done. Total records yielded: {fetched:,}")
