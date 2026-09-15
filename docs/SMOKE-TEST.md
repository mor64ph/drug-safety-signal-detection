# reportscope — manual smoke test

A self-verifying walkthrough of everything added, with exact expected values.
Every number below comes from the deployed data, so a mismatch is a real defect
rather than a judgement call.

**Target:** `https://reportscope.onrender.com`
Allow **45 seconds** for the first request. The free plan sleeps after ~15
minutes idle, and a cold start looks like a broken site.

---

## STEP 0 — Do this before deciding anything is missing

Two things hide the new work and neither is a bug in the app.

**0a. Hard-refresh.** Your browser has the old CSS and HTML cached.

- Windows: **Ctrl + Shift + R**, or **Ctrl + F5**
- Or open a **private window**, which is the cleaner test

**0b. Make the browser window at least 1000px wide.** Two of the new columns
(**EB05** and **Reported outcomes**) are deliberately hidden below 992px, so on
a narrow or half-screen window they are simply absent. Maximise the window, or
zoom out with **Ctrl + minus**.

If you are testing in a half-width window next to VS Code, you will not see
them. That is the single most likely reason the page looked unchanged.

---

## STEP 1 — Is the right build even deployed?

```
https://reportscope.onrender.com/healthz
```

**Expect exactly:** `{"drugs":361,"pairs":33852,"status":"ok"}`

Then:

```
https://reportscope.onrender.com/search?drug=corticosteroid
```

Find the **PANCYTOPENIA** row and read the `a (reports)` column.

| | |
|---|---|
| **11,600** | correct — the D-09 data repair is deployed |
| **492** | the old build. Redeploy on Render before continuing |

That one number is the cleanest deploy check there is: it is the largest
correction the repair made.

---

## STEP 2 — The masthead

Look at the top bar on any page.

- [ ] A small **dark teal square with four white bars**, left of the word
      "reportscope". Not a dash — if you see a dash, step 0a was skipped
- [ ] Far right: **"◐ Dark"** (or "◐ Light" if your OS is in dark mode)
- [ ] Browser tab shows the same square mark as a favicon

## STEP 3 — Theme

- [ ] Click the toggle. The whole page inverts. Label flips to the other word
- [ ] **Reload.** The theme persists
- [ ] Open a private window. It follows your **OS** setting, and there is
      **no white flash** before it renders dark
- [ ] In dark mode, the figures in the table are still legible and the
      sparklines still visible

## STEP 4 — Type and alignment

Open `https://reportscope.onrender.com/search?drug=atorvastatin`

- [ ] Look down the **ROR [95% CI]** column. The decimal points **line up**
      vertically. That is self-hosted Inter with tabular figures; previously
      the column drifted
- [ ] Body text is Inter, not the Windows default

---

## STEP 5 — The drug facts strip *(new)*

Same page, **above** the "Important limitation" panel. A bordered strip:

- [ ] **DRUGS / ON THE US MARKET** → `29.6 years  since 1996`
- [ ] **OPEN FDA RECALLS** → `8`
- [ ] A caveat paragraph stating a recall concerns **batches** and a shortage
      concerns **supply**, and that neither says the medicine is unsafe

### 5b. A drug with more going on

```
https://reportscope.onrender.com/search?drug=acetaminophen
```

- [ ] **ON THE US MARKET** → `57.7 years`
- [ ] **OPEN FDA RECALLS** → a count, with **`5 Class I`** beneath it
- [ ] **SUPPLY** → `To Be Discontinued`

### 5c. A withdrawn drug

```
https://reportscope.onrender.com/search?drug=cerivastatin
```

- [ ] **MARKET STATUS** → `No longer listed`, with *"figures here are
      historical"*
- [ ] **ON THE US MARKET** → `29.1 years  since 1997`

---

## STEP 6 — The statistics *(new)*

### 6a. EB05 column — needs a window ≥1000px

On the atorvastatin page, the table should read left to right:

`Reaction (PT) | a (reports) | ROR [95% CI] | IC025 | EB05 | Strength | Trend | Reported outcomes | Reasons to doubt it`

- [ ] **EB05** column is present
- [ ] Hover its header. The tooltip explains it is the FDA's own screening
      measure, its prior is estimated from the whole table, and the
      conventional bar is 2

Top three rows by IC025 should be:

| Reaction | a | ROR | IC025 | EB05 |
|---|---|---|---|---|
| TYPE 2 DIABETES MELLITUS | 12,041 | 21.51 | 3.76 | 13.61 |
| RHABDOMYOLYSIS | 6,123 | 6.59 | 2.47 | 5.58 |
| FOURNIER'S GANGRENE | 314 | 5.62 | 2.14 | 4.52 |

- [ ] All fifteen figures match

### 6b. BCPNN fixed the cerivastatin ranking — the headline result

On the **cerivastatin** page, sort by **IC025** (click the header).

- [ ] The **top row is RHABDOMYOLYSIS**, IC025 **4.28** — the reason the drug
      was withdrawn in 2001
- [ ] **AMYOTROPHIC LATERAL SCLEROSIS is NOT in the top three**, despite
      having by far the largest ROR on the page (**394.49**)

That is the whole point. The old formula ranked the ALS row **first** at IC025
7.94. It is a litigation artifact resting on 14 reports, and the Bayesian prior
demotes it on the statistics alone.

- [ ] Now sort by **EB05**. **ALS jumps to the top (123.07).** The two measures
      disagree, and that disagreement is expected and documented — MGPS shrinks
      toward an expected count that is near zero for a drug with only ~366
      reports in the whole database

---

## STEP 7 — Reported outcomes column

> **SKIP FOR NOW.** This column is being rebuilt. The first version read counts
> from a response capped at 1,000 reaction buckets, so any reaction below the
> cut-off was stored as zero — indistinguishable from "none reported".
> Measured: atorvastatin × TYPE 2 DIABETES MELLITUS stored 0 hospitalisations
> against a true **792**. Do not test this column until the rebuild is
> confirmed; exact expected values will be added here then.

---

## STEP 8 — Label interactions *(new, and the biggest feature)*

```
https://reportscope.onrender.com/worksheet
```

Enter, exactly:

- **Medicines:** `ibuprofen, ciprofloxacin, warfarin, vitamin d`
- **Symptoms:** `dizziness, syncope`

Submit.

- [ ] A panel **above** the symptom blocks: *"Taken together — what the
      official labels say"*
- [ ] **Exactly three pairs**, in this order:
      `ibuprofen + warfarin`, `ciprofloxacin + ibuprofen`,
      `ciprofloxacin + warfarin`
- [ ] **`vitamin d` appears in NO pair.** This is the test — it is the
      harmless concomitant and must produce nothing
- [ ] Each pair shows a **quoted sentence** from a real label, attributed
      (*"from the ciprofloxacin label, which mentions 'NSAID'"*)
- [ ] A closing note says a combination appearing here does **not** mean it is
      wrong to take them together, and that absence is **not** clearance
- [ ] The words **"seek medical care now"** appear

### 8b. The framing that must not break

- [ ] Medicines appear in the **order you typed them**, nowhere ranked
- [ ] **No strength badge** in any worksheet cell
- [ ] Each symptom block states how **unspecific** that symptom is before
      naming any medicine
- [ ] No page anywhere names one of your medicines as the likely cause

---

## STEP 9 — Print

Still on the worksheet result, switch to **dark mode**, then **Ctrl + P**.

- [ ] The preview is **light, on white, fully legible**
- [ ] The entry form and the theme toggle are **gone**
- [ ] Every disclaimer, the privacy note and the data date are **still there**

A regression here produces a blank sheet of paper, which is why it is a
step of its own.

---

## STEP 10 — Security fixes you can check from a browser

### 10a. Registration no longer reveals who has an account

1. Register with an address you control → you should land on
   **"Check your email"**, *not* the dashboard
- [ ] Confirmation email arrives in the **inbox**
2. Click the link → **"Address confirmed"** on the sign-in page
- [ ] You are **not** signed in. Visiting `/my` sends you to sign-in
3. Sign in manually, add a tracked drug
4. **Register the same address again**
- [ ] **Identical** "Check your email" page
- [ ] Your inbox receives *"Someone tried to create a reportscope account"*

Step 4 is the actual test. Any difference between step 1 and step 4 is a leak.

### 10b. Password reset evicts other sessions

- [ ] Sign in on your phone **and** your laptop
- [ ] Reset the password from one of them
- [ ] The **other** one is signed out

### 10c. Login throttle

- [ ] Enter a wrong password six times in a row → the sixth is refused with
      *"Too many attempts from this address"*

### 10d. Denial of service — paste this URL

```
https://reportscope.onrender.com/search?drug=.%2B.%2B.%2B.%2B.%2B.%2B.%2B.%2B.%2B.%2Bzz
```

- [ ] Responds in **well under a second**. Before the fix this took **31.3
      seconds** and froze the entire site for everyone

### 10e. The admin endpoint stays invisible

- [ ] `.../admin/analytics?admin_code=wrong` → **404**
- [ ] `.../admin/analytics?admin_code=wrÖng` → **404**, not 500. A 500 would
      tell an attacker a code is configured

---

## STEP 11 — Shareability

- [ ] Paste `https://reportscope.onrender.com` into WhatsApp, Slack or
      LinkedIn. You should get a **card with the mark, a title and a
      description** — not a bare URL

---

## What to report back

For anything that fails, note: the step number, the browser, the **window
width**, and whether you hard-refreshed. Those three explain most surprises
before anyone touches code.
