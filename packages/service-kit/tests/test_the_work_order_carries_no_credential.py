"""A `WorkOrder` says WHAT must happen, in no engine's vocabulary, and carries no secret.

docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md "The compute plane is decoupled", step 1 of the owner-ordered §7.4. It lifts the dict
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


def test_to_env_is_the_one_serialization_and_leaks_no_secret() -> None:
    env = _order(credential_ref="maintenance-scoped", params={"batch_size": "64"}).to_env()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()), env
    joined = " ".join(f"{k}={v}" for k, v in env.items())
    for forbidden in ("S3_SECRET", "aws_secret_access_key", "SECRET_ACCESS_KEY"):
        assert forbidden not in joined, f"to_env() emitted {forbidden}"
