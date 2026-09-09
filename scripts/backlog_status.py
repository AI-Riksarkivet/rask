"""What the lakehouse backlog says about itself, in one screen.

WHY A TOOL AND NOT A REPORT. The register re-derives its own counts and a gate proves the header
matches the rows (`tests/unit/test_the_lakehouse_backlog_counts_itself.py`) — but reading 3 800 lines
of markdown to answer "how is it going" is not something anyone will do twice, so in practice only
whoever last touched the file knows. This prints the same derivation on demand, from the same rows, so
the answer does not depend on someone having narrated it.

WORK ROWS ONLY, and that distinction is the whole point of the split: the D/R/M tables are decisions
already made, recommendations awaiting an acknowledgement, and questions that precede a decision. They
were being summed into the backlog and overstating it by 28.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


REGISTER = Path(__file__).resolve().parents[1] / "open_lakehouse_diff_left.md"

#: A lettered work row (`### A1 · …`) and a Q-section table row (`| Q17-5 | … |`). Struck = `~~id~~`.
_LETTERED = re.compile(r"^### (~~)?([A-Z]+\d*)[ ·]", re.MULTILINE)
_TABLE = re.compile(r"^\| (~~)?(Q\d+-\d+)~{0,2} \|(.*)$", re.MULTILINE)

#: The three tables that are NOT work. Counted apart so they cannot inflate the backlog again.
_KINDS = {k: re.compile(rf"^\| {k}\d+ \|", re.MULTILINE) for k in ("D", "R", "M")}

#: A row waiting on the OWNER rather than on an engineer. Matched on the phrases the register already
#: uses rather than a new marker nobody would apply retroactively.
_BLOCKED = re.compile(r"owner'?s? (bearer|OIDC)|OWNER (ACTION|MUST)|owner (decision|call)|OWNER RULING —", re.IGNORECASE)


def _severity(cells: str) -> str:
    parts = [c.strip() for c in cells.split("|")]
    return parts[1] if len(parts) > 1 and parts[1] in {"high", "med", "low"} else "unrated"


def main() -> int:
    text = REGISTER.read_text(encoding="utf-8")
    lettered, table = _LETTERED.findall(text), _TABLE.findall(text)
    struck = sum(1 for m, _ in lettered if m) + sum(1 for m, _, _ in table if m)
    tracked = len(lettered) + len(table)

    print(f"WORK  {tracked - struck} open / {tracked} tracked ({struck} struck)")
    print(f"      {len(lettered)} lettered, {len(table)} Q-rows")
    print("NOT WORK  " + ", ".join(f"{len(p.findall(text))} {k}" for k, p in _KINDS.items()))

    by_sev: dict[str, list[str]] = {}
    blocked: list[str] = []
    for mark, rid, cells in table:
        if mark:
            continue
        by_sev.setdefault(_severity(cells), []).append(rid)
        if _BLOCKED.search(cells):
            blocked.append(rid)

    print("\nOPEN Q-ROWS BY SEVERITY")
    for sev in ("high", "med", "low", "unrated"):
        ids = by_sev.get(sev, [])
        if ids:
            print(f"  {sev:8s} {len(ids):3d}   {' '.join(sorted(ids)[:14])}{' …' if len(ids) > 14 else ''}")

    if blocked:
        print(f"\nWAITING ON THE OWNER ({len(blocked)})")
        for rid in sorted(set(blocked)):
            title = next(c.split("|")[0].strip() for m, r, c in table if r == rid and not m)
            print(f"  {rid:9s} {title[:88]}")

    since = sys.argv[1] if len(sys.argv) > 1 else "midnight"
    # Full path to git, and a fixed argv: this is a read-only status tool, but a partial executable
    # path is how a PATH shim gets to run instead (S607), and the estate already pays for shims.
    git = shutil.which("git") or "/usr/bin/git"
    log = (
        subprocess.run(  # noqa: S603 — fixed argv, no shell, one caller-supplied `--since` value
            [git, "log", f"--since={since}", "--oneline", "--", REGISTER.name],
            capture_output=True,
            text=True,
            cwd=REGISTER.parent,
            check=False,
        )
        .stdout.strip()
        .splitlines()
    )
    print(f"\nREGISTER COMMITS since {since}: {len(log)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
