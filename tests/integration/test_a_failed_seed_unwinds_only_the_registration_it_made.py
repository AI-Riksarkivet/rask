"""A `/produce` whose seed fails takes back the registration it made, and only that one.

[[LH-194]]. The cascade head registers `bronze$events` before it seeds it, so a seed that raises leaves a record
governing a location that holds nothing; measured on the live estate 2026-09-23 as `bronze$events` at
`s3://lance-catalog/medallion/bronze` in the drift report's `absent_datasets`. The unwind is a deregister, and a
deregister is only this call's to make when this call created the record: the catalog answers a register of an id
it already governs with 409, and a head that unwound on that answer removed a live head another call had seeded.

The real catalog app on a real `dir` namespace, served by uvicorn, with authentication on. The producer reaches it
over HTTP with its own projected token, minted by the root conftest's loopback issuer for the account the chart
gives the producer and mapped by the catalog to the subject `chart/values.yaml` names for it. FGA is off: which
record the catalog holds is the claim, not who may change it. The seed's failure is the one injected fault.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, TableExistsRequest

from medallion.core.config import MedallionSettings
from medallion.services import produce as produce_module


PRODUCER_SA = "rask-sa-medallion-producer"
PRODUCER_SUBJECT = "service-medallion-producer"
NAMESPACE = "rask"
CATALOG_AUDIENCE = "rask-catalog"


@pytest.fixture
def catalog_url(sa_issuer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The catalog as deployed, authenticating services by their projected token, on a live socket."""
    root = tmp_path / "catalog"
    root.mkdir()
    env = {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(root),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": CATALOG_AUDIENCE,
        "RASK_SA_SUBJECTS": json.dumps({f"system:serviceaccount:{NAMESPACE}:{PRODUCER_SA}": PRODUCER_SUBJECT}),
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    connect("dir", {"root": str(root)}).create_namespace(CreateNamespaceRequest(id=["bronze"]))

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the catalog did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    get_settings.cache_clear()


@pytest.fixture
def settings(catalog_url: str, sa_issuer: Any, tmp_path: Path) -> MedallionSettings:
    """The producer's settings: its bronze under the catalog's root, its credential the file the kubelet projects."""
    token_file = tmp_path / "rask-catalog-token"
    token_file.write_text(sa_issuer.mint(PRODUCER_SA, audience=CATALOG_AUDIENCE, namespace=NAMESPACE))
    # As URIs, because that is how the catalog states a `dir` table's location back to a writer that checks it.
    root = (tmp_path / "catalog").as_uri()
    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_BRONZE_URI": f"{root}/medallion/bronze",
            "MEDALLION_BRONZE_NAMESPACE": "bronze",
            "MEDALLION_CATALOG_URL": catalog_url,
            "MEDALLION_CATALOG_ROOT": root,
            "MEDALLION_FGA_SERVICE_IDENTITY": PRODUCER_SUBJECT,
            "RASK_CATALOG_IDENTITY_TOKEN_FILE": str(token_file),
        }
    )


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The head's lineage event, which no sidecar is here to take."""
    events: list[str] = []

    async def publish(*_a: object, **kwargs: object) -> None:
        events.append(cast("str", kwargs["event_json"]))

    monkeypatch.setattr("service_kit.lakehouse.outbox.publish_lineage_with_outbox", publish)
    return events


def _seed_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> object:
        raise OSError("object store refused the write")

    monkeypatch.setattr(produce_module, "seed_bronze", refuse)


def _governed(settings: MedallionSettings) -> bool:
    """Whether the catalog holds a `bronze$events` record, read off the backend the catalog serves."""
    ns = connect("dir", {"root": settings.catalog_root})
    try:
        ns.table_exists(TableExistsRequest(id=["bronze", "events"]))
    except Exception:  # noqa: BLE001 - the dir backend answers an absent table by raising
        return False
    return True


async def _produce(settings: MedallionSettings, token: str) -> dict[str, str]:
    return await produce_module.produce(cast("Any", None), settings, token=token)


@pytest.mark.asyncio
async def test_a_failed_first_seed_leaves_no_record_governing_absent_bytes(
    settings: MedallionSettings, published: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_fails(monkeypatch)

    result = await _produce(settings, "first")

    assert result == {"status": "seed_failed", "token": "first", "detail": "bronze write failed: object store refused the write"}
    assert not _governed(settings), "the record this call registered outlived the write it was made to govern"
    assert published == []


@pytest.mark.asyncio
async def test_a_failed_reseed_keeps_the_head_another_call_registered(
    settings: MedallionSettings, published: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await _produce(settings, "earlier"))["status"] == "produced"
    _seed_fails(monkeypatch)

    result = await _produce(settings, "later")

    assert result == {"status": "seed_failed", "token": "later", "detail": "bronze write failed: object store refused the write"}
    assert _governed(settings), "a produce that did not create the head deregistered it, and its seeded rows are ungoverned"
    assert len(published) == 1
