# reportscope — tester guide

Thank you for testing this. It should take about twenty minutes. You do not
need any pharmacology or statistics to be useful here; the most valuable
feedback usually comes from somebody reading a page and finding it says
something it did not mean to say.

---

## What the tool does

Give it a drug and it searches 20.7 million reports in the FDA Adverse Event
Reporting System (FAERS) for reactions that appear alongside that drug far more
often than the rest of the database would predict. It ranks them, shows how many
reports each one rests on, and — for the top-ranked ones — measures the main
reasons the result might be misleading:

- whether the "reaction" is really the condition the drug is prescribed for
- whether the reports arrived steadily or in one burst after a news story
- whether the effect survives being compared against clinically similar drugs
  instead of against everything

You can create an account, list the drugs you take, and be told when the
reporting pattern for one of them changes.

## What it does not measure

This matters more than the feature list.

**It cannot measure risk.** FAERS records reports, not patients. Nothing in it
says how many people took a drug and were fine. Every rate, risk and incidence
figure needs that number, so none can be computed here — not approximately, not
with better methods, not at all.

**It cannot show cause.** A high score means a pair was reported together more
often than expected. Reporting is shaped by how new a drug is, by media
coverage, and by litigation, none of which is pharmacology.

**It is not a safety assessment of your medicines.** A reaction appearing next
to a drug on your list is not a prediction about you, and it is not a reason to
change anything. Take anything that worries you to your prescriber, not to this
tool.

**Coverage is finite.** 361 drugs have been scored. A drug that is missing has
not been examined — which is not the same as having been examined and found
clear.

---

## What to try

1. Search for a drug you know something about. A brand name is fine; Ozempic
   finds semaglutide.
2. Read the results table. Does anything about it read as more certain than the
   caveats say it is?
3. Create an account with a real address you can open. Confirm it from the
   email.
4. Add two or three of the drugs you actually take.
5. Open your dashboard. Read what it says about your own medicines.
6. Try to break the forms: a wrong password, an address that is already
   registered, a short password, a drug that does not exist, a reset link you
   have already used. Nothing should show you an error page with code 500.
7. Reset your password from the link on the sign-in page.
8. Delete your account from the account page, and check that signing in with it
   afterwards fails.

---

## Please flag anything alarming

**If anything on this site frightened you, or made you think differently about a
medicine you take, tell us — even if you later worked out it was fine.** That is
the single most important thing you can report. This tool puts reaction names
next to people's own prescriptions, and a page that reads as a warning when it
is only describing a reporting pattern is a defect, not a misunderstanding on
your part. Quote the wording that did it, and say what you thought it meant.

The same goes for anything that made a result look more solid than it is: a
number without its caveat, a badge that reads as a verdict, a heading that
sounds like a finding.

## How to report a problem

Send one email per issue to the address you were given, with:

- **What you did** — the page, and the drug or the form
- **What happened** — what you saw, ideally a screenshot
- **What you expected instead**
- The date and rough time, so it can be found in the logs

If you hit an error page, the page number on it (404, 429, 500) helps.

Do not include anything about your health you would not want written down. We
do not need your diagnosis to fix a layout problem, and the drugs you add to
your list are enough for anything else.

## What we store

Your email address, a hash of your password (never the password), the drugs you
choose to track, the searches you make while signed in, and the alerts we have
sent you. Deleting your account deletes all of it immediately. Nothing is passed
to anyone else.
