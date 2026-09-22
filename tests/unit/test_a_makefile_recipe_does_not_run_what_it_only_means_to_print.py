"""No executable Makefile recipe line contains a backtick.

MEASURED 2026-09-22, in a target added the same day. `k3s-converge` ended with

    echo ">> every stem converged — `make k3s-up` will now pass its own check"

meaning to name the next command. A backtick is COMMAND SUBSTITUTION, so printing that advice RAN a
full `make k3s-up`. It showed up in the deploy's own output as

    >> every stem converged — make[1]: Entering directory '/home/gabriel/Desktop/rask'

and the stem check then printed twice, because the nested make ran its own. It was harmless only
because `k3s-up` is idempotent; the same line in front of a destructive target is a deploy nobody
asked for, reported as a message.

THE SCOPE WAS MEASURED BEFORE THIS GATE WAS WRITTEN: zero executable recipe lines in the Makefile
carry a backtick — every genuine command substitution uses `$(...)`. So there is no exemption list and
none is expected; a backtick in a recipe is a mistake, not a style.
"""

from __future__ import annotations

import pathlib


REPO = pathlib.Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"

#: Recipe lines that are pure prose. `@#` and `#` are make/shell comments; `: #` is the no-op-plus-
#: comment idiom this Makefile uses to keep long rationale inside a recipe without running anything.
_COMMENT_PREFIXES = ("@#", "#", ": #")


def _executable_recipe_lines() -> list[tuple[int, str]]:
    """``(line number, text)`` for every recipe line make actually hands to a shell."""
    found: list[tuple[int, str]] = []
    for number, line in enumerate(MAKEFILE.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("\t"):
            continue
        body = line.lstrip("\t")
        if body.startswith(_COMMENT_PREFIXES):
            continue
        found.append((number, body))
    return found


def test_the_walk_finds_recipe_lines() -> None:
    """An empty walk would make the assertion below vacuously true — the failure mode this file's own
    subject matter is about."""
    assert len(_executable_recipe_lines()) > 100, "the Makefile walk found almost no executable recipe lines; it is reading the wrong thing"


def test_no_recipe_line_substitutes_a_command_it_only_meant_to_NAME() -> None:
    """`$(...)` is the Makefile's form for a substitution it means; a backtick is the one it does not."""
    offenders = [f"{number}: {text}" for number, text in _executable_recipe_lines() if "`" in text]

    assert not offenders, (
        "these recipe lines contain a backtick, which make hands to the shell as COMMAND SUBSTITUTION — "
        "so a command named inside a message is EXECUTED when the message is printed. Use single quotes "
        "to name a command, or `$(...)` to run one on purpose:\n  " + "\n  ".join(offenders)
    )
