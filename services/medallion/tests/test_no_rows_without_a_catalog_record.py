"""S5's defect is unreachable, and this pins the property that makes it so.

the retired plan `open_medallion_workflow.md` (its rulings now live in `docs/architecture/medallion-cascade.md`) §7 filed S5 as "compensation — the saga for a promotion that lands rows
then fails to register — today that leaves gold rows with no catalog record."

That state is no longer reachable, and NOT because a saga was built. It was closed by REORDERING: the
mover now asks the catalog where the table lives BEFORE writing (rule I2, applied to the write side),
and asking creates the table, which registers it. Registration therefore strictly precedes the first
row. There is no window in which rows exist unregistered, so there is nothing for a compensating
transaction to undo.

WHY A SAGA IS THE WRONG TOOL HERE, stated so it is not re-litigated. Compensation exists to unwind a
partial effect that cannot simply be repeated. Both steps here are idempotent — `ensure_stage_output`
is create-if-missing and the compute write is `mode="overwrite"` — so the answer to a failure between
them is RETRY, not rollback. Dapr's own activity guidance makes the same point from the other side
(DWF-ACT-002): the runtime retries activities, and an operation that is safe to repeat needs no
compensation, while one that is not needs an idempotency key rather than a saga.

The residual states are both benign and self-healing:
  * created-then-failed-write leaves an EMPTY registered table; the retry overwrites it, and no
    consumer sees it because the cascade advances on the published tag, not on the table existing.
  * written-then-failed-publish leaves rows the tag has not blessed — which is the DESIGNED boundary
    (`cascadeViaPublish`), not a leak.

So this file pins the ORDER, because the order is the entire fix. A refactor that moved the ask back
after the write would reopen S5 in full while every other test stayed green.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from medallion.core.config import MedallionSettings
from medallion.services import inprocess_executor, transform


class _Dapr:
    async def publish_event(self, **kwargs: Any) -> None:
        return None


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), str(tmp_path / "bronze.lance"))
    return tmp_path


def _settings(tmp_path: Path, **over: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "MEDALLION_FROM_NAMESPACE": "bronze",
        "MEDALLION_FROM_DATASET": "bronze$events",
        "MEDALLION_TO_NAMESPACE": "silver",
        "MEDALLION_TO_DATASET": "silver$features",
        "MEDALLION_PUB_TOPIC": "medallion.silver",
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_CATALOG_URL": "http://catalog.test",
        "MEDALLION_FROM_URI": str(tmp_path / "bronze.lance"),
        "MEDALLION_TO_URI": str(tmp_path / "composed.lance"),
    }
    return MedallionSettings(**{**base, **over})


def _event() -> dict[str, Any]:
    return {"data": {"token": "tok", "dataset": "bronze$events", "namespace": "bronze"}}


@pytest.fixture
def order(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> list[str]:
    """Record the sequence of the two steps that decide whether S5 is reachable."""
    seen: list[str] = []
    real_write = inprocess_executor.transform_stage

    def _ask(**k: Any) -> str:
        seen.append("register")
        return str(upstream / "vended.lance")

    def _write(from_uri: str, to_uri: str, *a: Any, **k: Any) -> Any:
        seen.append("write")
        return real_write(from_uri, str(upstream / "actual.lance"), *a, **k)

    monkeypatch.setattr(transform.catalog_register, "ensure_stage_output", _ask)
    monkeypatch.setattr(inprocess_executor, "transform_stage", _write)
    return seen


class TestRegistrationPrecedesTheFirstRow:
    def test_the_catalog_is_asked_before_anything_is_written(self, order: list[str], upstream: Path) -> None:
        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream), _event()))

        assert order == ["register", "write"], (
            f"got {order} — a write that precedes registration is exactly the S5 window: rows on disk "
            f"that the catalog has no record of, and nothing to roll them back"
        )

    def test_a_failed_write_leaves_no_unregistered_rows(self, monkeypatch: pytest.MonkeyPatch, upstream: Path, order: list[str]) -> None:
        """The failure S5 was written about. Because the ask already happened, the worst state is an
        empty REGISTERED table — governed, and overwritten by the retry."""

        def _boom(*a: Any, **k: Any) -> Any:
            order.append("write")
            raise RuntimeError("compute died mid-write")

        monkeypatch.setattr(inprocess_executor, "transform_stage", _boom)

        result = asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream), _event()))

        assert order[0] == "register", "the table was not registered before the write was attempted"
        assert result["status"] == "RETRY", f"a mid-write failure must be retried, not dropped: {result}"


class TestTheUngovernedShapeIsUnchanged:
    def test_no_catalog_url_still_writes_to_its_configured_uri(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        """A dev stack with no catalog has no registration to precede anything. It must keep working
        rather than acquiring a hard dependency as a side effect of closing S5."""
        wrote: list[str] = []
        real_write = inprocess_executor.transform_stage

        def _write(from_uri: str, to_uri: str, *a: Any, **k: Any) -> Any:
            wrote.append(to_uri)
            return real_write(from_uri, str(upstream / "actual.lance"), *a, **k)

        monkeypatch.setattr(inprocess_executor, "transform_stage", _write)

        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _settings(upstream, MEDALLION_CATALOG_URL=""), _event()))

        assert wrote == [str(upstream / "composed.lance")]
