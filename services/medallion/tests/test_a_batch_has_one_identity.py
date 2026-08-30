"""A batch is ONE object across the whole cascade — `docs/architecture/medallion-data-flow.md` change 9.

Until this landed, bronze, silver and gold were three unrelated runs in the lineage graph with
nothing joining them. The reason is precise and worth stating, because it looks like `token` should
already do this job: `publication_trigger` mints the NEXT tier's token from the publication EVENT id
(`token = str(data.get("event_id"))`), so the token changes at every tier boundary by construction.

That is CORRECT for an idempotency key — each hop is its own unit of redelivery, and a redelivered
silver trigger must collide with the previous silver trigger and not with bronze's. It is useless as
a batch identity, which is why both fields exist rather than one being made to serve twice.

**The boundary is the catalog.** A mover cannot hand the next mover anything directly — the tag move
is what wakes the next tier — so the identity rides `publish` -> `table_published` -> the next
trigger. Each hop of that path is asserted below, because a break in any one of them is invisible:
the cascade still runs, the graph still fills, and only the join silently returns nothing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.schemas.events import build_run_event
from medallion.services.publication_trigger import handle_publication
from medallion.services.trigger_guards import StageTrigger, parse_stage_trigger


def _parsed(data: dict[str, Any]) -> StageTrigger:
    """`parse_stage_trigger` returns `None` for an unparseable body; these bodies are all valid."""
    trigger = parse_stage_trigger({"data": data})
    assert trigger is not None, f"the guard refused a valid trigger: {data}"
    return trigger


class _Dapr:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


class TestTheIdentityIsMintedOnce:
    """At the head, from the ingest token — not fresh, and not per hop."""

    def test_a_redelivered_head_mints_the_SAME_batch(self) -> None:
        """A fresh uuid here would fork one batch into two on any redelivery.

        Dapr delivers at least once, so the head runs twice as a matter of routine. Seeding the batch
        id from the bronze-write token makes the second delivery name the batch the first one named.
        """
        from medallion.services.ingest_trigger import _cascade_token

        event = {"run": {"facets": {"lance": {"token": "ingest-42"}}}}
        assert _cascade_token(event) == _cascade_token(event) == "ingest-42"


class TestTheIdentitySurvivesTheTierBoundary:
    """The catalog is the only hop, so each leg of it is asserted separately."""

    def test_the_trigger_model_carries_it(self) -> None:
        trigger = _parsed({"token": "hop-2", "cascade_id": "ingest-42"})
        assert trigger.token == "hop-2"
        assert trigger.cascade_id == "ingest-42", "the batch id did not survive parsing"

    def test_token_and_batch_are_ALLOWED_to_differ(self) -> None:
        """The case the whole design turns on, asserted so nobody 'simplifies' one away.

        At silver the token is the publication event's id and the batch id is still the ingest's.
        A future refactor that made `cascade_id` default to `token` would look harmless and would
        silently restore the defect: every tier would again carry a different batch.
        """
        trigger = _parsed({"token": "evt-99", "cascade_id": "ingest-42"})
        assert trigger.token != trigger.cascade_id

    def test_an_absent_batch_id_is_None_not_empty(self) -> None:
        """A run driven by a producer that does not set it must be tellable from one named ``""``.

        Same rule the tenant follows: omitted means "no claim", and a blank string is a claim about
        a batch called nothing.
        """
        assert _parsed({"token": "t"}).cascade_id is None

    @pytest.mark.asyncio
    async def test_the_publication_head_propagates_it_onto_the_next_trigger(self) -> None:
        """THE hop that used to lose it — driven through the real head, not a hand-built dict.

        `table_published` carries the batch in `extra`; the head must copy it onto the trigger it
        publishes for the next tier. Asserted end-to-end through `handle_publication` so a rename on
        either side of the wire fails here rather than in a cluster.
        """
        dapr = _Dapr()
        settings = MedallionSettings(transform_routes={"silver": "medallion.silver"})
        await handle_publication(
            dapr,
            settings,
            {
                "data": {
                    "action": "table_published",
                    "object_id": "table:acme-silver$features",
                    "event_id": "evt-99",
                    "extra": {"project": "acme", "cascade_id": "ingest-42", "from_version": 3, "to_version": 7, "location": "s3://b/t"},
                }
            },
        )
        assert dapr.published, "the head published nothing for a routed lane"
        trigger = json.loads(dapr.published[0]["data"])
        assert trigger["cascade_id"] == "ingest-42", "the batch identity was lost at the tier boundary"
        # And the per-hop token is the EVENT's, which is the whole reason a second field exists.
        assert trigger["token"] == "evt-99"

    @pytest.mark.asyncio
    async def test_a_publication_with_NO_batch_id_omits_the_key(self) -> None:
        """A producer that sets none must not have one invented — `""` is a batch named nothing."""
        dapr = _Dapr()
        await handle_publication(
            dapr,
            MedallionSettings(transform_routes={"silver": "medallion.silver"}),
            {
                "data": {
                    "action": "table_published",
                    "object_id": "table:acme-silver$features",
                    "event_id": "evt-1",
                    "extra": {"project": "acme", "from_version": 1, "to_version": 2, "location": "s3://b/t"},
                }
            },
        )
        assert dapr.published
        assert "cascade_id" not in json.loads(dapr.published[0]["data"])


class TestTheGraphCanBeJoinedOnIt:
    """Queryable is the deliverable. If it is not on the run event, none of the above matters."""

    def _facet(self, **over: Any) -> dict[str, Any]:
        event = build_run_event(
            operation="embed",
            author="data_eng",
            job_namespace="medallion",
            inputs=[("bronze", "bronze$events")],
            output_namespace="silver",
            output_name="silver$features",
            **over,
        )
        return event["run"]["facets"]["lance"]

    def test_the_run_event_carries_the_batch(self) -> None:
        facet = self._facet(token="evt-99", cascade_id="ingest-42")
        assert facet["cascade_id"] == "ingest-42"
        assert facet["token"] == "evt-99", "the per-hop token must still be there — they answer different questions"

    def test_every_hop_of_one_batch_shares_the_id(self) -> None:
        """The actual query someone runs: 'show me every run of this ingest'."""
        hops = [self._facet(token=f"evt-{i}", cascade_id="ingest-42") for i in range(3)]
        assert {h["cascade_id"] for h in hops} == {"ingest-42"}
        assert len({h["token"] for h in hops}) == 3, "the per-hop tokens must stay distinct"

    def test_it_is_OMITTED_when_absent(self) -> None:
        """A run that predates the field, or a producer that sets none, carries no batch key at all."""
        assert "cascade_id" not in self._facet(token="evt-99")

    @pytest.mark.parametrize("event_type", ["COMPLETE", "FAIL"])
    def test_a_FAILED_hop_still_names_its_batch(self, event_type: str) -> None:
        """The batch someone most needs to find is the one that broke.

        A batch id present only on success would answer "which batches finished" and nothing about
        the one the person is actually asking after.
        """
        facet = self._facet(token="evt-99", cascade_id="ingest-42", event_type=event_type)
        assert facet["cascade_id"] == "ingest-42"
