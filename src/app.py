"""
M9: Flask lookup tool.

Routes:
  GET /             → search form
  GET /search       → HTML results page (query param: drug)
  GET /api/search   → JSON API (query param: drug)
  GET /reaction     → drugs reported with one reaction (query param: q)
  GET /api/reaction → JSON API (query param: q)
  GET /compare      → two drugs side by side (query params: a, b)

Works from precomputed results in data/results/ — no live API calls on request path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import pandas as pd
from flask import Flask, g, jsonify, render_template, request
from markupsafe import Markup, escape

from src.models import _setting as _read_setting

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RESULTS_DIR = _PROJECT_ROOT / "data" / "results"

app = Flask(
    __name__,
    template_folder=str(_PROJECT_ROOT / "templates"),
    static_folder=str(_PROJECT_ROOT / "static"),
)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

# Forwarded headers are handled by the server, not here. Waitress is told which
# proxy to trust in serve.py and rewrites wsgi.url_scheme and REMOTE_ADDR
# itself, so a ProxyFix wrapper would process the same headers a second time
# and could take the wrong hop when counting X-Forwarded-For.


def _setting(name: str, default: str = "") -> str:
    """
    Read configuration from the environment, falling back to .env.

    Hosts supply real environment variables; locally the same values sit in the
    .env file beside run.py, so one file configures both the pipeline and the
    server rather than the API key living in one place and the access code in
    another.

    The body moved to src.models so that Alembic can resolve DATABASE_URL
    without importing this module, which would build the application -- and its
    tables -- before the migration ran. This name and behaviour are unchanged.
    """
    return _read_setting(name, default)


# Gate for a review deployment. Two ways to configure it:
#
#   RXSIGNAL_ACCESS_CODE_HASH   sha256 of the code -- preferred
#   RXSIGNAL_ACCESS_CODE        the code itself -- simpler, less safe
#
# The hash form means the working code never appears in .env, in the container
# image, or in a host's settings page. Anyone who reads the config learns
# nothing usable, because sha256 cannot be run backwards. Generate both halves
# with `python scripts/make_access_code.py`.
ACCESS_CODE = _setting("RXSIGNAL_ACCESS_CODE")
ACCESS_HASH = _setting("RXSIGNAL_ACCESS_CODE_HASH").lower()
GATE_ENABLED = bool(ACCESS_CODE or ACCESS_HASH)


def _code_ok(supplied: str) -> bool:
    """Constant-time check, so timing cannot be used to recover the code."""
    if not supplied:
        return False
    if ACCESS_HASH:
        digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, ACCESS_HASH)
    return hmac.compare_digest(supplied, ACCESS_CODE)


# Signed-session key. The random fallback keeps a fresh checkout working, at the
# cost of invalidating every session on restart -- which is why serve.py refuses
# to start an ungated deployment without a configured one. Generate it with
# `python scripts/make_secret_key.py`.
SECRET_KEY = _setting("RXSIGNAL_SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    app.logger.warning(
        "RXSIGNAL_SECRET_KEY is not set. Using a random key: every restart will "
        "sign all users out and invalidate pending password resets. Run "
        "scripts/make_secret_key.py and put the line in .env."
    )
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Off by default so local HTTP works; switch it on wherever TLS terminates.
app.config["SESSION_COOKIE_SECURE"] = _setting("RXSIGNAL_COOKIE_SECURE", "").lower() in (
    "1", "true", "yes", "on",
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

# Drug names are letters, digits, spaces and a few separators. Anything else is
# either a mistake or an attempt at something; either way it should not reach
# the lookup.
_SAFE_QUERY = re.compile(r"^[A-Za-z0-9 ,.\-/+']{1,64}$")

_RATE_LIMIT = int(_setting("RXSIGNAL_RATE_LIMIT", "60"))  # requests/min/IP

# Public and findable by default. Set RXSIGNAL_ALLOW_INDEXING=0 to withdraw the
# whole site from search; individual paths in _NEVER_INDEX are excluded either
# way.
INDEXING_ALLOWED = _setting("RXSIGNAL_ALLOW_INDEXING", "1").lower() not in (
    "0", "false", "no", "off",
)
_hits: dict[str, deque] = defaultdict(deque)


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


@app.before_request
def _guard():
    """Rate limit, and gate the whole site when an access code is configured."""
    if request.path.startswith("/static/") or request.path == "/healthz":
        return None

    now = time.time()
    q = _hits[_client_ip()]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= _RATE_LIMIT:
        return jsonify({"error": "rate limit exceeded, try again shortly"}), 429
    q.append(now)

    if GATE_ENABLED:
        from_query = (request.args.get("code") or "").strip()
        supplied = from_query or request.cookies.get("reportscope_code", "")
        if not _code_ok(supplied):
            return render_template("gate.html"), 401
        # Remember it, so reviewers are not re-entering a code on every page.
        g.set_access_cookie = bool(from_query)
        g.access_value = supplied
    return None


@app.after_request
def _headers(resp):
    """
    Security headers. The CSP allows the Bootstrap CDN and inline styles the
    templates rely on, and nothing else.
    """
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data:; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    # robots.txt is advisory and read only at the site root. This header is
    # honoured per-response, so it is what actually keeps a token-bearing URL
    # out of an index even if a crawler ignores the file or follows a link
    # straight to it.
    if not _indexable(request.path):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if getattr(g, "set_access_cookie", False):
        resp.set_cookie("reportscope_code", getattr(g, "access_value", ""),
                        max_age=86400, httponly=True, samesite="Lax",
                        secure=request.is_secure)
    return resp


@app.errorhandler(404)
def _not_found(_e):
    return render_template("error.html", code=404,
                           message="That page does not exist."), 404


@app.errorhandler(429)
def _too_many(_e):
    return render_template("error.html", code=429,
                           message="Too many requests. Please wait a moment."), 429


@app.errorhandler(Exception)
def _server_error(e):
    """Never surface a stack trace: it leaks paths and library versions."""
    app.logger.exception("unhandled error: %s", e)
    return render_template(
        "error.html", code=500,
        message="Something went wrong on our side. Nothing is wrong with your search."
    ), 500


# Paths that must never be indexed, whatever the site-wide setting.
#
# Not a judgement about the content: these pages either carry a single-use
# token, describe one account, or exist only to be submitted. A reset link in a
# search index is a live credential, and an indexed account page is a privacy
# failure that no disclaimer covers.
_NEVER_INDEX = (
    "/register", "/login", "/logout", "/forgot", "/reset", "/verify",
    "/account", "/my", "/admin", "/api/", "/healthz", "/worksheet",
)


def _indexable(path: str) -> bool:
    """Whether this particular path may be indexed."""
    if not INDEXING_ALLOWED:
        return False
    return not any(path.startswith(p) for p in _NEVER_INDEX)


@app.route("/robots.txt")
def robots():
    """
    Crawl the reference pages; leave the rest alone.

    The drug, reaction and comparison pages are the reference material and are
    the reason to be findable at all. Everything in _NEVER_INDEX is excluded
    regardless: auth flows carry single-use tokens, account pages describe one
    person, and /worksheet only exists to receive a POST, so an indexed copy
    would be an empty form ranking against symptom queries.

    Set RXSIGNAL_ALLOW_INDEXING=0 to withdraw the whole site again.
    """
    if not INDEXING_ALLOWED:
        body = "User-agent: *\nDisallow: /\n"
    else:
        lines = ["User-agent: *"]
        lines += [f"Disallow: {p}" for p in _NEVER_INDEX]
        lines += [
            "Allow: /",
            "",
            # Crawlers enumerate query strings aggressively; 361 drugs times
            # thousands of reactions is a large crawl of near-duplicate pages.
            "Crawl-delay: 2",
            "",
            f"Sitemap: {_setting('APP_BASE_URL', '').rstrip('/')}/sitemap.xml",
        ]
        body = "\n".join(lines) + "\n"
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/sitemap.xml")
def sitemap():
    """
    The drug pages, so crawlers find the reference material without guessing.

    Only drugs are listed. Reaction pages are derivable from the same data and
    listing both would submit the same 33,852 pairs twice under different URLs,
    which reads as duplicate content.
    """
    base = _setting("APP_BASE_URL", "").rstrip("/") or request.url_root.rstrip("/")
    df = _load_results()
    drugs = sorted(df["drug"].dropna().unique()) if not df.empty else []

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
             f"<url><loc>{escape(base)}/</loc><priority>1.0</priority></url>"]
    for d in drugs:
        loc = f"{base}/search?drug={urllib.parse.quote(str(d))}"
        parts.append(f"<url><loc>{escape(loc)}</loc><priority>0.6</priority></url>")
    parts.append("</urlset>")
    return "\n".join(parts), 200, {"Content-Type": "application/xml; charset=utf-8"}


@app.route("/healthz")
def healthz():
    """Liveness probe for the host. Reports data presence, not contents."""
    df = _load_results()
    return jsonify({
        "status": "ok" if not df.empty else "no-data",
        "drugs": int(df["drug"].nunique()) if not df.empty else 0,
        "pairs": int(len(df)),
    })

# ---------------------------------------------------------------------------
# Data loading (lazy, cached at module level after first load)
# ---------------------------------------------------------------------------

_results_cache: pd.DataFrame | None = None


def _load_results() -> pd.DataFrame:
    """Load the precomputed scored results parquet, or empty DataFrame."""
    global _results_cache
    if _results_cache is not None:
        return _results_cache

    parquet_path = _RESULTS_DIR / "scored_pairs.parquet"
    if parquet_path.exists():
        try:
            _results_cache = pd.read_parquet(parquet_path)
            # Ensure reaction_pt is uppercase for consistent matching
            if "reaction_pt" in _results_cache.columns:
                _results_cache["reaction_pt"] = (
                    _results_cache["reaction_pt"].fillna("").str.upper()
                )
        except Exception as exc:
            print(f"[app] WARNING: Could not load {parquet_path}: {exc}")
            _results_cache = pd.DataFrame()
    else:
        _results_cache = pd.DataFrame()

    return _results_cache


_alias_cache: dict[str, str] | None = None


def _aliases() -> dict[str, str]:
    """
    Map every brand and spelling variant to the label used in the results table.

    People search for the name on the box. Ozempic, Wegovy and Rybelsus are all
    semaglutide; Lipitor is atorvastatin. Without this the tool answers "no
    results" for the terms most users will actually type.
    """
    global _alias_cache
    if _alias_cache is not None:
        return _alias_cache

    _alias_cache = {}
    path = _PROJECT_ROOT / "config" / "targets.json"
    if path.exists():
        try:
            targets = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            targets = {}
        for class_key, spec in (targets.get("classes") or {}).items():
            _alias_cache[class_key.lower()] = class_key
            for molecule, variants in (spec.get("molecules") or {}).items():
                _alias_cache[molecule.lower()] = molecule
                for v in variants or []:
                    _alias_cache[v.strip().lower()] = molecule
    return _alias_cache


def _search_results(drug_query: str) -> tuple[pd.DataFrame, str, int]:
    """
    Search precomputed results for a drug name.

    Returns (filtered_df, matched_drug_name, total_reports_a).
    """
    df = _load_results()
    if df.empty or not drug_query.strip():
        return pd.DataFrame(), drug_query, 0

    query_lower = drug_query.strip().lower()

    if "drug" not in df.columns:
        return pd.DataFrame(), drug_query, 0

    # Resolve brand names before matching, so "ozempic" finds semaglutide.
    query_lower = _aliases().get(query_lower, query_lower).lower()

    # Try exact match first, then substring
    mask_exact = df["drug"].str.lower() == query_lower
    if mask_exact.any():
        filtered = df[mask_exact].copy()
        matched_name = df[mask_exact]["drug"].iloc[0]
    else:
        # regex=False is load-bearing, not a micro-optimisation. str.contains
        # defaults to regex=True, which compiled the query as a pattern, and
        # _SAFE_QUERY admits '.', '+' and 64 characters -- everything needed for
        # catastrophic backtracking. Measured on this table: ".+" x 10 + "zz",
        # 22 characters, took 31.3s, and cost doubles roughly every 4 more.
        # Python's re holds the GIL for the whole scan, so one request blocked
        # all eight Waitress threads including /healthz, and /list multiplies it
        # by MAX_LIST_DRUGS. This is a substring search and never needed regex.
        mask_sub = df["drug"].str.lower().str.contains(
            query_lower, regex=False, na=False)
        if not mask_sub.any():
            return pd.DataFrame(), drug_query, 0
        filtered = df[mask_sub].copy()
        matched_name = df[mask_sub]["drug"].iloc[0]

    # a + b is the drug's report total and is identical on every row for that
    # drug. Summing column a instead would count a report once per reaction it
    # lists, inflating the figure several-fold.
    total_reports = 0
    if {"a", "b"}.issubset(filtered.columns) and not filtered.empty:
        first = filtered.iloc[0]
        total_reports = int(first["a"]) + int(first["b"])

    if "IC025" in filtered.columns:
        filtered = filtered.sort_values("IC025", ascending=False, na_position="last")

    return filtered, str(matched_name), total_reports


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# The underlying snapshot. FAERS publishes quarterly, so this ages: a figure
# from a stale extract looks exactly as current as a fresh one, which is why the
# date is shown in the interface rather than buried in a footer.
DATA_AS_OF = "30 July 2026"
DATA_TOTAL_REPORTS = 20_692_690


# One source for every column explanation, so the hover text on a header and the
# "How to read this table" panel cannot drift apart, and so the three tables that
# show these columns describe them identically.
COLUMN_HELP = {
    "reaction": (
        "The MedDRA preferred term recorded on the report. One clinical concept "
        "can appear under several terms, so a drug's reports are spread across "
        "them rather than pooled."
    ),
    "drug": (
        "The active ingredient. Brands sharing an ingredient are pooled, so "
        "products aimed at different conditions are counted together."
    ),
    "a": (
        "How many FAERS reports name both the drug and the reaction. Always read "
        "it with the ratio: a ratio of 300 built on 9 reports is far weaker than "
        "a ratio of 13 built on 8,820."
    ),
    "ror": (
        "How much more often this reaction appears in reports about this drug "
        "than in reports about everything else. It is a ratio of reports, not a "
        "measure of risk, and it cannot be turned into one. The bracketed range "
        "is the 95% confidence interval."
    ),
    "ic025": (
        "A conservative version of the same idea that deliberately penalises "
        "small counts. The list is ranked by it."
    ),
    "strength": (
        "A band on IC025 — above 2 strong, above 1 moderate, above 0 weak. It "
        "grades how disproportionate the reporting is. It is not a judgement "
        "about the drug."
    ),
    "comparator": (
        "The same calculation against clinically comparable drugs instead of the "
        "whole database. A large drop means most of the original figure was the "
        "illness, the route of administration or the reporting environment "
        "rather than the molecule. Statins and rhabdomyolysis falls from 12.96 "
        "to 5.24 and stays real; cerivastatin and motor neurone disease falls "
        "from 394 to 2.15 and collapses."
    ),
    "trend": (
        "When the reports arrived — one point per month, or per quarter once the "
        "history is long. Steady accrual tracks prescribing. A single tower is a "
        "burst of reporting."
    ),
}


@app.context_processor
def _inject_data_vintage():
    return {"data_as_of": DATA_AS_OF,
            "data_total": f"{DATA_TOTAL_REPORTS:,}",
            "column_help": COLUMN_HELP}


@app.context_processor
def _inject_canonical():
    """Absolute URLs for <link rel=canonical> and the Open Graph tags.

    A social card cannot resolve a relative image path, so these have to be
    absolute. APP_BASE_URL is preferred over request.url_root for the same
    reason the sitemap prefers it: url_root is built from the Host header, so
    a request carrying a forged Host would otherwise mint canonical and og:url
    values pointing at someone else's domain. The fallback only matters in
    local development, where the header is trustworthy.

    The query string is rebuilt from an allowlist rather than passed through or
    dropped. Dropping it is wrong: /search?drug=ozempic is the page a reader
    arrives on, and canonicalising it to a bare /search would tell Google every
    drug is the same page and collapse the whole reference set into one entry.
    Passing it through is worse: request.args also carries admin_code, reset
    tokens and ?next=, and a canonical tag is rendered into the HTML, so a
    pass-through would print a secret into the page that leaked it.
    """
    base = _setting("APP_BASE_URL", "").rstrip("/") or request.url_root.rstrip("/")

    keep = [(k, v) for k in ("drug", "q", "a", "b")
            for v in request.args.getlist(k) if v]
    qs = urllib.parse.urlencode(keep)

    # The page shell renders <meta name="robots"> from this, so the tag and the
    # X-Robots-Tag header are decided by one function instead of by a flag set
    # by hand in each template. Adding a path to _NEVER_INDEX is then enough.
    return {"canonical_base": base,
            "canonical_url": base + request.path + (f"?{qs}" if qs else ""),
            "indexable": _indexable(request.path)}


_DRUG_FACTS = (Path(__file__).resolve().parent.parent
               / "data" / "results" / "drug_facts.json")
_facts_cache: dict | None = None


def _drug_facts(name: str | None = None):
    """Approval date, market status, open recalls and shortages per drug.

    Built offline by scripts/build_drug_facts.py from the Drugs@FDA, NDC,
    enforcement and shortage bulk files. Loaded once and held, like the scored
    table -- nothing on the request path calls an API.

    A missing file is survivable: the extra lines simply do not render. Logged
    once, because "no open recall" and "the index was never built" look
    identical on the page and only one of them is a fact about the drug.
    """
    global _facts_cache
    if _facts_cache is None:
        try:
            _facts_cache = json.loads(_DRUG_FACTS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _facts_cache = {}
            app.logger.warning(
                "no drug facts index (%s): approval dates, recalls and "
                "shortages will not appear. Run scripts/build_drug_facts.py.",
                exc)
    if name is None:
        return _facts_cache
    return _facts_cache.get(name) or {}


_scale_cache: dict[str, str] | None = None


@app.context_processor
def _inject_corpus_scale():
    """Pre-formatted corpus size, for the page furniture.

    Memoised rather than computed per request: nunique() over 33,852 rows on
    every page view is pure waste, and the number cannot change without a
    restart because the table is loaded once at module level.
    """
    global _scale_cache
    if _scale_cache is None:
        df = _load_results()
        _scale_cache = {
            "drugs_total": f"{df['drug'].nunique():,}" if not df.empty else "0",
            "pairs_total": f"{len(df):,}",
        }
    return _scale_cache


DISCLAIMER = (
    "These results describe reporting patterns in voluntary, unvalidated case reports. "
    "They do not measure incidence, risk, or causation. A high score means this pair "
    "was reported together far more often than expected — not that the drug caused the "
    "reaction. Reporting is shaped by drug age, media coverage, and litigation patterns, "
    "none of which reflect pharmacology. Do not make medical decisions based on this tool."
)


def _catalogue() -> list[dict]:
    """
    Every drug in the results table with its report count and brand names.

    Coverage is finite, so a user who types a drug that was never scored needs
    to see the difference between "this drug has no signals" and "this drug was
    never examined".
    """
    df = _load_results()
    if df.empty or "drug" not in df.columns:
        return []

    brands: dict[str, list[str]] = {}
    for alias, canonical in _aliases().items():
        if alias != canonical.lower():
            brands.setdefault(canonical, []).append(alias)

    out = []
    for drug, grp in df.groupby("drug"):
        first = grp.iloc[0]
        out.append(
            {
                "drug": drug,
                "reports": int(first.get("a", 0)) + int(first.get("b", 0)),
                "reactions": len(grp),
                "strong": int((grp["tier"] == "strong").sum()) if "tier" in grp else 0,
                "brands": sorted(set(brands.get(drug, [])))[:4],
            }
        )
    return sorted(out, key=lambda r: -r["reports"])


@app.route("/")
def index():
    """Search form home page."""
    return render_template(
        "index.html",
        disclaimer=DISCLAIMER,
        catalogue=_catalogue(),
        reaction_options=_reaction_options(),
    )


@app.route("/api/drugs")
def api_drugs():
    """List every drug available in the precomputed table."""
    return jsonify({"count": len(_catalogue()), "drugs": _catalogue()})


def _display_pt(pt: str) -> str:
    """
    Restore the apostrophe in possessive MedDRA terms for display.

    openFDA stores them with a caret -- FOURNIER^S GANGRENE -- and the stored
    form has to be kept exactly, because it is what the search endpoint matches
    on. Only the rendering changes.
    """
    return (pt or "").replace("^", "'")


def _clean_query(raw: str) -> str:
    """
    Reject anything that is not plausibly a drug name.

    Rejects rather than truncates over-length input, so a caller is told their
    query was refused instead of silently receiving results for a prefix of it.
    Combining forms carry a slash ("amoxicillin/clavulanate"), so the character
    is allowed, but a doubled dot never appears in a drug name and is excluded.
    """
    q = (raw or "").strip()
    if not q or len(q) > 64 or ".." in q:
        return ""
    return q if _SAFE_QUERY.match(q) else ""


def _searching_user_id() -> int | None:
    """
    Owner of the current search, when there is one.

    Imported at call time: src.auth is registered at the bottom of this module.
    """
    try:
        from src.auth import current_user

        user = current_user()
        return user.id if user else None
    except Exception:
        return None


@app.route("/search")
def search():
    """Return HTML results page for a drug query."""
    drug_query = _clean_query(request.args.get("drug", ""))

    if not drug_query:
        return render_template(
            "results.html",
            drug_name="",
            matched_name="",
            total_reports=0,
            pairs=[],
            disclaimer=DISCLAIMER,
            error="Please enter a drug name.",
        )

    filtered, matched_name, total_reports = _search_results(drug_query)

    # Recorded here and not in /api/search: the top-searched list is meant to
    # show what people looked for, and scripted traffic would swamp it.
    from src import analytics

    analytics.record_search(
        drug_query,
        None if filtered.empty else matched_name,
        _searching_user_id(),
    )

    if filtered.empty:
        return render_template(
            "results.html",
            drug_name=drug_query,
            matched_name=matched_name or drug_query,
            total_reports=0,
            pairs=[],
            facts={},
            disclaimer=DISCLAIMER,
            error=f"No results found for '{drug_query}'. "
                  "Try a generic name (e.g. semaglutide, atorvastatin) or drug class.",
        )

    pairs = _format_pairs(filtered)

    # Rows without bias diagnostics are separated rather than interleaved.
    # Indication confounding hides exactly there -- atorvastatin x TYPE 2
    # DIABETES scores 21.5 because diabetics are prescribed statins -- and a
    # reader scanning a single table has no way to tell a checked row from an
    # unchecked one.
    checked = [p for p in pairs if p["diagnosed"]]
    unchecked = [p for p in pairs if not p["diagnosed"]]

    return render_template(
        "results.html",
        drug_name=drug_query,
        matched_name=matched_name,
        total_reports=total_reports,
        pairs=checked,
        unchecked=unchecked,
        facts=_drug_facts(matched_name),
        disclaimer=DISCLAIMER,
        error=None,
    )


def _slim(pairs: list[dict], keep_trend: bool) -> list[dict]:
    """
    Drop the monthly series from JSON responses unless it was asked for.

    Each row carries up to 88 months, which the HTML never needs because the
    sparkline is drawn server-side. Left in, one query for a common reaction
    returns megabytes, and at 60 requests a minute per address that is a
    bandwidth bill and a cheap way to exhaust the host. Callers who genuinely
    want the series can pass trend=1.
    """
    if keep_trend:
        return pairs
    return [{k: v for k, v in p.items() if k != "sparkline"} for p in pairs]


@app.route("/api/search")
def api_search():
    """JSON API endpoint for drug search."""
    drug_query = _clean_query(request.args.get("drug", ""))

    if not drug_query:
        return jsonify({"error": "drug parameter required", "results": []}), 400

    filtered, matched_name, total_reports = _search_results(drug_query)

    if filtered.empty:
        return jsonify(
            {
                "drug_query": drug_query,
                "matched_name": matched_name,
                "total_reports": 0,
                "results": [],
                "disclaimer": DISCLAIMER,
            }
        )

    pairs = _slim(_format_pairs(filtered), request.args.get("trend") == "1")

    return jsonify(
        {
            "drug_query": drug_query,
            "matched_name": matched_name,
            "total_reports": total_reports,
            "results": pairs,
            "disclaimer": DISCLAIMER,
        }
    )


# ---------------------------------------------------------------------------
# Formatting helper
# ---------------------------------------------------------------------------

def _format_pairs(df: pd.DataFrame) -> list[dict]:
    """Convert scored pairs DataFrame to a list of template-friendly dicts."""
    pairs: list[dict] = []

    bias_cols = {
        "notoriety_spike": "Notoriety spike",
        "indication_overlap": "Indication confounding",
    }

    def num(v):
        """pandas nulls are NaN, not None, and format as the string 'nan'."""
        return None if v is None or pd.isna(v) else float(v)

    for _, row in df.iterrows():
        ror, ror_lo, ror_hi = (num(row.get(k)) for k in ("ROR", "ROR_lower", "ROR_upper"))
        if ror is not None and ror_lo is not None and ror_hi is not None:
            ror_ci = f"{ror:.2f} [{ror_lo:.2f}–{ror_hi:.2f}]"
        elif ror is not None:
            ror_ci = f"{ror:.2f}"
        else:
            ror_ci = "—"

        ic025 = num(row.get("IC025"))
        ic025_str = f"{ic025:.2f}" if ic025 is not None else "—"

        # The credible interval, so a reader can see how precisely a figure is
        # estimated. Median width is 0.10 at a>=1000 and 2.22 at a<10, and the
        # point estimate alone hides that difference completely.
        ic975 = num(row.get("IC975"))
        ic_interval = (f"{ic025:.2f}–{ic975:.2f}"
                       if ic025 is not None and ic975 is not None else None)

        # Reported outcomes. Counts lead and the share follows, because a bare
        # percentage reads as a probability of harm and this is the share of
        # *reports* that recorded the outcome. The denominator is reports, not
        # patients, and seriousness is asserted by whoever filed the report.
        a_count = int(row.get("a", 0) or 0)
        outcomes = []
        for col, label in (("deaths", "death"),
                           ("hospitalisations", "hospitalisation"),
                           ("life_threatening", "life-threatening"),
                           ("disabling", "disability")):
            n = row.get(col)
            if n is None or pd.isna(n) or int(n) <= 0:
                continue
            n = int(n)
            # No share where the count exceeds the pair's own report total:
            # that ratio would exceed 100% and read as a rate. The count is
            # still shown, because the count is what was measured.
            share = (n / a_count) if a_count and n <= a_count else None
            outcomes.append({"label": label, "n": n, "share": share})

        bias_flags = [
            label for col, label in bias_cols.items()
            if col in row and row[col] is not None and not pd.isna(row[col]) and row[col]
        ]

        # The active comparator is the most informative diagnostic on the page:
        # it answers "does this survive a fair comparison?" rather than merely
        # warning that it might not.
        ac_ror = num(row.get("active_comparator_ror"))
        ac_shrink = num(row.get("active_comparator_shrinkage"))
        ac_note = None
        if ac_ror is not None and ror is not None:
            ac_note = f"{ror:.1f} → {ac_ror:.1f} vs similar drugs"
            if ac_shrink is not None and ac_shrink > 0:
                ac_note += f" ({ac_shrink:.0%} lower)"
                if ac_shrink >= 0.9:
                    bias_flags.append("Does not survive comparison")
            elif ac_shrink is not None and ac_shrink <= 0:
                ac_note += " (higher, not drug-specific)"

        sparkline_data = []
        raw = row.get("trend")
        if raw is not None and not (isinstance(raw, float) and pd.isna(raw)):
            try:
                monthly = json.loads(raw) if isinstance(raw, str) else dict(raw)
                sparkline_data = [{"month": k, "count": v}
                                  for k, v in sorted(monthly.items())]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        pairs.append(
            {
                "drug": str(row.get("drug") or ""),
                "reaction_pt": _display_pt(row.get("reaction_pt", "")),
                "a": int(row.get("a", 0)),
                "ROR_ci": ror_ci,
                "IC025": ic025_str,
                "IC025_raw": ic025,
                "ic_interval": ic_interval,
                "outcomes": outcomes,
                "tier": str(row.get("tier") or "none"),
                "diagnosed": bool(row.get("diagnosed", False)),
                "dme": bool(row.get("dme", False)),
                "labelled": None if pd.isna(row.get("labelled")) else bool(row.get("labelled")),
                "boxed": bool(row.get("boxed_warning")) if not pd.isna(row.get("boxed_warning")) else False,
                "signal": bool(row.get("signal", False)),
                "bias_flags": bias_flags,
                "ac_note": ac_note,
                "sparkline": sparkline_data,
                "burstiness": num(row.get("burstiness")),
            }
        )

    return pairs


# ---------------------------------------------------------------------------
# Trend sparkline
# ---------------------------------------------------------------------------
#
# bias.pair_trend() records when the reports for a pair arrived, and burstiness
# reduces that to one number. Neither is visible anywhere in the interface, so
# the reader is asked to take "notoriety spike" on trust. Drawn at 120x28 the
# difference is immediate: steady accrual tracking prescribing looks nothing
# like the single tower a law-firm advert produces.
#
# Inline SVG rather than a charting library. The CSP names cdn.jsdelivr.net and
# nothing else, and no sparkline is worth widening it.

_SPARK_W = 120
_SPARK_H = 28
_SPARK_PAD = 2
# Beyond this many points a 120px line is drawing less than 3px per reading and
# the shape stops being legible, so the series is rolled up to quarters.
_SPARK_MAX_POINTS = 40

_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# receiptdate is unvalidated at source. The extract carries months in 1919 and in
# the year 9200, and a single one of those stretches the axis across centuries
# and flattens every real movement in the series to a flat line -- 1,195 pairs
# have at least one. Anything outside the period the extract itself covers is
# dropped rather than drawn.
_TREND_FLOOR = 1968 * 12  # FAERS and its predecessor; nothing earlier exists


def _vintage_ceiling() -> int:
    """December of the extract's own year. A later report cannot be in it."""
    match = re.search(r"(\d{4})", DATA_AS_OF)
    year = int(match.group(1)) if match else 2100
    return year * 12 + 11


_TREND_CEILING = _vintage_ceiling()

# The sparkline carries no colour of its own. It used to write four hex values
# straight into the SVG as presentation attributes, which meant the stylesheet
# could not theme it: on a dark page the charts stayed light-mode grey with a
# near-white fill. The generated element carries class="spark" (plus
# "spark-spike"), and style.css maps those to --spark-* tokens per theme.
#
# A spike is still styled in the muted --tier-strong earth rather than a
# warning colour: it is a reason to read the number carefully, not a claim
# about the drug.

SPIKE_THRESHOLD = 0.25


def _month_index(ym: str) -> int | None:
    """'2019-04' -> a month ordinal, so gaps in the series can be measured."""
    if len(ym) != 7 or ym[4] != "-":
        return None
    try:
        year, month = int(ym[:4]), int(ym[5:7])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    index = year * 12 + (month - 1)
    return index if _TREND_FLOOR <= index <= _TREND_CEILING else None


def _month_label(index: int) -> str:
    return f"{_MONTH_ABBR[index % 12]} {index // 12}"


def _dense_months(points: list[dict]) -> list[tuple[int, int]]:
    """
    Expand a stored trend to one entry per month, zero-filling the gaps.

    pair_trend() only emits months that had a report. Plotting those by position
    would compress a quiet decade into the same width as a busy year and turn
    every sparse series into a plausible-looking trend.
    """
    counts: dict[int, int] = {}
    for point in points or []:
        index = _month_index(str(point.get("month", "")))
        if index is None:
            continue
        try:
            counts[index] = counts.get(index, 0) + int(point.get("count", 0) or 0)
        except (TypeError, ValueError):
            continue
    if not counts:
        return []
    return [(i, counts.get(i, 0)) for i in range(min(counts), max(counts) + 1)]


def _rollup(series: list[tuple[int, int]], months: int) -> list[tuple[int, int]]:
    """Bucket a dense monthly series into calendar quarters or years."""
    totals: dict[int, int] = {}
    for index, count in series:
        totals[index // months] = totals.get(index // months, 0) + count
    return [(b * months, totals.get(b, 0))
            for b in range(min(totals), max(totals) + 1)]


def sparkline_svg(points: list[dict] | None, burstiness: float | None = None) -> Markup:
    """
    Render one pair's reporting history as an inline SVG, or nothing at all.

    Returns empty markup for a pair that was never diagnosed, so an unchecked row
    shows a blank cell rather than a flat line that would read as "no activity".
    """
    series = _dense_months(points or [])
    if not series:
        return Markup("")

    monthly_total = sum(c for _, c in series)
    if monthly_total <= 0:
        return Markup("")

    # Recomputed only when the stored column is null. The parquet value is the
    # one the "Notoriety spike" flag was raised from, and the two must agree.
    if burstiness is None:
        burstiness = max(c for _, c in series) / monthly_total
    spike = burstiness > SPIKE_THRESHOLD

    inner_w = _SPARK_W - 2 * _SPARK_PAD
    inner_h = _SPARK_H - 2 * _SPARK_PAD
    baseline = _SPARK_H - _SPARK_PAD

    unit = "monthly"
    if len(series) > _SPARK_MAX_POINTS:
        series = _rollup(series, 3)
        unit = "quarterly"
    # Thirty years of quarters is still two readings per pixel at this width,
    # which draws noise rather than shape.
    if len(series) > inner_w:
        series = _rollup(series, 12)
        unit = "yearly"

    counts = [c for _, c in series]
    peak = max(counts) or 1

    label = (lambda i: str(i // 12)) if unit == "yearly" else _month_label
    title = (
        f"{label(series[0][0])} to {label(series[-1][0])}, "
        f"{monthly_total:,} report{'' if monthly_total == 1 else 's'} in total, "
        f"plotted {unit}"
    )
    if spike:
        title += (
            f". {burstiness:.0%} of them arrived in a single month. A burst like "
            "this usually follows a news story, a published study or a law-firm "
            "campaign rather than any change in the drug."
        )

    if len(series) == 1:
        body = (
            f'<rect x="{_SPARK_W / 2 - 1:.1f}" y="{_SPARK_PAD}" width="2" '
            f'height="{inner_h}"/>'
        )
    else:
        step = inner_w / (len(counts) - 1)
        coords = [(_SPARK_PAD + i * step, baseline - (c / peak) * inner_h)
                  for i, c in enumerate(counts)]
        # One path draws the line and the area beneath it. Two elements would
        # repeat every coordinate, and these tables run to hundreds of rows.
        line = "L".join(f"{x:.1f} {y:.1f}" for x, y in coords)
        body = (
            f'<path d="M{_SPARK_PAD} {baseline}L{line}'
            f'L{_SPARK_W - _SPARK_PAD} {baseline}Z" '
            f'stroke-width="1" stroke-linejoin="round"/>'
        )
        if spike:
            px, py = coords[counts.index(peak)]
            body += f'<circle cx="{px:.1f}" cy="{py:.1f}" r="1.8"/>'

    safe_title = escape(title)
    return Markup(
        f'<svg class="spark{" spark-spike" if spike else ""}" width="{_SPARK_W}" '
        f'height="{_SPARK_H}" viewBox="0 0 {_SPARK_W} {_SPARK_H}" '
        f'preserveAspectRatio="none" role="img" title="{safe_title}">'
        f'<title>{safe_title}</title>{body}</svg>'
    )


@app.template_global("spark")
def _spark(pair: dict | None) -> Markup:
    """Template entry point: takes a row from _format_pairs, returns the SVG."""
    if not pair:
        return Markup("")
    return sparkline_svg(pair.get("sparkline"), pair.get("burstiness"))


# ---------------------------------------------------------------------------
# Reaction-first lookup
# ---------------------------------------------------------------------------

def _reaction_key(raw: str) -> str:
    """
    Fold a typed reaction to the form stored terms are compared in.

    openFDA writes possessives with a caret, so a user typing the apostrophe in
    "fournier's gangrene" would otherwise match nothing at all. Both sides lose
    the character rather than the query being rewritten, which also lets someone
    who omits the punctuation entirely still land on the term.
    """
    return (raw or "").strip().upper().replace("^", "").replace("'", "")


_reaction_cache: list[dict] | None = None


def _reaction_index() -> list[dict]:
    """
    Every reaction term in the results table, commonest first.

    `a + c` is the reaction's total across all of FAERS and is identical on every
    row for that term, so it is read once rather than summed -- adding column a
    across drugs would count a report once per drug it names.
    """
    global _reaction_cache
    if _reaction_cache is not None:
        return _reaction_cache

    df = _load_results()
    if df.empty or "reaction_pt" not in df.columns:
        _reaction_cache = []
        return _reaction_cache

    out = []
    for term, grp in df.groupby("reaction_pt"):
        if not term:
            continue
        reports = 0
        if {"a", "c"}.issubset(grp.columns):
            reports = int(grp["a"].fillna(0).iloc[0]) + int(grp["c"].fillna(0).iloc[0])
        out.append(
            {
                "stored": str(term),
                "term": _display_pt(str(term)),
                "key": _reaction_key(str(term)),
                "drugs": int(len(grp)),
                "reports": reports,
                "strong": int((grp["tier"] == "strong").sum()) if "tier" in grp else 0,
            }
        )
    _reaction_cache = sorted(out, key=lambda r: (-r["reports"], r["term"]))
    return _reaction_cache


def _reaction_options(limit: int = 300) -> list[dict]:
    """Autocomplete list, capped so the page does not carry 879 hidden options."""
    return _reaction_index()[:limit]


def _most_widespread(limit: int = 2) -> list[dict]:
    """
    The terms reported with the largest number of drugs.

    Any page that sets several drugs beside each other has to say that overlap
    is the normal state of this data, and the claim is worth far more stated in
    the table's own current numbers than as prose a later extract would falsify.
    """
    return sorted(_reaction_index(), key=lambda r: (-r["drugs"], r["term"]))[:limit]


def _match_reactions(query: str) -> list[dict]:
    """Exact term if there is one, otherwise every partial match."""
    key = _reaction_key(query)
    if not key:
        return []
    terms = _reaction_index()
    exact = [t for t in terms if t["key"] == key]
    if exact:
        return exact
    return [t for t in terms if key in t["key"]]


def _reaction_results(stored_term: str) -> pd.DataFrame:
    """Every drug scored against one reaction, ranked the same way /search ranks."""
    df = _load_results()
    if df.empty or "reaction_pt" not in df.columns:
        return pd.DataFrame()
    filtered = df[df["reaction_pt"] == stored_term].copy()
    if "IC025" in filtered.columns:
        filtered = filtered.sort_values("IC025", ascending=False, na_position="last")
    return filtered


def _reaction_error(query: str, message: str, candidates: list[dict] | None = None,
                    status: int = 200):
    return render_template(
        "reaction_results.html",
        query=query,
        reaction=None,
        pairs=[],
        unchecked=[],
        candidates=candidates or [],
        reaction_options=_reaction_options(),
        disclaimer=DISCLAIMER,
        error=message,
    ), status


@app.route("/reaction")
def reaction():
    """Given a MedDRA term, the drugs most disproportionately reported with it."""
    query = _clean_query(request.args.get("q", ""))
    if not query:
        return _reaction_error(
            request.args.get("q", "")[:64],
            "Enter a reaction term using letters, numbers and spaces only — "
            "for example rhabdomyolysis, nausea, or pancreatitis.",
        )

    matches = _match_reactions(query)
    if not matches:
        return _reaction_error(
            query,
            f"No reaction matching '{query}' was scored. Terms come from MedDRA, "
            "so try the clinical word (pruritus rather than itching), or a "
            "fragment such as 'rhabdo'.",
        )
    if len(matches) > 1:
        return _reaction_error(
            query,
            f"{len(matches)} reaction terms match '{query}'. Pick one:",
            candidates=matches[:60],
        )

    match = matches[0]
    filtered = _reaction_results(match["stored"])
    if filtered.empty:
        return _reaction_error(query, f"No drugs are scored against {match['term']}.")

    pairs = _format_pairs(filtered)

    # Same separation as /search. An undiagnosed row here is worse, not better:
    # ranked against a single reaction, the drug prescribed for the condition the
    # term describes sits at the top and nothing on the row says so.
    return render_template(
        "reaction_results.html",
        query=query,
        reaction=match,
        pairs=[p for p in pairs if p["diagnosed"]],
        unchecked=[p for p in pairs if not p["diagnosed"]],
        candidates=[],
        reaction_options=_reaction_options(),
        disclaimer=DISCLAIMER,
        error=None,
    )


@app.route("/api/reaction")
def api_reaction():
    """JSON API for the reaction-first lookup."""
    query = _clean_query(request.args.get("q", ""))
    if not query:
        return jsonify({"error": "q parameter required", "results": []}), 400

    matches = _match_reactions(query)
    if not matches:
        return jsonify({"query": query, "matched_term": None, "results": [],
                        "disclaimer": DISCLAIMER}), 404
    if len(matches) > 1:
        return jsonify({
            "query": query,
            "matched_term": None,
            "candidates": [{"term": m["term"], "drugs": m["drugs"],
                            "reports": m["reports"]} for m in matches[:60]],
            "results": [],
            "disclaimer": DISCLAIMER,
        })

    match = matches[0]
    pairs = _slim(_format_pairs(_reaction_results(match["stored"])),
                  request.args.get("trend") == "1")
    return jsonify({
        "query": query,
        "matched_term": match["term"],
        "reaction_reports": match["reports"],
        "drugs": len(pairs),
        "results": pairs,
        "disclaimer": DISCLAIMER,
    })


# ---------------------------------------------------------------------------
# Two-drug comparison
# ---------------------------------------------------------------------------
#
# Every drug here is scored against the whole database independently. Putting
# two of them in adjacent columns invites exactly one wrong reading -- that the
# page says something about taking both -- and src/interactions.py exists
# because that was attempted, failed its known-answer controls and was withheld.
# The framing on compare.html is load-bearing, not decoration.

COMPARE_NOT_INTERACTION = (
    "This is not an interaction analysis. Each drug is compared against the whole "
    "database on its own, and the two columns never meet. Nothing on this page "
    "describes what happens when someone takes both drugs together, and no "
    "disproportionality method can answer that question from this data."
)


def _compare_rows(pairs_a: list[dict], pairs_b: list[dict]) -> list[dict]:
    """
    Union of two drugs' reactions, ranked by whichever side scores higher.

    Ranking on the larger IC025 keeps a reaction that is strong for one drug and
    absent from the other at the top, which is the comparison most readers came
    for. Sorting on the average would bury it.
    """
    by_a = {p["reaction_pt"]: p for p in pairs_a}
    by_b = {p["reaction_pt"]: p for p in pairs_b}

    rows = []
    for pt in sorted(set(by_a) | set(by_b)):
        left, right = by_a.get(pt), by_b.get(pt)
        scores = [p["IC025_raw"] for p in (left, right)
                  if p is not None and p["IC025_raw"] is not None]
        rows.append({
            "reaction_pt": pt,
            "left": left,
            "right": right,
            "only": "left" if right is None else ("right" if left is None else None),
            "rank": max(scores) if scores else float("-inf"),
            "diagnosed": bool((left and left["diagnosed"]) or (right and right["diagnosed"])),
        })
    rows.sort(key=lambda r: (-r["rank"], r["reaction_pt"]))
    return rows


def _compare_error(a_query: str, b_query: str, message: str):
    return render_template(
        "compare.html",
        a_query=a_query,
        b_query=b_query,
        left_name="",
        right_name="",
        rows=[],
        unchecked_rows=[],
        catalogue=_catalogue(),
        disclaimer=DISCLAIMER,
        not_interaction=COMPARE_NOT_INTERACTION,
        error=message,
    )


@app.route("/compare")
def compare():
    """Two drugs side by side over the union of their reactions."""
    a_raw = request.args.get("a", "")
    b_raw = request.args.get("b", "")
    a_query = _clean_query(a_raw)
    b_query = _clean_query(b_raw)

    if not a_raw and not b_raw:
        return _compare_error("", "", None)
    if not a_query or not b_query:
        return _compare_error(
            a_query, b_query,
            "Enter two drug names using letters, numbers and spaces only.",
        )

    left_df, left_name, _ = _search_results(a_query)
    right_df, right_name, _ = _search_results(b_query)

    missing = [q for q, d in ((a_query, left_df), (b_query, right_df)) if d.empty]
    if missing:
        return _compare_error(
            a_query, b_query,
            f"Not in the catalogue: {', '.join(missing)}. Try a generic name. "
            "A drug that is missing has not been examined, which is not the same "
            "as having been examined and found clear.",
        )
    if left_name == right_name:
        return _compare_error(
            a_query, b_query,
            f"Both names resolve to {left_name}. Brands sharing an active "
            "ingredient are the same drug here, so there is nothing to compare.",
        )

    rows = _compare_rows(_format_pairs(left_df), _format_pairs(right_df))

    return render_template(
        "compare.html",
        a_query=a_query,
        b_query=b_query,
        left_name=left_name,
        right_name=right_name,
        rows=[r for r in rows if r["diagnosed"]],
        unchecked_rows=[r for r in rows if not r["diagnosed"]],
        catalogue=_catalogue(),
        disclaimer=DISCLAIMER,
        not_interaction=COMPARE_NOT_INTERACTION,
        error=None,
    )


# ---------------------------------------------------------------------------
# Multi-drug view
# ---------------------------------------------------------------------------
#
# /compare widened to a whole medication list, and the framing problem widens
# with it. Two columns invite the reader to assume the page says something about
# taking both; ten columns of figures about ten drugs somebody actually takes
# together invite it far harder, and a reaction that shows up under several of
# them looks like the tool has found the combination. It has not, and it cannot:
# src/interactions.py was built to answer that question, failed all twelve of
# its known-answer controls and was withheld.

MAX_LIST_DRUGS = 10
# Eight is roughly what a reader will actually compare across ten panels before
# the page becomes a data dump. The shared section is computed over all of them.
LIST_PER_DRUG = 8
LIST_SHARED_ROWS = 60

LIST_NOT_INTERACTION = (
    "A reaction that appears under several of these drugs was reported with each "
    "of them separately — in different reports, almost always about different "
    "people. It does not mean that taking them together causes that reaction, or "
    "makes it worse, or makes it more likely. Each drug here is scored on its own "
    "against the whole database, and the columns never meet. This is not an "
    "interaction check, and no disproportionality method can produce one from "
    "this data."
)


def _resolve_drug_list(raw: str) -> tuple[list[dict], list[str]]:
    """
    Resolve a comma-separated list to catalogue drugs, keeping the failures.

    One name the catalogue does not contain must not cost the caller the other
    nine, so every entry is reported on individually and the request continues
    with whatever resolved.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").replace("\n", ",").split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            entries.append(name[:64])

    notes: list[str] = []
    if len(entries) > MAX_LIST_DRUGS:
        dropped = entries[MAX_LIST_DRUGS:]
        entries = entries[:MAX_LIST_DRUGS]
        notes.append(
            f"This page takes {MAX_LIST_DRUGS} drugs at a time, so "
            f"{', '.join(dropped)} {'was' if len(dropped) == 1 else 'were'} "
            "left out."
        )

    catalogue_size = len(_catalogue())
    resolved: list[dict] = []
    first_typed: dict[str, str] = {}

    for entry in entries:
        query = _clean_query(entry)
        if not query:
            notes.append(
                f"'{entry}' could not be read as a drug name — letters, numbers, "
                "spaces and the usual separators only."
            )
            continue

        filtered, matched_name, total_reports = _search_results(query)
        if filtered.empty:
            notes.append(
                f"'{query}' is not in the catalogue of {catalogue_size} scored "
                "drugs. A drug that is missing has not been examined, which is "
                "not the same as having been examined and found clear."
            )
            continue
        if matched_name in first_typed:
            notes.append(
                f"'{query}' and '{first_typed[matched_name]}' both resolve to "
                f"{matched_name}, so it appears once."
            )
            continue
        first_typed[matched_name] = query

        strong = filtered
        if "tier" in strong.columns:
            strong = strong[strong["tier"] == "strong"]
        strong_pairs = _format_pairs(strong)

        resolved.append(
            {
                "query": query,
                "drug": matched_name,
                "total_reports": total_reports,
                "reactions": int(len(filtered)),
                "strong_all": strong_pairs,
                "pairs": strong_pairs[:LIST_PER_DRUG],
            }
        )

    return resolved, notes


def _shared_reactions(drugs: list[dict]) -> list[dict]:
    """
    Reactions that are strong-tier for more than one of the listed drugs.

    Computed over every strong-tier reaction each drug has, not the eight shown
    in its own panel: otherwise the section would silently change meaning with
    the display cap, and a reaction shared by four drugs could be missing from
    it because it ranked ninth for one of them.
    """
    by_pt: dict[str, dict[str, dict]] = {}
    for entry in drugs:
        for pair in entry["strong_all"]:
            by_pt.setdefault(pair["reaction_pt"], {})[entry["drug"]] = pair

    rows = []
    for pt, hits in by_pt.items():
        if len(hits) < 2:
            continue
        scores = [p["IC025_raw"] for p in hits.values() if p["IC025_raw"] is not None]
        rows.append(
            {
                "reaction_pt": pt,
                "cells": [hits.get(entry["drug"]) for entry in drugs],
                "shared_by": len(hits),
                "top": max(scores) if scores else float("-inf"),
            }
        )

    rows.sort(key=lambda r: (-r["shared_by"], -r["top"], r["reaction_pt"]))
    return rows[:LIST_SHARED_ROWS]


@app.route("/list")
def drug_list():
    """A medication list, each drug scored separately, with the overlap named."""
    raw = request.args.get("drugs", "")
    drugs, notes = _resolve_drug_list(raw)

    return render_template(
        "drug_list.html",
        raw_drugs=", ".join(d["drug"] for d in drugs) or raw[:512],
        drugs=drugs,
        shared=_shared_reactions(drugs),
        notes=notes,
        catalogue=_catalogue(),
        widespread=_most_widespread(2),
        total_drugs=len(_catalogue()),
        max_drugs=MAX_LIST_DRUGS,
        per_drug=LIST_PER_DRUG,
        disclaimer=DISCLAIMER,
        not_interaction=LIST_NOT_INTERACTION,
        submitted=bool(raw.strip()),
    )


# ---------------------------------------------------------------------------
# M11: accounts, tracked drugs, notifications
# ---------------------------------------------------------------------------
#
# Imported at the foot of the module rather than the head. These blueprints use
# _setting, _aliases, _catalogue and _format_pairs from here, so the names have
# to exist before the import runs; the loop closes cleanly in this direction
# and not the other.

from src import analytics as _analytics  # noqa: E402
from src import auth as _auth  # noqa: E402
from src import user_features as _user_features  # noqa: E402
from src import worksheet as _worksheet  # noqa: E402
from src.models import init_app as _init_db_app, init_db as _init_db  # noqa: E402

try:
    _init_db()
except Exception as _exc:  # pragma: no cover - depends on the environment
    # The signal table is a parquet file and does not need the database. A
    # broken database should cost the account features, not the lookup tool
    # everyone else is using.
    app.logger.error("database unavailable, account features will fail: %s",
                     _exc.__class__.__name__)

_init_db_app(app)
app.register_blueprint(_auth.bp)
app.register_blueprint(_user_features.bp)
app.register_blueprint(_worksheet.bp)
app.register_blueprint(_analytics.bp)

# Registered after _guard, so the rate limiter and the access gate still run
# first on every request.
app.before_request(_auth.csrf_protect)


# ---------------------------------------------------------------------------
# Standalone run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Development only, and opt-in. The Werkzeug debugger executes arbitrary
    # code from the browser on any traceback, so debug=True is not something to
    # leave standing in a file that is about to be public -- a reader cannot
    # tell from this line alone that production runs serve.py instead, and the
    # honest fix is to make it impossible rather than to explain it.
    #
    #   RXSIGNAL_DEBUG=1 python src/app.py
    debug = _setting("RXSIGNAL_DEBUG", "0").lower() in ("1", "true", "yes", "on")
    app.run(debug=debug, port=5000)
