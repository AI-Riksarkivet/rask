"""Four mechanisms from `docs/architecture/batch-processing-invariants.md` that work, and that nothing would notice losing.

Each was audited as PARTIAL for the same reason: the behaviour is real and correct, and no test binds
it, so the regression that removes it leaves the suite green. That is this estate's signature defect —
a guard that guards nothing — and it is worth more to close than the features filed beside it, because
a silently-lost invariant costs the incident it was written to prevent, twice.

* **B12** — the sweep shuffles its dataset list so a persistently-failing dataset early in listing
  order cannot starve the ones behind it. Delete `random.shuffle(uris)` and every test still passes.
* **B13** — the stage runner's single-flight `_write_lock` is an `asyncio.Lock`, which is PROCESS-local. It
  is only a lock at all while `stageRunnerReplicas` is 1. Nothing ties the two together, so scaling the
  stage runner would silently turn overlapping `write_dataset(mode="overwrite")` calls back on.
* **B3** — `MEDALLION_RAY_CODE_VERSION` is the second axis of the submission id, which is what stops a
  rolling deploy re-attaching to the previous build's Ray job. It is fed by ONE chart line, pinned by
  no rendered-chart test; delete the line and the id silently returns to its pre-B3 form, green.
* **B5(i)** — `max_calls` is not a `.options()` key Ray honours in this path, so passing it is a
  silent no-op: the operator sets a worker-recycling knob, sees no error, and gets no recycling.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.chart_render import ESO_ARGS, RAY_ARGS


REPO = Path(__file__).resolve().parents[2]


class TestB12TheSweepDoesNotConsumeInListingOrder:
    def test_the_shuffle_is_still_there(self) -> None:
        """The invariant in its cheapest form. `discover_dataset_uris` returns object-store listing
        order, which is stable — so a dataset that fails every pass sits in front of the same
        successors forever, and they are the ones that never get maintained."""
        src = (REPO / "services/maintenance/src/maintenance/services/sweep.py").read_text()
        assert "random.shuffle(uris)" in src, (
            "the sweep consumes datasets in listing order again — a persistently-failing dataset early in that order starves every dataset behind it, silently"
        )

    def test_the_failure_retry_list_is_shuffled_too(self) -> None:
        """The same fairness argument under a mass incident, and the same silent loss."""
        src = (REPO / "services/maintenance/src/maintenance/services/sweep.py").read_text()
        assert "random.shuffle(failed)" in src


class TestB3TheDeployAxisIsFedByTheChart:
    def _render(self) -> str:
        helm = shutil.which("helm") or str(REPO / ".localbin/helm")
        if not Path(helm).exists():
            pytest.skip("helm not available")
        argv = [
            helm,
            "template",
            "rask",
            str(REPO / "chart"),
            *ESO_ARGS,
            *RAY_ARGS,
            "--set-string",
            "frontend.oidc.publicIssuer=http://localhost:8080/dex",
            "--set-string",
            "frontend.oidc.publicOrigin=http://localhost:8080",
            "--set",
            "image.localImages=true",
        ]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def test_every_stage_runner_receives_the_code_version(self) -> None:
        """The id's second axis. Without it `code` resolves to "" and the submission id returns to
        its pre-B3 form — which re-attaches a rolling deploy to the PREVIOUS build's job, so the new
        pod reports success over the old build's output. `test_an_unset_code_version_reproduces_the_
        previous_id_exactly` blesses the empty value on purpose, so nothing else catches this."""
        assert "MEDALLION_RAY_CODE_VERSION" in self._render(), (
            "no rendered pod receives MEDALLION_RAY_CODE_VERSION — the deploy axis of the submission "
            "id is fed by nothing, and the unit tests bless an empty code as backwards-compatible"
        )


# TestB5NoUnhonouredKnobCanBeSmuggledIntoRemoteArgs was DELETED with its subject at the
# dissolution (2026-08-28, open_ray-kernel.md): `ratch.core.runners.runner_ray_remote_args` —
# the runtime_env channel it pinned shut — no longer exists, which closes the hazard by
# construction rather than by shape-pin.
