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

import asyncio
from typing import Any, cast

from lakehouse.events import outbox
from lakehouse.events.cloud_events import lineage_envelope
from lakehouse.governed.secrets import SecretStore
from lakehouse.storage.options import StorageOptions
from lineage.api import relay
from lineage.core.config import LineageSettings, Signing
from lineage.models import RunEvent
from lineage.services.provenance import ProvenanceStore
from lineage.services.signature import store_published_keys
from tests.app_state import app_holding
from tests.provenance_store_double import ProvenanceStoreDouble
from tests.secret_store_double import SecretStoreDouble


IDENTITY = "service-medallion"
RUN_ID = "0198e0f2-1b2c-7a3d-8e4f-000000000002"


def _wire() -> dict[str, Any]:
    """An event as a producer puts it on the wire: with NO facet bags on the job or the outputs."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-24T06:00:00Z",
        "producer": "https://rask/medallion",
        "run": {"runId": RUN_ID, "facets": {"rask_author": {"sub": IDENTITY}}},
        "job": {"namespace": "lance", "name": "stage.silver"},
        "outputs": [{"namespace": "lance", "name": "silver$features"}],
    }


def test_the_relay_drains_a_staged_event_whose_signature_covers_exactly_the_staged_bytes(
    tmp_path: Any, event_signer: Any, secret_store: SecretStoreDouble, secret_session: SecretStore
) -> None:
    uri = f"file://{tmp_path}/outbox"
    signer = event_signer(IDENTITY)
    secret_store.hold(f"signing-public-{IDENTITY}", {"keys": signer.public})
    outbox.stage_envelope(uri, StorageOptions(), lineage_envelope(signer.sign(_wire())))
    settings = LineageSettings.model_validate({"outbox_uri": uri, "signing": Signing(event_signers=frozenset({IDENTITY}), event_delegators=frozenset())})
    repo = ProvenanceStoreDouble()
    app = app_holding(published_keys=store_published_keys(secret_session))

    outcome = asyncio.run(relay._drain_outbox(app, cast(ProvenanceStore, repo), settings, opts=StorageOptions(), publisher=None))

    assert (outcome.drained, outcome.refused) == (1, 0), (
        f"the staged event never drained: {outcome}, refused for {[refusal['reason'] for refusal in repo.refusals]}"
    )
    assert [event.run.run_id if isinstance(event, RunEvent) else event for event in repo.recorded] == [RUN_ID]
    assert list(outbox.list_events(uri, StorageOptions())) == [], "a drained event must not stay staged"
