"""The authz model is checked for VALIDITY, and by a check that can actually fail.

[[XC-044]]. All exit codes below were measured 2026-09-20 against fga CLI v0.6.4 — the exact version
`ci.yml:201` installs — with no pipe between the command and `$?`, because a `| tail` reports TAIL's
status and that mistake is what made the first reading of this wrong.

WHAT WAS ALREADY TRUE, so this gate is not sold as more than it is: an invalid model makes
`fga model test` exit **1**. The `ms-authz` job does fail today. What it fails WITH is
`Error: error running tests due to rpc error: code = Code(2056) desc = the relation type
'nonexistent#assignee' ... is not valid` — a test-runner error about a model defect, printed where a
reader is looking for assertion counts.

SO THE GATE BUYS TWO THINGS. It names the defect as a MODEL defect, first, before the suite runs; and
it stops the model from reaching the estate through the one path the suite does not cover — the drift
check downstream regenerates cleanly from an invalid model (`fga model transform` exits 0 and emits
23 KB of well-formed JSON), so a developer who follows this file's own header instruction to
regenerate `model.json` produces two copies that agree perfectly and are both invalid.

THE TRAP, AND THE REASON THIS SUITE EXISTS RATHER THAN A ONE-LINE DIFF:

    $ fga model validate --file <an invalid model>
    {"is_valid":false,"error":"the relation type 'nonexistent#assignee' on 'assignee' in object
     type 'role' is not valid"}
    $ echo $?
    0

**`validate` exits 0 on an invalid model.** The obvious way to close this row — add
`fga model validate --file …` to the job — produces a line that runs, prints the error, and passes.
That is worse than not adding it: the job would then name a check it does not perform, and the next
reader stops looking. The verdict is in the JSON body, so the gate reads `.is_valid` and nothing else
will do.

Asserted against the workflow TEXT rather than by running CI, for the reason the estate's other CI
gates are: a test that shelled out to `act` would run nowhere and prove nothing on a laptop.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = ROOT / "Makefile"


def _ms_authz() -> str:
    """The `ms-authz` job's text, from its key to the next top-level job key."""
    text = CI.read_text(encoding="utf-8")
    start = text.index("\n  ms-authz:")
    rest = text[start + 1 :]
    nxt = re.search(r"^  [a-z][\w-]*:$", rest[1:], re.MULTILINE)
    return rest[: nxt.start() + 1] if nxt else rest


def test_the_job_is_findable() -> None:
    """Without this every assertion below could pass by reading an empty string."""
    assert "fga model test" in _ms_authz(), "the ms-authz job no longer runs the model suite — this gate is reading the wrong text"


def test_the_model_is_VALIDATED() -> None:
    """Three checks on the model and none that asks, first and legibly, whether it is well-formed."""
    assert "model validate" in _ms_authz(), (
        "`ms-authz` never runs `fga model validate`, so an invalid model is reported as an rpc error "
        "from the test runner — and reaches the estate intact if `model.json` was regenerated from it"
    )


def test_the_validate_step_GATES_on_is_valid_rather_than_on_the_exit_code() -> None:
    """THE TRAP THIS SUITE EXISTS FOR. v0.6.4 exits 0 while printing `{"is_valid":false}`, so a bare
    `fga model validate --file …` is a line that runs, prints the error and passes. The verdict lives
    in the JSON body."""
    job = _ms_authz()

    assert "is_valid" in job, (
        "the validate step does not read `.is_valid` — `fga model validate` exits 0 on an INVALID "
        "model (measured, v0.6.4), so a step that only runs the command passes on one"
    )


def test_the_local_seam_validates_TOO() -> None:
    """`make fga-test` is what a developer runs and what `make check` calls. A gate that exists only
    in CI is found at review time, after the model has already been edited and pushed."""
    text = MAKEFILE.read_text(encoding="utf-8")
    start = text.index("\nfga-test:")
    target = text[start : start + 2000].split("\n\n")[0]

    assert "model validate" in target, "`make fga-test` does not validate the model, so the check exists only in CI"
    assert "is_valid" in target, "`make fga-test` runs validate without reading `.is_valid`, which passes on an invalid model"
