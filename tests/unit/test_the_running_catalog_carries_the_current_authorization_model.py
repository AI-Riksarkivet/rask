"""The catalog image the cluster RUNS must carry the `model.fga` this repo has.

[[XC-073]]. `chart/templates/openfga-model.yaml` runs
`python -m service_kit.governed.auth.write_model` **from inside the catalog image**, deliberately --
"so the hook reads the same bytes the catalog itself enforces". The consequence is that the model
deployed to OpenFGA is whatever the pinned TAG carries, and a pinned tag can lag the repo.

MEASURED 2026-09-23. `type estate` entered `model.fga` on 2026-09-22; the pinned and deployed catalog
was `main-c1b4d569`, built from a 2026-09-21 commit. So the hook wrote a model with no `estate` type,
and `rask-bootstrap-admin` then failed on the first tuple naming it -- 13 restarts, CrashLoopBackOff,
`Invalid tuple 'estate:rask#event_stager@user:service-ingest'. Reason: type 'estate' not found`. Every
`helm upgrade` since had reported `failed -- post-upgrade hooks failed`.

WHY THE DETECTION CANNOT LIVE IN THE IMAGE, which is the whole reason this file exists: the hook holds
only its own copy of the model. Nothing inside a container can know the repository is newer. The
comparison has to be made where both facts are visible -- here.

WHY THE PINS FILE IS THE RIGHT SUBJECT. `chart/values-live-pins.yaml` says so in its own header:
"what the cluster is RUNNING, not what anyone intended". That is exactly the claim under test -- not
"did someone intend to rebuild" but "is the estate answering authorization questions against the
model this repo defines".
"""

from __future__ import annotations

import pathlib
import re
import subprocess


REPO = pathlib.Path(__file__).resolve().parents[2]
PINS = REPO / "chart/values-live-pins.yaml"
MODEL = REPO / "packages/service-kit/src/service_kit/governed/auth/model.fga"

#: The pin the catalog image is recorded under, as `scripts/k3s-pins.sh` writes it.
_CATALOG_PIN = re.compile(r'lance-rest-catalog:\s*"[^"]*?-(?P<sha>[0-9a-f]{7,40})"')


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def test_the_pinned_catalog_is_not_older_than_the_authorization_model() -> None:
    """A pinned catalog older than `model.fga` deploys a model the repo has already moved past.

    SKIPS RATHER THAN FAILS when there is no pin: the file is generated per host by
    `make k3s-pins`, so an empty one means "nothing deployed here", which is not drift. It also skips
    when either commit is unknown to this clone (a shallow CI checkout), because a check that cannot
    see the history must not answer.

    IF THIS IS RED: the cluster is authorizing against a stale model. Rebuild and re-pin --
    `scripts/dagger-image.sh --name rest-catalog --push <registry>/lance-rest-catalog:main-<sha>`,
    then `make k3s-pins`. Do not edit the pin by hand; it records what RAN.
    """
    if not PINS.exists():
        return  # no cluster pinned from this checkout
    found = _CATALOG_PIN.search(PINS.read_text())
    if not found:
        return  # nothing deployed under a git-derived tag
    pinned = found.group("sha")

    model_commit = _git("log", "-1", "--format=%H", "--", str(MODEL.relative_to(REPO))).stdout.strip()
    if not model_commit:
        return  # no history for the model file in this clone

    # `cat-file -e` rather than trusting the tag: a pin can name a commit this clone never fetched,
    # and `merge-base` would then answer "not an ancestor" for the wrong reason entirely.
    if _git("cat-file", "-e", f"{pinned}^{{commit}}").returncode != 0:
        return  # the pinned commit is not in this clone; the comparison would be a guess

    is_ancestor = _git("merge-base", "--is-ancestor", model_commit, pinned).returncode == 0
    assert is_ancestor, (
        f"the pinned catalog image (main-{pinned}) predates the current `model.fga` "
        f"({model_commit[:8]}), so `openfga-model.yaml`'s hook writes a model this repo has already "
        "moved past -- and it writes it SUCCESSFULLY, so the only symptom is a different hook failing "
        "on a type the store has never heard of. Rebuild the catalog image and re-run `make k3s-pins`."
    )
