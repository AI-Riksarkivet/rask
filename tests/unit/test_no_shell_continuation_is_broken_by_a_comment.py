"""A `#` comment inside a backslash continuation silently truncates the command.

Bash joins `foo \\` with the next line, so a comment placed there starts a comment on the JOINED line
and swallows the rest of it — every following argument line then runs as its own command. `bash -n`
accepts all of it, because it is syntactically valid; it is just not the command anyone wrote.

DEMONSTRATED, not asserted:

    printf 'ARG:%s\\n' one \\
      # a comment
      --set two
    ->  ARG:one
        line 3: --set: command not found

This is how a `--set image.localImages=true` added with an explanatory comment above it would have
silently stopped reaching `helm upgrade` in both e2e stack scripts, after every image had been built
and loaded. Caught by the gate written in the same change, not by review and not by `bash -n`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SHELL = sorted(p for p in (_ROOT / "scripts").glob("*.sh"))
#: A line whose command continues onto the next one. A trailing `\` that is itself escaped (`\\`) does
#: not continue, so the count of trailing backslashes has to be odd.
_CONTINUES = re.compile(r"(?<!\\)(?:\\\\)*\\$")


def _broken_continuations(body: str) -> list[tuple[int, str]]:
    """Lines where a LIVE command is continued into a comment.

    The continued line must not itself be a comment: `scripts/verify_control_events.sh` documents a
    multi-line `helm upgrade` inside its header block, where every line starts with `#` and the
    trailing backslash is prose. Flagging that was this check's first result and its first false
    positive — nothing is truncated when nothing was running.
    """
    out: list[tuple[int, str]] = []
    lines = body.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.lstrip().startswith("#") or not _CONTINUES.search(line.rstrip("\n")):
            continue
        following = lines[i + 1].lstrip()
        if following.startswith("#"):
            out.append((i + 2, lines[i + 1].strip()))
    return out


def test_the_scan_reaches_the_scripts() -> None:
    """Anti-vacuity: the parametrisation below is empty if the glob stops matching."""
    assert len(_SHELL) >= 10, f"only {len(_SHELL)} shell scripts found under scripts/"
    assert any(_CONTINUES.search(line) for p in _SHELL for line in p.read_text(encoding="utf-8").splitlines()), (
        "no line continuation found anywhere — the pattern no longer matches what these scripts do"
    )


@pytest.mark.parametrize("script", [p.name for p in _SHELL])
def test_no_comment_sits_inside_a_continuation(script: str) -> None:
    broken = _broken_continuations((_ROOT / "scripts" / script).read_text(encoding="utf-8"))

    assert not broken, (
        f"{script} has a comment inside a line continuation, which ENDS the command and turns every "
        f"following argument line into its own: {broken}. `bash -n` accepts it. Put the comment above "
        "the invocation."
    )
