# Module 0 — Domain Primer

**Read this before writing any pipeline code.** Every trap in this project is a domain
trap wearing an engineering costume. You cannot debug what you cannot name.

## A correction to the framing, first

You asked for "biology prerequisites." Honest answer: **biology is the smallest part.**
The knowledge this project actually needs breaks down roughly:

| Discipline | Share | Why it bites you |
|---|---|---|
| Clinical coding & terminology (MedDRA, drug nomenclature) | ~40% | Wrong joins, split counts, diluted signals |
| Pharmacovigilance & epidemiology | ~35% | Wrong conclusions stated confidently |
| Actual biology/pharmacology (GLP-1) | ~15% | Can't tell plausible from spurious |
| Regulatory definitions | ~10% | Misread `serious`, `drugcharacterization` |

So: real biology is Tier 2 below, and it is genuinely needed — but the concepts that
will actually break your pipeline are Tier 1.

---

# TIER 1 — Non-negotiable concepts

## 1.1 Adverse Event != Side Effect != Adverse Drug Reaction

This is the single most important idea in the project, and it is the entire basis of
your "What this does not prove" section.

- **Adverse Event (AE):** *any* unfavourable medical occurrence in a patient who was
  taking a drug. **Causality is not implied, not assessed, and not claimed.** If you
  take Ozempic and get hit by a bus, that is a legitimate adverse event.
- **Adverse Drug Reaction (ADR):** an event where a causal relationship is at least
  *reasonably possible*. Requires clinical judgement.
- **Side effect:** a known, label-documented pharmacological effect.

**FAERS — the database behind this API — contains AEs.** Not ADRs. The submitter's
suspicion is recorded, but nobody adjudicated it. This is why "drug X causes Y" is
never an available conclusion, no matter how extreme the ROR.

> One-sentence version for your README: *a high score means this pair is reported
> together more than the database's background rate predicts — which is a statement
> about reporting behaviour, not about human physiology.*

## 1.2 Confounding by indication

The reason a drug was prescribed is often correlated with the event.

We measured this live — reported indications for semaglutide reports:

```
  28309  Product used for unknown indication
  21762  Type 2 diabetes mellitus
   5572  Weight control
   2334  Hypertension
```

Diabetics get neuropathy, retinopathy, and renal failure *from diabetes*. Those will
surface as "signals" for every antidiabetic drug. The comparator (everyone else in
FAERS) is not diabetic, so the contrast is contaminated.

**Mitigation:** compare against an *active comparator* — other antidiabetics — instead
of the whole database. Module 7.

## 1.3 MedDRA: the reaction vocabulary

Reactions are coded in **MedDRA**, a 5-level hierarchy:

```
SOC     System Organ Class      "Gastrointestinal disorders"     (27 total)
 HLGT   High-Level Group Term   "Gastrointestinal motility..."
  HLT   High-Level Term         "GI atony and hypomotility"
   PT   Preferred Term          "IMPAIRED GASTRIC EMPTYING"  <-- API gives you THIS
    LLT Lowest Level Term       "stomach paralysis", verbatim reporter text
```

`patient.reaction.reactionmeddrapt` is the **PT** level.

**Why this wrecks naive analysis — verified, and not how you'd guess.** We measured it.
There is **no unqualified `GASTROPARESIS` PT** in FAERS — the query returns NOT_FOUND
across all 20.7M reports, in every case variant. What exists is:

```
  5,512 (GLP-1) / 12,585 (all)   IMPAIRED GASTRIC EMPTYING     ROR 37.98
    817 /  1,882                 EARLY SATIETY                 ROR 36.98
    813 /  2,202                 GASTROINTESTINAL HYPOMOTILITY ROR 28.21
    952 / 12,144                 ILEUS                         ROR  4.10
     36 /    362                 DIABETIC GASTROPARESIS        ROR  5.31
     22 /  2,069                 SUBILEUS                      ROR  0.52
  ------------------------------------------------------------------------
  8,322 / 35,644                 naive union of all eight      ROR 14.93
```

Two findings that change the design:

1. **The strong signal is under a name nobody uses publicly.** `IMPAIRED GASTRIC
   EMPTYING` (ROR 38) is the real one. Querying the news word "gastroparesis" finds
   362 reports and concludes the concern is overstated.
2. **Naive grouping DESTROYS the signal here** — 14.93 is far below the best components
   (38, 37, 28), because pooling drags a strong term toward weak and negative ones.
   "Group related terms" is a hypothesis to test per case, not a rule.

Rule that follows: **never hardcode a term you haven't seen in `count` output.**
`NOT_FOUND` is a naming problem far more often than a finding. Also note
`DIABETIC GASTROPARESIS` embeds its own cause — the coding attributes the event to
diabetes before any analysis starts.

MedDRA is licensed, so we don't have the official hierarchy. Grouping maps are
hand-built, published in full, and results ship grouped AND ungrouped.

**Also: not every PT is a medical event.** Live from the API, semaglutide's #2 most
reported "reaction":

```
   7325  OFF LABEL USE
```

That is a *reporting artifact*, not a symptom. So are `DRUG INEFFECTIVE`,
`PRODUCT DOSE OMISSION`, `NO ADVERSE EVENT`. These need a stoplist.

## 1.4 Drug nomenclature — four different names for one thing

| Layer | Example | Field |
|---|---|---|
| Brand / trade | `OZEMPIC`, `WEGOVY`, `RYBELSUS` | `medicinalproduct` (free text) |
| Generic / INN | `semaglutide` | `openfda.generic_name` |
| Active substance | `SEMAGLUTIDE` | `activesubstance.activesubstancename` |
| Salt / ester form | `semaglutide sodium` | varies |

`medicinalproduct` is **free text typed by a human**. Expect misspellings, dosage
strings glued on, and language variants. `openfda.*` fields are FDA-normalised — but
**they are not always present** (we measured one report carrying openfda on only 9 of
16 drugs). Your resolver needs a fallback path.

**Class systems** (all under `openfda.pharm_class_*`):

- **EPC** — Established Pharmacologic Class -> `GLP-1 Receptor Agonist [EPC]`
- **MoA** — Mechanism of Action -> `Glucagon-like Peptide-1 (GLP-1) Agonists [MoA]`
- **PE** — Physiologic Effect
- **CS** — Chemical Structure

Verified live: the class string is `GLP-1 Receptor Agonist [EPC]` (421,338 reports).
The spelled-out `Glucagon-Like Peptide-1 Receptor Agonist [EPC]` returns NOT_FOUND.
**Guess the label, get nothing.**

## 1.5 `serious` is a legal term, not a severity judgement

`serious: 1` (serious) / `2` (non-serious). The regulatory definition: an event is
serious if it results in death, is life-threatening, requires or prolongs
hospitalisation, causes persistent disability, causes a congenital anomaly, or is
"other medically important."

A migraine that put someone in A&E is **serious**. A mild heart attack managed at home
might not be flagged. Do not read `serious` as "bad."

## 1.6 One report != one drug != one event

We measured three real reports:

```
  rec0: drugs=10  reactions=20
  rec1: drugs=16  reactions= 4
  rec2: drugs=40  reactions=47   <-- 40 x 47 = 1,880 pairs from ONE report
```

**The API matches at the report level.** Searching `generic_name:"semaglutide"` returns
reports where semaglutide appears *anywhere in the drug list*. Proof — counting drug
names inside those reports returns:

```
  66038  OZEMPIC        <-- the target
   7646  METFORMIN      <-- co-medication
   3558  ATORVASTATIN   <-- co-medication
   3159  ASPIRIN        <-- co-medication
```

Metformin is not a semaglutide alias. **This is the trap that invalidates most naive
analyses of FAERS.** The fix is `drugcharacterization`:

```
  1 = Suspect       72,856    <-- reporter believes this drug is implicated
  2 = Concomitant   26,169    <-- patient was merely also taking it
  3 = Interacting      223
  4 = ???                8    <-- not in the E2B standard. Dirty data is real.
```

---

# TIER 2 — The actual biology you need

## 2.1 What GLP-1 is

**Glucagon-like peptide-1** is an *incretin* — a gut hormone released by intestinal
**L-cells** when food arrives. Native GLP-1 is destroyed within ~2 minutes by the
enzyme **DPP-4**. The drug class consists of engineered analogues that resist DPP-4 and
therefore persist for days.

It does four things:

1. **Increases insulin secretion — glucose-dependently.** Only when blood glucose is
   high. *This is why GLP-1 drugs rarely cause hypoglycaemia on their own* — a testable
   prediction for our data.
2. **Suppresses glucagon** from pancreatic alpha-cells, reducing hepatic glucose output.
3. **Slows gastric emptying.** Food leaves the stomach more slowly.
4. **Increases satiety**, via receptors in the hypothalamus and brainstem.

## 2.2 Why this predicts the adverse event profile

This is the payoff — **the biology tells you which signals are mechanistically
expected**, which is one of the Bradford Hill criteria (plausibility).

Action #3 (slowed gastric emptying) *is the therapeutic mechanism*. Push it too far and
you get **nausea, vomiting, constipation, early satiety, and — at the extreme —
gastroparesis and ileus**. These are not mysterious off-target toxicities. They are the
drug's intended effect, overshot.

Our live data agrees. Top reported reactions for semaglutide:

```
  10674  NAUSEA
   6964  VOMITING
   6462  DIARRHOEA
   4905  DECREASED APPETITE
   4724  CONSTIPATION
```

Five of the top six are GI. **The biology predicted the data.** That is good evidence
the pipeline is reading reality and not noise.

## 2.3 Receptor distribution -> where surprises come from

GLP-1 receptors are not only in the pancreas. They appear in the **GI tract, heart,
kidney, brain, and thyroid C-cells**. Wherever the receptor is, an effect is possible.
Known and debated signals, worth treating as *prior hypotheses* rather than conclusions:

- **Gastroparesis / ileus** — ileus added to labelling around 2023
- **Pancreatitis** — long-debated, evidence inconsistent
- **Medullary thyroid carcinoma** — boxed warning, based on *rodent* C-cell tumours;
  human relevance unresolved
- **NAION** (sudden optic-nerve ischaemia) — an emerging signal around 2024-25
- **Pulmonary aspiration under anaesthesia** — food retained in a "fasted" stomach

> ⚠️ Treat every regulatory claim above as *needs verification*. Label status changes,
> and my knowledge has a cutoff. We verify against current labelling before publishing
> anything, and we cite it. This list is a hypothesis register, not a fact sheet.

## 2.4 The agents — and one that will fool you

| Molecule | Brands | Note |
|---|---|---|
| semaglutide | Ozempic (T2D), Wegovy (obesity), Rybelsus (oral) | |
| liraglutide | Victoza, Saxenda | |
| dulaglutide | Trulicity | |
| exenatide | Byetta, Bydureon | first-in-class |
| **tirzepatide** | Mounjaro, Zepbound | **dual GIP/GLP-1 — arguably not the same class** |

Two consequences you must handle:

1. **Tirzepatide is a different pharmacology.** Including it is a defensible choice or a
   distorting one — either way, *declare it*.
2. **Same molecule, different brand, different patient.** Ozempic is prescribed for
   diabetes; Wegovy is the identical molecule for obesity. Different populations,
   different comorbidities, different reporting cultures. Collapsing them into
   "semaglutide" silently merges two different populations.

---

# TIER 3 — Pharmacovigilance epidemiology

## 3.1 Why disproportionality exists at all

**You have no denominator.** FAERS tells you 10,674 nausea reports mention semaglutide.
It does *not* tell you how many people took semaglutide. Without that you cannot
compute a rate, and without a rate you cannot compute risk.

The workaround: use **the database as its own control**. Ask not "how often does this
happen" but "**is this pair over-represented relative to everything else in here**."
Hence *Reporting* Odds Ratio — reporting, not risk. The word is load-bearing.

## 3.2 The 2x2 table

For drug D and reaction R:

|  | Reaction R | Not R |
|---|---|---|
| **Drug D** | a | b |
| **Not D** | c | d |

- **ROR** = (a/b) / (c/d) = ad / bc
- **PRR** = [a/(a+b)] / [c/(c+d)]
- **IC** (Information Component) = log2( observed / expected ) — Bayesian, shrinks
  small counts toward zero. **This is what WHO actually uses**, and it is why a pair
  with a=3 does not outrank a pair with a=3,000.

Worked example we computed live, over all 20,692,690 reports — **statins x
rhabdomyolysis**:

```
a = 8,820      b = 426,752      c = 32,257      d = 20,224,861
ROR = (8820/426752) / (32257/20224861) = 12.96
```

Rhabdomyolysis from statins is real, known, and label-documented. **Our pipeline must
reproduce ROR ~= 13 or it is broken.** That is our positive control (Module 6).

## 3.3 The biases that will fool you

| Bias | What happens |
|---|---|
| **Notoriety / Weber effect** | Reporting spikes after media coverage. Reports peak in a drug's first ~2 years regardless of safety. |
| **Stimulated reporting** | Litigation adverts drive filings. A law-firm campaign looks identical to a safety event in the data. |
| **Channeling** | Sicker patients get newer drugs, so their events look drug-related. |
| **Masking / competition** | One drug with a huge signal *suppresses* others' scores, because it inflates the `c` and `d` cells. |
| **Duplicates** | The same case filed by patient, doctor, and manufacturer. There is a `duplicate` field — it is not reliable. |
| **Under-reporting** | Only a small and non-random fraction of real events are ever reported. |

Notoriety is testable: plot reports per month. A genuine pharmacological signal accrues
steadily with uptake; a news cycle is a **spike**. We build that detector in Module 7.

## 3.4 When is a signal "a signal"?

Conventional screening thresholds — these are *screening* rules, not proof:

- **a >= 3** — fewer than 3 cases is noise
- **lower bound of the 95% CI on ROR > 1**
- **IC025 > 0** — the Bayesian equivalent, more robust at low counts

## 3.5 What would move a hypothesis toward causality

Bradford Hill, the honest ladder. Disproportionality gives you **one rung**:

| Criterion | Can we? |
|---|---|
| **Strength** — big ROR | ✅ we measure this |
| **Plausibility** — is there a mechanism? | ✅ Tier 2 gives us this |
| **Temporality** — did drug precede event? | ⚠️ partially, via onset dates |
| **Dose-response** | ❌ not reliably in FAERS |
| **Consistency** — replicated elsewhere? | ❌ needs other data sources |
| **Experiment** | ❌ needs a trial |

**We can climb two rungs of six.** Say so out loud.

---

# Check yourself

Work these out before Module 1. They are all answerable from what is above, and each
one maps to a bug you would otherwise ship. *(Answers deliberately withheld — attempt
them first, then we will go through them together.)*

1. `count=patient.reaction.reactionmeddrapt.exact` on a semaglutide search returns
   `NAUSEA 10674`. Why can you **not** say "10,674 people got nausea from semaglutide"?
   Name three separate reasons.
2. `Type 2 diabetes mellitus` = 21,762 and `TYPE 2 DIABETES MELLITUS` = 3,285 are
   different buckets. What is the percentage error if you ignore this, and what does it
   tell you about how `.exact` works?
3. A report has 40 drugs and 47 reactions. How many drug-reaction rows should the
   flattener emit, and why is the obvious answer wrong?
4. `DRUG INEFFECTIVE` scores a very high ROR for some drug. Is that a safety signal?
5. Which gives the higher ROR for `NAUSEA` — semaglutide vs. *all of FAERS*, or
   semaglutide vs. *other antidiabetics only*? Which number is more honest, and why
   might the more honest one be *smaller*?
6. You find `THYROID CANCER` with ROR 8.0 and a=12. The boxed warning says rodent
   studies. Is that warning evidence *for* your signal being real, or a reason to
   suspect it is an artifact? (Genuinely subtle — think about who reads labels.)
