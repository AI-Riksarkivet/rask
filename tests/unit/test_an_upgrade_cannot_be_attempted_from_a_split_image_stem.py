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

import pathlib
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
#: A SCRUBBED path, but not one missing the interpreter the script legitimately needs. `/usr/bin:/bin`
#: alone is a dev host's answer: the uv image CI runs this in has no `/usr/bin/python3`, so the script
#: could not parse the cluster JSON and exited non-zero — which the assertions below read as "a split
#: stem was accepted". The point of scrubbing is that `KUBECTL` is the fake and nothing else reaches a
#: real cluster, not that the script must run without a language runtime. Derived from the interpreter
#: running this test, so it is correct on any host and in the container.
_MINIMAL_PATH = f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin"

PINS = REPO / "scripts" / "k3s-pins.sh"


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
            env={"PATH": _MINIMAL_PATH, "KUBECTL": str(fake), "KUBECONFIG": "/dev/null"},
            cwd=REPO,
            timeout=60,
        )
    finally:
        fake.unlink(missing_ok=True)

    assert done.returncode == 1, f"a split stem was accepted (rc={done.returncode}); stdout={done.stdout!r} stderr={done.stderr!r}"
    for expected in ("lance-rest-catalog", "tag-new", "tag-old", "rask-catalog", "rask-viewer"):
        assert expected in done.stderr, f"the refusal does not name {expected!r}, so it cannot be acted on: {done.stderr!r}"


def test_a_converged_estate_passes_and_says_so() -> None:
    """Without this the guard could pass by refusing everything, which is the other way to be useless.

    The Docker Hub short form `minio/minio:...` is third-party although its path head has no dot; read as
    first-party it becomes a second `minio` tag beside the estate's own build and a converged estate is refused
    ([[XC-075]]).
    """
    fake = REPO / "tests" / "unit" / "_fake_kubectl_converged.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'JSON'\n"
        '{"items":[\n'
        '{"metadata":{"name":"rask-catalog"},"spec":{"template":{"spec":{"containers":['
        '{"image":"localhost:5000/lance-rest-catalog:tag-one"}]}}}},\n'
        '{"metadata":{"name":"rask-viewer"},"spec":{"template":{"spec":{"containers":['
        '{"image":"localhost:5000/lance-rest-catalog:tag-one"}]}}}},\n'
        '{"metadata":{"name":"rask-minio"},"spec":{"template":{"spec":{"containers":['
        '{"image":"localhost:5000/minio:lakehouse-0123abcd"}]}}}},\n'
        '{"metadata":{"name":"upstream-store"},"spec":{"template":{"spec":{"containers":['
        '{"image":"minio/minio:RELEASE.2025-04-22T22-12-26Z"}]}}}}\n'
        "]}\nJSON\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    try:
        done = subprocess.run(  # noqa: S603
            ["bash", str(PINS), "--check-only"],
            capture_output=True,
            text=True,
            env={"PATH": _MINIMAL_PATH, "KUBECTL": str(fake), "KUBECONFIG": "/dev/null"},
            cwd=REPO,
            timeout=60,
        )
    finally:
        fake.unlink(missing_ok=True)

    assert done.returncode == 0, f"a converged estate was refused: stderr={done.stderr!r}"
    assert "converged" in done.stderr, f"a silent pass reads exactly like a check that did not run: {done.stderr!r}"
