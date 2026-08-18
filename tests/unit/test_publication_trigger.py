"""B8: the cascade fires on the PUBLICATION, and the trigger carries the delta.

Two defects sit behind "the cascade moves no data", and they are independent:

* it woke on a lineage WRITE — a signal that says bytes landed and nothing about whether the quality
  gate accepted them, so the cascade could move data `published` had refused;
* the trigger named a TABLE, not a range, so a consumer had to rescan or keep its own bookmark, and a
  table's second arrival woke nothing useful.

And a third, quieter one: without `project` on the trigger the mover cannot resolve its tier roots,
falls back to the empty `MEDALLION_FROM_URI`/`MEDALLION_TO_URI`, and SKIPS its compute path — the
cascade "runs" and moves nothing at all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from medallion.core.config import MedallionSettings
from medallion.services.publication_trigger import handle_publication


def _Settings() -> MedallionSettings:  # noqa: N802 — kept call-compatible with the stub it replaces
    """The REAL settings, not a hand-rolled stub — that stub is why the lane-name bug shipped.

    It carried exactly three attributes (`pubsub`, `bronze_topic`, `publish_timeout_seconds`), so the
    head could not read `bronze_namespace` even in principle, and the tests below happily asserted the
    CATALOG identifier was the lane name. A stub that cannot express the thing under test will always
    agree with whatever the code does.
    """
    return MedallionSettings()


class _Dapr:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self._fail = fail

    async def publish_event(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("broker unreachable")
        self.published.append(json.loads(kwargs["data"]))


def _event(action: str = "table_published", object_id: str = "table:lane$pages", **extra: Any) -> dict[str, Any]:
    return {"data": {"action": action, "object_id": object_id, "event_id": "evt-1", "extra": extra}}


def _with_actor(event: dict[str, Any], actor: str | None) -> dict[str, Any]:
    """Put `actor` where the real envelope carries it — TOP level of `data`, not inside `extra`.

    `CatalogControlEvent` declares `actor` as its own field (`service_kit/control_events.py`), and the
    catalog fills it with the verified `user:<sub>`. Nesting it under `extra` in a test would let a
    handler that reads the wrong place pass.
    """
    event["data"]["actor"] = actor
    return event


@pytest.mark.asyncio
async def test_a_publication_triggers_the_cascade_WITH_the_range() -> None:
    """The whole of D-R3 on the wire: the trigger names which rows are new, not merely that some are."""
    dapr = _Dapr()

    result = await handle_publication(dapr, _Settings(), _event(from_version=3, to_version=4))

    assert result == {"status": "SUCCESS"}
    assert len(dapr.published) == 1
    trigger = dapr.published[0]
    assert (trigger["from_version"], trigger["to_version"]) == (3, 4)
    # The LANE, not the catalog identifier. `table:lane$pages` is tenant `lane`, table `pages`; the
    # medallion lane for it is `bronze$pages`, the same string every tenant's publication produces.
    assert trigger["dataset"] == "bronze$pages"
    assert trigger["namespace"] == "bronze"


@pytest.mark.asyncio
async def test_the_trigger_carries_the_PROJECT_or_the_mover_moves_nothing() -> None:
    """The quiet half of B8.

    `handle_stage` resolves its tier roots only when the trigger carries a project; otherwise it uses
    `MEDALLION_FROM_URI`/`MEDALLION_TO_URI`, which default to empty, and its `if compute_enabled and
    from_uri and to_uri` guard silently skips the compute path. A cascade that runs and moves nothing
    looks identical to one that had nothing to move.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(from_version=None, to_version=1))

    assert dapr.published[0]["project"] == "lane"


@pytest.mark.asyncio
async def test_a_FIRST_publication_carries_a_null_from_rather_than_zero() -> None:
    """ "No prior publication" and "published from version 0" are different claims.

    Coercing None to 0 would make a first publication indistinguishable from one that followed a
    version nobody published, and the consumer's filter (`> from`) would silently change meaning.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(from_version=None, to_version=2))

    assert dapr.published[0]["from_version"] is None
    assert dapr.published[0]["to_version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _event(action="grant_added"),
        _event(action="table_created"),
        _event(action="warehouse_created"),
    ],
)
async def test_a_GOVERNANCE_notice_drives_nothing(event: dict[str, Any]) -> None:
    """The control topic carries every catalog mutation. Only a publication means data is READY —
    firing the cascade on a grant or a table create is the whole-table-granularity mistake again."""
    dapr = _Dapr()

    result = await handle_publication(dapr, _Settings(), event)

    assert result == {"status": "SUCCESS"}, "an event we ignore must be ACKED, not redelivered forever"
    assert dapr.published == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [{}, {"data": "not-a-dict"}, _event(object_id="warehouse:acme"), _event(object_id="table:nodelimiter")])
async def test_an_unparseable_event_is_ACKED_not_retried(event: dict[str, Any]) -> None:
    """A head that RETRYs on events it can never handle turns one malformed message into a permanent
    hot loop against the broker."""
    dapr = _Dapr()

    assert await handle_publication(dapr, _Settings(), event) == {"status": "SUCCESS"}
    assert dapr.published == []


@pytest.mark.asyncio
async def test_a_publish_OUTAGE_retries() -> None:
    """The one failure a redelivery can actually fix — and the one thing that must not be acked away,
    because the publication really did happen and the cascade really has not been told."""
    dapr = _Dapr(fail=True)

    assert await handle_publication(dapr, _Settings(), _event(from_version=1, to_version=2)) == {"status": "RETRY"}


@pytest.mark.asyncio
async def test_the_trigger_carries_the_CATALOG_VENDED_location() -> None:
    """I2 from the consuming end, and the reason the cascade moved nothing.

    The catalog vends a table at `s3://<warehouse>/<hash>_<ns>$<name>`; the mover composed
    `{project_root}/medallion/{namespace}` and read a path no catalog-written table has ever occupied.
    So the cascade fired correctly, woke the mover, and found an empty location — for every
    ingest-written table, silently.

    The fix is not for the mover to guess better. The catalog already HAS the location, so it puts it
    on the event and the trigger carries it; nothing downstream composes anything.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(from_version=1, to_version=2, location="s3://lane-wh/abc123_lane$pages"))

    assert dapr.published[0]["from_uri"] == "s3://lane-wh/abc123_lane$pages"


# ── the two naming systems, kept apart ────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_lane_name_carries_NO_tenant() -> None:
    """The bug this file previously asserted as correct.

    The catalog names a table `<tenant>$<table>`; the medallion names a lane `<tier>$<lane>`, and
    `transform.py:109` compares the arrived name against the RAW `settings.from_dataset`, documenting
    that "the trigger carries the unqualified name for every tenant". Publishing the catalog
    identifier meant `acme$events` was compared against `bronze$events`, so EVERY tenant's publication
    was dropped as another lane's — and the tenant leaked into a field that must be tenant-free.

    It appeared to work exactly once, in a cluster test whose tenant was NAMED `bronze` and whose
    table was named `events`: the two systems' strings collided and nothing was translated at all.
    Hence a tenant here that could never collide.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(object_id="table:acme$events", from_version=1, to_version=2))

    trigger = dapr.published[0]
    assert "acme" not in trigger["dataset"], f"the tenant leaked into the lane name: {trigger['dataset']}"
    assert trigger["dataset"] == "bronze$events"
    assert trigger["project"] == "acme", "the tenant must still travel — separately, in `project`"


@pytest.mark.asyncio
async def test_the_lane_name_is_EXACTLY_what_a_mover_compares_against() -> None:
    """Couples the two sides in one assertion instead of restating a literal on each.

    A mover's discriminator is `arrived != settings.from_dataset`, and a deployment sets that from
    `bronze_dataset`. Asserting the produced name equals that setting means a change to either side
    fails here rather than in a cluster, silently, as a DROP nobody sees.
    """
    dapr = _Dapr()
    settings = _Settings()

    await handle_publication(dapr, settings, _event(object_id="table:acme$events", from_version=1, to_version=2))

    assert dapr.published[0]["dataset"] == settings.bronze_dataset


@pytest.mark.asyncio
async def test_a_DIFFERENT_table_is_a_DIFFERENT_lane() -> None:
    """Lanes are per-table, not one global lane — the page lane and the events lane are distinct
    movers subscribed to the same topic, and each must see only its own."""
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(object_id="table:acme$pages", from_version=1, to_version=2))

    assert dapr.published[0]["dataset"] == "bronze$pages"


@pytest.mark.asyncio
async def test_a_NESTED_namespace_still_names_the_right_lane() -> None:
    """The lane is the table's OWN name, however deep its namespace nests.

    Catalog namespaces nest — `namespace#parent: [warehouse, namespace]` in the FGA model, and the
    create door takes a nested id up to MAX_NAMESPACE_DEPTH (8) — so `acme$bronze$pages` is the table
    `pages` inside the namespace `acme$bronze`. The head used `partition`, which takes the FIRST
    delimiter, so the table read as `bronze$pages` and the published lane became `bronze$bronze$pages`.

    That is not a cosmetic mis-name. `transform.py` compares the arrived name against
    `settings.from_dataset` and DROPs anything else, so the publication vanishes and the run reports
    nothing at all — the failure mode nobody goes looking for. Every live table measured flat on
    2026-08-16, which is the only reason this had not fired.
    """
    dapr = _Dapr()
    settings = _Settings()

    await handle_publication(dapr, settings, _event(object_id="table:acme$bronze$pages", from_version=1, to_version=2))

    assert dapr.published[0]["dataset"] == "bronze$pages"
    assert dapr.published[0]["dataset"] != "bronze$bronze$pages"


@pytest.mark.asyncio
async def test_a_NESTED_namespace_still_names_the_top_level_PROJECT() -> None:
    """The project is the TOP of the hierarchy, not whichever segment happens to sit next to the table.

    `project > warehouse > namespace > table`, and the mover needs the project to resolve its tier
    roots — without it it computes nothing. For `acme$bronze$pages` that is `acme`; the middle
    segments are tenancy detail this head deliberately discards rather than guesses at.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(object_id="table:acme$bronze$pages", from_version=1, to_version=2))

    assert dapr.published[0]["project"] == "acme"


@pytest.mark.asyncio
async def test_the_FLAT_case_is_byte_identical_to_before() -> None:
    """The depth fix must not move the case that already worked — every live table is this shape."""
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(object_id="table:acme$events", from_version=1, to_version=2))

    assert dapr.published[0]["dataset"] == "bronze$events"
    assert dapr.published[0]["project"] == "acme"


@pytest.mark.asyncio
async def test_the_trigger_carries_the_PUBLISHER_so_the_cascade_can_name_them() -> None:
    """The person who clicked publish is on the wire, and this head threw them away.

    The catalog stamps `actor=f"user:{token.sub}"` on every `table_published` control event
    (`publication.py:83`) — a VERIFIED sub, the last place that identity exists. By the time a silver
    or gold stage fails, the request is long gone and the mover authors as a chart role literal
    (`data_eng`), which addresses an inbox actor named `data_eng` and reaches nobody.

    So every mover run this head starts — the bronze→silver COMPLETE, the gold promotion, and every
    FAIL including a quality hold — was undeliverable by construction. The sibling head
    `ingest_trigger.py:136-138` carries exactly this; the copy was simply missing here.
    """
    dapr = _Dapr()

    result = await handle_publication(dapr, _Settings(), _with_actor(_event(), "user:CiQwOGE4Njg0Yi1kYjg4"))

    assert result == {"status": "SUCCESS"}
    trigger = dapr.published[0]
    assert trigger["originator"] == "CiQwOGE4Njg0Yi1kYjg4", "the bare sub, not the `user:` prefixed principal"


@pytest.mark.asyncio
async def test_a_SERVICE_publication_carries_no_originator_rather_than_a_fake_one() -> None:
    """`actor` is None on a service/unauthenticated mutation. Omitted, never sent blank: a
    present-but-empty originator is not an address, and `transform.py` treats present-and-wrong far
    more harshly than absent."""
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event())

    assert "originator" not in dapr.published[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["user:*", "user:", "*", "team:acme#member", "service:catalog"])
async def test_a_NON_PERSONAL_principal_is_DROPPED_not_carried(actor: str) -> None:
    """Trap 4: a wildcard, a bare prefix, a userset or a service is not an address. Carrying one
    writes into an inbox actor literally named `*` — worse than silence, because it looks delivered."""
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _with_actor(_event(), actor))

    assert "originator" not in dapr.published[0], f"{actor!r} was carried as if it named a person"
