# reportscope — project handoff

Written for someone picking this up cold: a new engineer, or a fresh Claude
conversation. It is deliberately dense and contains no credentials.

Paste or attach this to start a new conversation with full context. It replaces
reading the build history, which is ~9.7M tokens and cannot be transferred.

---

## 1. What it is

A pharmacovigilance signal-detection tool over FDA FAERS (the Adverse Event
Reporting System) — 20,692,690 reports, extract of **30 July 2026**.

Live: `https://reportscope.onrender.com`
Repos: `github.com/mor64ph/ReportScope` (private, wired to the live site) and
`github.com/mor64ph/drug-safety-signal-detection` (public, portfolio — history
rewritten, so **never** add its remote to the private working copy).

Stack: Flask + Waitress in Docker on Render (free plan, ~45s cold start),
Neon Postgres for accounts, Alembic migrations, Gmail SMTP, GitHub Actions for
the weekly digest. Scored data is a committed parquet baked into the image —
**the web process makes no outbound API calls at request time.**

| | |
|---|---|
| Drugs | 361 (305 molecules, 56 classes, 23 therapeutic areas) |
| Scored pairs | 33,852 |
| Bias diagnostics | 100% coverage |
| Known-answer controls | 22/22 passing |

## 2. Why it is not just another FAERS calculator

Naive analysis of FAERS produces confident nonsense — statins look like they
cause diabetes, because diabetics are prescribed statins. The differentiator is
**the bias layer**: every result carries measured reasons it might be wrong.

- **Indication confounding** — the "reaction" is the condition being treated
- **Notoriety burstiness** — reports arrived in one litigation-driven spike
- **Active comparator** — recomputed against clinically similar drugs.
  **1,074 pairs collapse by ≥90%** and are labelled as such
- **Expectedness** — cross-referenced against the official FDA label

Competitors: OpenVigil 2 (PRR/ROR/MGPS/BCPNN, a researcher's tool, no bias
diagnostics, nothing patient-facing) and WHO VigiAccess (150+ countries but
documented as lacking demographic analysis of specific ADRs and cross-drug
comparison). Neither ships a bias layer or a patient-facing worksheet.

## 3. Statistics — three measures, and which one ranks

| Measure | What it is | Role |
|---|---|---|
| ROR + 95% CI | odds ratio, no prior | shown |
| PRR, χ² | cross-checks | stored |
| **IC025** (BCPNN) | log2 lower bound, prior fixed in advance | **the ranking key** |
| EB05 (MGPS) | lower bound, prior fitted from this table; FDA's own screen | shown |

**IC025 ranks because it is the measure validated against the 22 controls.**

Tiers are bands on IC025: strong >2, moderate >1, weak >0. A binary
`ROR_lower > 1` gate fires on **79%** of pairs at N=20.7M and is useless.

Anchor (must reproduce or the pipeline is broken):
**statins × RHABDOMYOLYSIS → ROR 12.95, IC025 3.3152, IC975 3.3821,
EBGM 10.20, EB05 10.02.**

### The two Bayesian methods disagree, on purpose

Cerivastatin (withdrawn 2001, only ~366 reports in the whole database) has a
14-report ALS row that is a known litigation artifact.

- **IC025 ranks RHABDOMYOLYSIS first** (the real withdrawal reason) and pushes
  ALS out of the top three.
- **EB05 ranks ALS first (123.07)**, because MGPS shrinks toward an expected
  count that is near zero for a drug that small.

Neither is wrong; they answer different questions. This is test case S-14 — if
the pattern changes, one of them moved.

## 4. Expensive lessons — read before touching the pipeline

Every one of these cost a debugging cycle or shipped a wrong number.

**Field paths must be fully qualified.** `openfda.pharm_class_epc:"…"` returns
**0**; `patient.drug.openfda.pharm_class_epc` returns 421,338. No error either
way. `src/client.py` owns all query construction so it cannot recur.

**Country fields require `.exact` or they return HTTP 500** (not 0) — a variant
of the same trap that errors instead of failing silently.

**Class queries need `.exact`.** Plain phrase matching absorbs longer class
names: `Opioid Agonist [EPC]` plain = 354,618 vs exact = 268,375, because
`Partial Opioid Agonist [EPC]` contains the phrase. Molecule queries must stay
**non**-exact, because `generic_name.exact` fails on qualifiers.

**openFDA cannot see withdrawn drugs via `generic_name`.** Cerivastatin returns
0 there, 200 under `medicinalproduct`, 91 under `activesubstance`. Molecule
queries OR across all three.

**The 2×2 must count reports, not flattened rows.** A report with 40 drugs and
47 reactions expands to 1,880 rows; counting rows deflates every ROR by a
drug-dependent factor.

**Never build the background from a downloaded class corpus.** In a corpus of
GLP-1 reports every record contains a GLP-1, so cell `c` collapses and the ROR
silently becomes "GLP-1 vs statins".

**Frequency ranking hides the reactions that matter.** A top-60 list missed
fluoroquinolone × TENDON RUPTURE, lamotrigine × STEVENS-JOHNSON, opioid ×
RESPIRATORY DEPRESSION — all boxed warnings. Fixed by a 58-term designated
medical event sweep (`config/dme.txt`) run on every drug regardless of
frequency.

**The DME sweep had a severe bug (D-09).** It accepted a term's count bucket
from *any* chunk, but a chunk's response buckets every reaction in the matched
reports — so a DME term appearing as a co-occurrence in another chunk
overwrote its own correct count, last chunk winning, always an undercount.
**3,678 of 14,736 DME pairs were wrong, corrections up to 119×**
(corticosteroid × PANCYTOPENIA 492 → 11,600). Fix: accept a bucket only from
the chunk that owns the term.

**Never let a helper swallow API failures unconditionally.** Returning 0 on
error fixed one-bad-term-kills-the-batch but created a worse mode: once the
daily quota is gone, every background becomes 0, every pair is skipped, and the
run reports success with empty tables. `client.py` raises `QuotaExhausted`
after 8 consecutive failures.

**`requests` puts the full URL in `HTTPError`, and the API key is a query
parameter.** An HTTP 500 printed a live key into a terminal transcript. Both
raise paths in `call()` now redact, with `from None` — chaining would print the
unredacted original above the redacted one.

**Never set a threshold by intuition.** A GLP-1 × NAUSEA control was invented
at ≥5.0 and failed; the measured value is 4.10. Nausea appears in 3.76% of all
reports, so a common term cannot produce a large ratio.

**A runtime artifact needs THREE things:** a `.gitignore` exception, a
`.dockerignore` exception, and a Dockerfile `COPY`. Missing any one fails
differently and only the third fails loudly. Two builds broke on this.
`scripts/preflight.py` now checks all three.

**Headless screenshots crop, they do not reflow.** `--window-size=390` reports
`innerWidth: 512`. Cropped text looks exactly like horizontal overflow; this
produced two false mobile-overflow reports. Measure `scrollWidth` vs
`clientWidth` instead.

## 5. Withheld and negative results

**Drug interaction prediction is withheld.** The Ω (omega) measure scored
**0/12** on curated known pairs — warfarin+NSAID × GI HAEMORRHAGE at −1.19,
opioid+benzodiazepine × RESPIRATORY DEPRESSION at −2.94 despite boxed warnings
on both — while promoting confounded pairs like warfarin+NSAID × SEPSIS at
+3.71. The multiplicative independence baseline is unreachable when both drugs
already associate with the reaction. **Do not expose interaction output until
it passes those 12 pairs.** An additive baseline would need its own validation.

**What replaced it:** a label lookup, not an inference. The FDA label's
`drug_interactions` section is read and quoted. **13/13 known interactions
found, 0/4 false positives.** Requires the boxed warning to be included
(morphine's interaction sections contain zero mentions of benzodiazepine) and a
class-synonym map (lisinopril says "NSAID" three times and "nonsteroidal"
never).

**`primarysource.qualification` does NOT detect litigation artifacts.**
Hypothesis was that cerivastatin × ALS would be lawyer-heavy. Measured:
lawyer+consumer **7.7%**, versus 33.8% for atorvastatin × ALS and 8.0% for
atorvastatin × RHABDOMYOLYSIS (a genuine signal). The artifact has a *lower*
lay-reporter share. Mass-tort cases reach FAERS through the manufacturer and
are coded as physician. Still worth showing as provenance; not as a diagnostic.

**FAERS has no genetic data.** Pharmacogenomics is a *label section*, so it is
a lookup, not a model.

## 6. Key files

```
src/client.py        all query construction; rate limit pinned 0.25s; redact()
src/score.py         ROR/PRR/chi2, bcpnn(), mgps()/fit_mgps(), dme_counts()
src/bias.py          comparator, indication, burstiness
src/labels.py        expectedness + interactions (label endpoint, not FAERS)
src/worksheet.py     the discussion aid; never ranks drugs
src/app.py           routes, CSP/headers, rate limits, canonical/robots
src/auth.py          accounts, sessions, CSRF, session_epoch
src/security.py      constant_time_equal — bytes, not str
templates/_base.html the single page shell; pages configure via top-level set
config/targets.json  361 drugs; every EPC string read back from the API
config/dme.txt       58 designated medical events
docs/TEST-PLAN.md    ~180 test cases, risk-ordered
```

**Data artifacts** (committed, copied into the image): `scored_pairs.parquet`,
`label_interaction_pairs.json`, `drug_facts.json`.

## 7. Verification — run all five before any deploy

```bash
python scripts/preflight.py       # deployability, secrets, 3-mechanism packaging
python scripts/check_pages.py     # every template renders, head intact
python scripts/check_contrast.py  # WCAG AA both themes, parses the real CSS
python scripts/check_defects.py   # behavioural regression on D-01..D-08
python run.py --validate          # 22 known-answer statistical controls
```

Data rebuilds also self-check: the interaction build asserts 13/13 controls,
`build_drug_facts.py` asserts 12/12 approval dates.

## 8. Design rules that are not negotiable

- **No page states or implies risk, incidence or causation.** FAERS has no
  denominator; none of these numbers is a rate.
- **The worksheet never ranks the reader's medicines.** FATIGUE is reported
  with 360 of 361 drugs; a ranked page would name a culprit every time, and the
  action it prompts is stopping a prescribed medicine.
- **Absence of a finding is never presented as clearance.**
- **Tier words are qualified** as strength of *reporting disproportion*.
- Nothing typed into the worksheet is stored — POST only, no analytics, no row.

## 9. Open items

**Blocking UAT:** §10 of `docs/TEST-PLAN.md` — 13 pass/fail clinical-framing
items, needs sign-off in writing from a clinical reviewer. This is the only
open gate.

**Unproven, not broken:** actual SMTP delivery from a GitHub runner (the dry
run skips sending); the `src.snapshots` step (skipped on dry runs); real-device
mobile at ≤414px.

**Known and accepted:** 11 rows where a severity count exceeds `a` by exactly
one — extract drift between two sweeps, share suppressed on those rows.

**Next data, in order of value:** severity and reporter columns are already in;
then `enforcement`/`shortages` are ingested but not surfaced everywhere; then
label sections `pharmacogenomics`, `inactive_ingredient` (allergens),
`information_for_patients` (plain-language text); then MOA-based comparator
groups (only EPC is used today, `pharm_class_moa` is already in the pulls).

**Both of these are now resolved, and neither the way it was planned.**

*Public snapshot repo* — done, as
`github.com/mor64ph/drug-safety-signal-detection`, built by cloning this
working copy into `../reportscope-public` and rewriting history there. The clone
advice held: `.env` was never tracked, and a scan of all 243 blobs across every
commit found no secret value. What it did find was a work email address — on two
commits as the author, including the initial one, and *again* inside
`databricks/jobs/rxsignal_quarterly_job.json` as an `on_failure` recipient.
**Checking commit metadata alone reports clean**, because the second copy sat in
a file body; only a content grep over every blob found it. **Never add the public
remote to this repo:** its history still carries that address on those two
commits, and only the rewritten clone is safe to publish.

*Databricks medallion pipeline* — deleted, not run. It had drifted to
`ic025 = ic - 1.96*sqrt(var_ic)`, the frequentist bound `src/score.py` documents
as a defect and replaced with a Bate/Norén posterior. A second implementation of
the central statistic, unused and by then wrong, was the argument against
keeping it. The record-level `drugcharacterization` gap it was meant to close is
still open: the API matches at report level, so "ibuprofen AND NAUSEA" returns
19,517 reports and adding `drugcharacterization:1` keeps 19,487 of them.

**FAERS already has geography.** `occurcountry` is coded on 79.5% of reports
and **4,637,562 are non-US** — Europe 13.94%, Asia 5.42%, Oceania 0.82%,
Middle East 0.28%. Per-region stratification is viable for Europe and Asia,
class-level only for Australia, too thin per drug elsewhere. **Never pool RORs
across separate national databases** — different denominators, reporting
mandates and MedDRA versions.
