"""FORWARD gate for the comment rule recorded in `docs/DECISIONS.md` § "Comments carry rationale and
provenance, never a changelog of the prose (2026-08-30, owner ruling)".

WHY THIS EXISTS, AND WHY IT IS DIFF-AWARE
=========================================
The rule sorts module prose into three kinds: RATIONALE (why the shape is load-bearing) stays,
PROVENANCE (a dated measurement, a named pin) keeps its measurement, and HISTORY-OF-THE-PROSE — a
comment whose subject is a previous comment — is banned. The owner scoped the ban FORWARD ONLY: the
prose already written is left alone, because a retroactive drain is a large unreviewable diff whose
most likely casualty is the measurements the prose exists to preserve.

That scoping decides the gate's shape. A whole-tree lint would be red on its first run against prose
nobody is allowed to change, and a gate that is red by construction is a gate everyone learns to
ignore — strictly worse than no gate. So this checks ADDED lines only, against the base the change is
measured from, and is green at HEAD by construction rather than by luck.

WHAT IT CAN AND CANNOT SEPARATE — read this before trusting a green run
======================================================================
It fires ONLY on prose whose subject is PROSE: "this docstring used to say", "the comment above
claimed", "this line no longer says". That form carries no information about the code at all — strip
it and nothing about the behaviour becomes unknowable — which is what makes it mechanically safe to
ban.

It deliberately does NOT try to catch the rest of kind (3), a changelog of past *code* decisions,
because that is not separable from rationale by any pattern over the text. Both halves of this pair
are history:

    # The old order — validate every job, then sort — built a list of every job before the cap.   KEEP
    # Sorting moved above validation on 2026-08-12 when the profile came back.                    BAN

The first uses a past shape to explain why the present one is load-bearing (kind 1, and deleting it
would delete the reason). The second narrates the diff. Nothing in the wording tells them apart; only
a reader can. Those are left to review, and the rule doc says so. This gate is therefore a floor, not
a proof of conformance: passing it does not mean a comment obeys the rule.

MEASURED PRECISION (2026-08-30, `--report` over every tracked gated file)
========================================================================
53 lines in the tree match, and the precision claim here has already been corrected once — the
first version said "52 are genuine. ONE is a false positive", which an adversarial re-read of all 53
refuted. AT LEAST TWO are false positives, and a third is right for the wrong reason:

* `frontend/microfrontends/lakehouse/src/lib/storage/ObjectBrowser.svelte:50` — a generic error banner
  "threw away the only sentence that said what was actually wrong", where `sentence` means the
  server's problem+json detail, not a comment.
* `frontend/microfrontends/models/e2e/shell.spec.ts:14` — "the lakehouse's panel no longer advertises
  the routes it gave up" is a present-tense statement of what the test ASSERTS about the UI. Nothing
  in it narrates prose; `no longer` alone carried the match.
* `tests/unit/test_publish_saga.py:491` — "This used to say 'demo'" is history, so flagging it is
  defensible, but its subject is a FIXTURE LITERAL. The gate reports it as "a comment whose subject is
  a previous comment", and that reason is false.

So budget the error rate at ROUGHLY ONE IN TWENTY, not one in fifty, concentrated on prose that quotes
or describes user-visible TEXT — and treat even that as a floor rather than a measurement, because
sorting kind-(3) history from kind-(1) rationale is the reader's judgement the gate explicitly cannot
make. That is the whole reason this is forward-only and advisory: it is a floor under the rule, never
a proof of conformance. None of the three is worth fixing by narrowing the pattern — each narrowing
overfits to one site, and forward-only the gate never reads these lines again.

USAGE
=====
    uv run python scripts/comment_history_gate.py --staged        # pre-commit (prek.toml)
    uv run python scripts/comment_history_gate.py --base origin/main
    uv run python scripts/comment_history_gate.py --paths a.py b.ts   # whole files, for a new file
    uv run python scripts/comment_history_gate.py --report            # whole tree, ADVISORY, never gates
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import token
import tokenize
from pathlib import Path

from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parent.parent

#: The rule's single home. Cited in every finding so the gate never becomes the only statement of it.
RULE_DOC = 'docs/DECISIONS.md § "Comments carry rationale and provenance, never a changelog of the prose"'

#: These two files MUST contain the banned phrasings — one defines them, the other is the test that
#: proves they are caught. There is no per-line pragma on purpose: an escape hatch on a gate this
#: narrow would be used instead of the rewrite it is asking for.
EXEMPT_PATHS = frozenset(
    {
        "scripts/comment_history_gate.py",
        "tests/unit/test_comment_history_gate.py",
    }
)

#: Python is checked exactly (tokenize tells us which lines are comment or docstring). The others are
#: checked with a line-prefix heuristic — see `_prose_lines_by_prefix`. Markdown is NOT here: the rule
#: governs module prose, and docs legitimately keep a trail (DECISIONS.md carries superseded reasoning
#: under its own heading).
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
PREFIX_COMMENT_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".mjs", ".cjs", ".svelte", ".go"})

#: Directories whose contents are not ours to gate.
SKIP_DIR_PARTS = frozenset({".venv", "node_modules", "__pycache__", ".svelte-kit", "dist", "build", ".turbo", "third_party", "site"})


class Band(BaseModel):
    """One banned phrasing family, with the reason it is safe to ban mechanically."""

    name: str
    regex: str
    why: str


#: THE PRECISION RULE, and the whole reason the band table looks the way it does: a verb that CODE can
#: also perform is not admissible on its own. `read`, `call`, `describe`, `tell`, `name` and `assert`
#: all have a code reading — "routes.py used to read get_settings()", "the four surfaces that used to
#: read once on mount" — and measured over the tree on 2026-08-30 those six verbs account for every
#: false positive the band table produces without this rule. So a band fires only where the SUBJECT is
#: unmistakably text: either a prose noun stands right before the verb, or the verb is one only text
#: can perform.
#:
#: The asymmetry is deliberate. A false NEGATIVE costs one comment that slips through a gate the rule
#: doc already calls a floor. A false POSITIVE blocks a commit over correct prose, and the next thing
#: that happens is `--no-verify`.

#: Nouns that name the text itself and never the program. `test`, `file`, `module` and `text` are
#: deliberately ABSENT — all four name code as readily as prose.
_PROSE_NOUN = r"(?:docstring|doc-string|comment|line|bullet|sentence|paragraph|wording|phrasing|prose|caption|heading|blurb)s?"

#: Between the article and the prose noun: up to two words, apostrophes and backticks included, because
#: "the reader's docstring" and "the ``load`` docstring" are the shapes this actually meets.
_NOUN_GAP = r"(?:[\w'`\u2019-]+\s+){0,2}"

#: Verbs only text performs — admissible with ANY subject. This list is the gate's whole recall budget:
#: adding a polysemous verb buys one more catch and an unknown number of false positives.
_TEXT_ONLY = "say|says|said|claim|claims|claimed|imply|implies|implied|advertise|advertises|advertised|cite|cites|cited|mention|mentions|mentioned"

#: Verbs admissible ONLY when a prose noun has already fixed the subject as text. Each one reads as a
#: code verb when the subject is a module ("the loader reads the settings"), which is why they may
#: never be matched bare.
_NEEDS_A_PROSE_SUBJECT = "read|reads|state|states|stated|promise|promises|promised|describe|describes|described|assert|asserts|asserted"

_TEXT_ONLY_VERB = rf"(?:{_TEXT_ONLY})"
_SAYING_VERB = rf"(?:{_TEXT_ONLY}|{_NEEDS_A_PROSE_SUBJECT})"

#: The gap between the noun and its verb may not cross a clause boundary — no comma, semicolon, dash or
#: full stop. Unbounded, the matcher steps from "the comment" over "explaining why it" and lands on a
#: verb whose subject is the CODE, which is how a correct comment gets reported.
_TIGHT = r"[^.,;:\n\u2014-]{0,24}?"

BANDS: tuple[Band, ...] = (
    Band(
        name="prose-narrates-prose",
        regex=rf"\b(?:this|the|that|its|an?)\s+{_NOUN_GAP}{_PROSE_NOUN}\b{_TIGHT}\b(?:used to|no longer|previously|once)\s+{_SAYING_VERB}\b",
        why="A comment whose subject is a previous comment. Deleting it loses nothing about the code.",
    ),
    Band(
        # A bare past-tense saying verb is not sufficient: "the Ray copy's docstring said so outright"
        # CITES prose as evidence for a rationale, which is kind (1) and must pass. The banned form
        # goes further and declares the quoted prose WRONG or SUPERSEDED, so the contrast marker is
        # what separates "here is what another file claims" from "here is what this file claimed".
        name="prose-narrates-prose",
        regex=(
            rf"\b(?:this|the|that)\s+{_NOUN_GAP}{_PROSE_NOUN}\b{_TIGHT}"
            r"\b(?:said|claimed|asserted|promised|advertised|implied|described)\b"
            r"[^.\n]{0,120}?"
            r"\b(?:wrong|false|otherwise|the opposite|incorrect|misleading|never true|until\s+20\d\d-\d\d-\d\d)\b"
        ),
        why="A comment whose subject is a previous comment. Deleting it loses nothing about the code.",
    ),
    Band(
        name="prose-was-wrong",
        regex=(
            rf"\b(?:this|the|that|above|preceding)\s+{_NOUN_GAP}{_PROSE_NOUN}\b{_TIGHT}"
            r"\b(?:was|were|is|are)\s+"
            r"(?:wrong|false|a lie|incorrect|misleading|stale|outdated|out of date|corrected|rewritten|reworded|amended|superseded)\b"
        ),
        why="A verdict on a previous comment. The estate REWRITES falsified prose; it does not annotate that it was false.",
    ),
    Band(
        name="prose-was-wrong",
        regex=(
            r"\b(?:an? earlier|an? previous|the previous|the old|a prior|the prior)\s+(?:version|revision|draft)\s+of\s+"
            rf"(?:this|the)\s+{_NOUN_GAP}{_PROSE_NOUN}\b"
        ),
        why="A verdict on a previous comment. The estate REWRITES falsified prose; it does not annotate that it was false.",
    ),
    Band(
        name="used-to-say",
        regex=rf"\b(?:used to|no longer|previously)\s+{_TEXT_ONLY_VERB}\b",
        why="Only text says, claims, implies or cites. This narrates a previous revision of the prose whatever its subject.",
    ),
)

_COMPILED: tuple[tuple[Band, re.Pattern[str]], ...] = tuple((b, re.compile(b.regex, re.IGNORECASE)) for b in BANDS)


class Finding(BaseModel):
    """One banned line, with everything a reader needs to fix it without opening the gate."""

    path: str
    line: int
    band: str
    why: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.band}] {self.text.strip()[:140]}\n    {self.why}"


class Change(BaseModel):
    """A file and the line numbers a diff ADDED to it."""

    path: str
    added: set[int] = Field(default_factory=set)


def _prose_lines_by_tokenize(source: str) -> set[int] | None:
    """Exact comment + docstring line numbers, or None when the source will not parse."""
    marked: set[int] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return None
    previous = token.INDENT  # start of file: a leading string is the module docstring
    for tok in tokens:
        is_docstring = tok.type == token.STRING and previous in (token.NEWLINE, token.INDENT, token.DEDENT)
        if tok.type == token.COMMENT or is_docstring:
            marked.update(range(tok.start[0], tok.end[0] + 1))
        if tok.type not in (token.NL, token.COMMENT):
            previous = tok.type
    return marked


def _prose_lines_by_prefix(source: str, markers: tuple[str, ...]) -> set[int]:
    """Heuristic prose lines: the line, stripped, opens with a comment marker.

    This is the honest limit of the non-Python half — a banned phrase inside a `/* … */` block whose
    continuation lines carry no `*` is not seen. Python, which is where 46% of the estate's prose
    lives, is exact.
    """
    return {i for i, text in enumerate(source.splitlines(), start=1) if text.lstrip().startswith(markers)}


def prose_lines(path: str, source: str) -> set[int]:
    suffix = Path(path).suffix
    if suffix in PYTHON_SUFFIXES:
        exact = _prose_lines_by_tokenize(source)
        return exact if exact is not None else _prose_lines_by_prefix(source, ("#",))
    if suffix in PREFIX_COMMENT_SUFFIXES:
        return _prose_lines_by_prefix(source, ("//", "*", "/*", "<!--"))
    return set()


def is_gated(path: str) -> bool:
    p = Path(path)
    if path in EXEMPT_PATHS:
        return False
    if SKIP_DIR_PARTS & set(p.parts):
        return False
    return p.suffix in PYTHON_SUFFIXES or p.suffix in PREFIX_COMMENT_SUFFIXES


def scan_source(path: str, source: str, only_lines: set[int] | None = None) -> list[Finding]:
    """Findings for `source`, restricted to `only_lines` when a diff supplied them.

    A banned phrase may wrap across two physical lines, so each prose line is matched together with
    the prose line after it — otherwise a comment reflowed at 160 columns evades the gate for free.
    """
    if not is_gated(path):
        return []
    lines = source.splitlines()
    prose = prose_lines(path, source)
    findings: list[Finding] = []
    candidates = sorted(prose if only_lines is None else prose & only_lines)
    for number in candidates:
        text = lines[number - 1]
        window = text
        if number + 1 in prose and number + 1 <= len(lines):
            window = f"{text.rstrip()} {lines[number].strip().lstrip('#*/ ')}"
        for band, pattern in _COMPILED:
            match = pattern.search(window)
            if match is None:
                continue
            # Attribute a wrapped match to the line the phrase STARTS on, so a two-line comment does
            # not report twice.
            if match.start() >= len(text.rstrip()):
                continue
            findings.append(Finding(path=path, line=number, band=band.name, why=band.why, text=text))
            break
    return findings


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        msg = "git is not on PATH; the gate reads its change set from the repository"
        raise RuntimeError(msg)
    # argv is a literal tuple and git is resolved through shutil.which — no shell, no user string.
    return subprocess.run((git, *args), cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout  # noqa: S603


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff: str) -> list[Change]:
    """Added line numbers per file from a `--unified=0` diff."""
    changes: dict[str, Change] = {}
    current: Change | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
            current = changes.setdefault(path, Change(path=path))
            continue
        if raw.startswith("+++ /dev/null"):
            current = None
            continue
        if current is None:
            continue
        hunk = _HUNK.match(raw)
        if hunk is not None:
            start = int(hunk.group(1))
            count = int(hunk.group(2) or 1)
            current.added.update(range(start, start + count))
    return [c for c in changes.values() if c.added]


def _content(path: str, *, staged: bool) -> str | None:
    if staged:
        try:
            return _git("show", f":{path}")
        except subprocess.CalledProcessError:
            return None
    disk = REPO_ROOT / path
    if not disk.is_file():
        return None
    return disk.read_text(encoding="utf-8", errors="replace")


def scan_changes(diff: str, *, staged: bool) -> list[Finding]:
    findings: list[Finding] = []
    for change in parse_diff(diff):
        if not is_gated(change.path):
            continue
        source = _content(change.path, staged=staged)
        if source is None:
            continue
        findings.extend(scan_source(change.path, source, only_lines=change.added))
    return findings


def scan_tree() -> list[Finding]:
    """Whole-tree scan. ADVISORY — this is how the standing count is measured, never how a change is gated."""
    findings: list[Finding] = []
    for path in _git("ls-files").splitlines():
        if not is_gated(path):
            continue
        source = _content(path, staged=False)
        if source is None:
            continue
        findings.extend(scan_source(path, source))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="gate the staged diff (the pre-commit entry point)")
    mode.add_argument("--worktree", action="store_true", help="gate every uncommitted change against HEAD")
    mode.add_argument("--base", metavar="REF", help="gate this branch's added lines against REF's merge-base")
    mode.add_argument("--paths", nargs="+", metavar="FILE", help="gate whole files (a newly added file has no base to diff)")
    mode.add_argument("--report", action="store_true", help="ADVISORY whole-tree count; always exits 0")
    args = parser.parse_args(argv)

    if args.report:
        findings = scan_tree()
        for finding in findings:
            print(finding.render())
        print(f"\n{len(findings)} pre-existing line(s) match. ADVISORY ONLY — the rule is forward-only and these are not gated.")
        return 0

    if args.paths:
        findings = []
        for path in args.paths:
            resolved = Path(path).resolve()
            relative = str(resolved.relative_to(REPO_ROOT)) if resolved.is_relative_to(REPO_ROOT) else path
            source = resolved.read_text(encoding="utf-8", errors="replace") if resolved.is_file() else None
            if source is not None:
                findings.extend(scan_source(relative, source))
    elif args.staged:
        findings = scan_changes(_git("diff", "--cached", "--unified=0", "--no-color"), staged=True)
    elif args.worktree:
        findings = scan_changes(_git("diff", "HEAD", "--unified=0", "--no-color"), staged=False)
    else:
        try:
            base = _git("merge-base", "HEAD", args.base).strip()
        except subprocess.CalledProcessError:
            # A gate that dies with a traceback on an unfetched ref reads as a broken gate rather than
            # as a broken invocation, and in CI that difference decides whether anyone looks at it.
            print(f"cannot resolve a merge-base against {args.base!r} — fetch it, or name a ref this clone has.", file=sys.stderr)
            return 2
        findings = scan_changes(_git("diff", base, "--unified=0", "--no-color"), staged=False)

    if not findings:
        return 0
    print(f"Comment rule violated — {len(findings)} new line(s) narrate the PROSE rather than the code.", file=sys.stderr)
    print(f"Rule: {RULE_DOC}\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print("\nRewrite the sentence to state what is true now. If the past shape is the REASON for the", file=sys.stderr)
    print("present one, say that about the CODE ('the old order built the whole list before capping'),", file=sys.stderr)
    print("not about the comment ('this line used to say').", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
