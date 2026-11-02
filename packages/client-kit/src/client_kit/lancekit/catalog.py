"""A client's side of the catalog's doors, on the stock Lance Namespace SDK.

A client reaches the catalog through ``lance_namespace.connect("rest", ...)``, which builds pylance's ``RestNamespace``: the
Python SDK `lance_docs/ns_catalog/namespace/index.md` names for engines and frameworks. It depends on the lakehouse only
through that spec and the platform package (``service_kit``), as Lakekeeper's and Polaris's clients reach their catalog
with a stock engine client and share no server code.

:class:`NamespaceCatalogTransport` serves the catalog read path: ``query`` for a scan, ``describe`` for the current
version and ``version/list`` for the history. :class:`NamespaceCatalogWriteTransport` writes through ``merge_insert`` and
``delete``, so the catalog governs the write (its authorization, quality gate and announcement on lineage). Every error
the SDK raises is one of the spec's typed errors, and :func:`translate_catalog_errors` maps its code onto the platform's
DomainError hierarchy.

Two measured gaps in pylance 13.0.0's ``RestNamespace`` shape this module (2026-11-02, against the catalog app and against
pylance's own ``RestAdapter``):

* ITS COUNT DOOR DROPS THE PREDICATE AND THE VERSION. ``count_table_rows`` sends ``GET /v1/table/{id}/count_rows`` with no
  body where `lance_docs/ns_catalog/spec.yaml:1350` defines a POST carrying both, so a filtered count answered the table's
  total. A count is therefore the engine's: the table opened through the namespace on the credential DescribeTable vends
  (`spec.yaml:2840`), counted by Lance's pushdown at the version, as Lakekeeper's and Polaris's clients read a table on the
  credential their catalog vends.
* IT BOUNDS NO CALL. A catalog that accepts a connection and never answers held a call past 45 s, with no setting and
  under every timeout key tried, and the REST page documents only ``uri``, ``delimiter`` and ``headers.*``. So each call
  runs on :data:`_CALLS`, a small pool of its own, and its caller waits :data:`CALL_TIMEOUT_S` at most: a hung catalog
  costs a caller a 503, and holds at most the pool's threads until it answers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import lance
import lance_namespace
from lance_namespace import (
    CreateTableRequest,
    DeleteFromTableRequest,
    DescribeTableRequest,
    ErrorCode,
    LanceNamespaceError,
    ListTableVersionsRequest,
    MergeInsertIntoTableRequest,
)
from opentelemetry import propagate
from pydantic import BaseModel

from client_kit.lancekit.absence import reads_as_absent
from service_kit.arrow_ipc import encode_arrow_stream
from service_kit.exceptions import ConflictError, DomainError, ForbiddenError, NotFoundError, ServiceUnavailableError, UnauthorizedError, ValidationError
from service_kit.naming import CATALOG_DELIMITER
from service_kit.outage import reads_as_store_outage


if TYPE_CHECKING:
    import pyarrow as pa
    from lance_namespace import LanceNamespace, QueryTableRequest


log = logging.getLogger(__name__)

#: How long a caller waits on one catalog call, the bound the spec's generated client was given per request.
CALL_TIMEOUT_S: Final = 30.0
#: The threads catalog calls run on. Bounded so a catalog that holds its connections open holds at most these: further
#: calls wait in the queue and their callers time out, rather than each taking a thread of its own.
_CALLS: Final = ThreadPoolExecutor(max_workers=16, thread_name_prefix="catalog-call")

#: The spec's error codes a caller can act on, each as the platform error that carries it: the code-to-status table of
#: `lance_docs/ns_catalog/namespace/supported-catalogs/lance-rest.md` § Error Code to HTTP Status Mapping. Every other code
#: (unsupported, unavailable, internal, throttled) is the catalog's trouble, not the caller's.
_BY_CODE: Final[dict[ErrorCode, tuple[type[DomainError], str]]] = {
    **dict.fromkeys(
        (
            ErrorCode.NAMESPACE_NOT_FOUND,
            ErrorCode.TABLE_NOT_FOUND,
            ErrorCode.TABLE_INDEX_NOT_FOUND,
            ErrorCode.TABLE_TAG_NOT_FOUND,
            ErrorCode.TRANSACTION_NOT_FOUND,
            ErrorCode.TABLE_VERSION_NOT_FOUND,
            ErrorCode.TABLE_COLUMN_NOT_FOUND,
            ErrorCode.TABLE_BRANCH_NOT_FOUND,
        ),
        (NotFoundError, "not found in the catalog"),
    ),
    **dict.fromkeys(
        (
            ErrorCode.NAMESPACE_ALREADY_EXISTS,
            ErrorCode.NAMESPACE_NOT_EMPTY,
            ErrorCode.TABLE_ALREADY_EXISTS,
            ErrorCode.TABLE_INDEX_ALREADY_EXISTS,
            ErrorCode.TABLE_TAG_ALREADY_EXISTS,
            ErrorCode.CONCURRENT_MODIFICATION,
            ErrorCode.INVALID_TABLE_STATE,
            ErrorCode.TABLE_BRANCH_ALREADY_EXISTS,
        ),
        (ConflictError, "catalog write conflict"),
    ),
    **dict.fromkeys((ErrorCode.INVALID_INPUT, ErrorCode.TABLE_SCHEMA_VALIDATION_ERROR), (ValidationError, "catalog rejected the request")),
    ErrorCode.PERMISSION_DENIED: (ForbiddenError, "catalog denied the request"),
    # 401 stays 401: a missing or invalid credential read as "you lack a grant" sends its reader after an FGA tuple that
    # was never the problem.
    ErrorCode.UNAUTHENTICATED: (UnauthorizedError, "catalog rejected the credential"),
}


class CatalogVersion(BaseModel):
    """One entry of the catalog's version listing: the Lance version and its commit time in epoch millis, ``None`` when
    the catalog records none."""

    version: int
    timestamp_millis: int | None = None


def connect_catalog(base_url: str, *, token: str | None, delimiter: str = CATALOG_DELIMITER, headers: Mapping[str, str] | None = None) -> LanceNamespace:
    """The stock REST namespace for one caller: ``token`` rides every request as its bearer, and no other credential does.

    One per caller rather than one shared: the bearer is a header of the namespace, and a shared one would answer the next
    caller under the previous caller's identity. Measured 2026-11-02, a namespace and its describe cost 3.8 ms, and 200 of
    them left the process's thread count where it was. ``headers`` are the request's context headers (the spec's
    ``header.``-prefixed context, `lance_docs/ns_catalog/catalog/rest/index.md` § Context Header Mapping), and the caller's
    trace context rides beside them, so the catalog's span joins the caller's trace: the SDK's client is Rust, which no
    Python instrumentor reaches.
    """
    carrier: dict[str, str] = dict(headers or {})
    propagate.inject(carrier)
    properties = {"uri": base_url, "delimiter": delimiter} | {f"header.{name}": value for name, value in carrier.items()}
    if token:
        properties["header.Authorization"] = f"Bearer {token}"
    return lance_namespace.connect("rest", properties)


@contextmanager
def translate_catalog_errors() -> Generator[None]:
    """Map the SDK's typed spec errors onto the platform's DomainError hierarchy, so a catalog refusal renders as the same
    problem+json a direct read produces instead of an opaque 500.

    A refusal keeps the catalog's own detail, which its handlers redact. Anything else is logged and answered with a stable
    sentence, since its responder may be an ingress or a sidecar rather than the catalog.
    """
    try:
        yield
    except LanceNamespaceError as exc:
        mapped = _BY_CODE.get(exc.code)
        if mapped is None:
            log.exception("catalog_unavailable", extra={"code": int(exc.code)})
            raise ServiceUnavailableError("the catalog is unavailable") from exc
        error, sentence = mapped
        raise error(f"{sentence}: {exc}") from exc


def _bounded[T](call: Callable[[], T]) -> T:
    """``call`` on :data:`_CALLS`, its caller waiting :data:`CALL_TIMEOUT_S` at most."""
    future = _CALLS.submit(call)
    try:
        return future.result(timeout=CALL_TIMEOUT_S)
    except TimeoutError as exc:
        raise ServiceUnavailableError(f"the catalog did not answer within {CALL_TIMEOUT_S:.0f} s") from exc


class NamespaceCatalogTransport:
    """The catalog read path's doors through the stock SDK, for :class:`~client_kit.lancekit.reader.CatalogTableReader`."""

    def __init__(self, namespace: LanceNamespace) -> None:
        self._namespace = namespace
        self._lock = threading.Lock()
        self._opened: dict[tuple[str, ...], lance.LanceDataset] = {}

    def query(self, request: QueryTableRequest) -> bytes:
        """One page of a scan: ``POST /v1/table/{id}/query``, answered as Arrow IPC file bytes."""
        with translate_catalog_errors():
            data = _bounded(lambda: self._namespace.query_table(request)).data
        if not isinstance(data, bytes):
            raise ServiceUnavailableError("the catalog's query answered no Arrow body")
        return data

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int:
        """The rows of ``table_id`` matching ``filter`` at ``version``, counted on the table this transport opened.

        Raises:
            NotFoundError: The version is gone (reclaimed since it was listed, or never committed).
            ServiceUnavailableError: The table's store did not answer, or the count outlived its bound.
        """
        dataset = self._open(table_id)

        def counted() -> int:
            at = dataset if version is None else dataset.checkout_version(version)
            return at.count_rows(filter)

        try:
            return _bounded(counted)
        except (OSError, ValueError) as exc:
            if reads_as_absent(exc):
                raise NotFoundError(f"table version {version} not found") from exc
            if reads_as_store_outage(exc):
                raise ServiceUnavailableError("the table's store is unavailable") from exc
            raise

    def table_version(self, table_id: list[str]) -> int:
        """The table's current version, from a describe that asks for no credential: a vend would mint a store session
        for a number this read never reaches the store with."""
        request = DescribeTableRequest(id=table_id, load_detailed_metadata=True, vend_credentials=False)
        with translate_catalog_errors():
            response = _bounded(lambda: self._namespace.describe_table(request))
        if response.version is None:
            raise ServiceUnavailableError(f"catalog describe returned no version for {table_id}")
        return int(response.version)

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]:
        """The table's versions, latest first and capped at ``limit`` by the catalog itself (``descending``)."""
        request = ListTableVersionsRequest(id=table_id, limit=limit, descending=True)
        with translate_catalog_errors():
            response = _bounded(lambda: self._namespace.list_table_versions(request))
        return [CatalogVersion(version=int(entry.version), timestamp_millis=entry.timestamp_millis) for entry in response.versions or []]

    def _open(self, table_id: list[str]) -> lance.LanceDataset:
        """The table, opened once per transport through the namespace, on the read credential its describe vends."""
        key = tuple(table_id)
        with self._lock:
            if key not in self._opened:
                with translate_catalog_errors():
                    self._opened[key] = _bounded(lambda: lance.dataset(namespace_client=self._namespace, table_id=list(key)))
            return self._opened[key]


class NamespaceCatalogWriteTransport:
    """The catalog write path's doors through the stock SDK, for :class:`~client_kit.lancekit.writer.CatalogTableWriter`."""

    def __init__(self, namespace: LanceNamespace, table_id: list[str]) -> None:
        self._namespace = namespace
        self._id = list(table_id)

    def merge_upsert(self, delta: pa.Table, on: str) -> None:
        """Insert the unmatched rows of ``delta`` and update the matched ones, keyed on ``on``."""
        self._merge(delta, MergeInsertIntoTableRequest(id=self._id, on=on, when_matched_update_all=True, when_not_matched_insert_all=True))

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        """Insert the unmatched rows of ``delta``, leaving a matched row as it is."""
        self._merge(delta, MergeInsertIntoTableRequest(id=self._id, on=on, when_matched_update_all=False, when_not_matched_insert_all=True))

    def create(self, data: bytes, *, mode: str, properties: Mapping[str, str]) -> int | None:
        """Create the table from an Arrow IPC stream body, answering the version the catalog committed.

        ``mode="exist_ok"`` keeps a table that already exists and answers its version, which is what makes a replayed create
        safe. ``properties`` reach the catalog as the spec's JSON-encoded query parameter.
        """
        request = CreateTableRequest(id=self._id, mode=mode, properties=dict(properties))
        with translate_catalog_errors():
            response = _bounded(lambda: self._namespace.create_table(request, data))
        return response.version

    def delete(self, predicate: str) -> None:
        """Delete the rows matching ``predicate``."""
        request = DeleteFromTableRequest(id=self._id, predicate=predicate)
        with translate_catalog_errors():
            _bounded(lambda: self._namespace.delete_from_table(request))

    def _merge(self, delta: pa.Table, request: MergeInsertIntoTableRequest) -> None:
        body = encode_arrow_stream(delta)
        with translate_catalog_errors():
            _bounded(lambda: self._namespace.merge_insert_into_table(request, body))
