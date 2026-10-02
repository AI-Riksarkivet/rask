"""The outbox relay verifies the bytes the producer staged, because only those carry the producer's signature.

[[LH-064]]. The relay's refusal is the costliest in the estate: it neither retries nor parks, it records the verdict and retires
the object, and that object is a crashed publish's only durable copy. So the relay has to verify the document the producer
wrote and not a reconstruction of it. `RunEvent`'s facet bags are `Field(default_factory=dict)`, so a model dumped back to a
dict grows `job.facets = {}` and `outputs[].facets = {}` the producer never sent, the canonical bytes then differ from the signed
ones, and an honest producer's event is refused and destroyed. Measured 2026-10-02: handing the door that dump instead of the
staged bytes turns this test red.

The fixture deliberately OMITS those bags. An event that carries them round-trips losslessly and passes either way, so a fixture
built by a helper that fills them in would assert nothing. The bus subscription hands the door the same bytes the same way, and
`test_lineage_refuses_an_event_no_listed_signer_signed.py` drives it through the registered route with such an event.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import respx

from lineage.api import reconcile_cron
from lineage.core.config import LineageSettings, Signing
from service_kit.lakehouse import outbox


IDENTITY = "service-medallion"
RUN_ID = "0198e0f2-1b2c-7a3d-8e4f-000000000002"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


def _wire() -> dict[str, Any]:
    """An event as a producer puts it on the wire: with NO facet bags on the job or the outputs."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-24T06:00:00Z",
        "producer": "https://rask/medallion",
        "run": {"runId": RUN_ID, "facets": {"author": {"sub": IDENTITY}}},
        "job": {"namespace": "lance", "name": "stage.silver"},
        "outputs": [{"namespace": "lance", "name": "silver$features"}],
    }


class _Repo:
    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.refusals: list[str] = []

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        self.refusals.append(reason)

    async def ingest_event(self, event: Any) -> None:  # noqa: ANN401 - the drain's own shape
        self.ingested.append(event.run.run_id)


@respx.mock
def test_the_relay_drains_a_staged_event_whose_signature_covers_exactly_the_staged_bytes(tmp_path: Any, event_signer: Any) -> None:
    uri = f"file://{tmp_path}/outbox"
    signer = event_signer(IDENTITY)
    respx.get(f"{SECRETS}/signing-public-{IDENTITY}").mock(return_value=httpx.Response(200, json={"keys": signer.public}))
    outbox.stage_event(uri, {}, RUN_ID, json.dumps(signer.sign(_wire())))
    settings = LineageSettings.model_validate({"outbox_uri": uri, "signing": Signing(signers=frozenset({IDENTITY}), delegators=frozenset())})
    repo = _Repo()
    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))

    outcome = asyncio.run(reconcile_cron._drain_outbox(request, cast("Any", repo), settings, {}, None))

    assert (outcome.drained, outcome.refused) == (1, 0), f"the staged event never drained: {outcome}, refused for {repo.refusals}"
    assert repo.ingested == [RUN_ID]
    assert list(outbox.list_events(uri, {})) == [], "a drained event must not stay staged"
