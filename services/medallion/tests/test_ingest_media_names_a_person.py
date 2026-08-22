"""`POST /ingest-media` could never name the person who asked for a run.

The estate's inbox targets on six sources; for work a SERVICE performs on somebody's behalf that
source is ORIGINATOR, and it needs a verified subject captured at the door. `/produce` and `/train`
both have one — a dual-auth door returning the sub on the human path. `/ingest-media` was
token-guarded only, so no subject ever existed and no run it started could reach a person.

That silence is total and reported by nothing: `notifiable()` acks an event it cannot target with a
SUCCESS, so a media chain that fails tells its requester nothing, and no metric moves. The door is
the LAST place that identity exists — by the time a silver derive fails, the request is gone and the
mover authors as a chart role literal.
"""

from __future__ import annotations

import inspect

from medallion.api import ingest_media as route
from medallion.services import media_produce


class TestTheDoorResolvesAPerson:
    def test_the_route_no_longer_uses_the_token_only_guard(self) -> None:
        source = inspect.getsource(route)
        assert "require_dapr_token" not in source, (
            "the token door authenticates a SERVICE and resolves no principal — a run started through it can never name the person who asked for it"
        )

    def test_the_route_takes_a_verified_subject(self) -> None:
        params = inspect.signature(route.ingest_media).parameters
        assert "originator" in params, "the door must hand the verified sub to the service layer"


class TestTheSubjectReachesTheEvent:
    def test_the_service_accepts_an_originator(self) -> None:
        assert "originator" in inspect.signature(media_produce.ingest_media).parameters

    def test_it_is_stamped_on_the_run_event(self) -> None:
        """`lance.originator` is what the ORIGINATOR lane reads. Carrying the sub to the service and
        dropping it before the emit would look correct and target nobody."""
        source = inspect.getsource(media_produce)
        assert "originator=originator" in source, "the sub reached the service and was never emitted"

    def test_a_service_call_carries_NO_originator_rather_than_a_fake_one(self) -> None:
        """The shared token names no person. An unattributable run must stay unattributed — a
        role literal in that field addresses an inbox actor named after the role and reaches nobody."""
        source = inspect.getsource(media_produce)
        assert "originator=settings.producer_author" not in source
        assert 'originator="' not in source, "a literal originator is not a person"


class TestTheDoorIsPinned:
    def test_it_declares_no_project_query_param(self) -> None:
        """The media head's target is CONFIGURED. A caller-supplied `?project=` would let an admin of
        any other project pass the gate while the bytes still land in the configured tenant's bronze —
        authorization scope must equal write scope."""
        from medallion.api.produce_auth import authorize_ingest_media

        assert "project" not in inspect.signature(authorize_ingest_media).parameters

    def test_it_delegates_with_the_pin_explicit(self) -> None:
        from medallion.api import produce_auth

        body = inspect.getsource(produce_auth.authorize_ingest_media)
        assert "project=None" in body, "the pin must be explicit, not inherited from a default"


class TestTheMediaChainCarriesItPastBronze:
    """Link 2. The door and the bronze emit are not the chain.

    `/produce`'s cascade threads the human the whole way down — `_cascade_originator` reads it back
    off the bronze write event, every mover trigger re-carries it, and the gold stage still names the
    person who asked. The media head publishes its OWN trigger rather than going through
    `/bronze-arrival`, and that payload carried token/dataset/namespace only: the sub reached the
    bronze event and died there, so every media derive after it authored as a role literal.

    A partly-threaded chain is the worst shape — the head looks fixed and the failure that actually
    matters (a silver derive dying hours later) is the one that reaches nobody.
    """

    def test_the_trigger_payload_carries_the_originator(self) -> None:
        source = inspect.getsource(media_produce.ingest_media)
        _, _, trigger = source.partition("topic_name=settings.media_topic")
        assert trigger, "the media trigger publish moved — this test is asserting on nothing"
        assert "originator" in trigger, "the media trigger drops the human the head resolved"

    def test_the_mover_trigger_model_accepts_it(self) -> None:
        """Threading a field the guard drops is the same silence with more code."""
        from medallion.services.trigger_guards import StageTrigger

        assert StageTrigger(originator="user-1").originator == "user-1"
