"""A CI job that reaches `dagger` must install the CLI, however many hops away the call is.

`e2e-auth` ran `dagger call auth-chain` as a step and installed nothing. `e2e-stack` and `e2e-ray`
reach it further out — `make e2e-ci` runs `scripts/e2e_stack.sh` runs `scripts/dagger-image.sh` — and
installed nothing either. All three died on `!! dagger CLI not on PATH` the first time they ran in
five days, one step past the helm-repository fix, which is how the hop was found at all.

THE JOB THAT CALLS IT DIRECTLY IS THE EASY HALF. A gate matching only `run: dagger …` would have
caught `e2e-auth` and passed the other two, which is the failure mode this estate keeps meeting: a
check that covers the obvious spelling and reports coverage. So the search follows the invocation —
a `run:` block, the Makefile recipe it names, and the scripts either one names — and asks whether
`dagger` is reached at all.

BOUNDED AT TWO HOPS, deliberately and with the bound stated. Job → make target → script, or job →
script → script, is how every current lane reaches it; a third hop would need a script calling a
script calling a script, which nothing does. A deeper chain would go unseen, and that is a known
limit rather than a claim of completeness.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_MAKEFILE = _ROOT / "Makefile"
#: The pinned installer every lane that has one already uses.
_INSTALL = "dl.dagger.io"
_DAGGER_CALL = re.compile(r"(?<![\w-])dagger\s+(call|core|run)\b")


def _make_recipe(target: str) -> str:
    """The recipe lines of one Makefile target, or empty when there is no such target."""
    found = re.search(rf"^{re.escape(target)}:.*?\n((?:\t.*\n|#.*\n|\n)*)", _MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE)
    return found.group(1) if found else ""


def _script(name: str) -> str:
    path = _ROOT / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _reachable_text(run: str) -> str:
    """`run` plus, one hop out, the make recipes and scripts it names — and their scripts in turn."""
    parts = [run]
    for target in re.findall(r"\bmake\s+([a-z0-9][a-z0-9-]*)", run):
        parts.append(_make_recipe(target))
    for name in re.findall(r"\b(scripts/[A-Za-z0-9_.-]+\.(?:sh|py))", run):
        parts.append(_script(name))
    # Hop two: whatever those recipes and scripts themselves name.
    for text in list(parts[1:]):
        for name in re.findall(r"\b(scripts/[A-Za-z0-9_.-]+\.(?:sh|py))", text):
            parts.append(_script(name))
    return "\n".join(parts)


def _jobs() -> dict[str, dict]:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]


def test_the_workflow_parses_into_jobs_with_steps() -> None:
    """Anti-vacuity: every assertion below iterates these, and an empty parse would pass them all."""
    jobs = _jobs()

    assert len(jobs) > 5, f"only {len(jobs)} jobs parsed out of ci.yml"
    assert any(any("dl.dagger.io" in str(step.get("run", "")) for step in job.get("steps", [])) for job in jobs.values()), (
        "no job installs the Dagger CLI at all — the parse is wrong, or the estate stopped using Dagger"
    )


@pytest.mark.parametrize("name", sorted(_jobs()))
def test_a_job_that_reaches_dagger_installs_the_cli(name: str) -> None:
    job = _jobs()[name]
    steps = job.get("steps", [])
    runs = [str(step.get("run", "")) for step in steps]
    if not any(_DAGGER_CALL.search(_reachable_text(run)) for run in runs):
        return  # this lane never reaches dagger; nothing to install

    assert any(_INSTALL in run for run in runs), (
        f"job {name!r} reaches `dagger` — directly or through a make target or script it runs — and installs no CLI. "
        "It dies on `!! dagger CLI not on PATH`, which reads as a broken script rather than a missing step."
    )


def test_every_job_that_installs_the_cli_also_WARMS_the_engine() -> None:
    """Installing the CLI is not starting the engine, and the second is a network call.

    Measured 2026-09-24: four Dagger jobs across two runs died on `start engine: failed to pull image
    ... read: connection reset by peer` from registry.dagger.io, while that manifest answered 200 from
    outside CI. The image was fine; the runner's route to it was not, and every `dagger call` job is
    exposed to that. A warm step keeps the retry on the PULL and off the work — retrying the call
    itself would retry a failing TEST into passing, which is strictly worse than a flaky job.
    """
    jobs = _jobs()

    missing = []
    for name, job in jobs.items():
        steps = job.get("steps", [])
        if not any(_INSTALL in str(step.get("run", "")) for step in steps):
            continue
        if not any("dagger core version" in str(step.get("run", "")) for step in steps):
            missing.append(name)

    assert not missing, (
        f"these jobs install the Dagger CLI and never warm the engine: {missing}. A transient registry "
        "reset then fails the job on its first real call, and the verdict is about the network."
    )


def test_the_warm_step_RETRIES_and_still_fails_on_the_last_attempt() -> None:
    """A retry that swallowed the final failure would turn a genuinely unreachable registry into a
    green job — the opposite of the problem being fixed."""
    body = _CI.read_text(encoding="utf-8")
    warm = body[body.index("Warm the Dagger engine") : body.index("Warm the Dagger engine") + 900]

    assert "for attempt in" in warm, "the warm step does not retry, so a single reset still fails the job"
    assert warm.count("dagger core version") >= 2, "the warm step has no unguarded final attempt, so a real failure would be swallowed"


def test_every_installer_pins_the_SAME_version() -> None:
    """Two lanes on different engine versions is a difference nobody set out to have, and it surfaces
    as a cache miss or a module that will not load rather than as a version complaint."""
    versions = set(re.findall(r"DAGGER_VERSION=(\S+)", _CI.read_text(encoding="utf-8")))

    assert versions, "no pinned DAGGER_VERSION in the workflow — an unpinned installer takes whatever is newest"
    assert len(versions) == 1, f"CI installs more than one Dagger version: {sorted(versions)}"
