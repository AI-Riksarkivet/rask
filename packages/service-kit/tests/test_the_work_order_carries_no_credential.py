"""A `WorkOrder` says WHAT must happen, in no engine's vocabulary, and carries no secret.

docs/DECISIONS.md "The compute plane is decoupled" (§2.3), step 1 of the owner-ordered §7.4. It lifts the dict
`ray_submit.py` already builds — that dict IS the executor contract; only its transport and the
program's name were ever Ray-shaped.

TWO RULES THE SHAPE ENFORCES RATHER THAN DOCUMENTS:

* **`credential_ref` NAMES, never carries.** `ray_submit.py` already refuses to put `S3_SECRET` or
  `S3_KEY` in the body, because the Jobs API echoes `runtime_env` on an unauthenticated dashboard, and
  the estate spent three commits putting the Ray plane on a scoped credential the control plane cannot
  reach. A `WorkOrder` carrying `storage_options` would undo that by signature — so the model is
  `extra="forbid"` and offers no field that could hold one.
* **`to_env()` is the ONE serialization.** Ray's `runtime_env.env_vars` merge-over-process-env semantics
  are the ADAPTER's knowledge. An adapter that hand-rolls the mapping is how two submitters come to
  disagree about what a work order means.

FROZEN, because a work order crosses a submit boundary and is read again by a poller: a mutated copy
would make the submitter and the watcher disagree about the same run.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from service_kit.lakehouse.work_order import (
    WorkDestination,
    WorkIdentity,
    WorkOrder,
    WorkSource,
    WorkStamp,
)


def _order(**over: object) -> WorkOrder:
    base: dict[str, object] = {
        "task": "stage-transform",
        "source": WorkSource(uri="s3://b/bronze", table_id="acme-bronze$events", version_floor=4),
        "destination": WorkDestination(uri="s3://b/silver", table_id="acme-silver$features"),
        "stamp": WorkStamp(stage="silver", cardinality="1:1"),
        "identity": WorkIdentity(run_id="r-1", project="acme"),
        "idempotency_key": "k-1",
    }
    base.update(over)
    return WorkOrder.model_validate(base)


def test_it_names_no_engine() -> None:
    """The point of the contract. No field may mention ray, spark, a dashboard or a program path."""
    blob = _order().model_dump_json().lower()
    for engine_noun in ("ray", "spark", "flink", "runtime_env", "dashboard", "/home/"):
        assert engine_noun not in blob, f"the work order carries the engine noun {engine_noun!r}"


def test_a_credential_VALUE_cannot_be_attached() -> None:
    """`extra="forbid"` is the guard: there is no field for a secret, and inventing one is refused."""
    with pytest.raises(ValidationError):
        _order(storage_options={"aws_secret_access_key": "s3cr3t"})


def test_credential_ref_is_a_NAME() -> None:
    order = _order(credential_ref="maintenance-scoped")
    assert order.credential_ref == "maintenance-scoped"
    assert "s3cr3t" not in order.model_dump_json()


def test_it_is_FROZEN() -> None:
    """A submitter and a poller read the same order. A mutated copy makes them disagree about one run.

    `setattr` rather than a plain assignment, and not to dodge anything: `ty` reports the direct form as
    `invalid-assignment` — "Property `task` defined in `WorkOrder` is read-only" — which is the STRONGER
    guard, caught before the code runs. This asserts the runtime half that a type checker cannot: that
    pydantic refuses it in a build where nobody ran `ty`.
    """
    order = _order()
    # A VARIABLE attribute name, because ruff and ty disagree about how to write this and both are
    # right: ruff's B010 rewrites `setattr(x, "task", ...)` to a direct assignment, and ty then reports
    # that assignment as `invalid-assignment` on a read-only property. A dynamic name satisfies both —
    # B010 fires only on a CONSTANT attribute, and a type checker cannot resolve this statically, which
    # is precisely the point: the runtime refusal is what this asserts.
    attribute = "task"
    with pytest.raises(ValidationError):
        setattr(order, attribute, "something-else")


def test_to_env_is_the_one_serialization_and_leaks_no_secret() -> None:
    env = _order(credential_ref="maintenance-scoped", params={"batch_size": "64"}).to_env()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()), env
    joined = " ".join(f"{k}={v}" for k, v in env.items())
    for forbidden in ("S3_SECRET", "aws_secret_access_key", "SECRET_ACCESS_KEY"):
        assert forbidden not in joined, f"to_env() emitted {forbidden}"


def test_a_full_scan_is_None_not_zero() -> None:
    """`version_floor=None` means read everything; 0 asserts a prior version that may not exist — the
    same distinction `build_stage_trigger` already enforces on the wire."""
    assert _order(source=WorkSource(uri="s3://b/x", table_id="ns$t")).source.version_floor is None
