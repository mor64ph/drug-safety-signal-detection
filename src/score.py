"""
M5: Disproportionality scoring engine.

Implements ROR, PRR, chi2 and BCPNN for each
(drug_class/molecule, reaction_pt) pair.

IC025 is the primary ranking key: the lower bound of the 95% credible
interval on the Information Component, from a Bayesian posterior with a
proper prior. IC975 is the upper bound, carried so a reader can see how
precisely a figure is estimated -- the interval is 0.10 wide at a>=1000 and
2.22 wide at a<10, and a point estimate alone hides that entirely.
Screening gate: a >= 3 AND ROR_lower > 1 → signal=True.

References:
  - Bate et al. (1998), Norén (2006) for BCPNN
  - Evans et al. for PRR/chi2
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import minimize
from scipy.special import digamma, gammaln, polygamma
from scipy.stats import gamma as gamma_dist
import pandas as pd

from src.client import total as api_total

_BG_CACHE = Path(__file__).resolve().parent.parent / "data" / "raw" / "bg_totals.json"


# ---------------------------------------------------------------------------
# Contingency table helpers
# ---------------------------------------------------------------------------

def _contingency(
    df: pd.DataFrame,
    drug_col: str,
    drug_val: Any,
    reaction_val: str,
) -> tuple[int, int, int, int]:
    """
    Build 2x2 contingency table cells (a, b, c, d) at REPORT level.

    a: reports mentioning drug_val and reaction_val
    b: reports mentioning drug_val but not reaction_val
    c: reports mentioning reaction_val but not drug_val
    d: reports mentioning neither

    The unit of analysis is the report, not the flattened row. A report with
    40 drugs and 47 reactions produces 1,880 rows; counting rows would inflate
    b by the reaction count and deflate ROR by a factor that varies per drug.
    """
    reports = df["safetyreportid"]
    drug_reports = set(reports[df[drug_col] == drug_val].dropna())
    react_reports = set(
        reports[df["reaction_pt"].str.upper() == reaction_val.upper()].dropna()
    )
    all_reports = set(reports.dropna())

    a = len(drug_reports & react_reports)
    b = len(drug_reports) - a
    c = len(react_reports) - a
    d = len(all_reports) - a - b - c
    return a, b, c, max(d, 0)


# ---------------------------------------------------------------------------
# Metric calculations
# ---------------------------------------------------------------------------

def _ror(a: int, b: int, c: int, d: int) -> tuple[float, float, float]:
    """
    Reporting Odds Ratio and 95% CI.

    Returns (ROR, ROR_lower, ROR_upper). Returns (NaN, NaN, NaN) if
    any cell is zero.
    """
    if b == 0 or c == 0:
        return float("nan"), float("nan"), float("nan")
    ror = (a * d) / (b * c)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        se = np.sqrt(1 / max(a, 1) + 1 / b + 1 / c + 1 / max(d, 1))
    ln_ror = np.log(ror)
    ror_lower = np.exp(ln_ror - 1.96 * se)
    ror_upper = np.exp(ln_ror + 1.96 * se)
    return float(ror), float(ror_lower), float(ror_upper)


def _prr(a: int, b: int, c: int, d: int) -> float:
    """Proportional Reporting Ratio."""
    denom = (c + d)
    if (a + b) == 0 or denom == 0 or c == 0:
        return float("nan")
    return (a / (a + b)) / (c / denom)


def _chi2(a: int, b: int, c: int, d: int) -> float:
    """
    Pearson chi-squared for 2x2 table.

    Uses the formula from Evans et al. adjusted for FAERS sparse cells.
    """
    n = a + b + c + d
    if n == 0:
        return float("nan")
    exp_a = (a + b) * (a + c) / n
    if exp_a == 0:
        return float("nan")
    # Full chi2 formula for 2x2
    num = (a * d - b * c) ** 2 * n
    den = (a + b) * (c + d) * (a + c) * (b + d)
    if den == 0:
        return float("nan")
    return float(num / den)


def bcpnn(a, b, c, d):
    """
    BCPNN: the Information Component with a genuine Bayesian prior.

    Returns (IC, IC025, IC975) -- the posterior mean and the bounds of the 95%
    credible interval. Works on scalars or numpy arrays.

    This replaces a frequentist normal approximation that was previously
    mislabelled as Bayesian. That version computed

        IC      = log2(aN / ((a+b)(a+c)))
        var_IC  = (1 - a/(a+b))/(a ln2) + (1 - (a+c)/N)/((a+c) ln2)
        IC025   = IC - 1.96 sqrt(var_IC)

    which has no prior at all. Its interval widens as `a` falls, which looks
    like shrinkage, but nothing pulls the estimate itself toward zero -- so a
    large ratio measured on a handful of reports keeps its magnitude and simply
    acquires wider error bars, and the *lower bound* stays high.

    What that cost, measured on this table. Cerivastatin has only ~366 reports
    in the whole database. Its ALS row rests on 14 of them and is the known
    litigation artifact, caught elsewhere by the notoriety and comparator
    diagnostics. The old formula scored it **IC025 7.94, the highest-ranked row
    for that drug** -- ahead of rhabdomyolysis on 45 reports, the finding that
    actually withdrew the drug in 2001. BCPNN scores the same row 3.05 and
    rhabdomyolysis 4.28, putting the real signal first and the artifact fifth,
    on the statistics alone and before any diagnostic runs.

    The formulation is Bate et al. 1998 / Norén 2006, matching the reference
    implementation in the PhViD package: Beta priors on the two margins and on
    the cell, giving a posterior whose mean and variance are available in
    closed form through the digamma and trigamma functions. The priors are the
    standard weakly-informative choice (one pseudo-count on each margin), which
    is what supplies the pull toward independence that the old version lacked.

    Across 33,852 pairs the two agree closely where the data is thick -- the
    validated anchor, statins x rhabdomyolysis on 8,820 reports, moves from
    3.3232 to 3.3155 -- and diverge exactly where a prior should matter: mean
    absolute difference 0.025 at a>=100, rising to 0.357 at a<3. 739 pairs
    change tier, every one of them downward.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    c = np.asarray(c, dtype=float)
    d = np.asarray(d, dtype=float)

    n_total = a + b + c + d
    n_drug = a + b          # reports mentioning the drug
    n_rxn = a + c           # reports mentioning the reaction
    ln2 = np.log(2)

    with np.errstate(divide="ignore", invalid="ignore"):
        # One pseudo-count on each margin and on the cell.
        p1 = 1.0 + n_drug
        p2 = 1.0 + n_total - n_drug
        q1 = 1.0 + n_rxn
        q2 = 1.0 + n_total - n_rxn
        r1 = 1.0 + a
        # The prior on the complement of the cell, scaled so the cell prior is
        # consistent with the two marginal priors rather than chosen freely.
        r2 = n_total - a - 1.0 + (2.0 + n_total) ** 2 / (q1 * p1)

        mean = (digamma(r1) - digamma(r1 + r2)
                - (digamma(p1) - digamma(p1 + p2))
                - (digamma(q1) - digamma(q1 + q2))) / ln2
        var = (polygamma(1, r1) - polygamma(1, r1 + r2)
               + (polygamma(1, p1) - polygamma(1, p1 + p2))
               + (polygamma(1, q1) - polygamma(1, q1 + q2))) / ln2 ** 2

        sd = np.sqrt(np.clip(var, 0.0, None))
        lower = mean - 1.96 * sd
        upper = mean + 1.96 * sd

    # A pair nobody reported has no posterior worth quoting.
    empty = (a <= 0) | (n_drug <= 0) | (n_rxn <= 0) | (n_total <= 0)
    mean = np.where(empty, np.nan, mean)
    lower = np.where(empty, np.nan, lower)
    upper = np.where(empty, np.nan, upper)

    if mean.ndim == 0:
        return float(mean), float(lower), float(upper)
    return mean, lower, upper


def _nb_logpmf(n, alpha, beta, expected):
    """log P(n | lambda ~ Gamma(alpha, beta), n ~ Poisson(lambda * expected)).

    Integrating a Poisson over a gamma prior gives a negative binomial, which
    is what makes the mixture likelihood tractable in closed form.
    """
    p = beta / (beta + expected)
    return (gammaln(alpha + n) - gammaln(alpha) - gammaln(n + 1)
            + alpha * np.log(p) + n * np.log1p(-p))


def fit_mgps(a, expected, seed=(0.2, 0.1, 2.0, 4.0, 1 / 3)):
    """Fit the five MGPS hyperparameters by maximum likelihood.

    The model is DuMouchel (1999): each cell's relative reporting ratio lambda
    is drawn from a mixture of two gamma distributions, and the observed count
    is Poisson with mean lambda * expected. The mixture is what does the work --
    one component describes the bulk of cells where nothing is happening, the
    other the tail where something is -- and because the hyperparameters are
    estimated from *all* 33,852 cells at once, the prior is empirical rather
    than assumed. That is the difference between MGPS and BCPNN, which fixes
    its prior a priori.

    Returns (alpha1, beta1, alpha2, beta2, pi).
    """
    a = np.asarray(a, dtype=float)
    expected = np.asarray(expected, dtype=float)
    keep = np.isfinite(a) & np.isfinite(expected) & (expected > 0)
    n, e = a[keep], expected[keep]

    def unpack(theta):
        # Optimise unconstrained: exp keeps the four gamma parameters positive
        # and the logistic keeps the mixing weight inside (0, 1). A bounded
        # optimiser wandering to a negative alpha yields gammaln of a negative
        # number and a silent NaN objective.
        alpha1, beta1, alpha2, beta2 = np.exp(theta[:4])
        pi = 1.0 / (1.0 + np.exp(-theta[4]))
        return alpha1, beta1, alpha2, beta2, pi

    def neg_ll(theta):
        alpha1, beta1, alpha2, beta2, pi = unpack(theta)
        l1 = _nb_logpmf(n, alpha1, beta1, e)
        l2 = _nb_logpmf(n, alpha2, beta2, e)
        total = np.logaddexp(np.log(pi) + l1, np.log1p(-pi) + l2)
        if not np.all(np.isfinite(total)):
            return 1e12
        return -float(np.sum(total))

    start = np.array([np.log(seed[0]), np.log(seed[1]),
                      np.log(seed[2]), np.log(seed[3]),
                      np.log(seed[4] / (1 - seed[4]))])
    result = minimize(neg_ll, start, method="Nelder-Mead",
                      options={"maxiter": 4000, "xatol": 1e-6, "fatol": 1e-6})
    return unpack(result.x)


def mgps(a, b, c, d, params=None):
    """
    MGPS: EBGM with its 5th and 95th percentile credible bounds.

    Returns (ebgm, eb05, eb95, expected).

    EBGM is the posterior geometric mean of the relative reporting ratio, and
    **EB05 is the number the FDA screens on**, conventionally at a threshold of
    2. It is the third measure here and it answers a different question from
    the other two: ROR compares odds, BCPNN's IC025 is a log2 bound under a
    fixed prior, and EB05 is a bound under a prior estimated from this very
    table. Where they disagree, the disagreement is the finding.

    `expected` is the count under independence, (a+b)(a+c)/N -- the same
    denominator as the relative reporting ratio.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    c = np.asarray(c, dtype=float)
    d = np.asarray(d, dtype=float)

    n_total = a + b + c + d
    expected = (a + b) * (a + c) / n_total

    if params is None:
        params = fit_mgps(a, expected)
    alpha1, beta1, alpha2, beta2, pi = params

    with np.errstate(divide="ignore", invalid="ignore"):
        l1 = _nb_logpmf(a, alpha1, beta1, expected)
        l2 = _nb_logpmf(a, alpha2, beta2, expected)
        log_num = np.log(pi) + l1
        log_den = np.logaddexp(log_num, np.log1p(-pi) + l2)
        q = np.exp(log_num - log_den)          # posterior weight on component 1

        # The posterior for lambda is itself a two-gamma mixture, with the
        # observed count added to each shape and the expected count to each
        # rate. Both moments below follow from that.
        shape1, rate1 = alpha1 + a, beta1 + expected
        shape2, rate2 = alpha2 + a, beta2 + expected

        mean_log = (q * (digamma(shape1) - np.log(rate1))
                    + (1 - q) * (digamma(shape2) - np.log(rate2)))
        ebgm = np.exp(mean_log)

    def quantile(p):
        """The mixture quantile, by bisection in log space.

        There is no closed form for a quantile of a gamma mixture, and the
        components' scales differ by orders of magnitude across the table, so
        the search runs on log(lambda) to stay conditioned. 60 iterations puts
        the bracket well below display precision.
        """
        lo = np.full(ebgm.shape, -20.0)
        hi = np.full(ebgm.shape, 20.0)
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            x = np.exp(mid)
            cdf = (q * gamma_dist.cdf(x, shape1, scale=1.0 / rate1)
                   + (1 - q) * gamma_dist.cdf(x, shape2, scale=1.0 / rate2))
            too_high = cdf > p
            hi = np.where(too_high, mid, hi)
            lo = np.where(too_high, lo, mid)
        return np.exp(0.5 * (lo + hi))

    with np.errstate(divide="ignore", invalid="ignore"):
        eb05 = quantile(0.05)
        eb95 = quantile(0.95)

    empty = (a <= 0) | ~np.isfinite(expected) | (expected <= 0)
    ebgm = np.where(empty, np.nan, ebgm)
    eb05 = np.where(empty, np.nan, eb05)
    eb95 = np.where(empty, np.nan, eb95)

    if np.ndim(ebgm) == 0:
        return float(ebgm), float(eb05), float(eb95), float(expected)
    return ebgm, eb05, eb95, expected


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score(
    df: pd.DataFrame,
    drug_col: str = "drug_class",
    drug_filter: str | None = None,
    reaction_filter: str | None = None,
) -> pd.DataFrame:
    """
    Score all (drug, reaction_pt) pairs in the DataFrame.

    Args:
        df: Normalised DataFrame from M4.
        drug_col: Column to use for drug grouping ('drug_class' or 'molecule').
        drug_filter: If given, score only rows where drug_col == drug_filter.
        reaction_filter: If given, score only rows where reaction_pt matches.

    Returns:
        DataFrame of scored pairs sorted by IC025 descending.
        Columns: drug, reaction_pt, a, b, c, d, N, ROR, ROR_lower, ROR_upper,
                 PRR, chi2, IC, IC025, signal.
    """
    cols = [
        "drug", "reaction_pt", "a", "b", "c", "d", "N",
        "ROR", "ROR_lower", "ROR_upper", "PRR", "chi2",
        "IC", "IC025", "signal",
    ]

    work = df[[drug_col, "reaction_pt", "safetyreportid"]].copy()
    work["reaction_pt"] = work["reaction_pt"].fillna("").str.upper()
    work = work[(work["reaction_pt"] != "") & work[drug_col].notna()]
    work = work.dropna(subset=["safetyreportid"])
    if work.empty:
        return pd.DataFrame(columns=cols)

    # Report-level marginals over the WHOLE universe, before any filtering.
    # Counting flattened rows here would inflate b by the per-report reaction
    # count and deflate every ROR by a drug-dependent factor.
    N = work["safetyreportid"].nunique()
    drug_tot = work[[drug_col, "safetyreportid"]].drop_duplicates().groupby(drug_col).size()
    rxn_tot = (
        work[["reaction_pt", "safetyreportid"]].drop_duplicates()
        .groupby("reaction_pt").size()
    )

    pairs = work.drop_duplicates()
    if drug_filter:
        pairs = pairs[pairs[drug_col] == drug_filter]
    if reaction_filter:
        pairs = pairs[pairs["reaction_pt"] == reaction_filter.upper()]
    if pairs.empty:
        return pd.DataFrame(columns=cols)

    a_tbl = pairs.groupby([drug_col, "reaction_pt"]).size()
    if a_tbl.empty:
        return pd.DataFrame(columns=cols)

    idx = a_tbl.index
    drug_idx = idx.get_level_values(0)
    rxn_idx = idx.get_level_values(1)

    a = a_tbl.to_numpy(dtype=float)
    ab = drug_tot.reindex(drug_idx).to_numpy(dtype=float)
    ac = rxn_tot.reindex(rxn_idx).to_numpy(dtype=float)
    b = ab - a
    c = ac - a
    d = float(N) - a - b - c

    with np.errstate(divide="ignore", invalid="ignore"):
        ror = (a * d) / (b * c)
        se = np.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
        ln_ror = np.log(ror)
        ror_lo = np.exp(ln_ror - 1.96 * se)
        ror_hi = np.exp(ln_ror + 1.96 * se)
        prr = (a / ab) / (c / (c + d))
        chi2 = (a * d - b * c) ** 2 * N / (ab * (c + d) * ac * (b + d))
        ic, ic025, ic975 = bcpnn(a, b, c, d)

    # Degenerate tables (an empty margin) cannot yield a ratio.
    bad = (b <= 0) | (c <= 0) | (d <= 0)
    for arr in (ror, ror_lo, ror_hi, prr, chi2):
        arr[bad] = np.nan

    signal = (a >= 3) & np.isfinite(ror_lo) & (ror_lo > 1.0)

    result = pd.DataFrame(
        {
            "drug": drug_idx.to_numpy(),
            "reaction_pt": rxn_idx.to_numpy(),
            "a": a.astype(int),
            "b": b.astype(int),
            "c": c.astype(int),
            "d": d.astype(int),
            "N": int(N),
            "ROR": np.round(ror, 4),
            "ROR_lower": np.round(ror_lo, 4),
            "ROR_upper": np.round(ror_hi, 4),
            "PRR": np.round(prr, 4),
            "chi2": np.round(chi2, 4),
            "IC": np.round(ic, 4),
            "IC025": np.round(ic025, 4),
            "IC975": np.round(ic975, 4),
            "signal": signal,
        }
    )
    result["tier"] = [
        tier(v) if n >= 3 else "none" for v, n in zip(result["IC025"], result["a"])
    ]
    result = result.sort_values("IC025", ascending=False, na_position="last")
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# API-based scoring (for M6 validation against population counts)
# ---------------------------------------------------------------------------

def tier(ic025: float | None) -> str:
    """
    Grade the strength of evidence from IC025.

    A binary pass/fail on ROR_lower > 1 is close to useless against 20.7M
    reports: the confidence interval is narrow enough that trivial elevations
    clear it, and 79% of measured pairs did. IC025 bands separate "reported
    somewhat more than expected" from "reported far more than expected", which
    is the distinction a reader actually needs.

    These are thresholds on a reporting ratio. None of them indicates cause.
    """
    if ic025 is None or (isinstance(ic025, float) and np.isnan(ic025)):
        return "none"
    if ic025 > 2.0:
        return "strong"
    if ic025 > 1.0:
        return "moderate"
    if ic025 > 0.0:
        return "weak"
    return "none"


def _load_bg_cache() -> dict[str, int]:
    if _BG_CACHE.exists():
        try:
            return json.loads(_BG_CACHE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_bg_cache(cache: dict[str, int]) -> None:
    _BG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _BG_CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")


def load_dme() -> list[str]:
    """Serious events checked for every drug regardless of reporting frequency."""
    path = Path(__file__).resolve().parent.parent / "config" / "dme.txt"
    if not path.exists():
        return []
    return [
        line.strip().upper()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def dme_counts(drug_search: str, terms: list[str], chunk: int = 12) -> dict[str, int]:
    """
    Report counts for designated medical events, via the count endpoint.

    Restricting the search to reports containing at least one listed event makes
    those events dominate the response, so a handful of calls recovers counts
    that would otherwise require one call per term. Chunking keeps the matched
    set narrow enough that no target term is pushed past the 100-bucket cap by
    unrelated co-reported reactions.

    **A bucket is only accepted from the chunk that contains its term.** This
    matters and getting it wrong was defect D-09.

    The response for a chunk buckets *every* reaction appearing in the matched
    reports, not only the twelve searched for. So a DME term routinely appears
    in the results of chunks it does not belong to -- as a reaction that
    co-occurs with those chunks' terms. In that position its count is
    reports(drug AND that chunk's terms AND the term), a strict subset of the
    true reports(drug AND the term). The earlier version tested membership
    against the whole DME list and assigned with `=`, so whichever chunk came
    last silently won.

    Measured on acetaminophen x CARDIAC ARREST: chunk 1 owns the term and
    returns the correct 7,383; chunk 2 reports 1,011 and chunk 4 reports 3,259
    as co-occurrences. 3,259 was the value stored in the scored table. The
    error is always an undercount, and it propagates into b, d, ROR, PRR, chi2
    and IC -- so it understated cell `a` on the most serious events in the
    catalogue, which is the worst possible place for it.

    Restricting to the owning chunk is exactly right rather than merely safer:
    for a term inside the searched group, the group restriction cannot exclude
    any report containing that term.
    """
    from src.client import call, q_reaction, F_REACTION

    found: dict[str, int] = {}
    for i in range(0, len(terms), chunk):
        group = terms[i:i + chunk]
        owned = {t.upper() for t in group}
        clause = "(" + " OR ".join(q_reaction(t) for t in group) + ")"
        try:
            # Explicit limit: without one the endpoint returns its default
            # 100 buckets, and a chunk's co-occurring reactions can push a
            # searched term past that. 1,000 is the ceiling with a key.
            data = call({"search": f"{drug_search} AND {clause}",
                         "count": f"{F_REACTION}.exact",
                         "limit": 1000})
        except Exception:
            continue
        for bucket in data.get("results", []) or []:
            term = str(bucket.get("term", "")).upper()
            if term in owned:
                found[term] = int(bucket.get("count", 0))
    return found


def score_via_counts(
    drug_search: str,
    drug_label: str,
    max_terms: int = 100,
    stoplist: set[str] | None = None,
    grand_total: int = 20_692_690,
    dme_terms: list[str] | None = None,
) -> pd.DataFrame:
    """
    Build an exact, population-level scored table using the count endpoint.

    Background cells come from all 20.7M reports rather than a local sample,
    so the numbers match the validated control figures exactly. Downloading
    the drug's own reports would not give a usable background: every record
    in such a corpus contains the drug, collapsing cell c.

    Cost: 2 + (terms not already cached) API calls. Reaction background totals
    are drug-independent, so the cache is shared across every drug scored.

    Returns a DataFrame with the same columns as score().
    """
    from src.client import counts, q_reaction, F_REACTION

    ab = api_total(drug_search)
    if ab == 0:
        return pd.DataFrame()

    buckets = counts(drug_search, f"{F_REACTION}.exact", limit=max_terms)
    cache = _load_bg_cache()
    stop = {s.upper() for s in (stoplist or set())}

    # Frequency-ranked candidates, then the serious events that ranking hides.
    candidates: dict[str, int] = {}
    for bucket in buckets:
        term = str(bucket.get("term", "")).upper()
        if term:
            candidates[term] = int(bucket.get("count", 0))

    dme_found: dict[str, int] = {}
    if dme_terms:
        dme_found = dme_counts(drug_search, dme_terms)
        for term, n in dme_found.items():
            candidates.setdefault(term, n)

    rows: list[dict] = []
    fetched = 0
    for term, a in candidates.items():
        if not term or term in stop or a == 0:
            continue

        if term in cache:
            ac = cache[term]
        else:
            ac = api_total(q_reaction(term))
            cache[term] = ac
            fetched += 1

        b = max(ab - a, 0)
        c = max(ac - a, 0)
        d = max(grand_total - a - b - c, 0)
        if b == 0 or c == 0 or d == 0:
            continue

        ror, ror_lo, ror_hi = _ror(a, b, c, d)
        ic, ic025, ic975 = bcpnn(a, b, c, d)
        rows.append(
            {
                "drug": drug_label,
                "reaction_pt": term,
                "a": a, "b": b, "c": c, "d": d, "N": grand_total,
                "ROR": round(ror, 4), "ROR_lower": round(ror_lo, 4),
                "ROR_upper": round(ror_hi, 4),
                "PRR": round(_prr(a, b, c, d), 4),
                "chi2": round(_chi2(a, b, c, d), 4),
                "IC": round(ic, 4), "IC025": round(ic025, 4),
                "IC975": round(ic975, 4),
                "signal": bool(a >= 3 and ror_lo > 1.0),
                "tier": tier(ic025) if a >= 3 else "none",
                "dme": term in dme_found,
            }
        )

    if fetched:
        _save_bg_cache(cache)

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result.sort_values("IC025", ascending=False).reset_index(drop=True)


def score_from_api(drug_search: str, reaction_pt: str) -> dict:
    """
    Compute ROR using API total() calls for comparison mode (M6 validation).

    Uses the full FAERS population as the background, not the local DataFrame.
    This allows validation when only a subset has been fetched locally.

    Args:
        drug_search: OpenFDA search string for the drug (e.g. pharm_class filter).
        reaction_pt: Reaction MedDRA PT to test.

    Returns:
        dict with keys: a, b_proxy, c, d_proxy, N_total, ROR, ROR_lower, ROR_upper.
        Note: b and d are approximated from totals (not exact pairs).
    """
    from src.client import total as api_total, q_reaction

    N_total = 20_692_690  # Grand total as of 2026-07-30

    rxn_search = q_reaction(reaction_pt)

    # a: drug AND reaction
    drug_and_rxn = f"({drug_search}) AND {rxn_search}"
    a = api_total(drug_and_rxn)

    # a+b: drug total
    drug_total = api_total(drug_search)

    # a+c: reaction total
    ac = api_total(rxn_search)

    b = max(drug_total - a, 0)
    c = max(ac - a, 0)
    d = max(N_total - a - b - c, 0)

    ror, ror_lo, ror_hi = _ror(a, b, c, d)
    ic, ic025, ic975 = bcpnn(a, b, c, d)

    return {
        "drug_search": drug_search,
        "reaction_pt": reaction_pt,
        "a": a,
        "b_proxy": b,
        "c": c,
        "d_proxy": d,
        "N_total": N_total,
        "ROR": round(ror, 4) if not np.isnan(ror) else None,
        "ROR_lower": round(ror_lo, 4) if not np.isnan(ror_lo) else None,
        "ROR_upper": round(ror_hi, 4) if not np.isnan(ror_hi) else None,
        "IC": round(ic, 4) if not np.isnan(ic) else None,
        "IC025": round(ic025, 4) if not np.isnan(ic025) else None,
        "IC975": round(ic975, 4) if not np.isnan(ic975) else None,
    }
