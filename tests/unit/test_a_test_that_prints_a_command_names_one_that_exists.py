"""A failure message that tells a reader to run something must name something runnable.

`test_dummy_lane_e2e.py` printed `scripts/seed_medallion_fga.sh <project> <zone-warehouse-id>` while
that script took no arguments at all, and told the reader the script "does not know about" a link it
had since learned to seed. Advice in a failure message is read at the worst possible moment, by
someone who has just lost a lane and is deciding whether the platform or their estate is broken.
Wrong advice there costs more than no advice: it sends them to run a command that either does not
exist or writes tuples on the wrong warehouse, and then to distrust everything else the message says.

TWO CLAUSES, because the script drifted in both directions at once. The path must exist, and the
ARITY must match — a message showing two positional arguments against a script that reads none is the
exact shape that shipped, and a path check alone passes it.

The arity read is textual and deliberately one-sided: it asks whether the script references `$N` at
all, so a script that reads MORE positionals than a message shows is fine (a message may omit an
optional argument), while one that reads FEWER is the defect.

FUNCTION BODIES ARE STRIPPED FIRST, and without that this gate cannot fail. `$1` and `$2` inside a
shell function are that FUNCTION's arguments and say nothing about the script's own signature —
`seed_medallion_fga.sh` has a two-argument `link()` helper, so a version that read no script
arguments at all still matched `$2` and the gate passed the exact defect it was written for. Stripping
is bounded by a closing `}` in column zero, which is how every function in these scripts is written,
and `test_the_function_strip_actually_strips` keeps that assumption honest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SUITE = _ROOT / "tests" / "e2e-py"
#: `scripts/<name>.sh` inside a string, plus the arguments that follow it on the line. An argument is
#: either a `<placeholder>` a reader substitutes or an f-string `{expr}` the message fills in — both
#: are positional, and reading only the first form is how a two-argument message scanned as zero.
_INVOCATION = re.compile(r"(scripts/[A-Za-z0-9_.-]+\.sh)((?:\s+(?:<[^>]+>|\{[^}]+\}))*)")
_ARGUMENT = re.compile(r"<[^>]+>|\{[^}]+\}")


#: A `name() {` line through the next `}` in column zero. Bounded by that terminator rather than by a
#: brace count: an unterminated range would swallow the rest of the file and make every arity check
#: vacuous, which is the failure mode this strip exists to prevent in the first place.
_FUNCTION_OPEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(\)\s*\{")


def _script_level(body: str) -> str:
    """`body` with every shell function body removed, so `$N` means the SCRIPT's Nth argument."""
    out: list[str] = []
    inside = False
    for line in body.splitlines():
        if not inside and _FUNCTION_OPEN.match(line):
            inside = True
            continue
        if inside:
            inside = line.rstrip() != "}"
            continue
        out.append(line)
    return "\n".join(out)


def _invocations() -> list[tuple[str, str, int]]:
    """`(test file, script path, positional count)` for every command an e2e test prints."""
    out: list[tuple[str, str, int]] = []
    for path in sorted(_SUITE.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            for script, args in _INVOCATION.findall(line):
                out.append((path.name, script, len(_ARGUMENT.findall(args))))
    return out


def test_the_suite_prints_commands_at_all() -> None:
    """Anti-vacuity: the parametrisation below is empty the moment the regex or the glob stops
    matching, and an empty parametrisation is a gate reporting coverage it does not have."""
    found = _invocations()

    assert found, "no scripts/*.sh invocation found in any e2e test — the scan moved, not the suite"
    assert any(count > 0 for _, _, count in found), "no invocation shows an argument, so the arity clause below is measuring nothing"


@pytest.mark.parametrize(("test_file", "script", "positionals"), sorted(set(_invocations())))
def test_the_command_exists_and_takes_the_arguments_it_is_shown_with(test_file: str, script: str, positionals: int) -> None:
    path = _ROOT / script

    assert path.is_file(), f"{test_file} tells a reader to run {script}, which does not exist"

    body = _script_level(path.read_text(encoding="utf-8"))
    for n in range(1, positionals + 1):
        assert re.search(rf"\$\{{?{n}\b", body), (
            f"{test_file} shows {script} with {positionals} positional argument(s), but the script never reads ${n}. "
            "A reader who follows that message passes arguments the script ignores."
        )


def test_the_function_strip_actually_strips() -> None:
    """Anti-vacuity for the strip itself: it must remove something and keep something.

    A strip that removed nothing would restore the defect (function arguments read as the script's);
    one that removed everything would make every arity assertion pass on an empty string.
    """
    sample = 'top_before\nhelper() {\n  local a="$1"\n}\ntop_after="${2:?}"\n'
    stripped = _script_level(sample)

    assert "top_before" in stripped and "top_after" in stripped, f"the strip ate script-level lines: {stripped!r}"
    assert 'local a="$1"' not in stripped, f"the strip left a function body behind: {stripped!r}"
