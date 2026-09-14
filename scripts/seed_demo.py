"""
Create five demo accounts with realistic medication lists.

    python scripts/seed_demo.py

For walking someone through the signed-in half of the tool without asking them
to register first, and for checking the dashboard renders against real data
rather than a drug with one reaction.

The five lists are not arbitrary. Each one exercises something the dashboard has
to handle:

  atorvastatin + metformin + lisinopril   ordinary polypharmacy, and the
                                          indication-confounding case that
                                          undiagnosed rows would surface
  semaglutide                             the focus class, heavily reported
  clozapine + lorazepam                   boxed warnings and serious events
  warfarin + ibuprofen                    a pair with a known interaction
  sertraline                              a common drug with a long tail

Addresses use .test, which is reserved by RFC 6761 and cannot resolve. Seeding
can therefore never send mail to a real person, which matters on an instance
that also has live testers on it.

Passwords are random and printed once. Re-running resets them, so the printed
credentials always work.
"""

from __future__ import annotations

import logging
import secrets
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
from sqlalchemy import select  # noqa: E402

from src.models import (  # noqa: E402
    Notification,
    SearchHistory,
    User,
    UserMedication,
    get_session,
    hash_password,
    init_db,
    utcnow,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

CONSENT_VERSION = "2026-09-14"

DEMOS = [
    ("demo.polypharmacy@rxsignal.test", ["atorvastatin", "metformin", "lisinopril"],
     ["lipitor", "metformin", "vitamin d"]),
    ("demo.glp1@rxsignal.test", ["semaglutide"],
     ["ozempic", "wegovy", "semaglutide"]),
    ("demo.psych@rxsignal.test", ["clozapine", "lorazepam"],
     ["clozapine", "ativan"]),
    ("demo.anticoag@rxsignal.test", ["warfarin", "ibuprofen"],
     ["warfarin", "ibuprofen", "coumadin"]),
    ("demo.ssri@rxsignal.test", ["sertraline"],
     ["zoloft", "sertraline"]),
]


def _password() -> str:
    """Readable enough to type from a screen, random enough not to be a default."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def _top_pairs(df: pd.DataFrame, drug: str, n: int = 2) -> list[dict]:
    """
    Real strong, bias-checked rows for this drug, so the demo shows real data.

    Pairs flagged for indication confounding are left out. The top diagnosed
    strong row for atorvastatin is TYPE 2 DIABETES MELLITUS, which is an
    artefact of statins being prescribed to diabetics; on the results page it
    arrives with a badge saying so, but as a seeded alert it would be a
    demonstration of the exact misreading this tool exists to prevent.
    """
    if df.empty or "drug" not in df.columns:
        return []
    sub = df[df["drug"] == drug]
    if "diagnosed" in sub.columns:
        sub = sub[sub["diagnosed"].fillna(False).astype(bool)]
    if "tier" in sub.columns:
        sub = sub[sub["tier"] == "strong"]
    if "indication_overlap" in sub.columns:
        sub = sub[~sub["indication_overlap"].fillna(False).astype(bool)]
    if sub.empty:
        return []
    sub = sub.sort_values("IC025", ascending=False).head(n)
    return [
        {
            "reaction_pt": str(row["reaction_pt"]),
            "ic025": float(row["IC025"]),
            "a": int(row["a"]),
        }
        for _, row in sub.iterrows()
    ]


def main() -> int:
    init_db()
    db = get_session()

    from src.app import _load_results

    df = _load_results()
    if df.empty:
        print("  WARNING: no scored results found. Dashboards will be empty.")
    catalogue = set(df["drug"].unique()) if not df.empty and "drug" in df.columns else set()

    now = utcnow()
    credentials: list[tuple[str, str]] = []

    for email, drugs, queries in DEMOS:
        password = _password()
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                email_verified=True,
                password_hash=hash_password(password),
                created_at=now,
                is_active=True,
                consent_at=now,
                consent_version=CONSENT_VERSION,
                newsletter_opt_in=True,
                notify_opt_in=True,
            )
            db.add(user)
            db.flush()
        else:
            user.password_hash = hash_password(password)
            user.email_verified = True
            user.is_active = True

        credentials.append((email, password))

        existing = {m.drug for m in user.medications}
        for drug in drugs:
            if drug not in existing:
                db.add(UserMedication(user_id=user.id, drug=drug, added_at=now))

        if not db.scalar(
            select(SearchHistory.id).where(SearchHistory.user_id == user.id)
        ):
            for query in queries:
                matched = query if query in catalogue else None
                db.add(
                    SearchHistory(
                        user_id=user.id,
                        drug_query=query,
                        matched_drug=matched,
                        created_at=now,
                    )
                )

        if not db.scalar(
            select(Notification.id).where(Notification.user_id == user.id)
        ):
            for kind, pair in zip(
                ("new_signal", "strengthened"), _top_pairs(df, drugs[0])
            ):
                detail = (
                    f"reported disproportionately at strong strength, on "
                    f"{pair['a']:,} reports"
                    if kind == "new_signal"
                    else f"moved from moderate to strong, on {pair['a']:,} reports"
                )
                db.add(
                    Notification(
                        user_id=user.id,
                        drug=drugs[0],
                        reaction_pt=pair["reaction_pt"],
                        kind=kind,
                        detail=detail,
                        ic025=pair["ic025"],
                        prev_ic025=(
                            None if kind == "new_signal" else round(pair["ic025"] - 0.7, 4)
                        ),
                        created_at=now,
                    )
                )

    db.commit()

    print("\n  Demo accounts (addresses are on the reserved .test domain and")
    print("  cannot receive mail). Passwords are new on every run.\n")
    for email, password in credentials:
        print(f"      {email:<34} {password}")
    print("\n  Sign in at /login. Delete them from /account when you are done.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
