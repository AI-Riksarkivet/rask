"""Every lineage event the cascade emits carries a signature the bus door can check.

[[LH-064]]. The bus door reads `author.sub` off the payload and authorizes as it, and until a producer
signs, that stamp is a self-assertion any pod holding the shared Dapr app token can make. `maintenance`
signs already; the medallion is the second producer, and the larger one — nine emit sites across four
modules, every hop of the bronze->silver->gold cascade plus the media head and the train lane.

ONE DOOR, because nine were nine chances to forget. Each site repeated the same eight-argument
`publish_lineage_with_outbox` call with `json.dumps(event)`, so signing "at the emit" would have meant
signing in nine places and in every one added later — and a site that forgot would emit something
indistinguishable from a signed event until a reader checked. `emit_lineage` is now the only way out of
this service, which makes the signature a property of the service rather than of each caller, and the
source gate below keeps it that way.

SIGNED WITH THE SERVICE IDENTITY, NEVER THE ROLE. `settings.author` is a display name (`data_eng`) and
`settings.fga_service_identity` is what the run is authorized as — `build_run_event` has kept the two
apart since a role literal in `author.sub` got every cascade run refused. `verify_signed_event` requires
the signer to EQUAL the stamped subject, so signing as the role would refuse the estate's own events.

UNSIGNED IS A REAL ANSWER and stays one for the length of the rollout: the door verifies if a signature
is present and admits an event that carries none. What must never happen is a placeholder — something
that looks signed and verifies for nobody is worse than nothing, because the door refuses it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lineage_kit.signing import signature_of, verify_signed_event


KEY = "Rq7hVx2LmZ0aTpCw9NdEyGs4Bk1JfUo6XiPnMr3t"
PEER = "Zt3rMnPiX6oUfJ1kB4sGyEdN9wCpT0aZmL2xVh7R"
IDENTITY = "service-bronze-to-silver"
ROLE = "data_eng"


def _settings() -> Any:  # noqa: ANN401 — MedallionSettings, built from the env-shaped mapping
    from medallion.core.config import MedallionSettings

    return MedallionSettings.model_validate(
        {
            "MEDALLION_FGA_SERVICE_IDENTITY": IDENTITY,
            "MEDALLION_AUTHOR": ROLE,
            "MEDALLION_LINEAGE_OUTBOX_URI": "",
        }
    )


def _event() -> dict[str, Any]:
    """A cascade event in the shape `build_run_event` produces — no empty facet bags, because `_wire`
    strips them, and that stripping is exactly why the bytes on the wire are the bytes to sign."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-24T08:00:00Z",
        "producer": "https://rask/medallion",
        "run": {
            "runId": "0198e0f2-1b2c-7a3d-8e4f-5a6b7c8d9e0f",
            "facets": {"author": {"_producer": "https://rask/medallion", "name": ROLE, "sub": IDENTITY}},
        },
        "job": {"namespace": "lance-medallion", "name": "stage.silver"},
        "inputs": [{"namespace": "lance", "name": "bronze$events"}],
        "outputs": [{"namespace": "lance", "name": "silver$events"}],
    }


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Capture the JSON the outbox seam is handed — the bytes that are staged AND published."""
    from medallion.core import lineage_publish
    from service_kit.lakehouse import outbox

    captured: list[str] = []

    async def _capture(_client: object, **kwargs: Any) -> None:
        captured.append(str(kwargs["event_json"]))

    monkeypatch.setattr(outbox, "publish_lineage_with_outbox", _capture)
    lineage_publish.reset_signing_key()
    yield captured
    lineage_publish.reset_signing_key()


def _keyed(**by_identity: str) -> Any:  # noqa: ANN401 — the resolver callable the estate passes around
    """KEY-AWARE. A resolver answering one key whatever it is asked for cannot see the producer signing
    as the wrong identity, which is the mistake this whole binding exists to catch."""
    return lambda identity: by_identity.get(identity)


async def _emit(monkeypatch: pytest.MonkeyPatch, resolver: Any) -> None:  # noqa: ANN401
    from medallion.core import lineage_publish

    monkeypatch.setattr(lineage_publish, "dedicated_token_for", lambda _settings: resolver)
    await lineage_publish.emit_lineage(object(), _settings(), _event())


@pytest.mark.asyncio
async def test_an_emitted_event_verifies_with_this_service_s_own_key(published: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    await _emit(monkeypatch, _keyed(**{IDENTITY: KEY}))

    assert published, "nothing was published"
    assert verify_signed_event(json.loads(published[0]), key=KEY), f"the cascade's event does not verify: {published[0]}"


@pytest.mark.asyncio
async def test_a_PEER_s_key_does_not_verify_it(published: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. Without it every leg above would pass on a signature over a constant."""
    await _emit(monkeypatch, _keyed(**{IDENTITY: KEY}))

    assert not verify_signed_event(json.loads(published[0]), key=PEER)


@pytest.mark.asyncio
async def test_it_signs_as_the_SERVICE_IDENTITY_and_not_the_display_role(published: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """`verify_signed_event` requires signer == stamped subject, so signing as `author` would make the
    estate refuse its own cascade. The resolver holds a key for BOTH names, so the leg fails on the
    identity chosen rather than on a missing credential."""
    await _emit(monkeypatch, _keyed(**{IDENTITY: KEY, ROLE: PEER}))

    found = signature_of(json.loads(published[0]))
    assert found is not None, "the event is unsigned"
    assert found.identity == IDENTITY, f"signed as {found.identity!r}, which is the role a person reads and not the subject the door authorizes"


@pytest.mark.asyncio
async def test_NO_credential_emits_UNSIGNED_rather_than_a_placeholder(published: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The rollout property, and the one that must not be satisfied by faking a signature: the door
    admits an unsigned event and REFUSES one that does not verify, so a placeholder would take the
    cascade's provenance off the graph entirely."""
    await _emit(monkeypatch, _keyed())

    assert signature_of(json.loads(published[0])) is None, f"an unkeyed producer attached something anyway: {published[0]}"
    assert json.loads(published[0])["run"]["facets"]["author"]["sub"] == IDENTITY, "the event lost its author on the way out"


# --------------------------------------------------------------------------- #
# THE WIRING GATE. Everything above tests the door; none of it notices a tenth
# call site publishing around it.
# --------------------------------------------------------------------------- #

_SRC = Path(__file__).resolve().parents[1] / "src" / "medallion"


def test_NOTHING_in_this_service_publishes_lineage_around_the_signing_door() -> None:
    """A signature that any caller can skip is a signature the estate cannot rely on.

    The nine sites that used to call the outbox seam directly are what made "sign at the emit"
    unworkable, and adding a tenth is a one-line change nobody would read twice. `emit_lineage` is
    allowed to call the seam; nothing else in this service is.
    """
    callers = sorted(path.relative_to(_SRC).as_posix() for path in _SRC.rglob("*.py") if "publish_lineage_with_outbox(" in path.read_text(encoding="utf-8"))

    assert callers, "no call site found at all — the gate is matching nothing and would pass on anything"
    assert callers == ["core/lineage_publish.py"], f"these modules publish lineage around the signing door, so their events reach the bus unsigned: {callers}"
