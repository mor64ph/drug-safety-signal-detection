"""
M8: Interaction signals -- reactions reported for a drug PAIR beyond what
either drug alone would predict.

    NOT SHIPPED. This module fails its own known-answer controls and its
    output is not exposed in the application. Kept, with its results, because
    the negative finding is worth more than a silent deletion.

WHAT HAPPENED

Run against twelve drug pairs with established interactions, the measure failed
every one of them, and the failures were not marginal:

    warfarin + NSAID      x GI HAEMORRHAGE      n=1,565   omega025 -1.19
    opioid + benzodiazepine x RESPIRATORY DEPRESSION n=513 omega025 -2.94
    ACE inhibitor + NSAID x ACUTE KIDNEY INJURY  n=1,454   omega025 -1.78
    simvastatin + clarithromycin x RHABDOMYOLYSIS  n=322   omega025 -0.39

Opioid plus benzodiazepine carries a boxed warning on both classes for
respiratory depression specifically. Counts are ample, so this is not the
sparse-cell problem the module was designed around.

WHY

The multiplicative independence baseline becomes unreachable when both drugs are
already strongly associated with the reaction on their own. Warfarin raises
reported GI bleeding; NSAIDs raise reported GI bleeding; multiplying the two
predicts that 9.78% of reports naming both should mention it. The observed
figure is 4.56% -- far above background, clinically real, and less than half of
what the model demands. A genuine interaction is therefore scored negative.

What did clear the bar is worse than what did not. warfarin + NSAID x SEPSIS
reached omega025 3.71, which is hospitalised patients on both drugs rather than
any interaction, and methotrexate + NSAID surfaced ALOPECIA and NASOPHARYNGITIS,
which are methotrexate's own effects. The measure inverts: it suppresses the
real interactions and promotes the confounded ones.

WHAT WOULD BE NEEDED

An additive baseline rather than a multiplicative one, since the relevant
question is whether combined risk exceeds the sum of the parts, not the product.
That is a different estimator requiring its own validation against these same
twelve pairs, and it is not in place. Until it passes them, no interaction
output should reach a user.

Method as implemented: the Omega shrinkage measure (Noren et al. 2008).

This is the part that speaks to polypharmacy, which is where real harm
concentrates: the elderly patient on eight medicines is the archetypal adverse
reaction case and the archetypal trial exclusion.

Method: the Omega shrinkage measure (Noren et al. 2008), which is what the
Uppsala Monitoring Centre uses for interaction detection in VigiBase. It
compares the observed triple count against what independence predicts, then
shrinks the result toward zero so that sparse cells -- which dominate here --
cannot manufacture large values.

No record-level corpus is needed. Of the seven quantities the formula requires,
five already exist in the scored table and the reaction background cache; only
the two involving both drugs at once must be fetched, so a candidate triple
costs two API calls.

    n_111  reports naming drug A, drug B and reaction R      <- 1 call
    n_11.  reports naming drug A and drug B                  <- 1 call
    n_1.1  reports naming drug A and R                       <- have (cell a)
    n_.11  reports naming drug B and R                       <- have (cell a)
    n_1..  reports naming drug A                             <- have (a + b)
    n_.1.  reports naming drug B                             <- have (a + b)
    n_..1  reports naming R                                  <- have (a + c)
"""

from __future__ import annotations

import math

import pandas as pd

from src.client import total as api_total, q_reaction

GRAND_TOTAL = 20_692_690

# Below this, an interaction figure describes a handful of forms rather than a
# pharmacological pattern, and should not be published at all.
MIN_TRIPLE = 5


def expected_triple(
    n_11: int, n_1r: int, n_br: int,
    n_a: int, n_b: int, n_r: int,
    N: int = GRAND_TOTAL,
) -> float:
    """
    Reports expected to name both drugs and the reaction, if the pair added
    nothing beyond each drug's own association with that reaction.
    """
    denom = n_a * n_b * n_r
    if denom == 0:
        return 0.0
    return (n_11 * n_1r * n_br) / denom * N


def omega(n_111: int, expected: float) -> tuple[float, float]:
    """
    Omega and its shrunk lower bound (Noren 2008).

        omega    = log2((n + 0.5) / (E + 0.5))
        omega025 = omega - 3.3*(n+0.5)^-1/2 - 2*(n+0.5)^-3/2

    The penalty terms are large at small n by design. A triple of 3 reports
    cannot clear omega025 > 0 however extreme its raw ratio, which is the point:
    interaction cells are sparse almost by definition, and an unshrunk measure
    would report an interaction for nearly every pair of drugs that are commonly
    co-prescribed.
    """
    if expected <= 0:
        return float("nan"), float("nan")
    w = math.log2((n_111 + 0.5) / (expected + 0.5))
    penalty = 3.3 * (n_111 + 0.5) ** -0.5 + 2 * (n_111 + 0.5) ** -1.5
    return w, w - penalty


def score_pair(
    drug_a: str, search_a: str,
    drug_b: str, search_b: str,
    scored: pd.DataFrame,
    bg_totals: dict[str, int],
    top_n: int = 20,
    min_triple: int = MIN_TRIPLE,
) -> pd.DataFrame:
    """
    Score reactions for a drug pair, over reactions already strong for either.

    Candidate reactions are the highest-ranked ones for A or B rather than all
    of MedDRA, because every additional candidate costs a call and an untargeted
    sweep would spend thousands to find nothing.
    """
    rows_a = scored[scored["drug"] == drug_a]
    rows_b = scored[scored["drug"] == drug_b]
    if rows_a.empty or rows_b.empty:
        return pd.DataFrame()

    n_a = int(rows_a.iloc[0]["a"]) + int(rows_a.iloc[0]["b"])
    n_b = int(rows_b.iloc[0]["a"]) + int(rows_b.iloc[0]["b"])

    n_11 = api_total(f"({search_a}) AND ({search_b})")
    if n_11 < min_triple:
        return pd.DataFrame()

    a_map = dict(zip(rows_a["reaction_pt"], rows_a["a"]))
    b_map = dict(zip(rows_b["reaction_pt"], rows_b["a"]))
    shared = [pt for pt in a_map if pt in b_map]
    ranked = (
        scored[scored["reaction_pt"].isin(shared)
               & scored["drug"].isin([drug_a, drug_b])]
        .sort_values("IC025", ascending=False)["reaction_pt"].drop_duplicates()
        .head(top_n).tolist()
    )

    out = []
    for pt in ranked:
        n_r = bg_totals.get(pt)
        if not n_r:
            continue
        n_111 = api_total(f"({search_a}) AND ({search_b}) AND {q_reaction(pt)}")
        if n_111 < min_triple:
            continue
        exp = expected_triple(n_11, int(a_map[pt]), int(b_map[pt]), n_a, n_b, n_r)
        w, w025 = omega(n_111, exp)
        if math.isnan(w):
            continue
        out.append({
            "drug_a": drug_a, "drug_b": drug_b, "reaction_pt": pt,
            "n_both_drugs": n_11, "n_triple": n_111,
            "expected": round(exp, 2),
            "omega": round(w, 4), "omega025": round(w025, 4),
            "a_alone": int(a_map[pt]), "b_alone": int(b_map[pt]),
            "interaction": bool(w025 > 0),
        })

    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).sort_values("omega025", ascending=False).reset_index(drop=True)


def summarise(df: pd.DataFrame) -> str:
    """
    Describe the result, including the case where there is nothing to report.

    Returning "the counts are too thin to support this" is a legitimate outcome
    and a better one than publishing ratios built on four reports.
    """
    if df.empty:
        return ("No interaction signals: every candidate triple fell below the "
                f"minimum of {MIN_TRIPLE} reports. The data does not support an "
                "interaction claim for this pair, which is a finding about the "
                "data rather than evidence that no interaction exists.")
    n = int(df["interaction"].sum())
    if n == 0:
        return (f"{len(df)} triples measured, none clearing omega025 > 0. "
                "Co-reporting is consistent with each drug's own association.")
    return (f"{n} of {len(df)} triples exceed what independence predicts "
            f"(omega025 > 0). Sparse cells make these weaker than the "
            f"single-drug results; treat as the thinnest evidence here.")
