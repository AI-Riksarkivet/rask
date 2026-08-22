"""A service writing on a person's behalf could not name them, and the field to do it already existed.

`enforce_author` OVERWRITES `author` with the authenticating service's sub — "never trust the request
body" doing its job — so a catalog write made by a service for a human can never author as the human.
`lance.originator` is the field invented for exactly that, and the emitter carried it end to end:
protocol, no-op, HTTP emitter and the run-event builder all take `originator`.

Nothing could set it. `emit_write_event` is the trailer every catalog write goes through, and it had
no channel for one, so the capability was unreachable from any door. The annotator publishes a
project on a person's behalf with a service bearer; that publish reached the author's own inbox as
the SERVICE and the human's not at all.

The binding is per REQUEST and the emitter is per APP — built once in the lifespan and shared by every
concurrent request. Storing a claim on it would leak one caller's identity onto another caller's
event, which is worse than the silence it replaces: a row in the wrong person's inbox.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from catalog.core.lineage_emit import OriginatorBoundEmitter, emit_write_event


class _Recording:
    """Structural double for `LineageEmitter` — the estate's fake-by-shape pattern."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def project_for(self, top_ns: str) -> str | None:
        return None

    async def emit_create(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)

    async def emit_write(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


def _emit(bound: Any) -> None:
    asyncio.run(
        emit_write_event(
            bound,
            ["acme", "silver", "features"],
            delimiter="$",
            author="service-annotator",
            version=3,
            operation="create_table",
            authorization=None,
        )
    )


class TestTheClaimReachesTheEvent:
    def test_a_bound_originator_is_stamped(self) -> None:
        inner = _Recording()
        _emit(OriginatorBoundEmitter(inner, "alice"))
        assert inner.writes[0]["originator"] == "alice"

    def test_the_author_is_untouched(self) -> None:
        """The originator rides BESIDE the author, it does not replace it. The service really did
        perform the write, and lineage must keep saying so."""
        inner = _Recording()
        _emit(OriginatorBoundEmitter(inner, "alice"))
        assert inner.writes[0]["author"] == "service-annotator"

    def test_no_claim_leaves_the_event_byte_identical(self) -> None:
        inner = _Recording()
        _emit(OriginatorBoundEmitter(inner, None))
        assert inner.writes[0]["originator"] is None


class TestTheBindingCannotCrossRequests:
    def test_two_bindings_over_one_emitter_do_not_see_each_other(self) -> None:
        """The exact hazard that rules out stashing the claim on the app-scoped emitter."""
        inner = _Recording()
        _emit(OriginatorBoundEmitter(inner, "alice"))
        _emit(OriginatorBoundEmitter(inner, "bob"))
        _emit(OriginatorBoundEmitter(inner, None))
        assert [w["originator"] for w in inner.writes] == ["alice", "bob", None]

    def test_the_wrapper_holds_no_mutable_state(self) -> None:
        bound = OriginatorBoundEmitter(_Recording(), "alice")
        with pytest.raises((AttributeError, TypeError)):
            setattr(bound, "originator", "mallory")  # noqa: B010 - the point is that it is refused


class TestTheHeaderIsAClaimAndIsBounded:
    """It authorizes nothing — the plane re-derives every recipient's visibility at delivery — so an
    unverified header is sound. It still must not be an arbitrary string: the value becomes an inbox
    actor id."""

    @pytest.mark.parametrize("raw", ["", "   ", "a" * 200, "team:acme#member", "user:alice", "*"])
    def test_a_value_that_is_not_a_person_is_dropped(self, raw: str) -> None:
        from catalog.api.dependencies import originator_hint

        assert originator_hint(raw) is None

    def test_a_plain_subject_survives(self) -> None:
        from catalog.api.dependencies import originator_hint

        assert originator_hint(" alice ") == "alice"


class TestTheAnonymousSubjectIsNotAPerson:
    """`anon` is what every verified-subject dependency resolves to with OIDC OFF
    (`service_kit.governed.deps.ANONYMOUS_SUBJECT`). It is a real string and it passes every other
    shape check, so it would have become one shared inbox actor holding everybody's rows — the
    trap-4 shape, reached by a config switch rather than by a forged header."""

    def test_it_is_dropped(self) -> None:
        from catalog.api.dependencies import originator_hint

        assert originator_hint("anon") is None

    def test_the_literal_matches_the_estates(self) -> None:
        """Hard-coding it here would rot silently if the shared constant ever changed."""
        from catalog.core.lineage_emit import is_person_subject

        from service_kit.governed.deps import ANONYMOUS_SUBJECT

        assert not is_person_subject(ANONYMOUS_SUBJECT)
