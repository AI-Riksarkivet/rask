"""A recipe that runs `helm upgrade` and then cleans up must propagate helm's status, not the cleanup's.

MEASURED 2026-09-22, in `k3s-converge`, a target added the same day. Its recipe ended

    $(HELM) upgrade --install rask ./chart ... ; rm -f "$$LIVE"

and a shell's exit status is its LAST command's, so the status make saw was `rm`'s. Observed on a real
deploy::

    Error: UPGRADE FAILED: post-upgrade hooks failed: 1 error occurred:
        * timed out waiting for the condition
    ...
    EXIT=0

The target then printed ">> every stem converged" and the operator — me — read the run as a successful
deploy and went on to measure against it. It is the worst direction for this failure to go: a deploy
that half-applied reports clean, so the cluster and the release disagree and nothing says so.

NOT A STYLE RULE. `rm -f` is the right cleanup and belongs there; what cannot happen is the cleanup
DECIDING the outcome. The fix is three tokens — capture `$?`, clean up, exit with it — and the gate is
here because the next person adding a deploy target will reach for the same `; cleanup` tail.

THE SCOPE WAS MEASURED BEFORE THIS GATE WAS WRITTEN: `helm upgrade` appears in this Makefile on the
recipe lines this walk finds and nowhere else, so there is no exemption list and none is expected.
"""

from __future__ import annotations

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"

#: `$(HELM) upgrade` or a bare `helm upgrade` — the invocation whose status must survive.
_UPGRADE = re.compile(r"(\$\(HELM\)|\bhelm)\s+upgrade\b")
#: A status capture: `rc=$$?`, `status=$$?`, … Make doubles the `$`, so the recipe text carries `$$?`.
_CAPTURES_STATUS = re.compile(r"=\s*\$\$\?")


def _logical_recipe_commands() -> list[tuple[int, str]]:
    """``(line number, text)`` per LOGICAL recipe command — backslash continuations joined.

    Joining matters: `k3s-up`'s upgrade is spread over a dozen continued lines, so a walk over PHYSICAL
    lines would see an invocation with nothing after it and pass a recipe whose cleanup is three lines
    down. Reading the wrong unit is how a gate passes everything.
    """
    joined: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for number, line in enumerate(MAKEFILE.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("\t"):
            pending, start = [], 0
            continue
        body = line.lstrip("\t")
        if not pending:
            start = number
        if body.endswith("\\"):
            pending.append(body[:-1])
            continue
        joined.append((start or number, "".join([*pending, body])))
        pending = []
    return joined


def _helm_upgrade_commands() -> list[tuple[int, str, re.Match[str]]]:
    """Every logical recipe command that actually RUNS a helm upgrade.

    An `echo` is excluded because it cannot run one, and the Makefile contains exactly that case:
    `@echo ">> NOT bare 'helm upgrade' ..."` is advice ABOUT the command. Matching prose would make
    this gate report a line nobody can fix, which trains the next reader to ignore it.
    """
    found: list[tuple[int, str, re.Match[str]]] = []
    for number, body in _logical_recipe_commands():
        if body.startswith(("@#", "#", ": #", "@echo", "echo")):
            continue
        match = _UPGRADE.search(body)
        if match is not None:
            found.append((number, body, match))
    return found


def test_the_walk_finds_the_upgrades() -> None:
    """An empty walk would make the assertion below vacuously true — the same shape as the defect."""
    assert _helm_upgrade_commands(), "the Makefile walk found no `helm upgrade` recipe command; it is reading the wrong thing"


def test_a_helm_upgrade_is_the_LAST_word_on_whether_the_deploy_SUCCEEDED() -> None:
    """Either the upgrade ends its recipe command, or the command captures its status before moving on."""
    offenders: list[str] = []
    for number, _body, match in _helm_upgrade_commands():
        tail = match.string[match.end() :]
        # A `;` after the upgrade means another command runs and supplies the shell's exit status.
        if ";" in tail and not _CAPTURES_STATUS.search(tail):
            offenders.append(f"{number}: ...{tail[-120:]}")

    assert not offenders, (
        "these recipe commands run `helm upgrade` and then run something else, so the SHELL's exit "
        "status is the last command's and a FAILED upgrade reports success. Capture it first — "
        "`; rc=$$?; <cleanup>; exit $$rc` — or let the upgrade end the command:\n  " + "\n  ".join(offenders)
    )
