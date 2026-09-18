"""
Behavioural regression test for defects D-01 to D-08.

    python scripts/check_defects.py

Each of these was a real, reachable defect on the live site, and each is the
kind that passes a functional suite untouched: nothing crashes, every page
renders, and the application leaks who has an account, hands out a permanent
credential by email, or lets one address spend the whole memory allowance on
password guesses. So they are asserted on behaviour, not on the presence of a
fix.

Runs against a throwaway SQLite database, migrated from scratch, with SMTP
unset so nothing is delivered. It creates and destroys its own state and never
touches the configured DATABASE_URL.

Covers the security-relevant part of section 6 of docs/TEST-PLAN.md and the
abuse cases in section 8 that can be asserted without a browser. Sections 7
and 11 remain manual.
"""
from __future__ import annotations

import os
import re
import tempfile
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DB = Path(tempfile.gettempdir()) / "reportscope_defect_check.db"
if DB.exists():
    DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["RXSIGNAL_SECRET_KEY"] = "test-key-not-a-secret"
os.environ["RXSIGNAL_MAIL_OUTBOX"] = "1"   # mail -> data/outbox/, never sent
os.environ["RXSIGNAL_ADMIN_CODE"] = "adminsecret"
os.environ["RXSIGNAL_AUTH_RATE_LIMIT"] = "5"

import subprocess
subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
               check=True, capture_output=True)

from src.app import app                                     # noqa: E402
from src.models import User, get_session, utcnow            # noqa: E402
from sqlalchemy import select                               # noqa: E402

app.config["TESTING"] = True
fails: list[str] = []


def check(cond, label, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        fails.append(label)


def reset_throttle():
    """Clear the auth buckets between sections.

    Every request here comes from 127.0.0.1 and the D-07 limiter is per
    address across all auth paths, so the earlier sections exhaust the 5/min
    budget and later ones see 429 instead of what they are testing. That the
    first run failed this way is itself evidence D-07 works.
    """
    from src import app as m
    m._hits.clear()
    m._auth_hits.clear()


def token(client, path="/register"):
    page = client.get(path, base_url="http://localhost").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', page)
    return m.group(1) if m else ""


BASE = "http://localhost"

print("=== D-01: registration must not disclose membership ===")
with app.test_client() as c:
    t = token(c)
    r1 = c.post("/register", base_url=BASE, data={
        "csrf_token": t, "email": "alice@example.com",
        "password": "correct horse battery", "confirm": "correct horse battery",
        "consent": "on"})
    b1 = r1.get_data(as_text=True)
with app.test_client() as c:
    t = token(c)
    r2 = c.post("/register", base_url=BASE, data={
        "csrf_token": t, "email": "alice@example.com",
        "password": "another passphrase here", "confirm": "another passphrase here",
        "consent": "on"})
    b2 = r2.get_data(as_text=True)

check(r1.status_code == r2.status_code, "same status code",
      f"{r1.status_code} vs {r2.status_code}")
check("Check your email" in b1 and "Check your email" in b2,
      "both show the neutral check-email page")
check(len(b1) == len(b2), "identical response length", f"{len(b1)} vs {len(b2)}")
check("already has an account" not in b2.lower(), "no membership wording")
check("Set-Cookie" not in str(r1.headers) or "session" not in str(r1.headers.get("Set-Cookie", "")),
      "no session cookie on register (would itself leak)")

print()
print("=== D-02: /forgot must not differ by timing ===")
def timed_forgot(email):
    best = 9e9
    for _ in range(3):
        with app.test_client() as c:
            t = token(c, "/forgot")
            start = time.perf_counter()
            c.post("/forgot", base_url=BASE,
                   data={"csrf_token": t, "email": email})
            best = min(best, time.perf_counter() - start)
    return best

real = timed_forgot("alice@example.com")
fake = timed_forgot("nobody-here@example.com")
ratio = max(real, fake) / max(min(real, fake), 1e-9)
check(ratio < 3.0, "registered and unknown within 3x",
      f"real {real*1000:.1f}ms vs fake {fake*1000:.1f}ms = {ratio:.2f}x")

print()
reset_throttle()
print("=== D-03: admin gate must 404, not 500, on non-ASCII ===")
with app.test_client() as c:
    a1 = c.get("/admin/analytics?admin_code=wrong", base_url=BASE)
    a2 = c.get("/admin/analytics?admin_code=wr\u00d6ng", base_url=BASE)
    a3 = c.get("/admin/analytics?admin_code=adminsecret", base_url=BASE)
check(a1.status_code == 404, "ascii wrong code -> 404", str(a1.status_code))
check(a2.status_code == 404, "non-ascii wrong code -> 404", str(a2.status_code))
check(a3.status_code == 200, "correct code -> 200", str(a3.status_code))
with app.test_client() as c:
    t = token(c, "/login")
    bad = c.post("/login", base_url=BASE, data={
        "csrf_token": "wr\u00d6ng-token", "email": "x@y.z", "password": "p"})
check(bad.status_code == 400, "non-ascii CSRF token -> 400 not 500",
      str(bad.status_code))

print()
reset_throttle()
print("=== D-04: verification link expires and grants no session ===")
db = get_session()
user = db.scalar(select(User).where(User.email == "alice@example.com"))
fresh = user.verify_token
with app.test_client() as c:
    v = c.get(f"/verify/{fresh}", base_url=BASE)
    body = v.get_data(as_text=True)
    check(v.status_code == 200, "fresh link accepted", str(v.status_code))
    check("Address confirmed" in body, "shows the confirmation notice")
    check("Sign in" in body, "lands on the sign-in form")
    who = c.get("/my", base_url=BASE)
    check(who.status_code in (302, 401), "did NOT sign the visitor in",
          f"/my -> {who.status_code}")

db = get_session()
user = db.scalar(select(User).where(User.email == "alice@example.com"))
user.email_verified = False
user.verify_token = "stale-token-value-0123456789"
user.verify_sent_at = utcnow().replace(year=utcnow().year - 1)
db.commit()
with app.test_client() as c:
    old = c.get("/verify/stale-token-value-0123456789", base_url=BASE)
check(old.status_code == 410, "year-old link rejected with 410",
      str(old.status_code))

print()
reset_throttle()
print("=== D-05: password reset evicts existing sessions ===")
db = get_session()
user = db.scalar(select(User).where(User.email == "alice@example.com"))
user.email_verified = True
user.password_hash = __import__("src.models", fromlist=["x"]).hash_password("known passphrase")
db.commit()
uid = user.id

session_client = app.test_client()
t = token(session_client, "/login")
li = session_client.post("/login", base_url=BASE, data={
    "csrf_token": t, "email": "alice@example.com", "password": "known passphrase"})
mine = session_client.get("/my", base_url=BASE)
check(mine.status_code == 200, "signed in on client A", str(mine.status_code))

db = get_session()
user = db.scalar(select(User).where(User.id == uid))
user.reset_token = "reset-token-value-0123456789"
from src.models import token_expiry
user.reset_token_expires = token_expiry(hours=1)
db.commit()
reset_throttle()
with app.test_client() as c2:
    t2 = token(c2, "/reset/reset-token-value-0123456789")
    rp = c2.post("/reset/reset-token-value-0123456789", base_url=BASE, data={
        "csrf_token": t2, "password": "brand new passphrase",
        "confirm": "brand new passphrase"})
check(rp.status_code in (200, 302), "reset accepted", str(rp.status_code))

after = session_client.get("/my", base_url=BASE)
check(after.status_code in (302, 401), "client A session now REJECTED",
      f"/my -> {after.status_code}")

print()
reset_throttle()
print("=== D-07: login has its own throttle ===")
with app.test_client() as c:
    codes = []
    for i in range(8):
        t = token(c, "/login")
        r = c.post("/login", base_url=BASE, data={
            "csrf_token": t, "email": "alice@example.com", "password": "wrong"})
        codes.append(r.status_code)
check(429 in codes, "throttled within 8 POSTs", f"codes={codes}")
check(codes.count(429) >= 2, "stays throttled")

print()
print("=== D-08: rate-limit maps are swept ===")
from src import app as app_module
check(hasattr(app_module, "_sweep_rate_limits"), "sweep function exists")
app_module._last_sweep = 0.0
app_module._hits["203.0.113.99"].append(time.time() - 600)
app_module._sweep_rate_limits(time.time())
check("203.0.113.99" not in app_module._hits, "stale address removed")

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    raise SystemExit(1)
print("all defect checks passed")
