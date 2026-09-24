"""The catalog signs its lineage as itself, DECLARING the person it is acting for.

[[LH-064]]. The catalog is the third and last producer, and the only one whose events are authored by
a HUMAN: all eight emit sites pass `author=token.sub`, the signed-in subject, and the builder stamps
it as both `author.name` and `author.sub`. A service cannot sign as that person — it holds no
credential of theirs — so the first binding (signer == author) had nothing to offer here.

WHAT IT SIGNS IS AN ATTESTATION, not a proof the person acted. The catalog authenticated the bearer
at its own door; the signature says so, under the service's own key, with the subject named inside
what the HMAC covers. A reader of the graph can then answer "transmitted by the catalog, on behalf of
this person", which is strictly more than today, where only the person is recorded and nothing attests
to it. The person's own non-repudiation is not on offer and is not claimed.

AN UNAUTHORED EVENT IS LEFT UNSIGNED, and that is not laziness. `verify_signed_event` refuses an event
with no author — admitting one would make the binding optional and a forger would simply omit the
facet — so attaching a signature to an unauthored event would take that event OFF the graph. The
catalog emits some of those (a static metadata change carries no run author), and unsigned is exactly
what the door still admits.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from lineage_kit.signing import signature_of, verify_signed_event


KEY = "Nx8cV2bM4qW6eR9tY1uI3oP5aS7dF0gH2jK4lZ6x"
PEER = "Bv5nM7qA9sD1fG3hJ5kL7zX2cV4bN6mQ8wE0rT2y"
CATALOG = "service-catalog"
PERSON = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"


def _event(author: str | None) -> dict[str, Any]:
    facets: dict[str, Any] = {}
    if author is not None:
        facets["author"] = {"_producer": "https://rask/catalog", "name": author, "sub": author}
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-24T09:30:00Z",
        "run": {"runId": "0198e0f2-1b2c-7a3d-8e4f-aaaabbbbcccc", "facets": facets},
        "job": {"namespace": "lance", "name": "catalog.drop_table"},
        "outputs": [{"namespace": "lance", "name": "acme$customers"}],
    }


def _keyed(**by_identity: str) -> Any:  # noqa: ANN401 — the resolver callable the estate passes around
    """KEY-AWARE, so signing as the wrong identity fails on the identity rather than on a missing key."""
    return lambda identity: by_identity.get(identity)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    from service_kit.lakehouse import outbox

    captured: list[str] = []

    async def _capture(_client: object, **kwargs: Any) -> None:
        captured.append(str(kwargs["event_json"]))

    monkeypatch.setattr(outbox, "publish_lineage_with_outbox", _capture)
    yield captured


def _emitter(resolver: Any, identity: str = CATALOG) -> Any:  # noqa: ANN401 — DaprEmitter
    from dapr.aio.clients import DaprClient

    from catalog.core.lineage_emit import DaprEmitter

    # CAST, never an ignore: the emitter takes a DaprClient and the publish is intercepted before it is
    # touched, so a stand-in is honest here. Saying so in a cast keeps the claim checkable.
    return DaprEmitter(
        cast("DaprClient", SimpleNamespace()),
        "lance-pubsub",
        "lineage.events.v1",
        job_namespace="lance",
        timeout_seconds=1.0,
        service_identity=identity,
        token_resolver=resolver,
    )


async def _emit(resolver: Any, author: str | None) -> None:  # noqa: ANN401
    await _emitter(resolver)._send(_event(author), operation="drop_table", table_id="acme$customers", authorization=None)


@pytest.mark.asyncio
async def test_an_event_authored_by_a_PERSON_is_signed_as_a_DELEGATION(published: list[str]) -> None:
    await _emit(_keyed(**{CATALOG: KEY}), PERSON)

    assert published, "nothing was published"
    found = signature_of(json.loads(published[0]))
    assert found is not None, f"the catalog emitted unsigned: {published[0]}"
    assert found.identity == CATALOG, f"signed as {found.identity!r} rather than as itself"
    assert found.on_behalf_of == PERSON, "the delegation does not name the person, so it is a substitution rather than an attestation"
    assert verify_signed_event(json.loads(published[0]), key=KEY)


@pytest.mark.asyncio
async def test_the_person_stays_the_AUTHOR(published: list[str]) -> None:
    """The point of declaring rather than restamping: the graph keeps answering "which person did
    this", which is the field an audit reads."""
    await _emit(_keyed(**{CATALOG: KEY}), PERSON)

    assert json.loads(published[0])["run"]["facets"]["author"]["sub"] == PERSON


@pytest.mark.asyncio
async def test_a_PEER_key_does_not_verify_it(published: list[str]) -> None:
    """The control. Without it every leg here would pass on a signature over a constant."""
    await _emit(_keyed(**{CATALOG: KEY}), PERSON)

    assert not verify_signed_event(json.loads(published[0]), key=PEER)


@pytest.mark.asyncio
async def test_an_event_the_SERVICE_authored_is_SELF_signed(published: list[str]) -> None:
    """No delegation where there is nobody to act for — a declaration naming the signer would say
    the service vouched for itself, which is noise in the record."""
    await _emit(_keyed(**{CATALOG: KEY}), CATALOG)

    found = signature_of(json.loads(published[0]))
    assert found is not None
    assert found.on_behalf_of is None, f"a self-authored event declared a delegation: {found}"
    assert verify_signed_event(json.loads(published[0]), key=KEY)


@pytest.mark.asyncio
async def test_an_UNAUTHORED_event_is_left_UNSIGNED(published: list[str]) -> None:
    """Signing it would REFUSE it. `verify_signed_event` rejects an event with no author, so a
    signature here would take a static metadata change off the graph entirely."""
    await _emit(_keyed(**{CATALOG: KEY}), None)

    assert signature_of(json.loads(published[0])) is None, f"an unauthored event was signed, which the door refuses: {published[0]}"


@pytest.mark.asyncio
async def test_NO_credential_emits_UNSIGNED_rather_than_a_placeholder(published: list[str]) -> None:
    """The rollout property. A signature that verifies for nobody is worse than none, because the door
    refuses it while unsigned is admitted."""
    await _emit(_keyed(), PERSON)

    assert signature_of(json.loads(published[0])) is None, f"an unkeyed producer attached something anyway: {published[0]}"


# --------------------------------------------------------------------------- #
# THE WIRING HOPS. Everything above tests the emitter; none of it notices the
# factory dropping the identity, or the app never passing one.
# --------------------------------------------------------------------------- #


def test_the_FACTORY_hands_the_emitter_its_identity_and_resolver() -> None:
    """Hop one. A factory that accepted these and did not forward them would leave every emitter in
    production unsigned while every test above stayed green, because they construct the emitter
    directly."""
    from dapr.aio.clients import DaprClient

    from catalog.core.lineage_emit import make_emitter

    resolver = _keyed(**{CATALOG: KEY})
    emitter = make_emitter(
        enabled=True,
        transport="dapr",
        url="",
        client=None,
        dapr=cast("DaprClient", SimpleNamespace()),
        pubsub="lance-pubsub",
        topic="lineage.events.v1",
        job_namespace="lance",
        service_identity=CATALOG,
        token_resolver=resolver,
    )

    assert getattr(emitter, "_service_identity", "") == CATALOG, "the factory dropped the identity, so nothing it builds can sign"
    assert getattr(emitter, "_token_resolver", None) is resolver, "the factory dropped the resolver"


def test_the_APP_passes_both_when_it_builds_the_emitter() -> None:
    """Hop two, and the one a unit test cannot reach: the lifespan builds this emitter, and a
    construction that named neither argument would default both to empty and emit unsigned forever.

    Read off the call rather than the running app, because building it needs a Dapr sidecar. The
    keywords are what the factory dispatches on, so their presence is the property.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "catalog" / "main.py").read_text()
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "make_emitter"]

    assert calls, "main.py no longer builds the emitter here — this gate is measuring something that moved"
    named = {kw.arg for call in calls for kw in call.keywords}
    assert {"service_identity", "token_resolver"} <= named, f"the app builds an emitter that cannot sign: passed {sorted(n for n in named if n)}"
