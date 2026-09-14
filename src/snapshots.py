"""
M11: change detection.

The scored table always describes now. "New" is a statement about two points in
time, so it cannot be read off a single extract however carefully it is scored:
a pair at IC025 3.1 looks identical whether it has sat there for four years or
appeared last quarter. This module keeps the history that makes the difference
visible, and it is the only part of the application that can say a signal is
emerging rather than merely present.

Three changes are worth telling a user about:

  new_signal      absent, or below the floor, and now moderate or strong
  strengthened    IC025 up by at least 0.5 *and* a tier boundary crossed
  newly_labelled  the regulator's label now describes what the reports said

The conjunction in `strengthened` is deliberate. IC025 drifts upward for any
pair that keeps accruing reports, because the confidence bound tightens as the
count grows; movement alone is not news. Requiring a tier crossing as well means
the message corresponds to a change a reader would agree was a change.

Run one cycle with:  python -m src.snapshots
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import SQLAlchemyError

from src.models import (
    Notification,
    SignalSnapshot,
    User,
    UserMedication,
    get_session,
    utcnow,
)

log = logging.getLogger("rxsignal.snapshots")

TIER_RANK = {"none": 0, "weak": 1, "moderate": 2, "strong": 3}

IC025_RISE = 0.5


def _as_bool_or_none(value) -> bool | None:
    """
    Keep "not checked" distinct from "checked and absent".

    labelled arrives as pandas' nullable boolean, where NA means the label was
    never located. Coercing that to False would assert the reaction is missing
    from a label nobody read, and newly_labelled fires on exactly that
    transition.
    """
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return bool(value)


def record_snapshot(df: pd.DataFrame, snapshot_date: date | None = None) -> int:
    """
    Persist one day's scored pairs. Returns the number of rows written.

    Only tier != 'none' is stored. The full table is 33,852 rows of which most
    are below every threshold, and keeping a daily copy of rows that can never
    produce a notification would multiply the database size for nothing. A pair
    that climbs out of 'none' arrives as an absent key on the previous date,
    which detect_changes already treats as new.

    Re-running for the same date replaces that date rather than failing, so a
    partial run can simply be repeated.
    """
    if df is None or df.empty:
        log.warning("no scored rows to snapshot")
        return 0

    when = snapshot_date or utcnow().date()
    required = {"drug", "reaction_pt", "IC025", "tier", "a"}
    missing = required - set(df.columns)
    if missing:
        log.error("scored table is missing %s", ", ".join(sorted(missing)))
        return 0

    sub = df[df["tier"].fillna("none") != "none"]
    sub = sub[sub["IC025"].notna()]
    if sub.empty:
        return 0

    has_labelled = "labelled" in sub.columns
    rows = []
    for _, row in sub.iterrows():
        rows.append(
            {
                "snapshot_date": when,
                "drug": str(row["drug"])[:120],
                "reaction_pt": str(row["reaction_pt"])[:200],
                "ic025": float(row["IC025"]),
                "tier": str(row["tier"])[:16],
                "a": int(row["a"]),
                "labelled": _as_bool_or_none(row["labelled"]) if has_labelled else None,
            }
        )

    db = get_session()
    try:
        db.execute(delete(SignalSnapshot).where(SignalSnapshot.snapshot_date == when))
        db.execute(insert(SignalSnapshot), rows)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("snapshot failed for %s", when)
        return 0

    log.info("snapshot %s: %d rows", when, len(rows))
    return len(rows)


def snapshot_dates(limit: int = 10) -> list[date]:
    """Dates that have a snapshot, newest first."""
    return list(
        get_session().scalars(
            select(SignalSnapshot.snapshot_date)
            .distinct()
            .order_by(SignalSnapshot.snapshot_date.desc())
            .limit(limit)
        )
    )


def _load(when: date) -> dict[tuple[str, str], SignalSnapshot]:
    rows = get_session().scalars(
        select(SignalSnapshot).where(SignalSnapshot.snapshot_date == when)
    )
    return {(r.drug, r.reaction_pt): r for r in rows}


def detect_changes(prev_date: date, curr_date: date) -> list[dict]:
    """
    Compare two snapshots. Returns one dict per change, ready for fan-out.

    A pair can appear twice -- a signal that both strengthened and became
    labelled is two different pieces of news, and collapsing them would drop
    the one that says the regulator has caught up.
    """
    prev = _load(prev_date)
    curr = _load(curr_date)
    if not curr:
        log.warning("no snapshot rows for %s", curr_date)
        return []

    changes: list[dict] = []
    for key, now in curr.items():
        before = prev.get(key)
        now_rank = TIER_RANK.get(now.tier, 0)
        prev_rank = TIER_RANK.get(before.tier, 0) if before else 0

        if before is None or before.tier == "none":
            if now_rank >= TIER_RANK["moderate"]:
                changes.append(
                    {
                        "drug": now.drug,
                        "reaction_pt": now.reaction_pt,
                        "kind": "new_signal",
                        "detail": (
                            f"reported disproportionately for the first time at "
                            f"{now.tier} strength, on {now.a:,} reports"
                        ),
                        "ic025": now.ic025,
                        "prev_ic025": None,
                    }
                )
        else:
            if (now.ic025 - before.ic025) >= IC025_RISE and now_rank > prev_rank:
                changes.append(
                    {
                        "drug": now.drug,
                        "reaction_pt": now.reaction_pt,
                        "kind": "strengthened",
                        "detail": (
                            f"moved from {before.tier} to {now.tier}, on "
                            f"{now.a:,} reports"
                        ),
                        "ic025": now.ic025,
                        "prev_ic025": before.ic025,
                    }
                )
            if before.labelled is False and now.labelled is True:
                changes.append(
                    {
                        "drug": now.drug,
                        "reaction_pt": now.reaction_pt,
                        "kind": "newly_labelled",
                        "detail": (
                            "now described in the product label; it was not "
                            "found there at the previous check"
                        ),
                        "ic025": now.ic025,
                        "prev_ic025": before.ic025,
                    }
                )

    log.info("%s -> %s: %d change(s)", prev_date, curr_date, len(changes))
    return changes


def fan_out_notifications(changes: list[dict]) -> int:
    """
    Create one notification per (tracking user, change). Returns rows created.

    Users who switched change alerts off get nothing, including in the
    application: the toggle means "do not tell me", not "do not email me".
    Identical notifications are skipped, so re-running a cycle does not fill a
    dashboard with duplicates of news the user has already seen.
    """
    if not changes:
        return 0

    db = get_session()
    by_drug: dict[str, list[dict]] = {}
    for change in changes:
        by_drug.setdefault(change["drug"], []).append(change)

    created = 0
    now = utcnow()
    for drug, drug_changes in by_drug.items():
        user_ids = list(
            db.scalars(
                select(UserMedication.user_id)
                .join(User, User.id == UserMedication.user_id)
                .where(
                    UserMedication.drug == drug,
                    User.notify_opt_in.is_(True),
                    User.is_active.is_(True),
                )
            )
        )
        if not user_ids:
            continue

        for user_id in user_ids:
            for change in drug_changes:
                already = db.scalar(
                    select(Notification.id).where(
                        Notification.user_id == user_id,
                        Notification.drug == drug,
                        Notification.reaction_pt == change["reaction_pt"],
                        Notification.kind == change["kind"],
                    )
                )
                if already:
                    continue
                db.add(
                    Notification(
                        user_id=user_id,
                        drug=drug,
                        reaction_pt=change["reaction_pt"],
                        kind=change["kind"],
                        detail=change["detail"][:500],
                        ic025=change.get("ic025"),
                        prev_ic025=change.get("prev_ic025"),
                        created_at=now,
                    )
                )
                created += 1

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("notification fan-out failed")
        return 0

    if created:
        from src import analytics

        analytics.bump("notifications_created", amount=created)
    log.info("fan-out created %d notification(s)", created)
    return created


_BIAS_NOTES = {
    "indication_overlap": (
        "this reaction closely matches a condition the drug is prescribed for, "
        "so the figure is partly the illness rather than the drug"
    ),
    "notoriety_spike": (
        "more than a quarter of these reports arrived in a single month, which "
        "usually follows a news cycle rather than a change in the medicine"
    ),
}


def annotate_bias(changes: list[dict], df: pd.DataFrame) -> list[dict]:
    """
    Attach the bias diagnostics for each changed pair to its message.

    Snapshots carry scores, not diagnostics, so a change detected from them
    alone would announce atorvastatin x TYPE 2 DIABETES MELLITUS to somebody
    taking atorvastatin with nothing beside it -- a top-ranked, entirely
    spurious pair that exists because diabetics are prescribed statins. On the
    results page that row arrives wearing an "Indication confounding" badge. A
    notification has no badges, so the caveat has to travel inside the sentence.
    """
    if df is None or df.empty or not changes:
        return changes

    cols = {"indication_overlap", "notoriety_spike", "active_comparator_shrinkage"}
    if not cols & set(df.columns):
        return changes

    flags: dict[tuple[str, str], list[str]] = {}
    for _, row in df.iterrows():
        notes = [
            note for col, note in _BIAS_NOTES.items()
            if col in df.columns and row.get(col) is not None
            and not pd.isna(row.get(col)) and bool(row.get(col))
        ]
        shrink = row.get("active_comparator_shrinkage")
        if shrink is not None and not pd.isna(shrink) and float(shrink) >= 0.9:
            notes.append(
                "the effect all but disappears when compared against clinically "
                "similar drugs instead of against the whole database"
            )
        if notes:
            flags[(str(row["drug"]), str(row["reaction_pt"]))] = notes

    for change in changes:
        notes = flags.get((change["drug"], change["reaction_pt"]))
        if notes:
            change["detail"] = f"{change['detail']}. Read with care: " + "; ".join(notes)
    return changes


def run_cycle(snapshot_date: date | None = None) -> dict:
    """
    Snapshot today's table, compare it with the previous snapshot, notify.

    Intended for a scheduler after `run.py --score` has refreshed the parquet.
    The first run has nothing to compare against and correctly produces no
    notifications rather than announcing all 33,852 rows as new.
    """
    from src.app import _load_results
    from src.models import init_db

    init_db()
    when = snapshot_date or utcnow().date()
    df = _load_results()
    written = record_snapshot(df, when)

    dates = [d for d in snapshot_dates(limit=10) if d < when]
    if not dates:
        log.info("no earlier snapshot: nothing to compare yet")
        return {"rows": written, "changes": 0, "notifications": 0, "compared_with": None}

    previous = dates[0]
    changes = annotate_bias(detect_changes(previous, when), df)
    created = fan_out_notifications(changes)
    return {
        "rows": written,
        "changes": len(changes),
        "notifications": created,
        "compared_with": previous,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_cycle()
    print(
        f"  snapshot rows: {result['rows']:,}\n"
        f"  compared with: {result['compared_with'] or 'nothing (first run)'}\n"
        f"  changes:       {result['changes']:,}\n"
        f"  notifications: {result['notifications']:,}"
    )
