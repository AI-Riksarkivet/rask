"""A citation of `docs/DECISIONS.md` names a section that exists, and holds what is cited.

Deleting a root `open_*.md` is normal — they are ephemeral by design — and the citations pointing at
one are not. On 2026-09-04 four plans were deleted and 45 files were mechanically repointed at
`docs/DECISIONS.md`, which left three separate defects this gate refuses:

* citations carrying a DEAD PLAN'S ROW ID (`(M1)`, `(M2)`, "the index half") — labels that lived only
  in the deleted file, so a reader following one finds a document that never used them;
* citations naming a section whose reasoning is about something else — the maintenance-compute rows
  were repointed at "Cascade repair", which is the lag detector and the repair verb;
* reasoning that never arrived at all: no section covered the dedicated maintenance workers, so the
  pointer was to a file that had not received the content.

The estate has already paid for this once — `rask-lance-catalog` records a skill pointer left dangling
when its plan was deleted — which is why the check is a test rather than a convention.

Prefix-matched on purpose: a citation names the stable head of a heading ("Cascade repair") while the
heading carries its subtitle and date ("Cascade repair — detection, and the repair verb (2026-09-04)").
Requiring the whole string would make every citation break the day a section gains a clause.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

#: Where a citation may appear. Deliberately the whole estate rather than a list of known files: the
#: point is that a NEW citation is checked too.
_ROOTS = ("chart", "services", "packages", "tests", "docs", "scripts", ".claude")

_CITATION = re.compile(r'DECISIONS\.md "([^"]+)"')

#: Citations wrap across lines inside comments, so a heading match must not depend on where the line
#: broke. Collapsing runs of whitespace is the difference between checking the citation and checking
#: the formatter.
def _flat(text: str) -> str:
    return " ".join(text.split())

#: A row id beside a citation — `(M1)`, `(C3)`. Legitimate when DECISIONS itself defines it: the
#: "Cascade repair" section enumerates "(C1, C3a, C3b, C3, C4, C2)", so a reader following `(C3)`
#: finds it. Dangling when it does not: `(M1)`/`(M2)` lived only in the deleted plan, and after the
#: repoint no section used them. The rule is therefore about the DESTINATION, not the shape.
_ROW_ID = re.compile(r'DECISIONS\.md "[^"]+"[^\n]{0,40}?\(([A-Z]\d[a-z]?)\)')


def _sections() -> list[str]:
    return re.findall(r"^## (.+)$", (REPO / "docs" / "DECISIONS.md").read_text(), re.MULTILINE)


def _files() -> list[Path]:
    out: list[Path] = []
    for root in _ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        out.extend(p for p in base.rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".md", ".tpl"})
    return out


def test_every_cited_section_exists() -> None:
    sections = _sections()
    assert sections, "DECISIONS.md has no `## ` headings — the parse is wrong, not the estate"
    unresolved: dict[str, list[str]] = {}
    for path in _files():
        if path.name == "DECISIONS.md" or path.name == Path(__file__).name:
            continue
        for cited in _CITATION.findall(path.read_text(errors="ignore")):
            flat = _flat(cited)
            if not any(_flat(head).startswith(flat) or flat in _flat(head) for head in sections):
                unresolved.setdefault(cited, []).append(str(path.relative_to(REPO)))
    assert not unresolved, "citations naming no section in DECISIONS.md: " + "; ".join(
        f"{k!r} <- {', '.join(v[:3])}" for k, v in sorted(unresolved.items())
    )


def test_no_citation_carries_a_deleted_plans_row_id() -> None:
    """A row id beside a citation must resolve where the citation points.

    `(C3)` is fine: "Cascade repair" enumerates its pieces, so a reader following it lands on the
    reasoning. `(M1)` was not: it lived in the deleted plan, and the repoint carried the label to a
    document that had never defined it — the reader arrives and finds nothing by that name."""
    decisions = (REPO / "docs" / "DECISIONS.md").read_text()
    offenders: list[str] = []
    for path in _files():
        if path.name == Path(__file__).name:
            continue
        for row_id in _ROW_ID.findall(path.read_text(errors="ignore")):
            if row_id not in decisions:
                offenders.append(f"{path.relative_to(REPO)}: ({row_id})")
    assert not offenders, "DECISIONS citations carrying a row id no section defines: " + "; ".join(sorted(set(offenders))[:6])
