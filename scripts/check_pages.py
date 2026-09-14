"""
Check every page renders and carries the shared head.

    python scripts/check_pages.py

Exists because the page furniture is the kind of thing that breaks silently.
Before templates/_base.html, all seventeen pages carried their own doctype and
head, and they had drifted without anyone noticing: six wordings of the same
data-vintage footnote, two encodings of the same em-dash in <title>,
Bootstrap's JS on six pages out of seventeen, and no page anywhere with a meta
description or a social card. Nothing failed. It just looked unowned.

Three passes:

  1. Compile every template. Catches a syntax or inheritance error on the
     pages that need a signed-in session, without needing one. This is what
     would have caught the macro that a refactor dropped out of results.html:
     it rendered fine everywhere except the one page that used it.
  2. Render every public route and assert the head elements are present, that
     there is exactly one of each structural tag, and that canonical and
     noindex are the right way round.
  3. Assert no page template has grown its own scaffolding back.

Add a route to ROUTES when you add a page.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from src.app import app  # noqa: E402

# path, expected status, expected to be indexable
ROUTES = [
    ("/", 200, True),
    ("/search?drug=ozempic", 200, True),
    ("/search?drug=atorvastatin", 200, True),
    ("/reaction?q=RHABDOMYOLYSIS", 200, True),
    ("/compare?a=ozempic&b=metformin", 200, True),
    # /list is indexable, but ?drugs= is not in the canonical allowlist, so
    # every combination points back at the tool page rather than minting its
    # own indexable URL. Asserted explicitly below.
    ("/list?drugs=ozempic,metformin", 200, True),
    ("/worksheet", 200, False),
    ("/login", 200, False),
    ("/register", 200, False),
    ("/forgot", 200, False),
    ("/definitely-not-a-page", 404, False),
]

# (needle, human name) -- each must appear on every rendered page
REQUIRED = [
    ("favicon.svg", "favicon"),
    ("apple-touch-icon", "touch icon"),
    ("site.webmanifest", "web manifest"),
    ("inter-var-latin.woff2", "font preload"),
    ("og:image", "social image"),
    ("twitter:card", "twitter card"),
    ('name="description"', "meta description"),
    ("theme-color", "theme colour"),
    ("skip-link", "skip link"),
    ("app.js", "site script"),
    ("bootstrap.bundle.min.js", "bootstrap script"),
    # The theme has to be decided by a synchronous inline script in the head.
    # If this ever moves to an external or deferred file the page will paint
    # light and then repaint, which is the flash the arrangement exists to
    # avoid -- and nothing else would fail.
    ("reportscope-theme", "inline theme bootstrap"),
    ('content="light dark"', "color-scheme declaration"),
]

fails: list[str] = []


def check(cond: object, msg: str) -> None:
    if not cond:
        fails.append(msg)


def main() -> int:
    print("=== templates compile ===")
    names = sorted(p.name for p in (ROOT / "templates").glob("*.html"))
    for name in names:
        try:
            app.jinja_env.get_template(name)
            print(f"  ok    {name}")
        except Exception as exc:
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            fails.append(f"{name} does not compile: {exc}")

    print("\n=== routes render ===")
    app.config["TESTING"] = True
    with app.test_client() as client:
        for path, want, indexable in ROUTES:
            resp = client.get(path, base_url="http://localhost")
            body = resp.get_data(as_text=True)
            ok = resp.status_code == want
            print(f"  {'ok  ' if ok else 'FAIL'}  {resp.status_code:<4} {path}")
            check(ok, f"{path} returned {resp.status_code}, wanted {want}")
            if not ok:
                continue

            for needle, label in REQUIRED:
                check(needle in body, f"{path} is missing the {label}")

            for tag, n in (("<!DOCTYPE", 1), ("<html", 1), ("<head>", 1),
                           ("</head>", 1), ("<body>", 1), ("</html>", 1)):
                got = body.count(tag)
                check(got == n, f"{path} has {got} x {tag}, wanted {n}")

            # Only the <head> one. Inline SVGs carry <title> elements for
            # accessibility and the sparklines alone contribute 300+, so a
            # document-wide count means nothing here.
            head = body.split("</head>", 1)[0]
            titles = re.findall(r"<title>(.*?)</title>", head, re.S)
            check(len(titles) == 1, f"{path} has {len(titles)} titles in <head>")
            if titles:
                title = " ".join(titles[0].split())
                check("&mdash;" not in title,
                      f"{path} title carries a raw entity: {title!r}")
                check("reportscope" in title,
                      f"{path} title lacks the brand: {title!r}")

            has_canonical = 'rel="canonical"' in body
            has_noindex = 'name="robots"' in body
            check(has_canonical == indexable,
                  f"{path} canonical={has_canonical}, expected {indexable}")
            check(has_noindex == (not indexable),
                  f"{path} noindex={has_noindex}, expected {not indexable}")

            if path.startswith("/list"):
                canon = re.search(r'rel="canonical" href="(.*?)"', body)
                check(canon and canon.group(1).endswith("/list"),
                      "the /list canonical should drop ?drugs=, got "
                      f"{canon.group(1) if canon else None!r}")

            for desc in re.findall(
                    r'<meta name="description" content="(.*?)"', body, re.S)[:1]:
                check("\n" not in desc, f"{path} description has a newline")
                check(30 < len(desc) < 320,
                      f"{path} description is {len(desc)} chars")
                check("{{" not in desc and "{%" not in desc,
                      f"{path} description has unevaluated Jinja")

    print("\n=== no page carries its own scaffolding ===")
    for name in names:
        if name.startswith("_"):      # partials, by convention
            continue
        src = (ROOT / "templates" / name).read_text(encoding="utf-8")
        stray = [t for t in ("<!DOCTYPE", "<html ", "<head>", "</head>",
                             "<body>", "</body>", "</html>",
                             'include "_nav.html"', "bootstrap.min.css",
                             "bootstrap.bundle.min.js")
                 if t in src]
        check(not stray, f"{name} still contains {stray}")
        check(src.startswith('{% extends "_base.html" %}'),
              f"{name} does not extend the page shell")
        print(f"  {'ok  ' if not stray else 'FAIL'}  {name}")

    print()
    if fails:
        print(f"{len(fails)} FAILURES")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
