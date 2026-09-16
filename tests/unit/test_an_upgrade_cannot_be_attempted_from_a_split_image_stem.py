"""`make k3s-up` must refuse while any image stem in the cluster runs more than one tag.

[[LH-169]]. The chart carries ONE `image.tag` per image stem and TEN workloads share
`lance-rest-catalog`, so a `kubectl set image` on part of a stem leaves the release pinning the other
half — and the next `helm upgrade`, even one with byte-identical values, silently reverts whatever was
rolled forward. Measured 2026-09-16: six deployments on `main-3803cc1d` against four on
`main-9e5ff5b3`, while the image carrying the fixes was deployed nowhere.

THE DETECTION ALREADY EXISTED AND WAS UNREACHABLE FROM THE PATH THAT NEEDED IT. `scripts/k3s-pins.sh`
has refused a split stem since 2026-08-16 — correctly, because no honest pin file exists in that state
— but only when someone asked for a pin file. The destructive operation is `helm upgrade`, and nothing
stood in front of it. Same script, `--check-only`, run as a prerequisite: one implementation of the
rule, and the refusal now precedes the upgrade instead of sitting beside it.

READ OFF THE MAKEFILE AND THE SCRIPT, never a cluster. A test that needed a live k3s would skip in CI,
which is where a prerequisite is most likely to be dropped by someone shortening a recipe.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"
PINS = REPO / "scripts" / "k3s-pins.sh"


def _prerequisites(target: str) -> list[str]:
    """The prerequisite list off `target:`'s own rule line."""
    line = re.search(rf"^{re.escape(target)}:([^\n]*)$", MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert line, f"the Makefile has no `{target}:` rule — rename the target here or restore it there"
    return line.group(1).split("##")[0].split()


def test_the_release_upgrade_runs_the_stem_check_first() -> None:
    prerequisites = _prerequisites("k3s-up")

    assert "k3s-stem-check" in prerequisites, (
        f"`make k3s-up` runs `helm upgrade` with prerequisites {prerequisites} and no stem check, so an "
        "upgrade from a split estate reverts whichever half of the stem was rolled forward — silently, "
        "because the chart has one tag per stem and the release still pins the other one"
    )


def test_the_stem_check_is_the_pin_script_rather_than_a_second_implementation() -> None:
    """Two readings of "what is the cluster running" is how the pin file and the upgrade drifted apart
    in the first place."""
    recipe = re.search(r"^k3s-stem-check:.*?(?=^\w[\w-]*:)", MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    assert recipe, "the Makefile has no `k3s-stem-check` target"

    assert "k3s-pins.sh --check-only" in recipe.group(0), (
        "the stem check does not go through `scripts/k3s-pins.sh --check-only`, so the rule now has two implementations and only one of them gates the upgrade"
    )


def test_the_pin_script_accepts_check_only_and_writes_nothing() -> None:
    """The mode has to exist AND has to be inert: `k3s-up` would otherwise rewrite the pin file as a
    side effect of being asked whether it may run."""
    body = PINS.read_text(encoding="utf-8")

    assert '"${1:-}" == "--check-only"' in body, "`--check-only` is not parsed, so the prerequisite would fall through to a pin write"
    assert 'if [[ -n "$CHECK_ONLY" ]]; then exit 0; fi' in body, "`--check-only` does not stop before the `mv`, so it would write the pin file anyway"


def test_a_split_stem_is_refused_with_both_tags_named() -> None:
    """Driven against a FAKE kubectl, so the rule is exercised rather than described.

    Both tags and both owner sets have to appear: the operator's next move is "rebuild one image
    carrying every change and roll the whole stem", and they cannot make it from a message that says
    only that something diverged.
    """
    fake = REPO / "tests" / "unit" / "_fake_kubectl_split_stem.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'JSON'\n"
        '{"items":[\n'
        '{"metadata":{"name":"rask-catalog"},"spec":{"template":{"spec":{"containers":['
        '{"image":"localhost:5000/lance-rest-catalog:tag-new"}]}}}},\n'
        '{"metadata":{"name":"rask-viewer"},"spec":{"template":{"spec":{"containers":['
        '{"image":"localhost:5000/lance-rest-catalog:tag-old"}]}}}}\n'
        "]}\nJSON\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    try:
        done = subprocess.run(  # noqa: S603
            ["bash", str(PINS), "--check-only"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "KUBECTL": str(fake), "KUBECONFIG": "/dev/null"},
            cwd=REPO,
            timeout=60,
        )
    finally:
        fake.unlink(missing_ok=True)

    assert done.returncode == 1, f"a split stem was accepted (rc={done.returncode}); stdout={done.stdout!r} stderr={done.stderr!r}"
    for expected in ("lance-rest-catalog", "tag-new", "tag-old", "rask-catalog", "rask-viewer"):
        assert expected in done.stderr, f"the refusal does not name {expected!r}, so it cannot be acted on: {done.stderr!r}"


def test_a_converged_estate_passes_and_says_so() -> None:
    """Without this the guard could pass by refusing everything, which is the other way to be useless."""
    fake = REPO / "tests" / "unit" / "_fake_kubectl_converged.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'JSON'\n"
        '{"items":[\n'
        '{"metadata":{"name":"rask-catalog"},"spec":{"template":{"spec":{"containers":['
        '{"image":"localhost:5000/lance-rest-catalog:tag-one"}]}}}},\n'
        '{"metadata":{"name":"rask-viewer"},"spec":{"template":{"spec":{"containers":['
        '{"image":"localhost:5000/lance-rest-catalog:tag-one"}]}}}}\n'
        "]}\nJSON\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    try:
        done = subprocess.run(  # noqa: S603
            ["bash", str(PINS), "--check-only"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "KUBECTL": str(fake), "KUBECONFIG": "/dev/null"},
            cwd=REPO,
            timeout=60,
        )
    finally:
        fake.unlink(missing_ok=True)

    assert done.returncode == 0, f"a converged estate was refused: stderr={done.stderr!r}"
    assert "converged" in done.stderr, f"a silent pass reads exactly like a check that did not run: {done.stderr!r}"
