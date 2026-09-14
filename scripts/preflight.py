"""
Pre-deployment check. Run before pushing to Render.

    python scripts/preflight.py

Catches the things that only fail once deployed, where the feedback loop is a
build, a boot and a confusing log line rather than two seconds here.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def record(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))


def check_secrets_not_committed() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8") if (ROOT / ".gitignore").exists() else ""
    record(PASS if ".env" in gitignore else FAIL,
           ".env is gitignored",
           "" if ".env" in gitignore else "add .env to .gitignore before pushing")

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8") if (ROOT / ".dockerignore").exists() else ""
    record(PASS if ".env" in dockerignore else FAIL,
           ".env excluded from the image",
           "" if ".env" in dockerignore else "secrets would be baked into the image")

    # A key pasted into tracked source rather than .env.
    leaked = []
    for p in list(ROOT.glob("*.py")) + list((ROOT / "src").glob("*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r'API_KEY\s*=\s*["\'][A-Za-z0-9]{20,}["\']', text):
            leaked.append(p.name)
    record(FAIL if leaked else PASS, "no hardcoded API keys in source",
           ", ".join(leaked))


def check_docker_inputs() -> None:
    needed = ["Dockerfile", ".dockerignore", "docker-entrypoint.sh", "render.yaml",
              "requirements.txt", "alembic.ini", "serve.py",
              "data/results/scored_pairs.parquet"]
    missing = [n for n in needed if not (ROOT / n).exists()]
    record(FAIL if missing else PASS, "deployment files present", ", ".join(missing))

    entry = ROOT / "docker-entrypoint.sh"
    if entry.exists():
        raw = entry.read_bytes()
        # The Dockerfile strips CR, so this is advisory rather than fatal.
        record(WARN if b"\r" in raw else PASS, "entrypoint has LF line endings",
               "CRLF present; Dockerfile strips it, but keep it LF in git")

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied: list[str] = []
    for src in re.findall(r"^COPY\s+(.+?)\s+\./?", dockerfile, re.M):
        for token in src.split():
            path = token.rstrip("/")
            if path.startswith("--") or not path:
                continue
            copied.append(path)
            if not (ROOT / path).exists():
                record(FAIL, f"Dockerfile COPY source exists: {path}", "missing")

    # Existing on disk is not enough. The host builds from the repository, so a
    # COPY source that git ignores is absent at build time and the build fails
    # naming a path that is plainly present locally.
    import subprocess
    for path in copied:
        full = ROOT / path
        if not full.exists() or not full.is_file():
            continue
        try:
            r = subprocess.run(["git", "check-ignore", "-q", path],
                               cwd=ROOT, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            record(WARN, "git available to check ignore rules", "skipped")
            break
        if r.returncode == 0:
            record(FAIL, f"COPY source is git-ignored: {path}",
                   "present locally, absent in the repo the host builds from")

    # And the same question for .dockerignore, which is a separate mechanism
    # with separate rules. This check exists because it was missed: the git
    # exception for label_interaction_pairs.json was added and the docker one
    # was not, so the file was committed, the Dockerfile copied it, and the
    # build failed with
    #   "/data/results/label_interaction_pairs.json": not found
    # for a path that was right there in the commit.
    for path in copied:
        full = ROOT / path
        if not full.exists() or not full.is_file():
            continue
        if _docker_excluded(path):
            record(FAIL, f"COPY source is docker-ignored: {path}",
                   "add an exception to .dockerignore, or COPY will not find it")


def _docker_pattern(pattern: str) -> re.Pattern:
    """A .dockerignore pattern as a regex.

    Not fnmatch: Docker matches path segments, so `*` must not cross a `/`.
    fnmatch's `*` does cross it, which would make `data/*` swallow
    `data/results/x.json` and report an exclusion that Docker does not apply.
    """
    pattern = pattern.strip().rstrip("/")
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    # A directory pattern excludes everything beneath it.
    return re.compile("^" + "".join(out) + "(/.*)?$")


def _docker_excluded(path: str) -> bool:
    """Whether .dockerignore keeps `path` out of the build context.

    Last matching rule wins, which is Docker's own precedence and the reason
    an exception has to come after the pattern that excluded it.
    """
    ignore = ROOT / ".dockerignore"
    if not ignore.exists():
        return False
    posix = path.replace("\\", "/")
    excluded = False
    for raw in ignore.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if _docker_pattern(line[1:] if negated else line).match(posix):
            excluded = not negated
    return excluded


def check_data() -> None:
    try:
        import pandas as pd
    except ImportError:
        record(WARN, "pandas available for data check", "skipped")
        return

    parquet = ROOT / "data" / "results" / "scored_pairs.parquet"
    if not parquet.exists():
        record(FAIL, "scored table exists", "run: python run.py --score")
        return

    df = pd.read_parquet(parquet)
    record(PASS if len(df) > 1000 else FAIL, "scored table populated",
           f"{len(df):,} pairs across {df['drug'].nunique()} drugs")

    if "diagnosed" in df.columns:
        done = int(df["diagnosed"].fillna(False).sum())
        pct = 100 * done / len(df) if len(df) else 0
        # Undiagnosed rows are hidden and labelled, so this is a judgement call
        # rather than a hard failure -- but they are where confounding hides.
        record(PASS if pct >= 99 else WARN, "bias diagnostics complete",
               f"{done:,}/{len(df):,} rows ({pct:.0f}%)")

    anchor = df[(df["drug"] == "statin") & (df["reaction_pt"] == "RHABDOMYOLYSIS")]
    if anchor.empty:
        record(FAIL, "validation anchor present", "statin x rhabdomyolysis missing")
    else:
        ror = float(anchor.iloc[0]["ROR"])
        record(PASS if 12.5 < ror < 13.5 else FAIL,
               "validation anchor intact", f"ROR {ror:.2f}, expected ~12.96")


def check_config() -> None:
    from src.models import database_url

    url = database_url()
    record(PASS if "psycopg" in url or url.startswith("sqlite") else FAIL,
           "database URL resolves to an installed driver", url.split("://")[0])

    if url.startswith("postgresql"):
        try:
            import psycopg  # noqa: F401
            record(PASS, "psycopg importable", "")
        except ImportError:
            record(FAIL, "psycopg importable", "pip install 'psycopg[binary]'")

    from src.app import GATE_ENABLED
    record(PASS if GATE_ENABLED else WARN, "access gate configured locally",
           "" if GATE_ENABLED else "set RXSIGNAL_ACCESS_CODE_HASH on the host for closed UAT")

    from src.models import _setting
    if not _setting("SMTP_HOST"):
        record(WARN, "SMTP configured",
               "unset: mail goes to data/outbox/. Set it on the host or testers "
               "cannot verify their address")
    else:
        record(PASS, "SMTP configured", "")

    base = _setting("APP_BASE_URL")
    record(WARN if not base else PASS, "APP_BASE_URL set",
           "" if base else "set on the host; it builds the links inside emails")


def check_syntax() -> None:
    bad = []
    for p in ROOT.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            bad.append(f"{p.relative_to(ROOT)}: {exc}")
    record(FAIL if bad else PASS, "all Python parses", "; ".join(bad[:3]))


def main() -> int:
    for fn in (check_secrets_not_committed, check_docker_inputs, check_data,
               check_config, check_syntax):
        try:
            fn()
        except Exception as exc:
            record(FAIL, f"{fn.__name__} ran", str(exc)[:100])

    width = max(len(n) for _, n, _ in results) + 2
    for level, name, detail in results:
        print(f"  {level:<5} {name:<{width}} {detail}")

    fails = sum(1 for lv, _, _ in results if lv == FAIL)
    warns = sum(1 for lv, _, _ in results if lv == WARN)
    print(f"\n  {len(results) - fails - warns} passed, {warns} warning(s), {fails} failure(s)")
    if fails:
        print("  Not ready to deploy.")
    elif warns:
        print("  Deployable. Review the warnings -- most matter only on the host.")
    else:
        print("  Ready.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
