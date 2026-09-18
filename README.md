# reportscope

> Pharmacovigilance signal detection over 20.7 million FDA adverse-event reports.

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.x-lightgrey.svg)](https://flask.palletsprojects.com/)
[![Data](https://img.shields.io/badge/FAERS-20%2C692%2C690%20reports-teal.svg)](https://open.fda.gov/data/faers/)
[![Controls](https://img.shields.io/badge/known--answer%20controls-22%2F22-brightgreen.svg)](docs/TEST-PLAN.md)

Give it a drug and it returns the adverse events reported alongside that drug far
more often than the rest of the database would predict — ranked, with report
counts, credible intervals, and a measured profile of the reasons each result
might be wrong.

It is a screening instrument. Every result is a pair worth a human looking at,
and nothing more than that.

**Live: <https://reportscope.onrender.com>** — the first request is slow, because
the free tier sleeps after fifteen minutes idle and has to wake up. Measured cold
start on 2026-09-18: 43 seconds.

The live instance accepts public registration and stores an email address and an
Argon2id password hash per account. Sign-up is optional; every lookup works
without one.

---

## Contents

- [What it does](#what-it-does)
- [Limitations](#limitations)
- [Getting started](#getting-started)
- [Usage](#usage)
- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Testing](#testing)
- [Deployment](#deployment)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Tech stack](#tech-stack)
- [Documentation](#documentation)
- [Data and attribution](#data-and-attribution)
- [License](#license)

---

## What it does

**Five disproportionality measures**, computed offline for every drug-reaction
pair: ROR with 95% CI, PRR, chi-squared, BCPNN (`IC025`/`IC975`, Bate 1998 /
Norén 2006) and MGPS (`EB05`/`EB95`, DuMouchel 1999 — the measure the FDA
screens on). Ranking uses `IC025`, not raw ROR, because a pair with 9 reports
must not outrank one with 8,820.

**A bias layer, measured rather than disclaimed.** Each highly ranked pair
carries an active-comparator ratio (recomputed against clinically similar drugs
instead of the whole database), an indication-confounding flag, and a burstiness
figure showing what share of reports arrived in a single month. Cerivastatin ×
amyotrophic lateral sclerosis scores ROR 394.49 on 14 reports, and **2.40**
against other lipid-lowering drugs — a collapse the bias layer reports rather
than hides.

**Label expectedness** — the one input that is not FAERS. Each reaction is
checked against the regulator-approved drug label, because a disproportionate
result already printed on the label is usually the reporting system working as
designed, while the same score for an unlabelled reaction is a different object
entirely.

**A designated-medical-event sweep.** Candidate reactions from a frequency-ranked
API endpoint systematically bury rare severe events under common complaints, so
58 designated medical events are screened against every drug regardless of
frequency.

**Reported outcomes per pair** — how many of a pair's reports recorded a death,
hospitalisation, life-threatening event or disability, shown as counts first and
share second, and never as a risk.

**Interfaces**: drug lookup, reaction-first lookup, multi-drug comparison, a
printable discussion worksheet that quotes official label text on drug
interactions, accounts with tracked drugs and a weekly email digest. Light and
dark themes, WCAG AA verified in both, and a print stylesheet that forces the
light palette so a worksheet printed in dark mode is not a blank sheet.

**Coverage**: 33,852 scored pairs across 361 drugs, drawn from 371 configured
targets (305 molecules and 66 pharmacologic classes across 23 therapeutic
areas); the 10 without scored rows have no US approval or no reports. Validated
against 22 known-answer controls.

---

## Limitations

**Read this before the numbers, not after.**

**The denominator does not exist.** FAERS records reports, not patients. Nothing
in it says how many people took a drug and were fine. Every rate, risk and
incidence figure requires that number, so none can be computed here — not
approximately, not with better methods, not at all. These ratios compare reports
against reports. They describe the contents of a filing cabinet, not the human
body.

**Reporting is voluntary and unvalidated.** Nobody verifies these reports.
Measured over the 19,943,055 reports carrying a reporter qualification, **47.5%**
come from consumers rather than health professionals (physician 22.8%, other
health professional 20.5%, pharmacist 6.5%, lawyer 2.7%). A coded term may mean a
specialist confirmed it, or that somebody used the word after reading about it
online. Both produce the same database row.

**Reporting responds to attention, not only to biology.** New drugs are reported
more than old ones at identical true risk. A news cycle or a law firm advertising
for plaintiffs moves these numbers without changing what the drug does.

**Everything here is uncontrolled.** Disproportionality adjusts for nothing,
because the data does not contain the information required. Its single advantage
is screening every drug against every reaction continuously at almost no cost —
a real advantage, and the only one.

A high score is a **hypothesis**. It measures reporting behaviour, not
physiology, and nothing here rules out the many non-causal reasons two words end
up on the same form.

Not affiliated with the FDA. Not a medical device. Not medical advice.

---

## Getting started

### Prerequisites

- Python 3.12+
- An openFDA API key — [free and instant](https://open.fda.gov/apis/authentication/).
  Without one the ceiling is 1,000 requests/day and count buckets cap at 100;
  with one it is 120,000/day and 1,000 buckets. A full scoring run needs roughly
  20,000 requests, so the label and bias passes are impractical unauthenticated.

### Installation

```bash
git clone https://github.com/mor64ph/drug-safety-signal-detection.git
cd drug-safety-signal-detection
python -m venv .venv && . .venv/Scripts/activate   # Linux/macOS: . .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
```

Only `OPENFDA_API_KEY` matters for local use. Defaults below are read from the
settings code, not from `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `OPENFDA_API_KEY` | none | Raises the API ceiling to 120,000 requests/day. Required for the scoring, label and bias passes. |
| `DATABASE_URL` | `sqlite:///data/rxsignal.db` | Accounts store. No database server needed locally; `postgres://` URLs are rewritten onto psycopg 3. |
| `APP_BASE_URL` | `http://localhost:8000` | Absolute base for links in email. Must be set to the public URL in production, or verification links are unusable. |
| `RXSIGNAL_SECRET_KEY` | random per start | Signs session cookies. Unset means every restart signs all users out. Generate with `scripts/make_secret_key.py`. |
| `PORT` | `8000` | Listen port for `serve.py`. |
| `RXSIGNAL_RATE_LIMIT` | `60` | Requests per minute per IP. |
| `RXSIGNAL_AUTH_RATE_LIMIT` | `5` | Requests per minute per IP on login, register, reset. |
| `RXSIGNAL_TRUSTED_PROXY` | `*` | Which proxy may set `X-Forwarded-For`. Load-bearing: rate limiting is bypassable without it. |
| `RXSIGNAL_ACCESS_CODE_HASH` | none | Sets a site-wide access gate. Unset means the instance is open to anyone who finds it. |
| `RXSIGNAL_ADMIN_CODE` | none | Enables `/admin/analytics`. Unset means the route 404s. |
| `RXSIGNAL_ALLOW_INDEXING` | `1` | Set to `0` to serve `noindex` and disallow crawling. |
| `SMTP_HOST` | none | Unset writes mail to `data/outbox/` as `.eml` instead of sending. |
| `SMTP_PORT` | `587` | 465 is treated as implicit TLS. |
| `SMTP_USE_TLS` | `true` | |
| `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | none | |
| `RXSIGNAL_MAIL_OUTBOX` | unset | Set to `1` to force mail to `data/outbox/` even when SMTP is configured. Used by the test harness. |

### Running

The repository ships with the scored table already built, so the app runs
immediately:

```bash
python run.py --serve      # development, http://127.0.0.1:5000
python serve.py            # production via Waitress, http://0.0.0.0:8000
```

---

## Usage

```bash
python run.py --score       # score every drug-reaction pair (resumable)
python run.py --rescore     # rebuild every drug from scratch
python run.py --validate    # run the 22 known-answer controls
python run.py --serve       # start the web app
```

`--score` resumes: drugs already present in `data/results/` are skipped, so an
interrupted run loses nothing.

Supporting builds, each independent:

```bash
python scripts/fetch_bulk.py            # download the small openFDA bulk files
python scripts/build_drug_facts.py      # approval dates, recalls, shortages
python scripts/fetch_severity.py        # reported-outcome counts per pair
python scripts/fetch_label_interactions.py   # label-quoted drug interactions
```

---

## How it works

```text
openFDA drug/event API  (count endpoint, population-level totals)
        │
        ├── score        2x2 per pair -> ROR, PRR, chi2, BCPNN, MGPS
        ├── DME sweep    58 designated medical events, every drug
        ├── labels       expectedness against the drug label endpoint
        └── bias         active comparator, indication, burstiness
        │
        v
data/results/scored_pairs.parquet   (33,852 rows, precomputed)
        │
        v
Flask app  —  nothing on the request path calls an API
```

Two design decisions do most of the work:

**The 2×2 counts reports, not rows.** A report listing 40 drugs and 47 reactions
expands to 1,880 rows; counting those inflates cell `b` by the reaction count and
deflates every ratio by a drug-dependent factor.

**The background comes from the whole database.** Cells `c` and `d` are API
totals over all 20,692,690 reports. Deriving them from a downloaded corpus of the
target class cannot work — in a corpus of GLP-1 reports every record contains a
GLP-1, so `c` collapses and the ratio silently becomes "GLP-1 versus statins"
rather than "GLP-1 versus everything".

---

## Project structure

```text
src/
  client.py      openFDA client; all query construction lives here
  score.py       ROR, PRR, chi-squared, BCPNN, MGPS
  bias.py        active comparator, indication, notoriety diagnostics
  labels.py      label expectedness
  validate.py    known-answer control harness
  app.py         Flask application
  worksheet.py   printable discussion worksheet
  models.py      accounts, tracked drugs, notifications
config/          targets, stoplist, designated medical events
data/results/    precomputed scored table and derived indexes
scripts/         build steps and the pre-deploy check suite
docs/            primer, module plan, test plan, deployment
```

---

## Testing

Five suites, all run before deployment:

```bash
python scripts/preflight.py         # deployability: env, artifacts, imports
python scripts/check_pages.py       # every template compiles and carries the shared head
python scripts/check_contrast.py    # WCAG AA in both themes, parsed from style.css
python scripts/check_defects.py     # behavioural regression tests for nine fixed defects
python run.py --validate            # 22 known-answer controls
```

The control harness is the one that matters. Positive controls must reproduce
settled pharmacology — statin × rhabdomyolysis at ROR 12.96, the figure the
pipeline is broken if it cannot hit — and negative controls must *not* signal.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/FINDINGS.md](docs/FINDINGS.md) | What the measurements showed, and the API traps that produce silent wrong answers |
| [docs/00-PRIMER.md](docs/00-PRIMER.md) | Pharmacovigilance concepts from first principles |
| [docs/01-MODULES.md](docs/01-MODULES.md) | Module plan, including the retired ones and why |
| [docs/TEST-PLAN.md](docs/TEST-PLAN.md) | ~180 cases, risk-ordered, plus the nine-defect register |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Deployment and configuration |

`docs/FINDINGS.md` is the interesting one. It documents the interaction module
that failed all twelve of its known-answer controls and was withheld rather than
shipped, and several ways the openFDA API returns zero results with no error.

---

## Deployment

Docker image, deployed as a web service on Render with Postgres for accounts.
`render.yaml` describes the service; `autoDeploy` is off, so deploys are manual.
Alembic migrations run on container start via `docker-entrypoint.sh`.

The scored table is baked into the image rather than fetched at runtime, so the
container has no API dependency and no cold-start data load beyond reading a
parquet file.

Run `python scripts/preflight.py` before deploying. It checks that every runtime
artifact survives the three places it can be excluded — `.gitignore`,
`.dockerignore`, and a `COPY` in the Dockerfile — which is a failure mode that
has bitten this project twice. Full procedure in [docs/DEPLOY.md](docs/DEPLOY.md).

---

## Roadmap

- [x] Five disproportionality measures with known-answer validation
- [x] Bias diagnostics: active comparator, indication confounding, burstiness
- [x] Label expectedness via the openFDA label endpoint
- [x] Designated-medical-event sweep
- [x] Reported outcome counts per pair
- [x] Accounts, tracked drugs, weekly email digest
- [x] Light and dark themes, WCAG AA verified in both
- [ ] Drug-drug interaction scoring — built, failed all twelve known-answer
      controls, withheld. Needs an additive independence baseline; see
      [docs/FINDINGS.md](docs/FINDINGS.md)
- [ ] Age and sex stratification (a pooled ratio can reverse inside every
      subgroup it pools)
- [ ] Per-drug `drugcharacterization` filtering, which needs record-level data
      the API cannot provide
- [ ] Mechanism-of-action comparator groups (`pharm_class_moa` is already in the
      pulls; only EPC is used today)

---

## Contributing

This is a personal project and not currently accepting contributions. Bug
reports via issues are welcome.

If you do run it, the one rule worth stating: any change to a scoring measure has
to keep `python run.py --validate` at 22/22. A measure that produces
plausible-looking numbers and fails on settled pharmacology is worse than no
measure, because its output looks exactly as authoritative as the output that
works.

---

## Tech stack

| Layer | Used |
|---|---|
| Application | Flask, Jinja2, Waitress |
| Analysis | pandas, NumPy, SciPy (`digamma`/`polygamma` for BCPNN, Nelder–Mead for MGPS) |
| Storage | Parquet via PyArrow for scored data; SQLAlchemy + Alembic over SQLite or Postgres for accounts |
| Auth | Argon2id via `argon2-cffi` |
| Frontend | Bootstrap 5.3, self-hosted Inter, no build step and no JavaScript framework |
| Deployment | Docker on Render, Neon Postgres, GitHub Actions for the weekly digest |

There is no machine learning here. Every figure is closed-form over a 2×2
contingency table — the appropriate choice for a method that has to be auditable,
and for a question where no ground-truth label exists.

---

## Data and attribution

Data from the [openFDA](https://open.fda.gov/) drug/event endpoint — 20,692,690
reports, extract current to 2026-07-30. FAERS is US-centric, and that is not
neutral: US prescribing, US hospitalisation thresholds, and US
direct-to-consumer advertising and litigation all shape what gets reported.

Methods follow the published literature: ROR and PRR as conventionally defined,
BCPNN after Bate et al. (1998) and Norén et al. (2006), MGPS after DuMouchel
(1999). The designated-medical-event approach follows the EMA's DME list
principle.

## License

**No license.** All rights reserved — this is a portfolio project, published to
be read rather than reused. That is deliberate, not an oversight: without a
licence grant you may view the source but not copy, modify or redistribute it.
Open an issue if you want to use any of it.

## Author

[@mor64ph](https://github.com/mor64ph)
