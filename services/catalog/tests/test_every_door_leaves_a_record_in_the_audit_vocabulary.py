"""Every door a caller drives leaves a record in the closed audit vocabulary, and every data read says what it read ([[LH-231]]).

The trail exists to answer "who did what to which table, and was it allowed" by a query over its fields. That only
holds while every record carries the format it was written in, an action and an outcome from the closed vocabularies,
the subject it was made for and the request it was made under, and while every door keeps emitting. So one corpus of
requests is driven through the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding
([[LH-235]]): a create, a describe, every door that reads rows, a credential vend, a refusal, a grant and an erasure, whose
obligation is recorded once per ref it covers. The trail they leave is judged as a compliance query would read it, so a door that stops emitting, emits outside the vocabulary, or
reads without naming its columns and version fails here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from typing import Any

import pyarrow as pa
import pytest
from lance import blob_array, blob_field
from pydantic import BaseModel, SecretStr

from catalog.api.dependencies import get_vendor
from catalog.core.vending import Tier, VendedSession
from service_kit.governed.audit import AUDIT_FORMAT, AUDIT_LOGGER, AuditAction, AuditOutcome, configure_audit
from service_kit.governed.store_credential import S3KeyPair
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_PAYLOAD = b"hello-world"
_ROWS = encode_arrow_stream(
    pa.table(
        {"id": pa.array([1, 2], pa.int64()), "payload": blob_array([_PAYLOAD, b"x"])},
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
    )
)
_QUERY = {"k": 10, "vector": {"single_vector": []}, "columns": {"column_names": ["id"]}}
_TABLE = "/v1/table/db$t"
_MANAGED = "/management/v1/table/db$t"


class _Expected(BaseModel):
    """A record a door's trail must hold: its action and the fields it must carry."""

    action: AuditAction
    fields: dict[str, str | int]


class _Door(BaseModel):
    """One request of the corpus, as a caller sends it, the status it answers and what its trail must hold."""

    request_id: str
    caller: str
    method: str
    path: str
    keywords: dict[str, Any]
    status: int
    expected: list[_Expected]


#: The corpus, in the order it is sent. A read names the columns and the version it served; the change feed is read at
#: the head, so its record names the version it resolved rather than one the caller sent.
_CORPUS = [
    _Door(
        request_id="create",
        caller="alice",
        method="POST",
        path=f"{_TABLE}/create",
        keywords={"content": _ROWS, "headers": {"Content-Type": ARROW_STREAM_MEDIA_TYPE}},
        status=200,
        expected=[_Expected(action=AuditAction.AUTHZ, fields={"audit.outcome": "allow"})],
    ),
    _Door(
        request_id="describe",
        caller="alice",
        method="POST",
        path=f"{_TABLE}/describe",
        keywords={"json": {}},
        status=200,
        expected=[_Expected(action=AuditAction.AUTHZ, fields={"audit.outcome": "allow"})],
    ),
    _Door(
        request_id="query",
        caller="alice",
        method="POST",
        path=f"{_TABLE}/query",
        keywords={"json": _QUERY},
        status=200,
        expected=[_Expected(action=AuditAction.READ_DATA, fields={"audit.columns": "id", "audit.version": 1})],
    ),
    _Door(
        request_id="explain",
        caller="alice",
        method="POST",
        path=f"{_TABLE}/explain_plan",
        keywords={"json": {"query": _QUERY}},
        status=200,
        expected=[_Expected(action=AuditAction.READ_DATA, fields={"audit.columns": "id", "audit.version": 1})],
    ),
    _Door(
        request_id="analyze",
        caller="alice",
        method="POST",
        path=f"{_TABLE}/analyze_plan",
        keywords={"json": _QUERY},
        status=200,
        expected=[_Expected(action=AuditAction.READ_DATA, fields={"audit.columns": "id", "audit.version": 1})],
    ),
    _Door(
        request_id="count",
        caller="alice",
        method="POST",
        path=f"{_TABLE}/count_rows",
        keywords={"json": {}},
        status=200,
        expected=[_Expected(action=AuditAction.READ_DATA, fields={"audit.columns": "all", "audit.version": 1})],
    ),
    _Door(
        request_id="changes",
        caller="alice",
        method="POST",
        path=f"{_MANAGED}/changes",
        keywords={"json": {"begin_version": 0, "columns": ["id"]}},
        status=200,
        expected=[_Expected(action=AuditAction.READ_DATA, fields={"audit.columns": "id", "audit.version": 1})],
    ),
    _Door(
        request_id="blob",
        caller="alice",
        method="GET",
        path=f"{_MANAGED}/blobs?column=payload&row=0",
        keywords={},
        status=200,
        expected=[
            _Expected(
                action=AuditAction.READ_DATA,
                fields={"audit.columns": "payload", "audit.version": 1, "audit.row": 0, "audit.offset": 0, "audit.length": len(_PAYLOAD)},
            )
        ],
    ),
    _Door(
        request_id="vend",
        caller="alice",
        method="POST",
        path=f"{_MANAGED}/credentials",
        keywords={"json": {}},
        status=200,
        expected=[_Expected(action=AuditAction.VEND_CREDENTIALS, fields={"audit.outcome": "success"})],
    ),
    _Door(
        request_id="refusal",
        caller="bob",
        method="POST",
        path=f"{_TABLE}/describe",
        keywords={"json": {}},
        status=403,
        expected=[_Expected(action=AuditAction.AUTHZ, fields={"audit.outcome": "deny"})],
    ),
    _Door(
        request_id="grant",
        caller="alice",
        method="POST",
        path=f"{_MANAGED}/access/grant",
        keywords={"json": {"user": "user:dex~bob", "relation": "reader"}},
        status=200,
        expected=[
            _Expected(action=AuditAction.ACCESS_GRANT, fields={"audit.outcome": "success"}),
            _Expected(action=AuditAction.ACCESS_TUPLE_WRITE, fields={"audit.outcome": "success"}),
        ],
    ),
    _Door(
        request_id="erasure",
        caller="alice",
        method="POST",
        path=f"{_MANAGED}/erasure",
        keywords={"json": {"predicate": "id = 1"}},
        status=202,
        expected=[
            _Expected(action=AuditAction.ERASURE_OBLIGATION_RECORDED, fields={"audit.outcome": "success"}),
            _Expected(action=AuditAction.ERASURE_REF_OBLIGED, fields={"audit.ref": "main"}),
        ],
    ),
]


class _StsVendor:
    """The store's STS exchange, the one collaborator a ``dir`` estate cannot run: it issues a credential for every vend."""

    def vend(
        self, *, table_location: str, tier: Tier, session_name: str, bases: Sequence[str] = (), data_directories: Sequence[str] = (), branch: str = ""
    ) -> VendedSession | None:
        return VendedSession(
            key_pair=S3KeyPair(access_key_id=f"AK-{tier}", secret_access_key=SecretStr("SK")), endpoint=None, region="us-east-1", expires_at_millis=1
        )


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({k: v for k, v in record.__dict__.items() if k.startswith("audit.")})


@pytest.fixture
def trail() -> Iterator[list[dict[str, Any]]]:
    """The ``lance.audit`` records, each as the flat field set a compliance query reads."""
    handler = _Capture()
    logger = logging.getLogger(AUDIT_LOGGER)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        configure_audit(enabled=True)


def test_every_door_leaves_a_record_in_the_vocabulary_and_every_read_names_what_it_read(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    trail: list[dict[str, Any]],
) -> None:
    catalog = governed_catalog
    catalog.client.app.dependency_overrides[get_vendor] = _StsVendor
    assert catalog.client.post("/v1/namespace/db/create", json={}, headers=catalog.bearer("alice")).status_code == 200
    configure_audit(enabled=True)
    trail.clear()

    for door in _CORPUS:
        headers = {**catalog.bearer(door.caller), "X-Request-ID": door.request_id, **door.keywords.get("headers", {})}
        answer = catalog.client.request(door.method, door.path, headers=headers, **{k: v for k, v in door.keywords.items() if k != "headers"})
        assert answer.status_code == door.status, f"{door.request_id}: {answer.status_code} {answer.text}"

    outside = [
        r
        for r in trail
        if r.get("audit.format") != AUDIT_FORMAT or r.get("audit.action") not in set(AuditAction) or r.get("audit.outcome") not in set(AuditOutcome)
    ]
    assert outside == [], "records outside the format or the closed vocabularies"
    assert [r for r in trail if not r["audit.subject"]] == [], "records naming no subject"
    by_request = {door.request_id: [r for r in trail if r["audit.request_id"] == door.request_id] for door in _CORPUS}
    unauthenticated = [rid for rid, records in by_request.items() if not any(r["audit.action"] == AuditAction.AUTHN for r in records)]
    assert unauthenticated == [], "requests whose trail holds no authentication record"
    unverdicted = [rid for rid, records in by_request.items() if not any(r["audit.outcome"] in {"allow", "deny"} for r in records)]
    assert unverdicted == [], "requests whose trail holds no authorization verdict"
    missing = [
        (door.request_id, expected.action.value, expected.fields)
        for door in _CORPUS
        for expected in door.expected
        if not any(r["audit.action"] == expected.action and expected.fields.items() <= r.items() for r in by_request[door.request_id])
    ]
    assert missing == [], "doors that left no record, or one without what it must name"
