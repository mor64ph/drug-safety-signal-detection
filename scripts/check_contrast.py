"""
WCAG contrast for both themes, read out of static/style.css.

    python scripts/check_contrast.py

The values are parsed from the stylesheet rather than copied into this file.
A checker holding its own copy of the palette passes forever after someone
edits the CSS, which is worse than no checker.

What this caught when it was first written:

  * --ink-faint was #7b838b, measuring 3.85:1 on a panel and 3.50:1 on the
    page. It carries every small-caps label in the interface -- 19 selectors,
    all 11px to 13px, so all of them needed 4.5:1 and none had it.
  * --rule-firm is the border of .form-control and of the secondary button, so
    WCAG 1.4.11 asks 3:1 of it, and it measured 1.49:1. Raising the token
    would have darkened every table rule with it, so interactive borders were
    split onto --control-border and only that was raised.
  * The first dark accent kept --on-accent white, at 2.12:1. The accent
    inverts between themes, so the label on it has to invert too.

It also documents a decision: the strength ramp is *not* redeclared for dark.
White already clears 4.5:1 on all three bands and their luminances
(0.083 / 0.148 / 0.181) sit well above a near-black page, so carrying them
over unchanged preserves both the contrast and the monotonic ordering that
lets the ramp survive being read in greyscale.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "style.css"

# foreground token, background token, minimum ratio, what it is
CHECKS = [
    ("ink", "page", 4.5, "body text on the page"),
    ("ink", "surface", 4.5, "body text on a panel"),
    ("ink", "surface-2", 4.5, "body text on a tinted panel"),
    ("ink", "surface-3", 4.5, "body text on a deeper panel"),
    ("ink-soft", "surface", 4.5, "secondary prose"),
    ("ink-muted", "surface", 4.5, "muted prose on a panel"),
    ("ink-muted", "page", 4.5, "muted prose on the page"),
    ("ink-faint", "surface", 4.5, "small-caps labels on a panel"),
    ("ink-faint", "page", 4.5, "small-caps labels on the page"),
    ("accent", "page", 4.5, "a link on the page"),
    ("accent", "surface", 4.5, "a link on a panel"),
    ("accent-deep", "surface", 4.5, "a hovered link"),
    ("on-accent", "accent", 4.5, "the primary button label"),
    ("on-tier", "tier-strong", 4.5, "STRONG badge text"),
    ("on-tier", "tier-moderate", 4.5, "MODERATE badge text"),
    ("on-tier", "tier-weak", 4.5, "WEAK badge text"),
    # A hovered row repaints every cell opaquely, so it is a background in its
    # own right and all four of these are read against it. The dark theme had
    # no value at all here -- the token was a literal in the .table rule -- and
    # a hovered row came out near-white under near-white text.
    ("ink", "row-hover", 4.5, "figures in a hovered row"),
    ("ink-muted", "row-hover", 4.5, "a note in a hovered row"),
    ("ink-faint", "row-hover", 4.5, "a label in a hovered row"),
    ("accent", "row-hover", 4.5, "a reaction link in a hovered row"),
    ("ink", "tier-wash", 4.5, "text in a tinted strong row"),
    ("ink", "caution-wash", 4.5, "text in the limitation panel"),
    ("ink", "accent-wash", 4.5, "text in an accent panel"),
    ("danger", "surface", 4.5, "the delete-account warning"),
    ("danger", "danger-wash", 4.5, "the delete-account panel header"),
    # 1.4.11 non-text contrast: the boundary of an interactive control.
    ("control-border", "surface", 3.0, "a field or button border"),
    ("control-border", "page", 3.0, "a field border on the page"),
    # Not required by WCAG -- a sparkline is decorative and every figure it
    # depicts is also printed as a number -- but a chart nobody can see is
    # still a bug.
    ("spark-stroke", "surface", 3.0, "a sparkline"),
    ("spark-spike-stroke", "surface", 3.0, "a spiked sparkline"),
]

RAMP = ("tier-strong", "tier-moderate", "tier-weak")


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcol: str) -> float:
    h = hexcol.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


_TOKEN = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;")


_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _block(css: str, selector: str) -> dict[str, str]:
    """The colour tokens of the first declaration block for `selector`.

    The selector is matched at the start of a line, and comments are stripped
    before any of this runs. Both matter: a plain substring search found the
    word ":root" inside the stylesheet's own header comment, took the next
    brace after it -- @font-face -- and returned an empty palette. The failure
    was loud only because every later lookup raised KeyError.

    Declaration blocks do not nest, so the first closing brace ends it.
    """
    match = re.search(r"^" + re.escape(selector) + r"\s*(?:,[^{]*)?\{",
                      css, re.M)
    if not match:
        raise SystemExit(f"cannot find a rule for {selector!r} in {CSS.name}")
    end = css.index("}", match.end())
    return dict(_TOKEN.findall(css[match.end():end]))


def palettes() -> tuple[dict[str, str], dict[str, str]]:
    css = _COMMENT.sub("", CSS.read_text(encoding="utf-8"))

    # Everything from the first @media print onwards is the print override,
    # which deliberately restates the light values inside a dark selector.
    # Parsing that as the theme would compare light against light and pass
    # while telling us nothing.
    cutoff = css.find("@media print")
    if cutoff == -1:
        raise SystemExit("no @media print block found -- has the file moved?")
    themed = css[:cutoff]

    light = _block(themed, ":root")
    dark = dict(light)
    dark.update(_block(themed, '[data-bs-theme="dark"]'))
    return light, dark


def main() -> int:
    light, dark = palettes()
    print(f"parsed {len(light)} light tokens, "
          f"{sum(1 for k in dark if dark[k] != light.get(k))} dark overrides")

    fails = 0
    for name, pal in (("light", light), ("dark", dark)):
        print(f"\n=== {name} ===")
        for fg, bg, floor, label in CHECKS:
            missing = [t for t in (fg, bg) if t not in pal]
            if missing:
                print(f"  GONE       --{' --'.join(missing)} "
                      f"no longer defined ({label})")
                fails += 1
                continue
            r = ratio(pal[fg], pal[bg])
            ok = r >= floor
            fails += not ok
            print(f"  {'ok  ' if ok else 'FAIL'} {r:5.2f} (>={floor})  "
                  f"{label:<32} {fg} on {bg}")

        lums = [luminance(pal[t]) for t in RAMP]
        mono = all(lums[i] < lums[i + 1] for i in range(len(lums) - 1))
        print(f"  {'ok  ' if mono else 'FAIL'}        ramp luminance "
              f"{lums[0]:.3f} / {lums[1]:.3f} / {lums[2]:.3f}"
              f"   monotonic={mono}")
        fails += not mono

    print()
    if fails:
        print(f"{fails} failures")
        return 1
    print("every pair clears WCAG AA in both themes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
