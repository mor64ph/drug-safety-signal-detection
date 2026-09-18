# Findings and measurement notes

What this pipeline actually produced, and the failure modes worth knowing about
before trusting a number from the openFDA API. Every figure here came out of
this codebase rather than from the literature.

Moved out of `README.md`, which had grown into a research write-up.

- [The controls reproduce](#the-controls-reproduce)
- [A ratio is not a risk](#a-ratio-is-not-a-risk-and-a-large-ratio-is-not-a-large-risk)
- [The interaction module failed and is not shipped](#the-interaction-module-failed-its-controls-and-is-not-shipped)
- [Frequency ranking hides severe events](#frequency-ranking-hides-the-reactions-that-matter-most)
- [Expectedness: the one source that is not FAERS](#expectedness-the-one-source-that-is-not-faers)
- [The bias layer](#the-bias-layer)
- [The withdrawn drug ranks first](#the-withdrawn-drug-ranks-first-for-the-right-reason-and-the-wrong-one)
- [API traps that return zero without an error](#api-traps-that-return-zero-without-an-error)
- [Analysis decisions that change the output](#analysis-decisions-that-change-the-output)

---

## The controls reproduce

| Control | Expected | Measured | |
|---|---|---|---|
| Statin × rhabdomyolysis | ~12.96 | **12.96** [12.65–13.27] | pass |
| Statin × rhabdomyolysis, last 12 months | stable | 12.73 | pass |
| GLP-1 × impaired gastric emptying | elevated | **37.98** | pass |
| GLP-1 × nausea | elevated | 4.10 | pass |
| GLP-1 × headache | must not signal | no signal | pass |

The statin list also reconstructed the known mechanism without being told it:
rhabdomyolysis (12.96), then blood creatine phosphokinase increased (6.88, the
laboratory marker of muscle breakdown), then acute renal failure (5.08, which
rhabdomyolysis causes), then myalgia. That chain is the reason to believe the
rest of the output is worth reading.

## A ratio is not a risk, and a large ratio is not a large risk

Nausea is a genuine, well-documented GLP-1 class effect and it scores 4.10.
Impaired gastric emptying scores 37.98. The difference is not that one is more
real; it is that nausea appears in 3.76% of all 20.7 million reports, so a
common term cannot produce a large ratio no matter how strong the association.
Meanwhile 44% of every impaired-gastric-emptying report in FAERS involves a
GLP-1 agonist.

**Ranking by raw ratio is therefore wrong**, and the tool does not do it. A pair
with 9 reports can outrank one with 8,820. Ranking uses `IC025`, a lower
credible bound that penalises small counts, and the report count `a` is shown
beside every score so it cannot be ignored.

`IC025` was originally computed as `IC - 1.96*sqrt(var_IC)` — a
normal-approximation confidence bound with no prior, which is not BCPNN however
often it is described that way. The consequence was measurable: cerivastatin has
about 366 reports in the whole database, and its 14-report motor-neurone-disease
artifact scored `IC025` **7.94 — the drug's top-ranked row**, ahead of
rhabdomyolysis, the reason the drug was withdrawn. A proper Beta-prior posterior
via digamma/trigamma gives the artifact 3.05 and rhabdomyolysis 4.28, so the
real signal ranks first *before any bias diagnostic runs*.

## The interaction module failed its controls and is not shipped

Drug interactions were the most valuable thing this project could have produced,
because polypharmacy is where harm concentrates. The module was built, run, and
withheld.

Against twelve pairs with established interactions it failed every one:

| Pair | Reaction | n | ω₀₂₅ |
|---|---|---|---|
| warfarin + NSAID | GI haemorrhage | 1,565 | **−1.19** |
| opioid + benzodiazepine | respiratory depression | 513 | **−2.94** |
| ACE inhibitor + NSAID | acute kidney injury | 1,454 | **−1.78** |
| simvastatin + clarithromycin | rhabdomyolysis | 322 | **−0.39** |

Opioid plus benzodiazepine carries a boxed warning on both classes for exactly
that reaction. The counts are ample, so this is not a sparse-data problem.

The cause is the multiplicative independence baseline. Warfarin raises reported
GI bleeding and NSAIDs raise reported GI bleeding, so multiplying the two demands
that 9.78% of reports naming both mention it. The observed figure is 4.56% — far
above background, clinically real, and less than half what the model requires.
Genuine interactions therefore score negative.

What cleared the bar was worse than what did not: warfarin + NSAID × **sepsis**
at ω₀₂₅ 3.71 (hospitalised patients are on both drugs), and methotrexate + NSAID
surfacing alopecia and nasopharyngitis, which are methotrexate's own effects. The
measure inverts — suppressing real interactions while promoting confounded ones.

Fixing it means an additive baseline rather than a multiplicative one, which is a
different estimator needing its own validation against these same twelve pairs.
Until it passes them, no interaction output reaches a user. **A module that
produces plausible-looking numbers and fails on settled facts is worse than no
module**, because its numbers look exactly as authoritative as the ones that
work.

The worksheet answers the interaction question a different way: it quotes the
official label text verbatim. That is a claim a regulator-approved document
makes, not one this tool makes.

## Frequency ranking hides the reactions that matter most

Candidate reactions come from the API's count endpoint, which returns the most
*frequently* reported terms. That ordering systematically buries rare, severe
events beneath common complaints. What a frequency-ranked list of 60 terms missed
completely:

| | | |
|---|---|---|
| fluoroquinolone × tendonitis | ROR 36.4 | boxed warning |
| fluoroquinolone × tendon rupture | ROR 24.1 | boxed warning |
| lamotrigine × Stevens–Johnson syndrome | ROR 22.0 | boxed warning |
| opioid × respiratory depression | ROR 15.2 | the fatal one |
| allopurinol × toxic epidermal necrolysis | ROR 11.5 | often fatal |

Every one is real, labelled and clinically critical, and none appeared until it
was checked deliberately. `config/dme.txt` therefore lists 58 designated medical
events screened against every drug regardless of frequency — the principle behind
the EMA's Designated Medical Event list. For fluoroquinolones the sweep supplies
nine of the top twelve results, which is to say it supplies the drug's actual
safety profile.

The sweep is affordable because of one trick: restricting a search to reports
containing at least one of a dozen named reactions makes those reactions dominate
the response buckets, so one call returns counts for the whole chunk.

**That trick also produced the worst defect in the project.** A chunk's response
buckets *every* reaction in the matched reports, not only the twelve searched
for, so a term appearing as a co-occurrence in another chunk overwrote its own
correct count with a restricted, smaller one — and the last chunk won.
Acetaminophen × cardiac arrest returned 7,383 from its owning chunk, 1,011 from
another and 3,259 from a third; the stored value was 3,259. **3,678 of 14,736
swept pairs were wrong, with corrections up to 119×** (corticosteroid ×
pancytopenia, 492 → 11,600). It corrupted cell `a`, and therefore every measure
derived from it, on the most serious events in the catalogue. A bucket is now
accepted only from the chunk that owns its term.

## Expectedness: the one source that is not FAERS

Every other diagnostic here is computed from FAERS, so every one inherits its
weaknesses. The **drug label** is independent — the regulator-approved statement
of what a drug is known to do — and each reaction is checked against it.

The distinction carries most of the interpretive weight in real
pharmacovigilance. A reaction reported disproportionately *and already printed on
the label* is, ordinarily, the reporting system working: clinicians report what
they have been told to watch for, which is notoriety bias operating as designed.
The same score for a reaction that appears nowhere in the label is a different
object, and the only one that could represent something not yet known.

Verified against settled answers — clozapine × myocarditis, lamotrigine ×
Stevens–Johnson and ciprofloxacin × tendon rupture are all correctly identified
as **boxed warnings**, with clean negatives on implausible pairs.

Matching is necessarily approximate, and the flag reads "not found in label"
rather than "absent from the label". Labels are prose written for clinicians;
MedDRA is a controlled vocabulary. A label reading "delayed gastric emptying" and
a term reading `IMPAIRED GASTRIC EMPTYING` mean the same thing and share no exact
string.

**The label endpoint does not share the event endpoint's schema.** Event queries
are rooted at the report (`patient.drug.openfda.generic_name`); label queries are
rooted at the drug (`openfda.generic_name`). Passing an event path returns HTTP
404 which — combined with error-swallowing added for an unrelated reason — read
as "no label exists" for all 33,852 rows *and reported success*. The check now
raises if fewer than half of drugs resolve a label, because near-total failure is
a bug rather than a finding.

## The bias layer

Each highly ranked pair carries measurements rather than caveats.

**Active comparator** — the ratio recomputed against clinically similar drugs
instead of the whole database. Comparing a diabetes drug against every drug in
FAERS partly measures diabetes. This is the most informative single column:

| Pair | vs all FAERS | vs similar drugs | |
|---|---|---|---|
| Statin × rhabdomyolysis | 12.96 | **5.24** | survives |
| GLP-1 × impaired gastric emptying | 37.98 | **6.93** | survives |
| GLP-1 × blood glucose increased | 12.37 | **32.90** | not drug-specific |
| Cerivastatin × motor neurone disease | 394.49 | **2.15** | collapses |
| Exenatide × device leakage | 36.23 | **1.01** | collapses |

Shrinkage is not failure. Statin rhabdomyolysis falls by 60% and remains a real
signal. Collapse to near 1.0 is the meaningful outcome: it says the original
number was mostly about the illness, the injection device, or the reporting
environment rather than the molecule.

**Indication confounding** — flags reactions that match the reason the drug is
prescribed. Atorvastatin × type 2 diabetes scores 21.5 largely because diabetic
patients are prescribed statins.

**Burstiness** — the share of reports arriving in the single busiest month.
Steady accrual tracks prescribing; a spike tracks attention.

Diagnostics run on the highest-ranked pairs only, since each costs API calls.
Rows show **not assessed** rather than a blank, because a blank reads as a clean
bill of health.

### A negative result worth recording

`primarysource.qualification` does **not** detect litigation-shaped reporting.
The hypothesis was that the cerivastatin artifact would be lawyer- and
consumer-heavy. Measured: lawyer plus consumer is **7.7%** there, against
**33.8%** for atorvastatin × amyotrophic lateral sclerosis and 8.0% for
atorvastatin × rhabdomyolysis, a genuine signal. The artifact has a *lower*
lay-reporter share than the comparison. Mass-tort cases appear to reach FAERS
through the manufacturer and get coded as physician or other health
professional. Do not build a litigation diagnostic on this field; the
notoriety-spike and active-comparator diagnostics are the two that work.

## The withdrawn drug ranks first, for the right reason and the wrong one

Cerivastatin (Baycol) was withdrawn worldwide in 2001 after fatal
rhabdomyolysis. The pipeline, never told this, ranks it highest of all statins
for rhabdomyolysis: **ROR 70.56** against a class average of 12.96.

It also reports cerivastatin × motor neurone disease at **ROR 394 on 14
reports** — higher than the finding that actually got the drug withdrawn. Two
independent diagnostics catch it: the reports arrived in a single notoriety
spike, and against other lipid-lowering drugs the ratio collapses to **2.15**.
This is why the bias layer is not optional.

The two Bayesian measures disagree on this drug, and the disagreement is
expected. `IC025` ranks rhabdomyolysis first and pushes the artifact out of the
top three; `EB05` ranks the artifact **first at 123.07**, because MGPS shrinks
toward an expected count that is near zero for a drug with 366 reports in the
entire database. `IC025` remains the ranking key because it is the measure
validated against the 22 controls.

## API traps that return zero without an error

Each of these produces a plausible, wrong answer with no indication anything
went wrong. They are the reason all query construction is centralised in
`src/client.py`.

**Field paths must be fully qualified from the report root.**
`openfda.pharm_class_epc:"GLP-1 Receptor Agonist [EPC]"` returns **0**. The
correct path, `patient.drug.openfda.pharm_class_epc`, returns 421,338. The API
accepts the wrong one and reports no error.

**Possessive MedDRA terms come back in a form the API will not accept.** The
count endpoint gives `FOURNIER^S GANGRENE`, with a caret where the apostrophe
belongs. Querying that string returns HTTP 400 — raw, backslash-escaped and
wildcard forms alike — and substituting a real apostrophe matches nothing. Only
replacing the caret with a space *and* dropping `.exact` works. MedDRA is full of
possessives and discarding them would discard real findings: **57% of every
Fournier's gangrene report in FAERS names an SGLT2 inhibitor** (ROR 272),
matching the FDA's 2018 safety communication.

**Case sensitivity varies by field.** `reactionmeddrapt.exact` is
case-**insensitive**; `drugindication.exact` is case-**sensitive** and splits one
clinical concept across buckets — `TYPE 2 DIABETES MELLITUS` (54,836) and
`Type 2 diabetes mellitus` (52,081) counted separately. Reading only the first
understates the count by nearly half.

**Class queries need `.exact`; molecule queries must not have it.** A plain
phrase match also matches any *longer* class name containing the phrase, silently
merging distinct pharmacology: `Opioid Agonist [EPC]` plain returns 354,618
against 268,375 exact, because `Partial Opioid Agonist [EPC]` contains the
phrase. But `generic_name.exact` fails outright, because stored values carry
qualifiers such as `ORAL SEMAGLUTIDE`.

**openFDA cannot see withdrawn drugs.** `openfda.generic_name` is derived by
matching reports against *current* product labels, so a drug with no current
label is invisible there. Cerivastatin returns **0** under `generic_name`, but
**200** under `medicinalproduct` and **91** under `activesubstance`. Withdrawn
drugs carry the most important safety lessons in the field, and the obvious query
silently omits them, so molecule queries OR across all three fields.

**Count responses are capped, and the cap is indistinguishable from absence.**
The count endpoint returns at most 1,000 buckets with an API key and 100 without.
A term below the cut comes back absent, which is identical in shape to "never
reported" — so it gets stored as 0. This shipped once: atorvastatin × type 2
diabetes mellitus stored 0 hospitalisations against a true 792, and 0 deaths
against 219, while rhabdomyolysis was correct because it ranked above the cut.
The column was right where the reaction was common and silently zero where it was
not, which is the worst possible shape for a severity figure. A response that
fills the cap is now treated as truncated, and every ambiguous term resolved by a
direct lookup — **4,138 of them**, which is the measure of how often the cap was
hit.

**Never let a helper swallow API failures unconditionally.** Returning 0 on error
fixed one-bad-term-kills-the-batch and created something worse: once the daily
quota is exhausted every call fails, every background count becomes 0, every pair
is skipped, and the run reports success with empty tables. The client now counts
consecutive failures and raises after eight, so isolated bad queries stay
survivable while systemic failure stops the run loudly.

## Analysis decisions that change the output

**Report-level counting.** The 2×2 counts reports, not flattened rows. A report
listing 40 drugs and 47 reactions expands to 1,880 rows; counting those inflates
cell `b` by the reaction count and deflates every ratio by a drug-dependent
factor.

**Population background.** Cells `c` and `d` come from all 20,692,690 reports via
the API rather than a downloaded sample. Downloading the target class would not
provide a usable background at all: in a corpus of GLP-1 reports every record
contains a GLP-1, so `c` collapses and the ratio silently becomes "GLP-1 versus
statins" instead of "GLP-1 versus everything".

**Graded strength, not a binary flag.** The conventional gate — `a ≥ 3` and lower
bound above 1 — fires on **79%** of measured pairs at this sample size, because
20.7 million reports make intervals narrow enough that trivial elevations clear
it. Results are banded by `IC025` instead: strong above 2, moderate above 1, weak
above 0. Negative controls are asserted on `IC025` for the same reason: statins ×
headache is ROR 1.33 / `IC025` 0.36 / weak, against positive controls at 3.32 to
4.32 and strong. The pipeline separates them by a factor of ten; only the binary
flag failed.

**The stoplist is an analysis decision, not housekeeping.**
`config/stoplist.txt` documents every exclusion and why. Dosing errors dominated
the raw GLP-1 ranking — extra dose administered at 24.8, incorrect dose
administered at 10.9 — which is a true finding about pen injectors and
dose-escalation schedules rather than about the molecule. Excluding them changes
what the tool reports, so the reasoning is written down.

**Grouping preferred terms was tried and abandoned.** Rolling related MedDRA
terms into a single concept destroys the signal it is meant to clarify, because a
specific term's disproportionality is diluted by every vague sibling folded in
with it.

---

## Validation and the pipeline's own bugs

Two defects worth recording because they would have passed review:

**Validation ran a different code path from production.** The control harness
used the live API while the served table came from the local parquet, so the
controls could have passed while the displayed numbers stayed wrong. The
validated path and the production path must be the same one.

**A runtime data artifact needs three separate things, and only one fails
loudly:** an exception in `.gitignore`, an exception in `.dockerignore`, and a
`COPY` in the Dockerfile. This was got wrong twice — first the parquet, then the
label-interaction index, where the file was committed *and* copied and the build
still failed with "not found" for a path plainly in the commit.
`scripts/preflight.py` now checks all three.
