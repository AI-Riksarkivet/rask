"""The Ray adapter renders job metadata FROM THE ORDER, so the port loses nothing the lane stamped.

[[LH-159]] The Ray submission stamps five facts as job metadata and they are read BACK:
`rask.originator` recovers who a dead job was for, and `rask.transform` is pinned by
`test_ray_job_names_its_transform`. `RayJobsApiExecutor.submit` sent none of them — it posted only
`submission_id`, `entrypoint` and `runtime_env` — so routing the lane through the port would have
dropped the targeting facts SILENTLY, which `.claude/skills/rask-notifications` prices exactly: a run
that names nobody is undeliverable rather than under-delivered.

THAT GAP IS CLOSED BY THE ORDER BEING COMPLETE, not by widening the port's call signature. All five
facts now live on the `WorkOrder` — `identity.originator`, `identity.project`, `stamp.stage`,
`stamp.token`, `stamp.transform` — so the adapter DERIVES the metadata rather than being handed it.
Handing it to `submit()` would let each adapter render the same facts its own way, which is the
divergence `to_env()` being the one serialization exists to prevent.

EMPTY VALUES ARE OMITTED, matching the lane: a blank `rask.token` is not a token, and a metadata key
carrying `""` reads as a stamped fact that is simply absent.
"""

from __future__ import annotations

from medallion.services.rayjobs_api_executor import RayJobsApiExecutor
from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkOrder, WorkSource, WorkStamp


def _order(**stamp: str) -> WorkOrder:
    return WorkOrder(
        task="transform",
        source=WorkSource(uri="s3://lake/b", table_id="acme-bronze$events"),
        destination=WorkDestination(uri="s3://lake/s", table_id="acme-silver$events"),
        stamp=WorkStamp(stage="silver", cardinality="1:1", **stamp),
        identity=WorkIdentity(run_id="r1", project="acme", originator="alice"),
        idempotency_key="k1",
    )


def test_the_adapter_can_render_metadata() -> None:
    """RED before the fix: the adapter had no way to express what the lane stamps."""
    assert hasattr(RayJobsApiExecutor, "job_metadata"), "the adapter cannot stamp metadata, so the port drops it"


def test_every_fact_the_lane_stamps_is_derivable() -> None:
    """The five keys, from the order alone — no argument, no second source of truth."""
    meta = RayJobsApiExecutor().job_metadata(_order(token="tok", transform="browserlane"))

    assert meta["rask.originator"] == "alice", "the person a dead job was for must survive the submission"
    assert meta["rask.project"] == "acme"
    assert meta["rask.stage"] == "silver"
    assert meta["rask.token"] == "tok"
    assert meta["rask.transform"] == "browserlane"


def test_an_absent_fact_is_omitted_rather_than_blanked() -> None:
    """A key carrying `""` reads as a stamped fact that is simply absent — the lane omits, so does this.

    Same rule [[XC-066]] settled for `runtime_env`: absent and empty are different claims, and only one
    of them defers.
    """
    meta = RayJobsApiExecutor().job_metadata(_order())

    assert "rask.token" not in meta, f"a blank token was stamped as a fact: {meta}"
    assert "rask.transform" not in meta, f"a blank transform was stamped as a fact: {meta}"
