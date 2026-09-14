# reportscope — Module Plan

**What we are building:** a tool that takes a drug (or a personal medication list) and
returns the adverse events reported alongside it *far more often than the FAERS
background rate would predict* — ranked, with evidence counts, bias flags, and explicit
uncertainty. Framed throughout as **pairs worth a human looking at**, never as
diagnosis, risk, or advice.

**Focus class:** GLP-1 receptor agonists. **Control class:** statins (known answer).

---

## Dependency graph

```
M0 Primer ✅
   |
M1 Recon ──> M2 Fetch ──> M3 Flatten ──> M4 Normalise ──> M5 Score
                                                            |
                          ┌─────────────────────────────────┤
                          v                 v               v
                     M6 Validate      M7 Bias        M8 Interactions
                          └─────────────────┬───────────────┘
                                            v
                                       M9 Tool ──> M10 Honesty layer
```

M5 is the earliest point where you have "results." **M6 is the earliest point where you
should believe them.** Do not skip from M5 to M9.

---

## M1 — Reconnaissance & feasibility
**Status: ~70% done during setup**

Establish what the API will and will not give us, empirically, and freeze it in writing.

**Already banked:**
- Base `https://api.fda.gov/drug/event.json`, no key needed
- `limit` max **999** without a key (1000 returns `API_KEY_MISSING` — undocumented)
- `skip` max **25,000**, hard. *This is the defining architectural constraint.*
- Rate limit **240/min, 1,000/day** per IP without a key
- `count` returns max **100 buckets** without a key
- Boolean syntax needs **space-encoded** ` AND `, not literal `+AND+`
- Grand total **20,692,690** reports; data current to **2026-07-30**
- `GLP-1 Receptor Agonist [EPC]` = 421,338 reports

**Still to do:**
- Field availability census — what % of records actually carry `patientonsetage`,
  `patientsex`, `drugcharacterization`, `openfda.generic_name`, `receiptdate`
- Resolve the `NOT_FOUND` we hit on one compound AND query (real absence, or quirk?)
- Confirm exact casing of target reaction PTs before hardcoding any of them

**Concept:** *measure the boundaries of an external system before designing against it.*
Most pipeline rewrites come from discovering a hard limit late.

**Exit criteria:** a written constraints table nothing downstream has to re-discover.

---

## M2 — The fetcher
**Deliverable:** `src/fetch.py`

A paginated, cached, resumable, polite client.

**The core problem:** `skip` caps at 25,000 but the GLP-1 slice has 421,338 reports.
Straight pagination **cannot reach past record 25,000.** This is not a bug to work
around later; it dictates the design.

**The solution — partition the query space.** Slice by `receivedate` into windows small
enough that each returns <25,000 records, then paginate within each window:

```
receivedate:[20230101 TO 20230131] -> 8,400 records -> paginate fully
receivedate:[20230201 TO 20230228] -> 9,100 records -> paginate fully
...
```

Adaptive refinement: probe a window's `meta.results.total` *first* (1 cheap request);
if it exceeds 25,000, split the window in half and recurse.

**Must handle:**
- Cache every raw response to `data/raw/` keyed by query hash — never re-fetch while
  iterating downstream. With a 1,000/day budget this is not an optimisation, it is
  what makes the project possible.
- Sleep between calls; exponential backoff with jitter on 429/5xx
- `NOT_FOUND` is a legitimate empty result, **not** an error — do not retry it
- Resumability: killed mid-run, restart skips completed windows

**Concept:** *when an API caps pagination, you partition the query space rather than the
result set.* Plus caching as a correctness tool, not just a speed one.

**Exit criteria:** pull 10,000+ records across multiple date windows without dying or
getting rate-limited; kill it mid-run and confirm it resumes.

---

## M3 — The flattener
**Deliverable:** `src/flatten.py`

Nested JSON -> one row per (report, drug, reaction). **This is most of the work and it
is the part that is actually the job.**

**The combinatorial trap:** a report with 40 drugs and 47 reactions cross-joins to
**1,880 rows**. One patient would then outweigh 1,880 single-drug reports. Handling
this is the single highest-leverage decision in the project:

- Filter to `drugcharacterization = 1` (Suspect) — drops ~26% of drug entries that are
  merely concomitant
- Carry `n_drugs` / `n_reactions` per report so downstream can weight or exclude
- Keep `safetyreportid` on every row so you can always collapse back to reports

**Must survive:** missing `patient.drug`, missing `patient.reaction`, absent `openfda`
block, missing age/sex, `patientonsetageunit` in days/months/years, and the
**undocumented `drugcharacterization = 4`** we found. Never crash — record a null and
count it.

**Concept:** *defensive parsing of semi-structured real-world data, and choosing your
unit of analysis deliberately.* Report-level vs. pair-level is a modelling decision
disguised as a schema decision.

**Exit criteria:** flatten the full cached slice with zero crashes; emit a data-quality
report of null rates per field.

---

## M4 — Normalisation & entity resolution
**Deliverable:** `src/normalise.py`

Make "the same thing" actually equal.

- **Case folding — and it varies by field.** Measured: `reactionmeddrapt.exact` is
  case-INsensitive, but `drugindication.exact` is case-SENSITIVE (`HYPERTENSION`
  408,489 vs `Hypertension` 113,106 — **21.7% lost** if you query one casing), as are
  `medicinalproduct.exact` and `pharm_class_epc.exact`. Test per field; never assume.
  Also: `generic_name.exact` fails in ALL casings because stored values carry
  qualifiers (`ORAL SEMAGLUTIDE`) — use non-exact there.
- **Drug -> molecule.** `OZEMPIC`, `RYBELSUS`, `WEGOVY`, `semaglutide sodium` -> one
  entity. Prefer `openfda.generic_name`; fall back to `activesubstance`, then fuzzy
  match on `medicinalproduct`. **Keep brand as a separate column** — Ozempic vs Wegovy
  is a real population difference we may want back (Primer 2.4).
- **Reaction grouping.** Optional roll-up of related PTs into concepts, every mapping
  hand-written and documented. Report results *both* grouped and ungrouped.
- **Noise stoplist.** `OFF LABEL USE` (semaglutide's #2 "reaction"), `DRUG INEFFECTIVE`,
  `PRODUCT DOSE OMISSION`, `NO ADVERSE EVENT` are reporting artifacts, not events.

**Concept:** *entity resolution, and the fact that every normalisation choice is a
finding-altering decision that belongs in the methods section.*

**Exit criteria:** a documented mapping file; before/after counts showing what merged.

---

## M5 — The disproportionality engine
**Deliverable:** `src/score.py`

Build the 2x2 for every pair and score it.

|  | Reaction R | Not R |
|---|---|---|
| **Drug D** | a | b |
| **Not D** | c | d |

- **ROR** = ad/bc, with a 95% CI via `SE(ln ROR) = sqrt(1/a + 1/b + 1/c + 1/d)`
- **PRR** and chi-square for cross-checking
- **IC / IC025** — Bayesian shrinkage. **Do not rank on raw ROR.** A pair with a=3 can
  show ROR=50 on pure noise; IC025 shrinks it toward zero. This is what WHO uses and
  it is the main statistical upgrade over the original brief.
- Screening gate: `a >= 3` AND `ROR_lower_CI > 1`

**The design fork worth understanding:** the `c` and `d` cells can come from either
(i) the local flattened sample, or (ii) exact API totals via `meta.results.total`.
Option (ii) is unbiased and covers all 20.7M reports; option (i) is free and fast.
**Do both and compare** — the gap between them is itself a finding about sampling bias,
and it costs one extra query per pair.

**Concept:** *a disproportionality score is four numbers in a formula, not a model — but
choosing the comparator and the estimator is where the judgement lives.*

**Exit criteria:** ranked pair table; sample-based and population-based RORs compared.

---

## M6 — Validation harness ⭐
**Deliverable:** `src/validate.py`

**The module that separates a real detector from a ranked list of noise.** Nothing in
the original brief. Do not skip it.

**Positive controls** — known-true signals the pipeline *must* reproduce:
- statins x rhabdomyolysis -> **ROR ~= 12.96** (already computed by hand; target)
- GLP-1 x nausea/vomiting -> strongly elevated (mechanistically predicted, Primer 2.2)

**Negative controls** — pairs that must *not* rank high:
- GLP-1 x an unrelated event with no plausible mechanism
- Any drug x `DRUG INEFFECTIVE` (artifact, should be stoplisted)

**Also:** re-run on a held-out date window. A real pharmacological signal is stable
across time windows; an artifact often is not.

**Concept:** *you cannot evaluate an unsupervised method without planting known answers
in it.* If statins/rhabdomyolysis does not come back ~13, every other number is
untrustworthy — and you would never know from the output alone.

**Exit criteria:** controls pass within tolerance, written up as a table.

---

## M7 — Bias diagnostics
**Deliverable:** `src/bias.py`

Attach a *why you should doubt this* profile to every surfaced pair.

- **Notoriety detector.** Reports per month for the pair. Steady accrual tracks real
  uptake; a sharp spike suggests a news cycle or litigation campaign. Quantify
  burstiness (e.g. max-month share of total) and **flag it in the output**.
- **Indication confounding.** If the reaction PT closely matches a common
  `drugindication` for that drug, flag it. Catches the diabetes-complications trap.
- **Active comparator.** Recompute ROR against *other antidiabetics* instead of all of
  FAERS. Expect signals to shrink. **The smaller number is the more honest one.**
- **Stratification** by age band and sex — check for Simpson's paradox.

**Concept:** *quantifying your own confounders and shipping them next to the result,
rather than disclaiming them in prose.* This is the difference between a caveat and a
measurement.

**Exit criteria:** every top-ranked pair carries bias flags and a trend sparkline.

---

## M8 — Interaction signals
**Deliverable:** `src/interactions.py`

The genuinely novel piece: reactions reported for a drug **pair** beyond what either
drug alone predicts. Directly serves polypharmacy, which is where real harm clusters.

Compare observed co-reporting against a multiplicative-independence baseline; flag
large positive departures. Small cells are the enemy here, so shrinkage from M5 and the
`a >= 3` gate matter more, not less.

**Concept:** *interaction effects on sparse contingency data, and the discipline to
stay quiet when cells are too small.*

**Exit criteria:** interaction table with the same controls and flags applied. If the
counts are too thin to support it honestly, **say so and ship without it** — that is a
legitimate outcome, not a failure.

---

## M9 — The lookup tool
**Deliverable:** `src/app.py` (Flask — already installed)

Enter a drug or a med list -> ranked "reported far more than expected" profile, with
evidence counts, bias flags, trend, and stratification.

Serves precomputed results from M5-M8; no live API calls on the request path (the
1,000/day cap makes that impossible anyway).

**Design constraints, non-negotiable:**
- The limitation framing is **in the primary view**, not hidden behind a link
- Show **counts alongside every score** — `n=12` reads very differently from `n=1,204`
- Never rank by raw ROR alone; never present a number without its uncertainty
- No diagnosis, no recommendation, no "dangerous drug" language anywhere

**Concept:** *interface design as an honesty mechanism.* A UI that shows ROR=34 in large
type and the caveat in small type is a misleading artifact regardless of correct maths.

---

## M10 — The honesty layer
**Deliverable:** `README.md` — the **"What this does not prove"** section

Per the brief, the single most important paragraph in the project. Ours can go further
than prose, because M6 and M7 give us *measured* limitations: the sample-vs-population
ROR gap, the active-comparator shrinkage, the burstiness flags.

Must state plainly: voluntary unvalidated reports; no denominator so no rates; reporting
shaped by drug age, media, and litigation; duplicates present; and the one-sentence
test — **"a high score is a hypothesis because it measures reporting behaviour, not
physiology, and nothing here rules out the hundred non-causal reasons two words co-occur
on a form."**

---

## Definition of done (from the brief, plus ours)

| # | Criterion | Module |
|---|---|---|
| 1 | Pull 10,000+ records without dying or getting rate-limited | M2 |
| 2 | Flattening handles missing fields instead of crashing | M3 |
| 3 | Explain in one sentence why a high score is a hypothesis | M10 |
| 4 | **Reproduce a known signal within tolerance** | M6 |
| 5 | **Every surfaced pair ships with its bias flags** | M7 |
| 6 | **A non-technical person can use it without being misled** | M9 |
