"""
M1: TLS-safe OpenFDA API client.

Handles corporate proxy TLS interception by trusting the Windows CA store.
Rate limit: 240/min, 1000/day per IP without key.
Hard constraints:
  - limit max 999 (1000 triggers API_KEY_MISSING)
  - skip max 25,000 hard
  - Grand total: 20,692,690 reports (as of 2026-07-30)
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# Path for the CA bundle, written once per session
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CA_BUNDLE_PATH = _PROJECT_ROOT / "winca.pem"

BASE_URL = "https://api.fda.gov/drug/event.json"
_HARD_SKIP_MAX = 25_000

# An openFDA API key is free, issued instantly, and raises the daily ceiling
# from 1,000 requests to 120,000. Scoring 361 drugs costs roughly 4,000 calls,
# which is four days unauthenticated and about an hour with a key.
#   https://open.fda.gov/apis/authentication/
# Set OPENFDA_API_KEY (or RXSIGNAL_API_KEY) in the environment.
def _read_key() -> str:
    """
    Find the API key in the environment, or in a .env file beside run.py.

    The file is the easier route on Windows: setx does not affect the current
    shell, so a key set that way appears to be ignored until the terminal is
    restarted.
    """
    for var in ("OPENFDA_API_KEY", "RXSIGNAL_API_KEY"):
        if os.environ.get(var):
            return os.environ[var].strip()

    env_file = _PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() in ("OPENFDA_API_KEY", "RXSIGNAL_API_KEY"):
                return value.strip().strip('"').strip("'")
    return ""


API_KEY = _read_key()

# Without a key limit is capped at 999: 1000 returns API_KEY_MISSING, which is
# undocumented. With a key the documented maximum applies.
_LIMIT_MAX = 1000 if API_KEY else 999
_COUNT_MAX = 1000 if API_KEY else 100

# 240 requests/minute applies to BOTH tiers -- a key raises the daily ceiling,
# not the per-minute one. 0.25s is the floor that respects it. An earlier 0.1s
# for keyed requests implied 600/min and earned a 429 partway through a long run.
_MIN_SLEEP = 0.25

# Field paths MUST be fully qualified from the report root. The unqualified
# forms ("openfda.pharm_class_epc", "reactionmeddrapt") are accepted by the
# API and match nothing, returning 0 rather than an error -- verified against
# every casing. Build queries from these constants, never by hand.
F_PHARM_CLASS = "patient.drug.openfda.pharm_class_epc"
F_GENERIC_NAME = "patient.drug.openfda.generic_name"
F_BRAND_NAME = "patient.drug.openfda.brand_name"
F_MEDICINALPRODUCT = "patient.drug.medicinalproduct"
F_INDICATION = "patient.drug.drugindication"
F_REACTION = "patient.reaction.reactionmeddrapt"


def q_class(epc: str) -> str:
    """
    Search clause for a pharmacologic class (EPC) string.

    .exact is required, not optional. A plain phrase match also matches any
    longer class name that contains the phrase, silently merging distinct
    pharmacology: "Opioid Agonist [EPC]" picks up 354,618 reports rather than
    268,375 because "Partial Opioid Agonist [EPC]" contains it, folding
    buprenorphine in with morphine. Same for Calcium Channel Blocker, which
    absorbs the dihydropyridine subclass.
    """
    return f'{F_PHARM_CLASS}.exact:"{epc}"'


def q_reaction(pt: str) -> str:
    """
    Search clause for a MedDRA Preferred Term. .exact is case-insensitive here.

    Possessive terms are a special case. The count endpoint returns them with a
    caret where the apostrophe belongs -- FOURNIER^S GANGRENE -- and that string
    cannot be queried back: raw, backslash-escaped and wildcard forms all return
    HTTP 400, and substituting a real apostrophe matches nothing. Replacing the
    caret with a space and dropping .exact does work, so possessives fall back to
    that. MedDRA is full of them (Crohn's, Parkinson's, Raynaud's), and dropping
    them would discard real signals: 57% of all Fournier's gangrene reports in
    FAERS name an SGLT2 inhibitor.
    """
    term = pt.upper()
    if "^" in term:
        return f'{F_REACTION}:"{term.replace("^", " ")}"'
    return f'{F_REACTION}.exact:"{term}"'


F_SUBSTANCE = "patient.drug.activesubstance.activesubstancename"


def q_molecule(variants: list[str]) -> str:
    """
    Search clause matching a molecule across every field that can name it.

    generic_name alone is not enough. openFDA derives it by matching reports
    against CURRENT product labels, so a drug with no current label is invisible
    there: cerivastatin returns 0 under generic_name but 200 under
    medicinalproduct and 91 under activesubstance. Withdrawn drugs are exactly
    the ones worth studying, so all three fields are searched.
    """
    clauses = []
    for v in variants:
        v = v.strip()
        if not v:
            continue
        clauses.append(f'{F_GENERIC_NAME}:"{v}"')
        clauses.append(f'{F_SUBSTANCE}:"{v}"')
        clauses.append(f'{F_MEDICINALPRODUCT}:"{v}"')
    return "(" + " OR ".join(clauses) + ")"


# ---------------------------------------------------------------------------
# TLS helpers
# ---------------------------------------------------------------------------

def _write_windows_ca_bundle(dest: Path) -> Path:
    """Extract Windows ROOT + CA store certs and write a PEM bundle."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    pem: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            for der, _enc, _trust in ssl.enum_certificates(store):
                try:
                    pem.append(ssl.DER_cert_to_PEM_cert(der))
                except Exception:
                    pass
        except Exception:
            pass
    dest.write_text("".join(pem), encoding="utf-8")
    return dest


class _RelaxedStrictAdapter(HTTPAdapter):
    """HTTP adapter that uses the Windows CA bundle and relaxes X509_STRICT."""

    def __init__(self, cafile: Path, **kw: Any) -> None:
        self._cafile = str(cafile)
        super().__init__(**kw)

    def _ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self._cafile)
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **kw: Any
    ) -> None:
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_context=self._ctx(),
            **kw,
        )


def _build_session() -> requests.Session:
    """
    A session that trusts the right certificates on whichever host this runs on.

    The Windows path exists solely to survive a TLS-intercepting corporate
    proxy: it rebuilds the trust store from the OS so the proxy's root is
    included, and clears VERIFY_X509_STRICT because that root is not strictly
    conformant. Verification stays on throughout.

    None of that applies off Windows, and applying it anyway is actively
    harmful: ssl.enum_certificates does not exist on Linux, the failure is
    swallowed, and the result is an EMPTY ca bundle that trusts nothing. Every
    request then fails with a certificate error that says nothing about the
    cause. Containers use the system trust store instead.
    """
    session = requests.Session()
    if sys.platform != "win32":
        return session

    cafile = _write_windows_ca_bundle(_CA_BUNDLE_PATH)
    if not cafile.exists() or cafile.stat().st_size == 0:
        # Trust-store export produced nothing. The default bundle is a better
        # bet than a file guaranteed to reject every certificate.
        return session

    adapter = _RelaxedStrictAdapter(cafile=cafile)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Singleton session (one per import)
_session: requests.Session | None = None
_last_call_time: float = 0.0


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _build_session()
    return _session


# ---------------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------------

def call(params: dict[str, Any], retries: int = 5) -> dict[str, Any]:
    """
    Make a single GET to the FDA drug/event endpoint.

    Rate-limits to _MIN_SLEEP between calls and does exponential back-off
    on 429/5xx.  Returns the parsed JSON dict.

    Raises requests.HTTPError for unrecoverable errors.
    """
    global _last_call_time

    session = _get_session()

    if API_KEY and "api_key" not in params:
        params = {**params, "api_key": API_KEY}

    elapsed = time.time() - _last_call_time
    if elapsed < _MIN_SLEEP:
        time.sleep(_MIN_SLEEP - elapsed)

    delay = 1.0
    for attempt in range(retries):
        try:
            _last_call_time = time.time()
            response = session.get(BASE_URL, params=params, timeout=30)
            if response.status_code == 404:
                # Legitimate empty result — not an error
                return {"meta": {"results": {"total": 0}}, "results": []}
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            # requests builds the HTTPError message from the full request URL,
            # and the key is a query parameter -- so letting this propagate
            # prints the secret into whatever catches it. redact() existed for
            # this and claimed to cover every path, but nothing was applying it
            # here: an HTTP 500 from the count endpoint put the live key into a
            # terminal transcript.
            #
            # `from None` rather than `from exc`, deliberately. Chaining would
            # attach the original exception, and Python prints the chained
            # message above the new one -- so the redaction would be defeated
            # by the traceback that reports it.
            raise RuntimeError(redact(str(exc))) from None
        except requests.exceptions.ConnectionError as exc:
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            raise RuntimeError(redact(
                f"Connection failed after {retries} attempts: {exc}")) from None

    raise RuntimeError("call() exhausted all retries")


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

class QuotaExhausted(RuntimeError):
    """Raised when failures stop looking like bad queries and start looking systemic."""


def redact(text: str) -> str:
    """
    Strip the API key from anything that might be printed or logged.

    requests puts the full request URL into HTTPError messages, and the key is
    a query parameter, so an unhandled error prints the secret into the log --
    which is how a key ends up committed, pasted into a bug report, or sitting
    in a CI transcript.

    This used to claim that every path surfacing an exception went through
    here. It did not: call() let requests' own HTTPError propagate untouched,
    so an HTTP 500 printed the live key. Both raise paths in call() now redact,
    and neither chains the original exception -- see the comment there.
    """
    if API_KEY and text:
        text = text.replace(API_KEY, "***REDACTED***")
    return re.sub(r"api_key=[^&\s]+", "api_key=***REDACTED***", text or "")


_consecutive_failures = 0
_FAILURE_LIMIT = 8


def total(search: str) -> int:
    """
    Return the total report count for a search string.

    A malformed query returns 0 rather than raising. Terms come back from the
    count endpoint in forms the search endpoint will not accept, and in a run
    spanning a hundred drugs one such term should not destroy the batch. Callers
    treat a zero background as "cannot score this pair" and skip it, which is the
    honest outcome -- better than a ratio computed against a background we failed
    to measure.

    Swallowing failures is only safe while they are isolated. Once the daily
    quota is gone EVERY call fails, and silently returning 0 would empty every
    table while reporting success. A run of consecutive failures therefore
    raises instead, so the run stops with its progress saved.
    """
    global _consecutive_failures
    params = {"search": search, "limit": 1}
    try:
        data = call(params)
    except Exception as exc:
        _consecutive_failures += 1
        if _consecutive_failures >= _FAILURE_LIMIT:
            raise QuotaExhausted(
                f"{_consecutive_failures} consecutive request failures - "
                f"daily quota or connectivity, not a bad query. Last error: {exc}"
            ) from exc
        return 0
    _consecutive_failures = 0
    try:
        return int(data["meta"]["results"]["total"])
    except (KeyError, TypeError, ValueError):
        return 0


# The count endpoint returns 100 buckets without a key and 1,000 with one.
_COUNT_MAX = 1000 if API_KEY else 100


def counts(search: str, field: str, limit: int = 100) -> list[dict[str, Any]]:
    """
    Return count buckets for a field (count endpoint).

    The ceiling is 100 buckets anonymously and 1,000 with a key. This used to
    clamp to 100 unconditionally, with a docstring correctly stating the limit
    applied "without API key" -- so once a key was configured, every caller
    asking for more was silently truncated and had no way to tell.

    That cost a 75-minute rebuild. The severity fetch asked for 1,000 buckets
    per chunk, got 100, and stored 0 for every term below the cut-off: five of
    twelve terms in one measured chunk, including FOURNIER^S GANGRENE, which
    returns 255 at limit=1000 and nothing at limit=100. A zero that means "not
    in the response" is indistinguishable from a zero that means "never
    reported", which is the worst possible failure for a severity figure.
    """
    params = {"search": search, "count": field,
              "limit": min(limit, _COUNT_MAX)}
    data = call(params)
    return data.get("results", [])


def records(search: str, limit: int = _LIMIT_MAX, skip: int = 0) -> list[dict[str, Any]]:
    """
    Return a page of raw report records.

    limit: capped at _LIMIT_MAX=999 (1000 triggers API_KEY_MISSING).
    skip: must be <= _HARD_SKIP_MAX.
    Returns empty list on 404 (legitimate empty result).
    """
    limit = min(limit, _LIMIT_MAX)
    if skip > _HARD_SKIP_MAX:
        raise ValueError(
            f"skip={skip} exceeds hard limit of {_HARD_SKIP_MAX}. "
            "Use date partitioning in fetch.py instead."
        )
    params = {
        "search": search,
        "limit": limit,
        "skip": skip,
    }
    data = call(params)
    return data.get("results", [])
