# reportscope — test plan

Written for whoever runs testing, including non-technical testers doing UAT.

**Status:** pre-UAT. Live at `https://reportscope.onrender.com` with real
accounts. 33,852 scored pairs, 361 drugs, FAERS extract of 30 July 2026.

---

## 1. What this plan is for, and the one thing it is mostly about

Most of what can go wrong here is not a crash. This application's specific
danger is that **it works perfectly and a reader draws a conclusion the data
cannot support**, stops a prescribed medicine, and comes to harm. A test plan
for this product that only checks status codes and form validation has tested
the least dangerous part of it.

So the priority order is deliberately not the usual one:

| Priority | Area | Why it ranks here |
|---|---|---|
| **P0** | Clinical-safety framing (§10) | A wrong number is recoverable. A correctly computed number presented as a risk estimate is not. |
| **P0** | Statistical correctness (§5) | Everything downstream inherits it. |
| **P0** | Abuse and availability (§8) | One request took the whole site down as recently as this week. |
| **P1** | Accounts, authorization, email (§6, §7) | Real health-adjacent data on real people. |
| **P1** | Deployment and packaging (§13) | Two separate builds have already failed on this. |
| **P2** | Accessibility, themes, responsive (§11, §12) | Credibility with clinical users; legal exposure in institutional settings. |
| **P2** | Functional breadth (§4) | Visible, self-reporting, cheap to fix. |

**Exit criteria are in §15.** Do not start UAT with any P0 open.

---

## 2. What already runs automatically

Run all four before any manual pass. They are the regression net; everything in
this document assumes they are green.

```bash
python scripts/preflight.py          # deployability, secrets, packaging
python scripts/check_pages.py        # every template renders, head is intact
python scripts/check_contrast.py     # WCAG AA in both themes
python run.py --validate             # 22 known-answer statistical controls
```

Plus, on any data rebuild:

```bash
python scripts/fetch_label_interactions.py --pairs-only   # 13/13 interaction controls
python scripts/build_drug_facts.py                        # 12/12 approval controls
python scripts/rescore_bayesian.py                        # tier movement report
```

**Gap to close:** there is no automated browser test and no automated auth test.
Sections 6, 7, 11 and 12 are entirely manual today. That is the single biggest
weakness in this plan.

---

## 3. Environments and test data

| Environment | Use | Notes |
|---|---|---|
| Local (`python serve.py`) | Functional, statistical, abuse | SQLite, `SMTP_HOST` unset writes mail to `data/outbox/` |
| Render (live) | Email, cold start, headers, UAT | Postgres. **Has real user data — never run destructive tests here** |

**Accounts to create for testing**, on the live instance:

- `tester-a@…` — verified, tracks 3 drugs, notifications on
- `tester-b@…` — verified, tracks 1 drug (for cross-user isolation tests)
- `tester-c@…` — registered and **never verified** (the unverified path is a
  distinct state with its own banner and restrictions)
- One account you are willing to delete, for the deletion test

**Reference values** (from the committed extract; any drift is a defect):

| Check | Expected |
|---|---|
| `/healthz` | `{"drugs":361,"pairs":33852,"status":"ok"}` |
| statin × RHABDOMYOLYSIS | ROR 12.96, IC025 3.3155, IC975 3.3824 |
| cerivastatin top row by IC025 | RHABDOMYOLYSIS (4.28), **not** ALS |
| atorvastatin first approval | 1996-12-17, 29.6 years marketed |
| atorvastatin NDC products | 515, `marketed_now: true` |
| cerivastatin | `marketed_now: false`, 0 products |

---

## 4. Functional tests

Each: **steps → expected**. `[A]` = covered by an automated check.

### 4.1 Drug search
| ID | Case | Expected |
|---|---|---|
| F-01 `[A]` | `atorvastatin` | Results, title `atorvastatin — reported adverse events · reportscope` |
| F-02 `[A]` | `Ozempic` (brand) | Resolves to **semaglutide**, states the resolution |
| F-03 | `OZEMPIC`, `ozempic `, ` Ozempic` | All resolve identically |
| F-04 | `sema` (fragment) | Resolves to semaglutide via substring |
| F-05 | `statin` (class) | Class-level result, not an error |
| F-06 | `zzzznotadrug` | "Not in the catalogue" — and explicitly **not** "no signals found". The distinction is the point |
| F-07 | Empty submit | Handled, no traceback |
| F-08 | 64-character input | Accepted; 65+ rejected cleanly |
| F-09 | `aspirin'` , `co-codamol`, `vitamin d3` | Apostrophe, hyphen, digit all accepted |
| F-10 | Drug with a withdrawn status (`cerivastatin`) | Shows as no longer marketed |

### 4.2 Reaction-first lookup
| ID | Case | Expected |
|---|---|---|
| F-11 `[A]` | `RHABDOMYOLYSIS` | Ranked drug list |
| F-12 | `rhabdo` (fragment) | Offers matching terms rather than failing |
| F-13 | `FOURNIER'S GANGRENE` | Resolves despite the caret-encoded stored form |
| F-14 | `headache` (very common) | Renders, and says how unspecific the term is |

### 4.3 Compare and multi-drug
| ID | Case | Expected |
|---|---|---|
| F-15 `[A]` | Compare semaglutide vs metformin | Side-by-side; states it is **not** an interaction check |
| F-16 | Compare a drug with itself | Handled sensibly |
| F-17 | Compare with one field blank | Clear error, no traceback |
| F-18 `[A]` | `/list` with 2 drugs | Both scored separately |
| F-19 | `/list` with 11 drugs | Caps at 10, names the dropped one |
| F-20 | `/list` with 10 unknown names | Each reported individually, not a single failure |
| F-21 | `/list` canonical tag | Points to `/list`, **not** `/list?drugs=…` |

### 4.4 Worksheet
| ID | Case | Expected |
|---|---|---|
| F-22 | 3 medicines + 2 symptoms | One block per symptom, medicines in entered order |
| F-23 | **Nothing is ranked** | No sort by ROR/IC025 anywhere; no strength badge in cells |
| F-24 | `ibuprofen, ciprofloxacin, warfarin` | 3 interaction pairs with quoted label text |
| F-25 | Add `vitamin d` to F-24 | Vitamin produces **no** interaction row |
| F-26 | Symptoms not in MedDRA | Says so, and says the absence means nothing |
| F-27 | Medicines only, no symptoms | Error asks for both |
| F-28 | Interaction section with no scored symptoms | Interaction block still appears |
| F-29 | Submit, then browser Back | Entries restored; no "Working…" button stuck |
| F-30 | **Privacy** | Check `data/outbox/`, DB tables, analytics counters and access log — the symptom list appears in **none** of them |

---

## 5. Statistical and data-integrity tests

| ID | Case | Expected |
|---|---|---|
| S-01 `[A]` | `run.py --validate` | 22/22 |
| S-02 `[A]` | Anchor | statin × rhabdo ROR 12.96 ± 0.01 |
| S-03 | 2×2 sums | For each row, `a+b+c+d == N` exactly |
| S-04 | `a` vs severity | `deaths ≤ a` etc. on every row where `severity_base_ok` is true (32,975). **877 rows (2.6%) fail and have their share suppressed — see D-09.** No displayed share may exceed 100% |
| S-05 | IC025 ≤ IC ≤ IC975 | On every row |
| S-06 | Tier consistency | tier follows IC025 bands; `a<3` ⇒ tier `none` |
| S-07 | BCPNN prior behaviour | Interval width falls monotonically with `a` (median 2.22 at a<10, 0.10 at a≥1000) |
| S-08 | Cerivastatin ranking | rhabdomyolysis outranks ALS |
| S-09 | Stoplist | `DRUG INEFFECTIVE`, `EXTRA DOSE ADMINISTERED` absent from all results |
| S-10 | Diagnostics coverage | `diagnosed` true on 33,852/33,852 |
| S-11 | Displayed vs stored | A figure on screen matches the parquet for the same pair. *(This is the bug class where validation passed while the served numbers were wrong.)* |
| S-12 | Rounding | No figure displayed with more precision than computed |

---

## 6. Accounts, authentication, session

**Four known defects are open here (§14). Test them to confirm scope, not to
discover them.**

| ID | Case | Expected |
|---|---|---|
| A-01 | Register, verify, sign in, sign out | Full happy path |
| A-02 | Register with an existing address | **Currently discloses existence (defect D-01).** Should be indistinguishable from a new signup |
| A-03 | Password: 7 chars, 200 chars, all spaces, unicode, emoji | Consistent validation; no 500 |
| A-04 | Wrong password / unknown address / disabled account | **Identical** response and timing |
| A-05 | Forgot-password for a real vs fake address | Identical body; **currently distinguishable by timing (D-02)** |
| A-06 | Reset link | Single-use, expires in 1 h, rejected after use |
| A-07 | Old verification link | **Currently never expires and signs you in (D-04)** |
| A-08 | Password reset | **Currently does not evict existing sessions (D-05)** |
| A-09 | Session cookie | `HttpOnly`, `SameSite=Lax`, `Secure` on the live site |
| A-10 | CSRF | Every state-changing POST rejected without a token (expect 400) |
| A-11 | Session fixation | Cookie and CSRF token both change on login |
| A-12 | Unverified account | Cannot add drugs; banner persists; resend works and is throttled |
| A-13 | Account deletion | Data actually gone; confirm in the DB |
| A-14 | `?next=` | `//evil.example`, `/\evil.example`, `https://evil.example` all stay same-origin |

### 6.1 Authorization (cross-user)
| ID | Case | Expected |
|---|---|---|
| Z-01 | As A, POST delete on **B's** medication ID | B's row survives |
| Z-02 | As A, request B's notification/history IDs | No data returned |
| Z-03 | Signed out, request every `/my/*` and `/account/*` | Redirect to login, no content leak |
| Z-04 | `/admin/analytics` without a code | 404 |
| Z-05 | `/admin/analytics?admin_code=wrÖng` | **Currently 500 (D-03)**; should be 404 |

---

## 7. Email

| ID | Case | Expected |
|---|---|---|
| E-01 | Verification email | Arrives in **inbox, not spam**; link works once |
| E-02 | Reset email | Arrives; link works; expires |
| E-03 | Links in email | Absolute, use `APP_BASE_URL`, **not** the Host header |
| E-04 | Host-header poisoning | Send a forged `Host`; the emailed link is unaffected |
| E-05 | Weekly digest | **Not currently scheduled (D-06).** Once cron exists: fires, content correct, unsubscribe works |
| E-06 | Unsubscribe | Actually stops mail |
| E-07 | `SMTP_HOST` unset locally | Mail lands in `data/outbox/`, nothing silently dropped |

---

## 8. Input validation, abuse, availability

| ID | Case | Expected |
|---|---|---|
| X-01 | **Regex DoS regression** — `GET /search?drug=.%2B.%2B.%2B.%2B.%2B.%2B.%2B.%2B.%2B.%2Bzz` | Responds in <1 s. *Before the fix this took 31.3 s and blocked all 8 threads.* **Run this on every deploy.** |
| X-02 | Same payload ×10 via `/list` | Fast; previously ~5 min of blocked CPU |
| X-03 | `/healthz` during X-02 | Still responds |
| X-04 | XSS attempts in every text field: `<script>`, `"><img onerror=1>`, `{{7*7}}`, `${7*7}` | Rendered as text; `{{7*7}}` does **not** become 49 |
| X-05 | SQL/NoSQL in all fields | No error, no data leak |
| X-06 | Path traversal in params: `../../etc/passwd` | Rejected |
| X-07 | Oversized body (>16 KB) | Rejected cleanly by Waitress |
| X-08 | Rate limit | 61 requests/min from one IP → throttled |
| X-09 | `X-Forwarded-For` spoofing | Cannot bypass the limiter *(safe only because of `trusted_proxy` in serve.py — retest if the server changes)* |
| X-10 | Login brute force | **Currently 60 guesses/min/IP (D-07)**; argon2 at 8×64 MiB approaches the 512 MiB ceiling |
| X-11 | Null bytes, 4-byte emoji, RTL overrides in every field | No 500 |
| X-12 | Concurrent load: 20 users × 10 searches | No errors, no thread starvation |
| X-13 | Cold start | First request after idle succeeds (may be slow); no timeout error page |

---

## 9. Performance

| ID | Case | Target |
|---|---|---|
| P-01 | Warm drug search | < 1.5 s |
| P-02 | Cold start first paint | < 30 s, no error |
| P-03 | `/list` with 10 drugs | < 5 s |
| P-04 | Worksheet with 12 medicines + 10 symptoms | < 5 s |
| P-05 | Memory at 8 concurrent logins | Under the 512 MiB plan ceiling |
| P-06 | Font and CSS | Font preloads; no layout shift on swap |

---

## 10. Clinical-safety and content review — **the P0 section**

This needs a clinician or pharmacovigilance reviewer, not a tester. Your two
J&J contacts are the right people. **Every item is pass/fail, not advisory.**

| ID | Check | Fail condition |
|---|---|---|
| C-01 | No page states or implies **risk, incidence, or causation** | Any phrasing a lay reader would read as "this drug causes X" |
| C-02 | Disclaimer visible **before** any figure on every results path | A figure reachable without it |
| C-03 | Worksheet never names a culprit | Any ranking, highlighting or ordering that implies one |
| C-04 | Interaction section framing | Reads as "ask your prescriber", not "these are dangerous together" |
| C-05 | Absence of an interaction is **not** presented as clearance | Any wording suggesting "safe" |
| C-06 | "Seek care now" guidance present for new severe symptoms | Absent or buried |
| C-07 | Severity columns read as **reported outcomes**, not rates | Any percentage presented as a patient risk |
| C-08 | Tier words (`strong`/`moderate`/`weak`) qualified as strength of *reporting disproportion* | Unqualified anywhere |
| C-09 | Drug age framed as reporting context, not as a safety property | "Newer drug = more dangerous" implication |
| C-10 | Recall and shortage notices scoped to product batches, not the molecule's safety | Conflation of the two |
| C-11 | No dosing, no substitution, no "talk to us instead of your doctor" | Any of them |
| C-12 | Data vintage stated on every page | Missing |
| C-13 | Read the whole site as an anxious patient who has just been prescribed something | Any page that would plausibly cause someone to stop a medicine |

---

## 11. Accessibility

| ID | Case | Expected |
|---|---|---|
| Y-01 `[A]` | Contrast | WCAG AA in both themes |
| Y-02 | Keyboard only, every flow | Completable; visible focus throughout |
| Y-03 | Skip link | First tab stop, becomes visible, jumps to `#main` |
| Y-04 | Screen reader (NVDA/VoiceOver) on a results table | Headers announced; figures intelligible |
| Y-05 | Tables | Proper `th`/`scope`; no layout tables |
| Y-06 | Forms | Every input has a label; errors associated with fields |
| Y-07 | Sparklines | `<title>` present and meaningful; never the sole carrier of information |
| Y-08 | Tier colour | Word always accompanies colour |
| Y-09 | 200% browser zoom | No loss of content or function |
| Y-10 | `prefers-reduced-motion` | Transitions suppressed |
| Y-11 | Theme toggle | Reachable by keyboard; `aria-pressed` correct; accessible name changes |

---

## 12. Themes, print, responsive, cross-browser

| ID | Case | Expected |
|---|---|---|
| T-01 | First load with OS dark | Renders dark with **no white flash** |
| T-02 | Toggle, then reload | Choice persists |
| T-03 | Toggle, then change OS setting | Explicit choice still wins |
| T-04 | Private/incognito | No crash when `localStorage` throws |
| T-05 | **Print the worksheet in dark mode** | Comes out **light**, fully legible. *A regression here yields a blank sheet.* |
| T-06 | Print results | Table readable; nav and buttons gone; all caveats retained |
| T-07 | Sparklines in dark | Visible |
| T-08 | **Real devices at 360 / 390 / 414 px** | No horizontal overflow. *Headless `--window-size` does not set the layout viewport — it crops, which mimics overflow. Use a real device or DevTools emulation.* |
| T-09 | Chrome, Firefox, Safari, Edge; iOS Safari, Android Chrome | Functional parity |
| T-10 | Tab title and favicon | Mark visible, title correct |
| T-11 | Paste the URL into WhatsApp / Slack / LinkedIn | Social card renders with image and description |

---

## 13. Deployment and packaging

Two builds have already failed here. **This section is not optional.**

| ID | Case | Expected |
|---|---|---|
| D-01 | Every runtime artifact has **all three**: `.gitignore` exception, `.dockerignore` exception, Dockerfile `COPY` | `preflight.py` enforces it |
| D-02 | Fresh clone → `docker build` | Succeeds with no local files present |
| D-03 | Container starts | Migrations run, app serves, `/healthz` ok |
| D-04 | Env var missing (`SECRET_KEY`, `DATABASE_URL`) | Refuses to start with a clear message, not a traceback |
| D-05 | Security headers on live | CSP, HSTS `max-age=31536000`, `X-Content-Type-Options`, `frame-ancestors none` |
| D-06 | `robots.txt` and `sitemap.xml` | Reference pages allowed; `/my`, `/account`, `/admin`, `/worksheet` excluded |
| D-07 | `noindex` vs canonical | Mutually exclusive and correct per path; **no `admin_code` or token ever in a canonical URL** |
| D-08 | Rollback | Previous deploy can be restored |

---

## 14. Known defect register

Carry these into UAT knowingly, or fix first. IDs referenced above.

| ID | Defect | Severity | Status |
|---|---|---|---|
| D-01 | `/register` discloses whether an address has an account | **High** | Open |
| D-02 | `/forgot` distinguishable by response timing | Medium | Open |
| D-03 | `compare_digest` raises on non-ASCII → 500 reveals admin code is configured | Medium | Open |
| D-04 | Verification tokens never expire **and** following one signs you in | Medium | Open |
| D-05 | Password reset does not invalidate existing sessions | Medium | Open |
| D-06 | Weekly digest cron never scheduled — notifications do not fire | **High** (feature is inert) | Open |
| D-07 | No login-specific rate limit | Medium | Open |
| D-08 | Rate-limit table grows unbounded | Low | Open |
| D-09 | 877 pairs have a serious-outcome count above their own `a`, concentrated in CARDIAC ARREST, SEPSIS, ACUTE KIDNEY INJURY, PANCYTOPENIA, SEIZURE. Severity counts reconcile against the API exactly; the stored `a` does not (acetaminophen × CARDIAC ARREST: stored 3,259, measured 7,383). A 10-row random sample matched `a` exactly, so this is term-specific, not systemic. **Root cause unresolved** — share is suppressed on those rows rather than clamped | **High** (affects ROR on those rows) | Open |
| — | Mobile ≤414 px never measured on a real layout viewport | Unknown | Open |
| — | No automated browser or auth tests | Process gap | Open |

---

## 15. Exit criteria

**Do not begin UAT unless all of these hold.**

1. All four automated suites green, on the deployed commit.
2. X-01 and X-02 (regex DoS) verified fast **on the live URL**.
3. Section 10 signed off by a clinical reviewer, in writing. No open fail.
4. D-01 and D-06 fixed — one leaks who has an account, the other means the
   feature people sign up for does nothing.
5. T-05 verified: worksheet prints legibly from dark mode.
6. Z-01 verified on the live instance with two real accounts.
7. E-01 verified end to end on the live instance, landing in an inbox.
8. A rollback has been performed successfully at least once.

**During UAT:** keep a defect log with reproduction steps, browser, and
screenshot. Ask testers for the one thing this document cannot test for — the
sentence they read that made them think about changing a medicine.

---

## 16. Out of scope

- Drug interaction **prediction** — withheld by design after scoring 0/12.
- Any claim about risk, incidence or causation — not a feature, so not a test.
- Non-US data — the extract is FAERS only, though 4.6 M reports are non-US.
- Load beyond ~50 concurrent users — the free plan is not sized for it, and
  that is a capacity decision, not a defect.
