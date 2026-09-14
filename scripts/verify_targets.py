"""
Check every pharmacologic class string in config/targets.json against the API.

A mistyped EPC string is not an error. It is a successful query returning zero
reports, which is indistinguishable from a drug nobody has ever reported. This
script makes that difference visible before the strings reach a results table.

Usage:  python scripts/verify_targets.py
Cost:   one call per distinct class string (about 30).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.client import total, q_class  # noqa: E402


def main() -> int:
    targets = json.loads((_ROOT / "config" / "targets.json").read_text(encoding="utf-8"))

    strings: dict[str, list[str]] = {}
    for name, spec in (targets.get("areas") or {}).items():
        for epc in spec.get("classes") or []:
            strings.setdefault(epc, []).append(f"area:{name}")
    for name, spec in (targets.get("classes") or {}).items():
        epc = spec.get("pharm_class_epc")
        if epc:
            strings.setdefault(epc, []).append(f"class:{name}")

    print(f"verifying {len(strings)} distinct class strings\n")
    dead: list[str] = []
    for epc in sorted(strings):
        n = total(q_class(epc))
        flag = "  " if n else "!!"
        print(f"{flag} {n:>10,}  {epc}")
        if not n:
            dead.append(epc)

    # A class declared with no molecules and no EPC can never be scored.
    orphans = [
        name for name, spec in (targets.get("classes") or {}).items()
        if not spec.get("pharm_class_epc") and not spec.get("molecules")
    ]

    print()
    if dead:
        print(f"FAIL: {len(dead)} class string(s) match nothing:")
        for epc in dead:
            print(f"  {epc}  (referenced by {', '.join(strings[epc])})")
    if orphans:
        print(f"FAIL: {len(orphans)} class(es) with neither EPC nor molecules: {orphans}")
    if not dead and not orphans:
        print("OK: every class string resolves to reports.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
