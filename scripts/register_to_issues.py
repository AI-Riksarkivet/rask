"""Move the work register `open_backlog_left_new2.md` to GitHub Issues on AI-Riksarkivet/rask.

One-shot migration tool (owner decision 2026-10-10). It is DELETED in the commit that reduces the
register, together with the register itself; nothing else may come to depend on it.

    uv run python scripts/register_to_issues.py              # --dry-run, the default: plan.json + preview.md
    uv run python scripts/register_to_issues.py --apply      # writes to GitHub; only after the owner reviewed the plan

The parse, both modes share it:

- Every open row of the counted sections (PHASE 1 · LAKEHOUSE / CROSS-CUTTING / CONTROLPLANE AND
  NOTIFICATIONS / AFTER LAUNCH, PHASE 2 · COMPUTE, FRONTEND, LOW PRIORITY) becomes one issue whose body
  is the row verbatim. Every bullet under `Parked findings (not counted)`, its `No-prod parking`
  subsection included, becomes one issue labelled `parked`, carrying the paragraph that says who found it.
- The two `Left this register` sections are closed rows and are not migrated; they are counted.
- Everything above the first counted section (FOCUS NOW, the production short list, the owner rulings,
  the open decisions, the counts) plus each section's intro paragraph becomes the body of the pinned
  `Phase 1 map` issue (label `wayfinder:map`).
- A sentence in a row that names another row as a precondition, an enabler or something it waits on
  becomes a native `blocked_by` edge. Every edge keeps the sentence it came from, so the owner can check it.
- Every register line is accounted for: assigned to an issue, to the map, skipped as closed, or listed in
  the preview as unassigned. A blank line is counted, not listed.

Beside the register, owner 2026-10-10: open items from HANDOFF-lakehouse.md (re-checked against the code),
open_alert.md, open_anno_active.md, TODO.md and lakehouse-e2e-trace.md become `parked` issues keyed
`PARK-<SOURCE>-<n>`, and each NEW ROWS body of docs/audits/2026-09-26/lakehouse-map.md is appended to its
parked issue. Every issue with a HIGH/MEDIUM/LOW tag gets `severity:high|medium|low`.

`--apply` refuses unless the plan derived now is byte-identical to the reviewed plan.json, and is
resumable: labels that exist are kept, the four pre-existing issues gain `parked` only if they lack it, an
issue whose title already starts with its key is skipped, the map is created once and pinned if it is not,
and an edge GitHub already holds is not posted again. A secondary rate limit is retried with backoff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field


REPO = "AI-Riksarkivet/rask"
ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "open_backlog_left_new2.md"
OUT_DIR = ROOT / "docs" / "audits" / "2026-10-10" / "register-to-issues"
MAP_TITLE = "Phase 1 map"
MAP_LABEL = "wayfinder:map"
TITLE_MAX = 256

#: Two `- *Re-audited 2026-10-05:*` trailers stand directly under a section header, outside any row. Commit
#: fd99d857 appended each section's last trailer after the NEXT header: the one under PHASE 1 · CROSS-CUTTING
#: belonged to LH-273 (closed at helm rev 284, so it is closed-row residue), the one under PHASE 1 · AFTER
#: LAUNCH / From lakehouse belonged to XC-096 (open), whose body it rejoins. Keyed on (section, text prefix).
STRAY_TRAILERS: dict[tuple[str, str], str | None] = {
    ("PHASE 1 · CROSS-CUTTING", "- *Re-audited 2026-10-05:* still open as written"): None,
    ("PHASE 1 · AFTER LAUNCH", "- *Re-audited 2026-10-05:* partly done; What is left rewritten"): "XC-096",
}

#: Cue matches read by hand on 2026-10-10 and found not to be a dependency, keyed (blocked, blocker).
EDGE_EXCLUSIONS: dict[tuple[str, str], str] = {
    ("LH-099", "LH-227"): "excluded by hand: the sentence says which row owns the orphan-.txn case, not that LH-099 waits on it",
    ("LH-330", "LH-164"): "excluded (owner, 2026-10-10): LH-330 waits on decision D6, which LH-164 part 1 names, not on the row",
    ("LH-256", "LH-283"): "excluded by hand: LH-256's How moves a site a later row touches into that row's commit, so LH-256 closes without LH-283",
    ("LH-207", "LH-288"): "excluded by hand: LH-207's closes-when names no client-side copy case; LH-288's ruling bounds a hole LH-207 says it only narrows",
    ("LH-218", "LH-129"): "excluded by hand: LH-218's closes-when is the in-process lane only and says the Ray lane's root credential is LH-129's",
}

SECTION_LABEL: dict[str, str] = {
    "PHASE 1 · LAKEHOUSE": "phase-1",
    "PHASE 1 · CROSS-CUTTING": "phase-1",
    "PHASE 1 · CONTROLPLANE AND NOTIFICATIONS": "phase-1",
    "PHASE 1 · AFTER LAUNCH": "phase-1-after-launch",
    "PHASE 2 · COMPUTE": "phase-2",
    "FRONTEND": "frontend",
    "LOW PRIORITY": "low",
}
PARKED_SECTION = "Parked findings (not counted)"
CLOSED_PREFIX = "Left this register"

LABELS: dict[str, tuple[str, str]] = {
    "blocks-prod": ("b60205", "Left unfixed, makes a Phase 1 criterion false on a production deployment"),
    "criterion-1": ("1d76db", "Phase 1 criterion 1: provenance and lineage correct"),
    "criterion-2": ("1d76db", "Phase 1 criterion 2: catalog correct for lance-ns and authz/governance"),
    "criterion-3": ("1d76db", "Phase 1 criterion 3: not coupled to a workflow engine or Ray"),
    "criterion-4": ("1d76db", "Phase 1 criterion 4: events correct"),
    "criterion-5": ("1d76db", "Phase 1 criterion 5: resilient"),
    "phase-1": ("0e8a16", "Phase 1, production short list and its enablers"),
    "phase-1-after-launch": ("c2e0c6", "Phase 1 hardening after launch (owner, 2026-10-02)"),
    "phase-2": ("fbca04", "Phase 2: compute"),
    "frontend": ("5319e7", "Frontend row"),
    "low": ("d4c5f9", "Low priority"),
    "parked": ("cccccc", "Found, not admitted by the owner; uncounted and unscheduled"),
    "needs-triage": ("ededed", "Maintainer needs to evaluate this issue"),
    "needs-info": ("d876e3", "Waiting on an owner decision"),
    "ready-for-agent": ("0e8a16", "Fully specified, ready for an AFK agent"),
    "ready-for-human": ("0e8a16", "Requires human implementation"),
    "wontfix": ("ffffff", "This will not be worked on"),
    "severity:high": ("b60205", "The row's severity tag: HIGH"),
    "severity:medium": ("fbca04", "The row's severity tag: MEDIUM"),
    "severity:low": ("c5def5", "The row's severity tag: LOW"),
    MAP_LABEL: ("000000", "The pinned map: order, short list, rulings, open decisions"),
}

_H2 = re.compile(r"^## (.+)$")
_H3 = re.compile(r"^### (.+)$")
_ROW_HEAD = re.compile(r"^\*\*([A-Z]+-\d+) · (.+)\*\*\s*$")
_TAG_LINE = re.compile(r"^`[^`]*` · \*\*(HIGH|MEDIUM|LOW)\*\*(.*)$")
_WHY = re.compile(r"^- \*Why:\* (.*)$")
_BLOCKED = re.compile(r"^- \*\*blocked:\*\*")
_NOT_WORKABLE = re.compile(r"^- \*\*not workable now:\*\*")
_PARKED_ITEM = re.compile(r"^- ([A-Z]+-\d+(?: \(prod half\))?) · (.+)$")
_ISSUE_KEY = re.compile(r"^([A-Z]+(?:-[A-Z]+)*-\d+(?: \(prod half\))?): ")
_PARKED_SEVERITY = re.compile(r"^(HIGH|MEDIUM|LOW|OBSERVATION)\b")
_TITLE_CRITERION = re.compile(r"^Criterion ([1-5]) proof\b")
_CLOSED_ITEM = re.compile(r"^- ([A-Z]+-\d+) — ")
_ROW_ID = re.compile(r"\b(?:LH|XC|CP|CTL|FE|LOW|LIN)-\d{3}\b")
_ID_RANGE = re.compile(r"\b((?:LH|XC|CP|CTL|FE|LOW|LIN))-(\d{3}) to (?:\1-)?(\d{3})\b")
_CRITERION_DIGIT = re.compile(r"(?<![\w.])([1-5])(?![\w.])")
_SENTENCE_SPLIT = re.compile(r"(?<=\.)\s+(?=[A-Z*(])|;\s+|\n")
_FORWARD_CUE = re.compile(r"\b(waits? on|depends on|blocked (?:on|by)|preconditions?(?: are| before [^:]*)?:?|needs|its enabler)\b", re.IGNORECASE)
_REVERSE_CUE = re.compile(r"\b(precondition for|enabler of)\b", re.IGNORECASE)
_POSSESSIVE_ENABLER = re.compile(r"\b((?:LH|XC|CP|CTL|FE|LOW|LIN)-\d{3})['\u2019]s enabler\b")
_ID_THEN_ITS_ENABLER = re.compile(r"\b((?:LH|XC|CP|CTL|FE|LOW|LIN)-\d{3}), its enabler\b")
_ENABLER_TAG = re.compile(r"\*\*enabler: ((?:LH|XC|CP|CTL|FE|LOW|LIN)-\d{3})\*\*")
_SHORT_LIST_LINE = re.compile(r"^- Criterion (\d)[^:]*: (.+)$")


class LabelSpec(BaseModel):
    name: str
    color: str
    description: str


class Issue(BaseModel):
    key: str = Field(description="The row id the title starts with; the idempotency key")
    title: str
    kind: Literal["counted", "parked"]
    section: str
    group: str | None = None
    severity: str | None = None
    labels: list[str]
    body: str
    first_line: int
    last_line: int

    @computed_field
    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @computed_field
    @property
    def body_chars(self) -> int:
        return len(self.body)


class Edge(BaseModel):
    blocked: str
    blocker: str
    source_row: str
    rule: str
    sentence: str


class DroppedEdge(BaseModel):
    blocked: str
    blocker: str
    source_row: str
    reason: str
    sentence: str


class MapIssue(BaseModel):
    title: str = MAP_TITLE
    labels: list[str] = Field(default_factory=lambda: [MAP_LABEL])
    body: str


class SkippedRow(BaseModel):
    key: str
    line: int
    text: str


class RegisterLine(BaseModel):
    line: int
    section: str
    text: str


class Plan(BaseModel):
    source: str
    source_sha256: str
    extra_sources: dict[str, str] = Field(default_factory=dict, description="path -> sha256 of every non-register source read")
    notes: list[str] = Field(default_factory=list, description="what the extra-source pass dropped, folded or could not match")
    severity_unlabelled: list[str] = Field(default_factory=list)
    source_lines: int
    labels: list[LabelSpec]
    issues: list[Issue]
    map_issue: MapIssue
    edges: list[Edge]
    dropped_edges: list[DroppedEdge]
    skipped_closed: list[SkippedRow]
    unassigned: list[RegisterLine]
    blank_lines: int


class _Accounting(BaseModel):
    assigned: set[int] = Field(default_factory=set)
    skipped: set[int] = Field(default_factory=set)


def _expand_ids(text: str) -> list[str]:
    """Row ids in reading order, with `XC-091 to XC-095` expanded to every id in the range."""
    found: list[tuple[int, str]] = [(m.start(), m.group(0)) for m in _ROW_ID.finditer(text)]
    for m in _ID_RANGE.finditer(text):
        prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        found.extend((m.start(), f"{prefix}-{n:03d}") for n in range(lo + 1, hi))
    seen: dict[str, None] = {}
    for _, rid in sorted(found):
        seen.setdefault(rid, None)
    return list(seen)


def _criteria(title: str, tag_rest: str, why: str | None, short_list: dict[str, set[int]], key: str) -> set[int]:
    found = set(short_list.get(key, set()))
    found.update(int(d) for d in _TITLE_CRITERION.findall(title))
    found.update(int(d) for d in re.findall(r"criterion (\d)", tag_rest, re.IGNORECASE))
    if why is not None:
        lead = why.split(":", 1)[0]
        if lead.lower().startswith("criteri"):
            found.update(int(d) for d in _CRITERION_DIGIT.findall(lead))
    return found


def _short_list(lines: list[str]) -> dict[str, set[int]]:
    """Row id -> criteria, from the `Production short list` section's per-criterion lines."""
    out: dict[str, set[int]] = {}
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = line == "## Production short list"
            continue
        if inside and (m := _SHORT_LIST_LINE.match(line)):
            for rid in _ROW_ID.findall(m.group(2)):
                out.setdefault(rid, set()).add(int(m.group(1)))
    return out


def _title(key: str, text: str) -> str:
    title = f"{key}: {text.strip()}"
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


def _footer(section: str, group: str | None, first: int, last: int, sha: str) -> str:
    where = f"{section} / {group}" if group else section
    return f"\n\n---\n_Migrated from `open_backlog_left_new2.md` § {where}, lines {first}-{last} (sha256 {sha[:12]})._"


def _parse(text: str) -> Plan:  # noqa: C901 - one pass over the register's line grammar, each branch one line kind
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    acct = _Accounting()
    short_list = _short_list(lines)

    first_counted = next(i for i, ln in enumerate(lines) if (m := _H2.match(ln)) and m.group(1) in SECTION_LABEL)
    head = lines[:first_counted]
    acct.assigned.update(range(1, first_counted + 1))
    section_notes: list[str] = []

    issues: list[Issue] = []
    skipped: list[SkippedRow] = []
    section = ""
    group: str | None = None
    provenance: list[str] = []
    strays: dict[str, tuple[int, str]] = {}
    i = first_counted
    while i < len(lines):
        ln = lines[i]
        lineno = i + 1
        if m := _H2.match(ln):
            section, group, provenance = m.group(1), None, []
            (acct.skipped if section.startswith(CLOSED_PREFIX) else acct.assigned).add(lineno)
            i += 1
            continue
        if m := _H3.match(ln):
            group, provenance = m.group(1), []
            (acct.skipped if section.startswith(CLOSED_PREFIX) else acct.assigned).add(lineno)
            i += 1
            continue
        if not ln.strip():
            i += 1
            continue

        if section.startswith(CLOSED_PREFIX):
            acct.skipped.add(lineno)
            if m := _CLOSED_ITEM.match(ln):
                skipped.append(SkippedRow(key=m.group(1), line=lineno, text=ln))
            i += 1
            continue

        if section in SECTION_LABEL and (m := _ROW_HEAD.match(ln)):
            key, head_title = m.group(1), m.group(2)
            j = i + 1
            while j < len(lines) and not (_ROW_HEAD.match(lines[j]) or _H2.match(lines[j]) or _H3.match(lines[j])):
                j += 1
            end = j
            while end > i and not lines[end - 1].strip():
                end -= 1
            body_lines = lines[i:end]
            acct.assigned.update(range(i + 1, end + 1))
            tag = next((_TAG_LINE.match(b) for b in body_lines if _TAG_LINE.match(b)), None)
            severity = tag.group(1) if tag else None
            tag_rest = tag.group(2) if tag else ""
            why = next((w.group(1) for b in body_lines if (w := _WHY.match(b))), None)
            labels = [SECTION_LABEL[section]]
            if "**blocks-prod**" in tag_rest:
                labels.append("blocks-prod")
            labels.extend(f"criterion-{c}" for c in sorted(_criteria(head_title, tag_rest, why, short_list, key)))
            if any(_BLOCKED.match(b) for b in body_lines):
                labels.append("needs-info")
            issues.append(
                Issue(
                    key=key,
                    title=_title(key, head_title),
                    kind="counted",
                    section=section,
                    group=group,
                    severity=severity,
                    labels=labels,
                    body="\n".join(body_lines) + _footer(section, group, i + 1, end, sha),
                    first_line=i + 1,
                    last_line=end,
                )
            )
            i = j
            continue

        if section == PARKED_SECTION and (m := _PARKED_ITEM.match(ln)):
            key, rest = m.group(1), m.group(2)
            parts = rest.split(" · ")
            severity = parts[0] if _PARKED_SEVERITY.match(parts[0]) else None
            title_text = parts[1] if severity and len(parts) > 1 else parts[0]
            context = "\n\n".join(provenance)
            body = ln + (f"\n\n**Parked under:** {group or section}\n\n{context}" if context else f"\n\n**Parked under:** {group or section}")
            acct.assigned.add(lineno)
            issues.append(
                Issue(
                    key=key,
                    title=_title(key, title_text),
                    kind="parked",
                    section=section,
                    group=group,
                    severity=severity,
                    labels=["parked"],
                    body=body + _footer(section, group, lineno, lineno, sha),
                    first_line=lineno,
                    last_line=lineno,
                )
            )
            i += 1
            continue

        if section == PARKED_SECTION:
            provenance = [ln]
            acct.assigned.add(lineno)
            i += 1
            continue

        stray = next((target for (sec, prefix), target in STRAY_TRAILERS.items() if sec == section and ln.startswith(prefix)), "")
        if stray != "":
            if stray is None:
                acct.skipped.add(lineno)
                skipped.append(SkippedRow(key="LH-273 (stray re-audit trailer)", line=lineno, text=ln))
            else:
                strays[stray] = (lineno, ln)
                acct.assigned.add(lineno)
            i += 1
            continue

        if section in SECTION_LABEL and not ln.startswith(("- ", "`", "**")):
            section_notes.append(f"- **{section}{' / ' + group if group else ''}:** {ln}")
            acct.assigned.add(lineno)
        i += 1

    for iss in issues:
        if iss.key in strays:
            lineno, ln = strays.pop(iss.key)
            row, footer = iss.body.split("\n\n---\n_Migrated", 1)
            iss.body = f"{row}\n{ln}\n\n---\n_Migrated{footer}"
            iss.body = iss.body.replace(f"lines {iss.first_line}-{iss.last_line}", f"lines {iss.first_line}-{iss.last_line} and {lineno}")
    if strays:
        raise ValueError(f"stray trailers name rows the register no longer holds: {sorted(strays)}")

    map_body = "\n".join(head).rstrip()
    if section_notes:
        map_body += "\n\n## Section notes\n\n" + "\n".join(section_notes)
    map_body += f"\n\n---\n_Migrated from `open_backlog_left_new2.md` lines 1-{first_counted} and the section intros (sha256 {sha[:12]})._"

    keys = Counter(iss.key for iss in issues)
    duplicates = [k for k, n in keys.items() if n > 1]
    if duplicates:
        raise ValueError(f"row keys are not unique, so idempotency would skip a row: {duplicates}")

    edges, dropped = _edges(issues)

    unassigned: list[RegisterLine] = []
    blanks = 0
    current = ""
    for n, ln in enumerate(lines, start=1):
        if m := _H2.match(ln):
            current = m.group(1)
        if n in acct.assigned or n in acct.skipped:
            continue
        if not ln.strip():
            blanks += 1
            continue
        unassigned.append(RegisterLine(line=n, section=current, text=ln))

    return Plan(
        source=REGISTER.name,
        source_sha256=sha,
        source_lines=len(lines),
        labels=[LabelSpec(name=n, color=c, description=d) for n, (c, d) in LABELS.items()],
        issues=issues,
        map_issue=MapIssue(body=map_body),
        edges=edges,
        dropped_edges=dropped,
        skipped_closed=skipped,
        unassigned=unassigned,
        blank_lines=blanks,
    )


def _edges(issues: list[Issue]) -> tuple[list[Edge], list[DroppedEdge]]:
    """Blocked-by edges from the cues a row uses to name another row it waits on or enables."""
    known = {iss.key for iss in issues}
    candidates: list[Edge] = []
    for iss in issues:
        body = iss.body.split("\n\n---\n_Migrated", 1)[0]
        candidates.extend(
            Edge(blocked=m.group(1), blocker=iss.key, source_row=iss.key, rule="tag **enabler: X**", sentence=m.group(0)) for m in _ENABLER_TAG.finditer(body)
        )
        for sentence in _SENTENCE_SPLIT.split(body):
            if _ENABLER_TAG.search(sentence):
                continue
            candidates.extend(
                Edge(blocked=m.group(1), blocker=iss.key, source_row=iss.key, rule="X's enabler", sentence=sentence.strip())
                for m in _POSSESSIVE_ENABLER.finditer(sentence)
            )
            candidates.extend(
                Edge(blocked=iss.key, blocker=m.group(1), source_row=iss.key, rule="X, its enabler", sentence=sentence.strip())
                for m in _ID_THEN_ITS_ENABLER.finditer(sentence)
            )
            cues = sorted(
                [(m.start(), m.end(), "forward", m.group(1)) for m in _FORWARD_CUE.finditer(sentence)]
                + [(m.start(), m.end(), "reverse", m.group(1)) for m in _REVERSE_CUE.finditer(sentence)]
            )
            for n, (_start, end, direction, cue) in enumerate(cues):
                stop = cues[n + 1][0] if n + 1 < len(cues) else len(sentence)
                for rid in _expand_ids(sentence[end:stop]):
                    if direction == "forward":
                        candidates.append(Edge(blocked=iss.key, blocker=rid, source_row=iss.key, rule=f"cue '{cue.lower()}'", sentence=sentence.strip()))
                    else:
                        candidates.append(Edge(blocked=rid, blocker=iss.key, source_row=iss.key, rule=f"cue '{cue.lower()}'", sentence=sentence.strip()))

    edges: list[Edge] = []
    dropped: list[DroppedEdge] = []
    seen: set[tuple[str, str]] = set()
    for e in candidates:
        pair = (e.blocked, e.blocker)
        if pair in EDGE_EXCLUSIONS:
            dropped.append(DroppedEdge(blocked=e.blocked, blocker=e.blocker, source_row=e.source_row, reason=EDGE_EXCLUSIONS[pair], sentence=e.sentence))
            continue
        if e.blocked == e.blocker:
            continue
        if e.blocked not in known or e.blocker not in known:
            missing = e.blocker if e.blocker not in known else e.blocked
            dropped.append(
                DroppedEdge(
                    blocked=e.blocked,
                    blocker=e.blocker,
                    source_row=e.source_row,
                    reason=f"{missing} is not an open or parked row (closed, merged or unknown)",
                    sentence=e.sentence,
                )
            )
            continue
        if pair in seen:
            continue
        seen.add(pair)
        edges.append(e)
    return edges, dropped


_BFF_CALL = re.compile(r"\brequestJSON\(")


class _Sources(BaseModel):
    """Every non-register file the extra-source pass reads, with its sha256 for the --apply guard."""

    digests: dict[str, str] = Field(default_factory=dict)

    def lines(self, rel: str) -> list[str]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        self.digests[rel] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text.split("\n")

    def excerpt(self, rel: str, first: int, last: int) -> str:
        return "\n".join(self.lines(rel)[first - 1 : last])


def _parked(key: str, title: str, body: str, source: str, *, labels: list[str] | None = None) -> Issue:
    return Issue(
        key=key,
        title=_title(key, title),
        kind="parked",
        section="Outside the register",
        group=source,
        labels=["parked", *(labels or [])],
        body=f"{body}\n\n---\n_Parked from `{source}` (owner, 2026-10-10)._",
        first_line=0,
        last_line=0,
    )


def _handoff(src: _Sources, notes: list[str]) -> list[Issue]:
    rel = "HANDOFF-lakehouse.md"
    out: list[Issue] = []
    calls: list[str] = []
    for zone_file in ("storage/storage.ts", "data/catalog.ts"):
        zrel = f"frontend/microfrontends/lakehouse/src/lib/{zone_file}"
        calls.extend(f"`{zrel}:{n}`" for n, ln in enumerate(src.lines(zrel), start=1) if _BFF_CALL.search(ln) and "const requestJSON" not in ln)
    if calls:
        out.append(
            _parked(
                "PARK-HANDOFF-1",
                "The lakehouse zone still reads the S3 browser and the table history through the BFF JSON helper",
                f"Re-checked 2026-10-10: still open. BFF `requestJSON` call sites today: {', '.join(calls)}.\n\n{src.excerpt(rel, 9, 30)}",
                f"{rel}:9-30",
                labels=["frontend"],
            )
        )
    else:
        notes.append("HANDOFF §1 (requestJSON sites) dropped: no BFF requestJSON call remains in storage.ts or catalog.ts.")

    pairs = {
        "createWarehouse": (
            "frontend/microfrontends/home/src/lib/remote/warehouses.remote.ts",
            "frontend/microfrontends/lakehouse/src/lib/data/remote/warehouses.remote.ts",
        ),
        "checkAccess": (
            "frontend/microfrontends/home/src/lib/remote/access.remote.ts",
            "frontend/microfrontends/lakehouse/src/lib/data/remote/access-objects.remote.ts",
        ),
    }
    still: list[str] = []
    for name, files in pairs.items():
        sites = [f"`{f}:{n}`" for f in files for n, ln in enumerate(src.lines(f), start=1) if f"export const {name} = " in ln]
        if len(sites) == len(files):
            still.append(f"{name} at {' and '.join(sites)}")
    if still:
        out.append(
            _parked(
                "PARK-HANDOFF-2",
                "The createWarehouse and checkAccess schemas are hand-copied in the home and lakehouse zones",
                f"Re-checked 2026-10-10: still open, each declared twice: {'; '.join(still)}.\n\n{src.excerpt(rel, 46, 70)}",
                f"{rel}:46-70",
                labels=["frontend"],
            )
        )
    else:
        notes.append("HANDOFF §2 (duplicated schemas) dropped: createWarehouse and checkAccess are no longer both declared in both zones.")

    shell = ["frontend/packages/ui/src/lib/shell/project-switcher.svelte", "frontend/packages/ui/src/lib/shell/app-shell.svelte"]
    templated = [f"{f}:{n}" for f in shell for n, ln in enumerate(src.lines(f), start=1) if "href" in ln and ("displayName" in ln or "shellProject" in ln)]
    if templated:
        out.append(
            _parked(
                "PARK-HANDOFF-3",
                "The project switcher templates its 'Select project' placeholder into a link",
                f"Re-checked 2026-10-10: still open at {', '.join(templated)}.\n\n{src.excerpt(rel, 94, 101)}",
                f"{rel}:94-101",
                labels=["frontend"],
            )
        )
    else:
        notes.append(
            "HANDOFF §5 half B (placeholder as href) dropped, already fixed: the switcher's links come from the membership list and `/projects` "
            "(project-switcher.svelte:92,105), and the breadcrumb links a project only when one is active (app-shell.svelte:139-141); "
            "no href is built from the 'Select project' text."
        )
    return out


def _alert(src: _Sources) -> Issue:
    rel = "open_alert.md"
    lines = src.lines(rel)
    decision = next(n for n, ln in enumerate(lines, start=1) if ln.startswith("## 4. The decision"))
    end = next(n for n, ln in enumerate(lines, start=1) if n > decision and ln.startswith("## 5."))
    body = (
        "Decision: does the estate page? (A) `alerting.enabled: true` with a real `alerting.webhookUrl`; "
        "(B) GreptimeDB Enterprise triggers replace vmalert; (C) declare that the estate does not page and delete `chart/alerting/`.\n\n"
        "Owner ruling 2026-10-04: everything downstream of the OTel Collector is out of scope (GreptimeDB is being replaced "
        "by OpenObserve or the Grafana stack), so no GreptimeDB or vmalert fixes. See also parked XC-118.\n\n"
        f"{src.excerpt(rel, 1, 7)}\n\n{src.excerpt(rel, decision, end - 1)}"
    )
    return _parked(
        "PARK-ALERT-1",
        "Decide whether the estate pages: alerting on, GreptimeDB Enterprise, or no paging",
        body,
        f"{rel}:1-7,{decision}-{end - 1}",
        labels=["needs-info"],
    )


def _anno(src: _Sources) -> Issue:
    rel = "open_anno_active.md"
    lines = src.lines(rel)
    checklist = [
        f"- [ ] {ln.removeprefix('### ').removeprefix('## ')} ({rel}:{n})"
        for n, ln in enumerate(lines, start=1)
        if 236 <= n <= 507 and (ln.startswith("### ") or n == 236)
    ]
    body = (
        "Epic: the annotator plane's open gap list. Annotation is outside Phase 1 (owner, 2026-09-28), so this stays parked.\n\n"
        f"### Recommended order ({rel}:41-61)\n\n{src.excerpt(rel, 41, 61)}\n\n### Gaps\n\n" + "\n".join(checklist) + "\n\n"
        f"Sections 6 and 7 ({rel}:508-560) are implementation conventions and comparison caveats, not gaps; they are in the full text below.\n\n"
        f"<details><summary>Full text, {rel}:236-560</summary>\n\n{src.excerpt(rel, 236, 560)}\n\n</details>"
    )
    return _parked("PARK-ANNO-1", "Annotation, AI-assist and active-learning gaps (epic)", body, f"{rel}:41-61,236-560")


def _todo(src: _Sources) -> Issue:
    rel = "TODO.md"
    items: list[str] = []
    for n, ln in enumerate(src.lines(rel), start=1):
        if m := re.match(r"^(\d+[a-z]?)\. (.*)$", ln):
            tracked = " (tracked by LOW-016)" if m.group(1) == "17" else ""
            items.append(f"- [ ] {m.group(1)}. {m.group(2)}{tracked} ({rel}:{n})")
    body = f"The owner's UI and product notes, {len(items)} entries, verbatim.\n\n" + "\n".join(items)
    return _parked("PARK-TODO-1", "UI and product notes from TODO.md (umbrella)", body, rel)


def _trace(src: _Sources) -> list[Issue]:
    rel = "lakehouse-e2e-trace.md"
    lines = src.lines(rel)
    start = next(n for n, ln in enumerate(lines, start=1) if ln.startswith("### Unowned recommendations"))
    recs: dict[int, tuple[int, int]] = {}
    current: int | None = None
    for n in range(start + 1, len(lines) + 1):
        ln = lines[n - 1]
        if m := re.match(r"^(\d)\. ", ln):
            current = int(m.group(1))
            recs[current] = (n, n)
        elif current is not None and ln.startswith("   "):
            recs[current] = (recs[current][0], n)
        elif ln.startswith(("---", "## ")):
            break
    titles = {
        2: "Decide whether a cascade started through the shared app token names an originator",
        3: "Decide how a Ray stage job that fails after committing records its committed version in lineage",
        4: "Decide whether table_published notifies watchers",
    }
    return [
        _parked(
            f"PARK-TRACE-{k}",
            titles[k],
            f"Recommendation {k} of `{rel}` (a recommendation, not a ruling or a row):\n\n{src.excerpt(rel, *recs[k])}",
            f"{rel}:{recs[k][0]}-{recs[k][1]}",
            labels=["needs-info"],
        )
        for k in (2, 3, 4)
    ]


def _fold_lakehouse_map(src: _Sources, issues: list[Issue], notes: list[str]) -> None:
    """Append each NEW ROWS full body of the 2026-09-26 map to its parked issue; the register says the full text lives only there."""
    rel = "docs/audits/2026-09-26/lakehouse-map.md"
    lines = src.lines(rel)
    first = next(n for n, ln in enumerate(lines, start=1) if ln.startswith("## 1. NEW ROWS"))
    last = next(n for n, ln in enumerate(lines, start=1) if n > first and ln.startswith("## ") and not ln.startswith("## 1."))
    parked = {iss.key: iss for iss in issues if iss.kind == "parked"}
    counted = {iss.key for iss in issues if iss.kind == "counted"}
    folded: list[str] = []
    elsewhere: list[str] = []
    n = first
    while n < last:
        m = _ROW_HEAD.match(lines[n - 1])
        if m is None:
            n += 1
            continue
        end = n + 1
        while end < last and not (_ROW_HEAD.match(lines[end - 1]) or lines[end - 1].startswith("#")):
            end += 1
        stop = end - 1
        while stop > n and not lines[stop - 1].strip():
            stop -= 1
        key = m.group(1)
        if key in parked:
            iss = parked[key]
            row, footer = iss.body.split("\n\n---\n_Migrated", 1)
            iss.body = f"{row}\n\n## Full finding\n\n_From `{rel}:{n}-{stop}`._\n\n{src.excerpt(rel, n, stop)}\n\n---\n_Migrated{footer}"
            folded.append(key)
        else:
            elsewhere.append(f"{key} ({'an open row' if key in counted else 'not open or parked in the register'})")
        n = end
    notes.append(f"lakehouse-map NEW ROWS folded into their parked issues ({len(folded)}): {', '.join(folded)}.")
    notes.append(f"lakehouse-map NEW ROWS not folded because the row is not parked ({len(elsewhere)}): {', '.join(elsewhere)}.")


def _severity(issues: list[Issue]) -> list[str]:
    unlabelled: list[str] = []
    for iss in issues:
        word = iss.severity.replace(",", " ").split()[0] if iss.severity else ""
        if word in {"HIGH", "MEDIUM", "LOW"}:
            iss.labels.append(f"severity:{word.lower()}")
        else:
            unlabelled.append(f"{iss.key} ({iss.severity or 'no severity'})")
    return unlabelled


def build_plan() -> Plan:
    plan = _parse(REGISTER.read_text(encoding="utf-8"))
    src = _Sources()
    notes: list[str] = []
    _fold_lakehouse_map(src, plan.issues, notes)
    plan.issues.extend([*_handoff(src, notes), _alert(src), _anno(src), _todo(src), *_trace(src)])
    plan.severity_unlabelled = _severity(plan.issues)
    keys = Counter(iss.key for iss in plan.issues)
    if dupes := [k for k, c in keys.items() if c > 1]:
        raise ValueError(f"issue keys are not unique: {dupes}")
    plan.extra_sources = src.digests
    plan.notes = notes
    return plan


def _preview(plan: Plan) -> str:
    out: list[str] = [
        "# Register to GitHub Issues: dry-run preview",
        "",
        f"Source `{plan.source}`, {plan.source_lines} lines, sha256 `{plan.source_sha256}`. "
        f"Generated by `scripts/register_to_issues.py --dry-run`; nothing was written to GitHub.",
        "",
        "## Totals",
        "",
        f"- Issues: **{len(plan.issues)}** ({sum(i.kind == 'counted' for i in plan.issues)} counted rows, "
        f"{sum(i.kind == 'parked' and i.first_line > 0 for i in plan.issues)} parked findings from the register, "
        f"{sum(i.first_line == 0 for i in plan.issues)} parked issues from files outside it), plus 1 map issue.",
        f"- Blocked-by edges: **{len(plan.edges)}**; candidate edges dropped (one end not migrated, or excluded by hand): **{len(plan.dropped_edges)}**.",
        f"- Closed rows skipped (`Left this register`): **{len(plan.skipped_closed)}**.",
        f"- Register lines assigned to nothing: **{len(plan.unassigned)}** non-blank; {plan.blank_lines} blank lines are not listed.",
        "",
        "## Issues per label",
        "",
        "| Label | Issues |",
        "| --- | --- |",
    ]
    counts = Counter(lab for iss in plan.issues for lab in iss.labels)
    out.extend(f"| `{name}` | {counts.get(name, 0)} |" for name in LABELS if name != MAP_LABEL)
    out.append(f"| `{MAP_LABEL}` | 1 (the map) |")
    out += ["", f"Issues with no `severity:*` label ({len(plan.severity_unlabelled)}): {', '.join(plan.severity_unlabelled)}."]
    out += ["", "## Sources outside the register", "", "| File | sha256 |", "| --- | --- |"]
    out.extend(f"| `{path}` | `{digest[:16]}` |" for path, digest in sorted(plan.extra_sources.items()))
    out += ["", "### What the extra-source pass dropped, folded or left", ""]
    out.extend(f"- {note}" for note in plan.notes)

    out += ["", "## Issue titles", "", "| Key | Labels | Title |", "| --- | --- | --- |"]
    for iss in plan.issues:
        out.append(f"| {iss.key} | {', '.join(iss.labels)} | {iss.title.replace('|', '\\|')} |")

    out += ["", "## Blocked-by edges", "", "Read: *blocked* is blocked by *blocker*. Check each against its sentence.", ""]
    out += ["| # | Blocked | Blocker | Rule | From row | Sentence |", "| --- | --- | --- | --- | --- | --- |"]
    for n, e in enumerate(plan.edges, start=1):
        out.append(f"| {n} | {e.blocked} | {e.blocker} | {e.rule} | {e.source_row} | {e.sentence.replace('|', '\\|')} |")
    out += ["", "### Candidate edges dropped", "", "| Blocked | Blocker | From row | Reason | Sentence |", "| --- | --- | --- | --- | --- |"]
    for d in plan.dropped_edges:
        out.append(f"| {d.blocked} | {d.blocker} | {d.source_row} | {d.reason} | {d.sentence.replace('|', '\\|')} |")

    out += ["", "## Closed rows skipped", "", ", ".join(s.key for s in plan.skipped_closed)]

    out += ["", "## Register lines assigned to nothing", ""]
    if plan.unassigned:
        out += ["| Line | Section | Text |", "| --- | --- | --- |"]
        out.extend(f"| {u.line} | {u.section} | {u.text.replace('|', '\\|')} |" for u in plan.unassigned)
    else:
        out.append(
            "None. Every non-blank line of the register is in an issue body, in the map body, or among the skipped closed rows "
            f"({plan.blank_lines} blank lines)."
        )

    out += ["", "## Map issue body", "", f"Title `{plan.map_issue.title}`, label `{MAP_LABEL}`, pinned. Body follows verbatim.", "", "````markdown"]
    out += [plan.map_issue.body, "````", ""]
    return "\n".join(out)


_RATE_LIMITED = re.compile(r"secondary rate limit|rate limit exceeded|submitted too quickly|HTTP 429|abuse detection", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry-after:\s*(\d+)", re.IGNORECASE)
_MAX_ATTEMPTS = 7


def _gh(*args: str, stdin: str | None = None) -> str:
    """Run gh, backing off on GitHub's secondary rate limit (403/429), honouring a Retry-After when gh prints one."""
    argv = ["gh", *args]
    delay = 60.0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        # argv is gh plus the plan's own strings and never passes through a shell.
        done = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)  # noqa: S603
        if done.returncode == 0:
            return done.stdout
        err = done.stderr.strip()
        if not _RATE_LIMITED.search(err) or attempt == _MAX_ATTEMPTS:
            raise RuntimeError(f"gh {' '.join(args[:3])} failed ({done.returncode}): {err}")
        wait = float(m.group(1)) if (m := _RETRY_AFTER.search(err)) else delay
        print(f"rate limited ({attempt}/{_MAX_ATTEMPTS}), sleeping {wait:.0f}s: {err[:160]}", file=sys.stderr)
        time.sleep(wait)
        delay = min(delay * 2, 900.0)
    raise AssertionError("unreachable")


def _issue_number(url: str) -> int:
    m = re.search(r"/issues/(\d+)\s*$", url.strip())
    if m is None:
        raise RuntimeError(f"gh issue create returned no issue URL: {url!r}")
    return int(m.group(1))


#: Issues that predate the migration; owner 2026-10-10: label them `parked`, do not close them.
PREEXISTING_TO_PARK = (23, 95, 96, 97)


def _apply(plan: Plan) -> None:
    """Every step checks GitHub before it writes, so a re-run after any failure resumes where the last one stopped."""
    existing_labels = {row["name"] for row in json.loads(_gh("label", "list", "-R", REPO, "--limit", "500", "--json", "name"))}
    for spec in plan.labels:
        if spec.name not in existing_labels:
            _gh("label", "create", spec.name, "-R", REPO, "--color", spec.color, "--description", spec.description)
            print(f"label + {spec.name}")

    for number in PREEXISTING_TO_PARK:
        held = {row["name"] for row in json.loads(_gh("issue", "view", str(number), "-R", REPO, "--json", "labels"))["labels"]}
        if "parked" not in held:
            _gh("issue", "edit", str(number), "-R", REPO, "--add-label", "parked")
            print(f"#{number} + parked")

    listed = json.loads(_gh("issue", "list", "-R", REPO, "--state", "all", "--limit", "5000", "--json", "number,title"))
    number_of: dict[str, int] = {}
    map_number: int | None = None
    for row in listed:
        title = str(row["title"])
        if title == MAP_TITLE:
            map_number = int(row["number"])
        if m := _ISSUE_KEY.match(title):
            number_of[m.group(1)] = int(row["number"])

    for iss in plan.issues:
        if iss.key in number_of:
            print(f"skip {iss.key} (#{number_of[iss.key]} exists)")
            continue
        url = _gh("issue", "create", "-R", REPO, "--title", iss.title, "--body-file", "-", "--label", ",".join(iss.labels), stdin=iss.body)
        number_of[iss.key] = _issue_number(url)
        print(f"issue {iss.key} -> #{number_of[iss.key]}")
        time.sleep(1.0)

    if map_number is None:
        url = _gh("issue", "create", "-R", REPO, "--title", MAP_TITLE, "--body-file", "-", "--label", MAP_LABEL, stdin=plan.map_issue.body)
        map_number = _issue_number(url)
        print(f"map -> #{map_number}")
    if not json.loads(_gh("issue", "view", str(map_number), "-R", REPO, "--json", "isPinned"))["isPinned"]:
        _gh("issue", "pin", str(map_number), "-R", REPO)
        print(f"map #{map_number} pinned")

    db_id: dict[int, int] = {}
    for e in plan.edges:
        blocked, blocker = number_of[e.blocked], number_of[e.blocker]
        if blocker not in db_id:
            db_id[blocker] = int(_gh("api", f"repos/{REPO}/issues/{blocker}", "--jq", ".id"))
        held = {int(x["id"]) for x in json.loads(_gh("api", f"repos/{REPO}/issues/{blocked}/dependencies/blocked_by?per_page=100"))}
        if db_id[blocker] in held:
            print(f"edge {e.blocked} <- {e.blocker} exists")
            continue
        _gh("api", "--method", "POST", f"repos/{REPO}/issues/{blocked}/dependencies/blocked_by", "-F", f"issue_id={db_id[blocker]}")
        print(f"edge {e.blocked} (#{blocked}) blocked by {e.blocker} (#{blocker})")
        time.sleep(0.5)


def _plan_json(plan: Plan) -> str:
    # Bodies are left out: --apply re-derives them from the sources, and each body_sha256 pins what it will send.
    return plan.model_dump_json(indent=2, exclude={"issues": {"__all__": {"body"}}}) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="write plan.json and preview.md (the default)")
    mode.add_argument("--apply", action="store_true", help="create labels, issues, the map and blocked_by edges on GitHub")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="where the dry run writes plan.json and preview.md")
    args = parser.parse_args(argv)

    plan = build_plan()
    if args.apply:
        if (args.out / "plan.json").read_text(encoding="utf-8") != _plan_json(plan):
            print("refusing: the plan derived now differs from the reviewed plan.json; re-run --dry-run and review again", file=sys.stderr)
            return 1
        if plan.unassigned:
            print(f"refusing: {len(plan.unassigned)} register lines are assigned to nothing; see preview.md", file=sys.stderr)
            return 1
        _apply(plan)
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "plan.json").write_text(_plan_json(plan), encoding="utf-8")
    (args.out / "preview.md").write_text(_preview(plan), encoding="utf-8")
    print(f"{len(plan.issues)} issues, {len(plan.edges)} edges, {len(plan.unassigned)} unassigned lines -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
