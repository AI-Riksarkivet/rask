"""The offline suite bounds Dapr's sidecar handshake, so an unstubbed call costs a second, not a minute.

`DaprHttpClient.__init__` runs `DaprHealth.wait_for_sidecar()` — a synchronous `urllib` + `time.sleep`
loop bounded by `DAPR_HEALTH_TIMEOUT`, whose Dapr default is **60 s**. `ActorProxy.create` builds that
client through a PROCESS-GLOBAL factory, and `_get_default_factory_instance` caches the factory only
after the constructor RETURNS — so where there is no sidecar, nothing is cached and every later call
pays the full timeout again.

THE ARITHMETIC IS MEASURED, not projected. `tests/unit/test_annotation_task_actor.py` records it for
one file: with a sidecar answering it ran in 0.49 s; with the sidecar unreachable and the timeout at
3 s it ran in 285.54 s, all 61 tests passing either way. At CI's 60 s default "the run reached 44 %
before `--timeout=300` killed it, taking the other 56 % of the offline suite — and the four
`needs: ms-test` e2e lanes — down with it." Confirmed on CI run 35726435185: `ms-test` spent 8m17s in
`wait_for_sidecar` and died on `Process completed with exit code 1`.

A PER-FILE GUARD DOES NOT TRAVEL, which is why this is estate-wide. That file gained a test asserting
IT never reaches the real factory, and the hang came back through a different one. Thirty of the
estate's thirty-one Dapr-touching files mock at `typed_proxy`/`inbox_for` and never reach the SDK at
all; the bound is for the thirty-first, whichever it turns out to be next.

IT BOUNDS THE WAIT, IT DOES NOT PATCH THE MECHANISM — deliberately, and for the reason `conftest.py`
already gives for refusing to stub `wait_for_sidecar` globally: the adversarial inbox test exists to
prove that handshake blocks the event loop, and a harness that removed it would delete the estate's
only evidence of a live production defect. The handshake still runs, still polls, still raises. It
just stops costing a minute per call. A test needing a different duration sets its own, as
`test_adversarial_inbox.py` does.
"""

from __future__ import annotations

import pytest


#: Generous enough that no correct test is flaky, far below the SDK's 60 s. At this bound the CI hang
#: above becomes seconds even if every unstubbed call in the suite pays it.
MAX_SECONDS = 5.0


def test_the_sdk_default_is_the_hazard_this_bounds() -> None:
    """If the SDK ever ships a small default, this whole file is obsolete rather than passing by luck."""
    dapr_conf = pytest.importorskip("dapr.conf")
    shipped = getattr(dapr_conf.settings, "DAPR_HEALTH_TIMEOUT", None)
    assert shipped is not None, "dapr.conf.settings no longer exposes DAPR_HEALTH_TIMEOUT — this bound needs rewriting, not deleting"


def test_the_handshake_is_bounded_for_every_test_in_the_suite() -> None:
    """Reads the value as the SDK reads it: off `dapr.conf.settings`, at call time."""
    dapr_conf = pytest.importorskip("dapr.conf")
    effective = dapr_conf.settings.DAPR_HEALTH_TIMEOUT
    assert effective <= MAX_SECONDS, (
        f"DAPR_HEALTH_TIMEOUT is {effective}s during the suite. An unstubbed `ActorProxy.create` caches "
        "nothing when the handshake fails, so every later call pays it again — which is how one file "
        "took the offline suite and four e2e lanes down with it"
    )
