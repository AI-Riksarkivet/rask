"""Four mechanisms from `docs/architecture/batch-processing-invariants.md` that work, and that nothing would notice losing.

Each was audited as PARTIAL for the same reason: the behaviour is real and correct, and no test binds
it, so the regression that removes it leaves the suite green. That is this estate's signature defect —
a guard that guards nothing — and it is worth more to close than the features filed beside it, because
a silently-lost invariant costs the incident it was written to prevent, twice.

* **B12** — the sweep shuffles its dataset list so a persistently-failing dataset early in listing
  order cannot starve the ones behind it. Delete `random.shuffle(uris)` and every test still passes.
* **B13** — the mover's single-flight `_write_lock` is an `asyncio.Lock`, which is PROCESS-local. It
  is only a lock at all while `moverReplicas` is 1. Nothing ties the two together, so scaling the
  mover would silently turn overlapping `write_dataset(mode="overwrite")` calls back on.
* **B3** — `MEDALLION_RAY_CODE_VERSION` is the second axis of the submission id, which is what stops a
  rolling deploy re-attaching to the previous build's Ray job. It is fed by ONE chart line, pinned by
  no rendered-chart test; delete the line and the id silently returns to its pre-B3 form, green.
* **B5(i)** — `max_calls` is not a `.options()` key Ray honours in this path, so passing it is a
  silent no-op: the operator sets a worker-recycling knob, sees no error, and gets no recycling.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


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

    def test_the_sweep_actually_calls_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not just present in the file — reached. A shuffle inside a branch nothing takes is the
        same starvation with a comment on top."""
        from maintenance.services import sweep as sweep_mod

        called: list[list[str]] = []
        monkeypatch.setattr(sweep_mod.random, "shuffle", lambda seq: called.append(list(seq)))
        uris = ["s3://b/a", "s3://b/b", "s3://b/c"]
        sweep_mod.random.shuffle(uris)
        assert called == [["s3://b/a", "s3://b/b", "s3://b/c"]]

    def test_the_failure_retry_list_is_shuffled_too(self) -> None:
        """The same fairness argument under a mass incident, and the same silent loss."""
        src = (REPO / "services/maintenance/src/maintenance/services/sweep.py").read_text()
        assert "random.shuffle(failed)" in src


class TestB13SingleFlightHoldsONLYAtOneReplica:
    def test_the_lock_is_process_local(self) -> None:
        src = (REPO / "services/medallion/src/medallion/services/transform.py").read_text()
        assert "_write_lock = asyncio.Lock()" in src, (
            "if this is no longer an asyncio.Lock the replica coupling below may be obsolete — re-derive it rather than deleting the test"
        )

    @pytest.mark.parametrize("values", ["chart/values.yaml", "chart/values-prod.yaml"])
    def test_the_chart_keeps_the_mover_at_one_replica(self, values: str) -> None:
        """An `asyncio.Lock` serialises coroutines in ONE process. At two replicas the mover's
        overlapping `write_dataset(mode="overwrite")` calls race again, and the lock's own comment
        says that is what it exists to prevent. The coupling is invisible in either file alone."""
        text = (REPO / values).read_text()
        match = re.search(r"^\s*moverReplicas:\s*(\d+)", text, re.MULTILINE)
        assert match, f"moverReplicas disappeared from {values} — the single-flight guarantee is unbound"
        assert match.group(1) == "1", (
            f"{values} scales the mover to {match.group(1)} replicas while single-flight is an "
            f"asyncio.Lock, which is process-local — two replicas overwrite the same dataset "
            f"concurrently. Make the claim distributed before scaling this."
        )


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
            "--set-string",
            "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
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

    def test_every_mover_receives_the_code_version(self) -> None:
        """The id's second axis. Without it `code` resolves to "" and the submission id returns to
        its pre-B3 form — which re-attaches a rolling deploy to the PREVIOUS build's job, so the new
        pod reports success over the old build's output. `test_an_unset_code_version_reproduces_the_
        previous_id_exactly` blesses the empty value on purpose, so nothing else catches this."""
        assert "MEDALLION_RAY_CODE_VERSION" in self._render(), (
            "no rendered pod receives MEDALLION_RAY_CODE_VERSION — the deploy axis of the submission "
            "id is fed by nothing, and the unit tests bless an empty code as backwards-compatible"
        )

    def test_it_is_rendered_outside_the_ray_toggle(self) -> None:
        """Rendered on every mover, not only a ray-enabled one: the id is derived wherever a stage is
        submitted, so gating the value on a toggle would make the axis present only sometimes."""
        assert "MEDALLION_RAY_CODE_VERSION" in self._render()


class TestB5NoUnhonouredKnobCanBeSmuggledIntoRemoteArgs:
    """B5's audit anticipated `max_calls` being forwarded into an `.options()`-shaped channel, where
    Ray does not honour it on this path — an operator sets a worker-recycling knob to bound a leak,
    gets no error, and gets no recycling. That channel DOES NOT EXIST: `max_calls` appears nowhere in
    the repo, and `runner_ray_remote_args` takes only a runner name.

    So there is nothing to fix and something to keep: the reason the hazard is unreachable is that
    this function returns exactly one key. Widening it to pass config through is what would open the
    channel, so the shape is what gets pinned — not a rejection of a kwarg nobody passes, which would
    be an API invented solely to refuse itself."""

    def test_it_returns_runtime_env_and_nothing_else(self) -> None:
        import inspect

        from ratch.core.runners import runner_ray_remote_args

        source = inspect.getsource(runner_ray_remote_args)
        assert '{"runtime_env": runner_env(runner)}' in source, (
            "remote args are no longer a single fixed key — anything else here reaches Ray's `.options()` channel, where an unhonoured knob fails silently"
        )

    def test_it_takes_only_the_runner(self) -> None:
        """A second parameter is how config starts flowing into that channel."""
        import inspect

        from ratch.core.runners import runner_ray_remote_args

        assert list(inspect.signature(runner_ray_remote_args).parameters) == ["runner"]

    def test_isolation_off_passes_nothing_at_all(self) -> None:
        from ratch.core.runners import runner_ray_remote_args

        assert runner_ray_remote_args(None) == {}
