# rxsignal

A lookup tool over the FDA Adverse Event Reporting System (FAERS). Give it a drug
and it returns the adverse events reported alongside that drug far more often than
the rest of the database would predict — ranked, with report counts, confidence
intervals, and a measured profile of the reasons each result might be wrong.

It is a screening instrument. Every result is a pair worth a human looking at, and
nothing more than that.

**Focus class:** GLP-1 receptor agonists (semaglutide, liraglutide, dulaglutide,
exenatide, tirzepatide, lixisenatide).
**Control class:** statins, where the answer is already known.

---

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env        # then paste an openFDA API key into it
python run.py --score       # score drugs, check labels, attach bias flags
python run.py --validate    # check against 13 known answers
python run.py --interactions# score curated drug pairs (M8)
python run.py --serve       # http://127.0.0.1:5000
```

**Get an API key.** It is free, instant, and needs no approval:
<https://open.fda.gov/apis/authentication/>. Without one the ceiling is 1,000
requests per day; with one it is 120,000, and the count-bucket cap rises from 100
to 1,000. A full run over 361 drugs needs roughly 20,000 requests, so the label
and bias passes are not practical unauthenticated.

`--score` resumes. Drugs already in `data/results/` are skipped, so an
interrupted run loses nothing — which matters, because several have been
interrupted.

---

## What this does not prove

This is the most important section. Read it before the numbers, not after.

**The denominator does not exist.** FAERS records reports, not patients. Nothing in
it says how many people took a drug and were fine. Every rate, risk, and incidence
figure you have ever seen requires that number, so none of them can be computed
here — not approximately, not with better methods, not at all. The ratios in this
tool compare reports against reports. They describe the contents of a filing
cabinet, not the human body.

**Reporting is voluntary and unvalidated.** Nobody checks these reports. Around 48%
come from consumers rather than clinicians, and a coded term such as
`IMPAIRED GASTRIC EMPTYING` may mean a gastroenterologist confirmed it with a gastric
emptying study, or that somebody used the word after reading about it online. Both
produce the same database row.

**Reporting responds to attention, not only to biology.** New drugs are reported more
than old ones at identical true risk. A published study, a news cycle, or a law firm
advertising for plaintiffs will all move these numbers, and none of them changes what
the drug does. The tool measures this directly rather than warning about it: see
burstiness below.

**Duplicates are present.** FAERS de-duplication is imperfect. The same patient can
appear more than once, particularly in litigation-heavy periods.

**Everything here is uncontrolled.** Randomised trials control for measured and
unmeasured differences between groups; cohort and case-control studies control for
measured ones. Disproportionality controls for nothing, because the data does not
contain the information required — you cannot adjust for a variable that is absent
from most records. Its single advantage is that it screens every drug against every
reaction continuously at almost no cost, which is a real and substantial advantage,
and the only one.

**The one-sentence test.** A high score is a hypothesis because it measures reporting
behaviour rather than physiology, and nothing here rules out the hundred non-causal
reasons two words end up on the same form.

---

## What the measurements actually showed

These are not illustrations. They came out of this pipeline.

### The controls reproduce

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
rhabdomyolysis causes), then myalgia. That chain is the reason to believe the rest
of the output is worth reading.

### A ratio is not a risk, and a large ratio is not a large risk

Nausea is a genuine, well-documented GLP-1 class effect, and it scores 4.10.
Impaired gastric emptying scores 37.98. The difference is not that one is more
real; it is that nausea appears in 3.76% of all 20.7 million reports, so a common
term cannot produce a large ratio no matter how strong the association. Meanwhile
44% of every impaired-gastric-emptying report in FAERS involves a GLP-1 agonist.

**Ranking by raw ratio is therefore wrong**, and the tool does not do it. A pair
with 9 reports can outrank one with 8,820. Ranking uses IC025, a lower confidence
bound that penalises small counts, and the report count `a` is shown beside every
score so it cannot be ignored.

### The interaction module failed its controls and is not shipped

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
that 9.78% of reports naming both mention it. The observed figure is 4.56% —
far above background, clinically real, and less than half what the model
requires. Genuine interactions therefore score negative.

What cleared the bar was worse than what did not: warfarin + NSAID × **sepsis**
at ω₀₂₅ 3.71 (hospitalised patients on both drugs), and methotrexate + NSAID
surfacing alopecia and nasopharyngitis, which are methotrexate's own effects.
The measure inverts — suppressing real interactions while promoting confounded
ones.

Fixing it means an additive baseline rather than a multiplicative one, which is a
different estimator needing its own validation against these same twelve pairs.
Until it passes them, no interaction output reaches a user. **A module that
produces plausible-looking numbers and fails on settled facts is worse than no
module**, because the numbers look exactly as authoritative as the ones that work.

### Ranking candidates by frequency hides the reactions that matter most

Candidate reactions for each drug come from the API's count endpoint, which
returns the most *frequently* reported terms. That ordering systematically buries
rare, severe events beneath common complaints. Measured examples of what a
frequency-ranked list of 60 terms missed completely:

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

### Expectedness: the one source that is not FAERS

Every other diagnostic here is computed from FAERS, so every one inherits its
weaknesses. The **drug label** is independent — the regulator-approved statement
of what a drug is known to do — and each reaction is checked against it.

The distinction carries most of the interpretive weight in real
pharmacovigilance. A reaction reported disproportionately *and already printed on
the label* is, in the ordinary case, the reporting system working: clinicians
report what they have been told to watch for, which is notoriety bias operating
as designed. The same score for a reaction that appears nowhere in the label is a
different object, and the only one that could represent something not yet known.

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
404, which — combined with error-swallowing added for a different reason — read
as "no label exists" for all 33,852 rows and reported success. The check now
raises if fewer than half of drugs resolve a label, because near-total failure is
a bug rather than a finding.

### openFDA returns possessive terms in a form it will not accept back

The count endpoint gives `FOURNIER^S GANGRENE`, with a caret where the apostrophe
belongs. Querying that string returns HTTP 400 — raw, backslash-escaped, and
wildcard forms alike — and substituting a real apostrophe matches nothing. Only
replacing the caret with a space and dropping `.exact` works. MedDRA is full of
possessives, and discarding them would discard real findings: **57% of every
Fournier's gangrene report in FAERS names an SGLT2 inhibitor** (ROR 272), matching
the FDA's 2018 safety communication.

### The withdrawn drug ranks first, for the right reason and the wrong one

Cerivastatin (Baycol) was withdrawn worldwide in 2001 after fatal rhabdomyolysis.
The pipeline, never told this, ranks it highest of all statins for rhabdomyolysis:
**ROR 70.56** against a class average of 12.96.

It also reports cerivastatin × motor neurone disease at **ROR 394** on 14 reports —
higher than the finding that actually got the drug withdrawn. Two independent
diagnostics catch it: the reports arrived in a single notoriety spike, and against
other lipid-lowering drugs the ratio collapses from 394 to **2.15**. This is what
litigation-shaped reporting looks like from the inside, and it is why the bias layer
is not optional.

### openFDA cannot see withdrawn drugs

`openfda.generic_name` is derived by matching reports against *current* product
labels. A drug with no current label is therefore invisible there. Cerivastatin
returns **0** under `generic_name`, but **200** under `medicinalproduct` and **91**
under `activesubstance`. The tool searches all three. Withdrawn drugs carry the most
important safety lessons in the field, and the obvious query silently omits them.

### Field paths fail silently

`openfda.pharm_class_epc:"GLP-1 Receptor Agonist [EPC]"` returns **0**. The correct
path is `patient.drug.openfda.pharm_class_epc`, which returns 421,338. The API
accepts the wrong one and reports no error. So does `Biguanide [EPC]`, which is not
a string this data contains, and a literal `+AND+` instead of a space-encoded one.
Three ways to get zero results and no indication that anything went wrong. Query
construction is centralised in `src/client.py` for this reason.

### Case sensitivity varies by field

`reactionmeddrapt.exact` is case-**insensitive**. `drugindication.exact` is case-
**sensitive**, and splits one clinical concept across buckets: GLP-1 indications
return `TYPE 2 DIABETES MELLITUS` (54,836) and `Type 2 diabetes mellitus` (52,081)
separately. Reading only the first understates the count by nearly half. The
indication diagnostic case-folds and merges before use.

---

## The bias layer

Each top-ranked pair carries measurements rather than caveats.

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
signal. Collapse to near 1.0 is the meaningful outcome, and it says the original
number was mostly about the illness, the injection device, or the reporting
environment rather than the molecule.

**Indication confounding** — flags reactions that match the reason the drug is
prescribed. Atorvastatin × type 2 diabetes scores 21.5 largely because diabetic
patients are prescribed statins; cerivastatin surfaces hyperlipidaemia, which is
the indication itself.

**Burstiness** — the share of reports arriving in the single busiest month. Steady
accrual tracks prescribing; a spike tracks attention.

Diagnostics run on the highest-ranked pairs only, since each costs API calls. Rows
show **not assessed** rather than a blank, because a blank reads as a clean bill of
health.

---

## Design decisions that change the results

**Report-level counting.** The 2×2 table counts reports, not flattened rows. A report
listing 40 drugs and 47 reactions expands to 1,880 rows; counting those would inflate
cell `b` by the number of reactions per report and deflate every ratio by a factor
that varies between drugs. See `src/score.py`.

**Population background.** Cells `c` and `d` come from all 20,692,690 reports via the
API rather than from a downloaded sample. Downloading the target class would not
provide a usable background at all: in a corpus of GLP-1 reports, every record
contains a GLP-1, so `c` collapses and the ratio silently becomes "GLP-1 versus
statins" instead of "GLP-1 versus everything".

**Graded strength, not a binary flag.** The conventional gate — `a ≥ 3` and lower
confidence bound above 1 — fires on 79% of measured pairs at this sample size,
because 20.7 million reports make confidence intervals narrow enough that trivial
elevations clear it. Results are banded by IC025 instead: strong (>2), moderate (>1),
weak (>0).

**The stoplist is an analysis decision.** `config/stoplist.txt` documents every
exclusion and why. Dosing errors dominated the raw GLP-1 ranking — extra dose
administered at 24.8, incorrect dose administered at 10.9 — which is a true finding
about pen injectors and dose-escalation schedules, not about the molecule. Excluding
them changes what the tool reports, so the reasoning is written down.

---

## Modules

| | Module | Purpose |
|---|---|---|
| M1 | `src/client.py` | TLS-safe API client, centralised query construction |
| M2 | `src/fetch.py` | Date-partitioned paginator around the hard 25,000 skip cap |
| M3 | `src/flatten.py` | Nested JSON to one row per report × drug × reaction |
| M4 | `src/normalise.py` | Stoplist, groupings, molecule resolution |
| M5 | `src/score.py` | ROR, PRR, chi², IC, IC025 |
| M6 | `src/validate.py` | Positive and negative controls |
| M7 | `src/bias.py` | Comparator, indication, notoriety diagnostics |
| M8 | `src/interactions.py` | Drug-pair signals beyond independence |
| M9 | `src/app.py` | Flask lookup tool |
| M10 | this file | What the numbers do not mean |

`M2`/`M3` build a local record-level corpus for work needing individual reports.
The scored table served by the app is built from population-level API counts and
does not require it.

---

## Data

openFDA drug/event endpoint — 20,692,690 reports, current to 2026-07-30. FAERS is
US-centric, which is not neutral: US prescribing, US hospitalisation thresholds, and
US direct-to-consumer advertising and litigation all shape what gets reported.

Not affiliated with the FDA. Not a medical device. Not advice.
