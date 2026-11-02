"""The catalog's write transport: a table written through the spec's REST doors.

:class:`RestCatalogWriteTransport` writes through ``POST /v1/table/{id}/merge_insert`` and ``/delete``, so the catalog
governs the write (its authorization, quality gate and the announcement on lineage). The base URL comes from the
caller and the transport carries a urllib3 ``Retry``; catalog errors are translated onto the DomainError hierarchy so a
denial or a conflict keeps its status through the caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lancekit.arrow_ipc import encode_arrow_stream
from service_kit.lancekit.catalog_client import catalog_api_client
from service_kit.lancekit.catalog_client import request_headers as _request_headers
from service_kit.lancekit.reader import translate_catalog_errors


if TYPE_CHECKING:
    import pyarrow as pa


class RestCatalogWriteTransport:
    """Live REST catalog transport — ``POST /v1/table/{id}/merge_insert`` + ``/delete``
    via the lance-ns client's ``DataApi.merge_insert_into_table`` / ``delete_from_table``.
    Host-agnostic (base URL from settings) + retry-friendly; catalog errors are
    translated onto the DomainError hierarchy so denials/conflicts keep their
    status through our services."""

    def __init__(
        self,
        base_url: str,
        table_id: list[str],
        *,
        delimiter: str = CATALOG_DELIMITER,
        token: str | None = None,
        retries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        from lance_namespace_urllib3_client.api.data_api import DataApi

        # ONE client (and so one connection pool) per catalog, shared with every other transport
        # aimed at it — this ctor may run once per request.
        self._api = DataApi(catalog_api_client(base_url, retries=retries))
        self._token = token
        self._id_str = delimiter.join(table_id)
        self._delimiter = delimiter
        self._timeout = timeout

    def request_headers(self) -> dict[str, str]:
        """THIS caller's bearer, per request — never a default header on the shared client."""
        return _request_headers(self._token)

    def merge_upsert(self, delta: pa.Table, on: str, *, matched_when: str | None = None) -> None:
        """Insert the unmatched rows and update the matched ones, only where ``matched_when`` holds when it is given.

        ``matched_when`` is the spec's ``when_matched_update_all_filt``, an SQL condition over ``source.<col>`` and
        ``target.<col>``: ``source.seq > target.seq`` keeps a row's value from moving backward.
        """
        # merge flags are direct params; `body` is the Arrow-IPC STREAM delta.
        with translate_catalog_errors():
            self._api.merge_insert_into_table(
                self._id_str,
                on,
                encode_arrow_stream(delta),
                delimiter=self._delimiter,
                when_matched_update_all=True,
                when_matched_update_all_filt=matched_when,
                when_not_matched_insert_all=True,
                _request_timeout=self._timeout,
                _headers=self.request_headers(),
            )

    def merge_insert_only(self, delta: pa.Table, on: str) -> None:
        with translate_catalog_errors():
            self._api.merge_insert_into_table(
                self._id_str,
                on,
                encode_arrow_stream(delta),
                delimiter=self._delimiter,
                when_matched_update_all=False,
                when_not_matched_insert_all=True,
                _request_timeout=self._timeout,
                _headers=self.request_headers(),
            )

    def delete(self, predicate: str) -> None:
        from lance_namespace_urllib3_client import DeleteFromTableRequest

        with translate_catalog_errors():
            self._api.delete_from_table(
                self._id_str,
                DeleteFromTableRequest(predicate=predicate),
                delimiter=self._delimiter,
                _request_timeout=self._timeout,
                _headers=self.request_headers(),
            )
