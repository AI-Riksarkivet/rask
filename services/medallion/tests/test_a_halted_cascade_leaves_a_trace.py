"""A pre-flight halt is an ACK. Three of them left no series, and one did not halt at all.

`handle_stage` refuses a trigger before it reads or writes anything for six reasons. A DROP is an
ack — Dapr neither redelivers nor dead-letters — so a refusal the app does not record is an event
that simply ceases to exist. Two of the six are counted (`malformed`, `unconfined_uri`, whose own
docstring calls them "the only DROPs that leave no trace" — it was describing a subset, not a fact),
one is counted as another lane's routine dispatch (`record_other_lane`), and three had a log line and
nothing else:

  * an unsafe `project` — a tenant id shaped like a path traversal, which is the same
    someone-is-publishing-what-they-should-not signal `unconfined_uri` exists to raise;
  * a tenant trigger arriving with registry resolution switched off — a DEPLOYMENT gap that
    permanently halts every tenant cascade on this mover while nothing is red;
  * a lane whose identity cannot be resolved — an operator's declaration mistake, likewise permanent.

WHY A COUNTER AND NOT A LINEAGE EVENT. Ruled 2026-08-16 (`docs/DECISIONS.md`, "Lineage records what
happened to DATA; an authorization denial is not a data event"), against a proposal to emit an
OpenLineage FAIL from exactly these branches: nothing is read and nothing is written, so a FAIL would
mint provenance for a run that never ran, and a permanently misconfigured mover would emit one on
every trigger forever. "A repeating operational condition is a METRIC, not an event." This is that
metric, with the closed reason vocabulary the counter already requires.

AND ONE OF THE SIX WAS NOT A DROP AT ALL. `resolve_transform_async` — the first thing the handler
calls after the shape guard — raises `UndeclaredTransformError` for a mover that names a transform
the catalog has no declaration for, or that cannot be looked up at all (no control root). That call
sits outside every `try`, so the exception escapes into the subscription route: precisely the
"a raising handler poisons the subscription" failure DATA-CONTRACT §7.3 and this module's own
docstring forbid. The `except (UndeclaredTransformError, ValueError)` further down looks like the
guard for it and is not — `resolve_stage_identity` cannot raise that type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import transform


class _Dapr:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def refusals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every `(transition, reason)` the handler counted."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(transform, "record_refused", lambda transition, reason: seen.append((transition, reason)))
    return seen


def _settings(tmp_path: Path, **over: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "MEDALLION_FROM_NAMESPACE": "bronze",
        "MEDALLION_FROM_DATASET": "bronze$events",
        "MEDALLION_TO_NAMESPACE": "silver",
        "MEDALLION_TO_DATASET": "silver$features",
        "MEDALLION_PUB_TOPIC": "medallion.silver",
        "MEDALLION_FROM_URI": str(tmp_path / "bronze.lance"),
        "MEDALLION_TO_URI": str(tmp_path / "silver.lance"),
    }
    return MedallionSettings(**{**base, **over})


@pytest.mark.asyncio
async def test_an_unsafe_project_is_counted_not_only_logged(refusals: list[tuple[str, str]], tmp_path: Path) -> None:
    """A tenant id that would become an S3 prefix and a lineage name. Refusing is right; silence is not."""
    status = await transform.handle_stage(cast(Any, _Dapr()), _settings(tmp_path), {"data": {"token": "t", "project": "../evil"}})

    assert status == {"status": "DROP"}
    assert refusals == [("bronze->silver", "bad_project")], f"the refusal left no series to alert on: {refusals}"


@pytest.mark.asyncio
async def test_a_tenant_trigger_with_routing_OFF_is_counted(refusals: list[tuple[str, str]], tmp_path: Path) -> None:
    """Fail-closed is correct and PERMANENT: redelivery cannot configure a registry, so every tenant
    cascade on this mover stops here until an operator acts — which they cannot do unprompted."""
    status = await transform.handle_stage(cast(Any, _Dapr()), _settings(tmp_path), {"data": {"token": "t", "project": "acme"}})

    assert status == {"status": "DROP"}
    assert refusals == [("bronze->silver", "routing_disabled")], f"a permanently halted tenant lane is invisible: {refusals}"


@pytest.mark.asyncio
async def test_a_declared_lane_with_no_namespace_is_counted(refusals: list[tuple[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The record resolved, and named a table id that carries no namespace — an operator's mistake,
    deterministic, and equally invisible."""
    from service_kit.lakehouse.transform_specs import TransformSpec

    spec = TransformSpec(
        name="derive",
        project="acme",
        from_id="acme-bronze$events",
        to_id="nonamespace",
        entrypoint="/home/ray/jobs/ray_stage_job.py",
    )

    async def _declared(_settings: Any, *, project: str = "") -> TransformSpec:
        return spec

    monkeypatch.setattr(transform, "resolve_transform_async", _declared)

    settings = _settings(tmp_path, MEDALLION_TRANSFORM="derive", MEDALLION_CONTROL_ROOT=str(tmp_path / "control"))
    status = await transform.handle_stage(cast(Any, _Dapr()), settings, {"data": {"token": "t", "project": "acme"}})

    assert status == {"status": "DROP"}
    assert refusals == [("bronze->silver", "unresolvable_lane")], f"an undeclared-shaped lane halted the mover silently: {refusals}"


@pytest.mark.asyncio
async def test_an_UNDECLARED_transform_DROPS_rather_than_poisoning_the_subscription(refusals: list[tuple[str, str]], tmp_path: Path) -> None:
    """The one that was not a drop.

    A mover naming a transform it cannot resolve raises `UndeclaredTransformError` out of
    `handle_stage`. Nothing in the handler catches it, so the subscription route answers 500 and the
    broker redelivers into the identical refusal forever — a deterministic condition being retried,
    which is the failure mode the validate-or-DROP rule exists to prevent. It is also the loudest
    possible way to say nothing useful: no counter, and a stack trace per delivery.
    """
    settings = _settings(tmp_path, MEDALLION_TRANSFORM="derive")  # named, and no control root to read it from

    status = await transform.handle_stage(cast(Any, _Dapr()), settings, {"data": {"token": "t", "project": "acme"}})

    assert status == {"status": "DROP"}, f"a deterministic declaration failure must ack, not raise: {status}"
    assert refusals == [("bronze->silver", "unresolvable_lane")], f"and it must leave the same trace as its sibling: {refusals}"


@pytest.mark.asyncio
async def test_a_refused_trigger_emits_NO_lineage(refusals: list[tuple[str, str]], tmp_path: Path) -> None:
    """The other half of the 2026-08-16 ruling, pinned so a later reading of "the halt tells nobody"
    does not close it by minting provenance for a run that never touched data.

    The gap it names is real — the person whose cascade stopped is still told nothing — and its home
    is the CONTROL lane (`extra.subject` = the trigger's originator), not this one.
    """
    dapr = _Dapr()

    await transform.handle_stage(cast(Any, dapr), _settings(tmp_path), {"data": {"token": "t", "project": "../evil", "originator": "alice"}})

    assert dapr.calls == [], f"a halt that read and wrote nothing published lineage for it: {dapr.calls}"
