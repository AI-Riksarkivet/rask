"""The catalog signs every event it emits as itself, declaring the person it authenticated, and withholds what it cannot sign.

[[LH-064]], [[XC-078]]. Every catalog event is authored by the signed-in PERSON, and the service holds no credential of
theirs, so it signs as ITSELF and DECLARES the subject it acts for: an attestation ("the catalog authenticated this person
and says so"), never a claim that the person signed. A create that commits no rows is a DatasetEvent and an insert a RunEvent,
and both carry the author, so both are signed. The control events the same writes publish name the person as their actor (`user:<sub>`) and
are signed the same way. A write nobody authenticated has no author, and a signature on a lineage event with none could
never verify, so that event goes out unsigned; its control events name no actor, which is the catalog acting for itself,
so they are signed with no delegation.

DRIVEN THROUGH THE REAL APP over HTTP, on a real `dir` namespace, because the claim spans the whole wiring: the secret
store, the lifespan that builds the holder, the emitters it is handed, and the door that stamps the author. A test of an
emitter alone passes with a lifespan that never gives it a key. The sidecar's secret API is answered by respx and its
publish is a recording stand-in, so what the verifier sees is exactly what the catalog put on the bus.

A SIGNER WITHOUT ITS KEY TAKES NO WRITE IT WOULD ANNOUNCE. A write's announcement is signed after its Lance commit, so a
catalog that cannot sign refuses the write first, ServiceUnavailable, and commits nothing it could not announce
([[XC-095]], owner 2026-10-17); the pod reports not ready meanwhile. The one write it still takes is one it never
announces: a data write under the provenance namespace, whose rows are lineage's record of the announcements
(`catalog.services.provenance_namespace`). An unsigned control event would never leave the outbox either: the control
relay delivers only staged bytes this catalog signed (`test_the_control_lane_has_a_relay.py`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import respx
from fastapi.testclient import TestClient
from lance_namespace import (
    CreateNamespaceRequest,
    CreateTableRequest,
    DescribeTableRequest,
    NamespaceExistsRequest,
    NamespaceNotFoundError,
    TableNotFoundError,
    connect,
)

from lineage_kit import SIGNATURE_FACET, RefusalReason, SignatureError, verify_control_signature, verify_signature
from service_kit.control_events import CONTROL_TOPIC
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal
from service_kit.governed.signing_key import signing_key_name
from service_kit.lakehouse import outbox


#: The catalog's own service account, whose principal it signs as on the estate (`lance.saSubject`).
CATALOG_ACCOUNT = "system:serviceaccount:default:rask-sa-catalog"
CATALOG = f"kubernetes~{CATALOG_ACCOUNT}"
#: The principal of a person the dev Dex's password connector reports, under the idp id `local` maps to.
PERSON = "dex~08a8684b-db88-4b73-90a9-3cd1661f5466"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"
ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}
LINEAGE_TOPIC = "lineage.events.v1"
#: The namespace the catalog keeps lineage's tables in, as the chart names it (`services.lineage.provenanceNamespace`).
PROVENANCE = "provenance"


def _person(key: str) -> PersonPrincipal:
    """The verified caller `key` names, as the catalog's person door resolves it."""
    principal = Principal.parse(key)
    return PersonPrincipal(principal=principal, claims=IDToken(iss="https://dex", sub=principal.subject, aud="lance", exp=1, iat=1))


class _Sidecar:
    """What the catalog does with its Dapr client: publish, and close at shutdown."""

    published: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    async def publish_event(self, *, topic_name: str = "", data: str = "", **_kwargs: object) -> None:
        _Sidecar.published.append((topic_name, json.loads(data)))

    async def close(self) -> None:
        return None


class _Published:
    """One identity's public keys, as lineage would read them."""

    def __init__(self, *keys: str) -> None:
        self._keys = keys

    def published(self, identity: str) -> Sequence[str]:
        return self._keys

    def refresh(self, identity: str) -> Sequence[str]:
        return self._keys


def _rows(count: int = 3) -> bytes:
    sink = pa.BufferOutputStream()
    table = pa.table({"id": pa.array(range(count), pa.int64())})
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def _on(topic: str) -> list[dict[str, Any]]:
    return [event for published_on, event in _Sidecar.published if published_on == topic]


def _control_verdict(event: dict[str, Any], public: str) -> tuple[str, str | None] | RefusalReason:
    """Who a door holding the catalog's published key finds signed a control event, or the reason it refuses it."""
    try:
        verified = verify_control_signature(event, source=_Published(public), signers=frozenset({CATALOG}), delegators=frozenset({CATALOG}))
    except SignatureError as exc:
        return exc.reason
    return verified.identity, verified.on_behalf_of


type Boot = Callable[[httpx.Response, httpx.Response, object], TestClient]


@pytest.fixture
def control_outbox(tmp_path: Path) -> str:
    return f"file://{tmp_path}/control-outbox"


@pytest.fixture
def boot(tmp_path: Path, control_outbox: str, monkeypatch: pytest.MonkeyPatch, respx_allows_unused_routes: None) -> Iterator[Boot]:
    """Boot the real catalog with signing configured: `boot(seed, keys, token)` answers the running client.

    The order is the one `tests/integration/conftest.py::real_ns_client` documents: env, then the settings cache, then
    the app imported inside the fixture. `token` is what the catalog's door resolves for the caller: a subject, or None
    for a write nobody authenticated. Both lanes are on, and the control lane stages, as the chart deploys them.
    """
    env = {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path / "warehouse"),
        "LANCE_SECRETS_FROM_DAPR": "true",
        "RASK_SIGNING_IDENTITY": CATALOG,
        "LANCE_LINEAGE_EMIT_ENABLED": "true",
        "LANCE_LINEAGE_TRANSPORT": "dapr",
        "LANCE_CONTROL_EMIT_ENABLED": "true",
        "LANCE_CONTROL_OUTBOX_URI": control_outbox,
        "LANCE_PROVENANCE_NAMESPACE": PROVENANCE,
        "APP_API_TOKEN": "the-estate-app-token",
        "DAPR_HTTP_PORT": "3500",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LANCE_S3_SECRET_ACCESS_KEY", raising=False)
    from catalog import main
    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.api.security import CurrentToken
    from catalog.core.config import get_settings

    monkeypatch.setattr(main, "DaprClient", _Sidecar)
    _Sidecar.published = []
    get_settings.cache_clear()
    with ExitStack() as stack:

        def _boot(seed: httpx.Response, keys: httpx.Response, token: object) -> TestClient:
            stack.enter_context(respx.mock)
            respx.get(f"{SECRETS}/lance").mock(return_value=httpx.Response(200, json={"minio-access-key": "test", "minio-secret-key": "the-s3-secret"}))
            respx.get(f"{SECRETS}/signing-key-{signing_key_name(CATALOG)}").mock(return_value=seed)
            respx.get(f"{SECRETS}/signing-public-{signing_key_name(CATALOG)}").mock(return_value=keys)
            namespace = connect("dir", {"root": str(tmp_path / "warehouse")})
            main.app.dependency_overrides[get_namespace] = lambda: namespace
            main.app.dependency_overrides[get_storage_options] = lambda: {}
            main.app.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: token
            stack.callback(main.app.dependency_overrides.clear)
            return stack.enter_context(TestClient(main.app))

        yield _boot
    get_settings.cache_clear()


def _write_a_table_and_a_row(client: TestClient) -> None:
    assert client.post("/v1/namespace/db/create", json={}).status_code == 200
    assert client.post("/v1/table/db$t/create", content=_rows(0), headers=ARROW_STREAM).status_code == 200
    assert client.post("/v1/table/db$t/insert", content=_rows(), headers=ARROW_STREAM).status_code == 200


@pytest.mark.parametrize(
    ("caller", "on_behalf_of"),
    [
        pytest.param(_person(PERSON), PERSON, id="a-person-the-catalog-authenticated"),
        pytest.param(ServicePrincipal(service_account=CATALOG_ACCOUNT), None, id="the-catalog-itself"),
    ],
)
def test_every_event_a_write_causes_is_signed_as_the_catalog_for_the_subject_it_authenticated(
    boot: Boot, event_signer: Any, caller: PersonPrincipal | ServicePrincipal, on_behalf_of: str | None
) -> None:
    subject = caller.subject
    pair = event_signer(CATALOG)
    client = boot(httpx.Response(200, json={"seed": pair.seed}), httpx.Response(200, json={"keys": pair.public}), caller)

    _write_a_table_and_a_row(client)

    lineage = _on(LINEAGE_TOPIC)
    shapes = {"DatasetEvent" if "dataset" in event and "run" not in event else "RunEvent" for event in lineage}
    verified = [verify_signature(event, source=_Published(pair.public), signers=frozenset({CATALOG}), delegators=frozenset({CATALOG})) for event in lineage]
    assert shapes == {"DatasetEvent", "RunEvent"}, f"the writes did not announce both event shapes: {sorted(shapes)}"
    assert {(v.identity, v.on_behalf_of) for v in verified} == {(CATALOG, on_behalf_of)}
    # A control event's actor is always the `user:` principal the door resolved, so the catalog declares it even for itself.
    assert {event["action"]: _control_verdict(event, pair.public) for event in _on(CONTROL_TOPIC)} == {
        "namespace_created": (CATALOG, f"user:{subject}"),
        "table_created": (CATALOG, f"user:{subject}"),
    }


def test_a_write_nobody_authenticated_is_announced_unsigned_and_its_control_events_signed_as_the_catalog(boot: Boot, event_signer: Any) -> None:
    pair = event_signer(CATALOG)
    client = boot(httpx.Response(200, json={"seed": pair.seed}), httpx.Response(200, json={"keys": pair.public}), None)

    _write_a_table_and_a_row(client)

    lineage = _on(LINEAGE_TOPIC)
    unsigned = [event for event in lineage if SIGNATURE_FACET not in (event.get("run") or event["dataset"])["facets"]]
    assert lineage, "nothing was announced"
    assert unsigned == lineage, "an event with no author was signed, and a signature on it could never verify"
    assert {event["action"]: _control_verdict(event, pair.public) for event in _on(CONTROL_TOPIC)} == {
        "namespace_created": (CATALOG, None),
        "table_created": (CATALOG, None),
    }


def _made_natively(root: Path, table: list[str]) -> None:
    """``table`` made on the `dir` root directly, as nothing that announces would make it. Its namespace is made the same
    way, except the provenance namespace, which the catalog makes itself at boot (`catalog.services.provenance_namespace`)
    and which is waited for rather than raced: the `dir` backend answers a concurrent create 409 code 14."""
    namespace = connect("dir", {"root": str(root)})
    if table[:-1] == [PROVENANCE]:
        deadline = time.monotonic() + 30
        while True:
            try:
                namespace.namespace_exists(NamespaceExistsRequest(id=table[:-1]))
                break
            except NamespaceNotFoundError:
                assert time.monotonic() < deadline, "the catalog never made its provenance namespace"
                time.sleep(0.1)
    else:
        namespace.create_namespace(CreateNamespaceRequest(id=table[:-1]))
    namespace.create_table(CreateTableRequest(id=table), _rows())


def _version(root: Path, table: list[str]) -> int | None:
    """The version ``table`` is at on the `dir` root, or None when it does not exist."""
    try:
        return connect("dir", {"root": str(root)}).describe_table(DescribeTableRequest(id=table, load_detailed_metadata=True)).version
    except TableNotFoundError:
        return None


@pytest.mark.parametrize(
    ("door", "table", "answer", "version"),
    [
        pytest.param("/v1/namespace/fresh/create", ["fresh", "t"], (503, 17), None, id="a-namespace-it-would-announce"),
        pytest.param("/v1/table/db$t/insert", ["db", "t"], (503, 17), 1, id="an-insert-it-would-announce"),
        pytest.param(f"/v1/table/{PROVENANCE}$feed/insert", [PROVENANCE, "feed"], (200, None), 2, id="an-insert-it-never-announces"),
    ],
)
def test_a_catalog_without_its_key_takes_only_the_writes_it_never_announces(
    boot: Boot, control_outbox: str, tmp_path: Path, door: str, table: list[str], answer: tuple[int, int | None], version: int | None
) -> None:
    """A write it would announce is refused before it commits, so the table stays at its version and a namespace is never
    made; lineage's own write under the provenance namespace, which it never announces, is served."""
    client = boot(httpx.Response(500), httpx.Response(404), _person(PERSON))
    if version is not None:
        _made_natively(tmp_path / "warehouse", table)

    written = client.post(door, content=_rows(), headers=ARROW_STREAM) if door.endswith("/insert") else client.post(door, json={})
    readiness = client.get("/readyz")

    assert (written.status_code, written.json().get("code") if written.status_code != 200 else None) == answer, written.text
    assert _version(tmp_path / "warehouse", table) == version
    assert _Sidecar.published == [], f"a catalog without its key announced {len(_Sidecar.published)} events"
    staged = [json.loads(body)["action"] for _key, body in outbox.list_events(control_outbox, {})]
    assert staged == [], f"a catalog without its key staged {staged} unsigned, and a staged object proves nothing about who wrote it"
    assert (readiness.status_code, sorted(readiness.json()["components"])) == (503, ["namespace", "signing"])
