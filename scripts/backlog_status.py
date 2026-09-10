"""What `open_backlog_left.md` says about itself, derived from the rows rather than asserted.

`make backlog`. The register states its own counts and
`tests/unit/test_the_backlog_counts_itself.py` gates them; this is the same derivation in a form you
read rather than assert against, plus the two cuts that decide what to work next: what is HIGH in the
phase currently in focus, and what is waiting on an owner decision.

Phase order is the owner's (2026-09-10): finish the lakehouse, then compute, then the controlplane.
So the phases print in that order and the low-priority section prints last — a backlog that sorts by
severity alone would put an annotator row above a catalog row, which is exactly the mis-prioritisation
the phase split exists to prevent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REGISTER = Path(__file__).resolve().parents[1] / "open_backlog_left.md"

_SECTION = re.compile(r"^## (PHASE [123] · [^\n]+|FRONTEND[^\n]*|LOW PRIORITY[^\n]*)$", re.MULTILINE)
_GROUP = re.compile(r"^### (.+)$", re.MULTILINE)
#: An item renders as `**<ID> · <title>**` then a meta line `<services> · <severity>[ · **blocked:** …]`.
#: The id is stable (`LH-005`), never positional — see `scripts/backlog_close.py`. Keyed on that shape
#: rather than a line count, because this printed six zeroes the first time the id format changed and a
#: report of zero open items reads exactly like a drained backlog.
_ITEM = re.compile(r"^\*\*([A-Z]+-\d+) · (.+?)\*\*\s*\n`([^`]*)` · ([^\n·]+?)(?: · \*\*blocked:\*\* (.+?))?\s*$", re.MULTILINE)


def main() -> int:
    if not REGISTER.exists():
        print(f"!! {REGISTER.name} is gone — the backlog is drained, or you are in the wrong directory")
        return 1

    text = REGISTER.read_text(encoding="utf-8")
    bounds = [(m.start(), m.group(1)) for m in _SECTION.finditer(text)] + [(len(text), None)]

    total = 0
    blocked: list[tuple[str, str, str]] = []
    focus_high: list[tuple[str, str]] = []

    print("\n\033[1mopen_backlog_left.md\033[0m — what is LEFT, in delivery order\n")
    for i, (start, name) in enumerate(bounds[:-1]):
        body = text[start : bounds[i + 1][0]]
        items = _ITEM.findall(body)
        total += len(items)
        sev = {"**HIGH**": 0, "med": 0, "low": 0}
        for _n, _t, _svc, s, _b in items:
            sev[s.strip()] = sev.get(s.strip(), 0) + 1
        groups = len(_GROUP.findall(body))
        print(f"  {name}")
        print(f"      {len(items):3d} items in {groups} groups   high {sev.get('**HIGH**', 0)}  med {sev.get('med', 0)}  low {sev.get('low', 0)}")

        for n, title, _svc, s, b in items:
            if b:
                blocked.append((n, title, b))
            if s.strip() == "**HIGH**" and name and name.startswith("PHASE 1"):
                focus_high.append((n, title))

    print(f"\n  TOTAL {total} open items\n")

    print("\033[1mIN FOCUS — phase 1 (lakehouse + its cross-cutting), HIGH only\033[0m")
    for n, title in focus_high:
        print(f"  {n:>4}. {title[:150]}")

    print(f"\n\033[1mWAITING ON AN OWNER DECISION ({len(blocked)})\033[0m")
    for n, title, why in blocked:
        print(f"  {n}  {title[:110]}")
        print(f"          -> {why[:150]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
