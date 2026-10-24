"""The object stores the viewer's browser may read, and what each one is for.

The registry is the viewer's own configuration: ``ViewerSettings.stores`` (``RASK_STORES``, the chart's ``storage.stores``), else
:data:`DEFAULT_STORES`. The browser lists it (``GET /api/stores``) and resolves every request's store name against it,
so a bucket nobody registered is never read. A store is registered WITH A ROLE, which is what a reader groups by.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum

from pydantic import BaseModel, Field


log = logging.getLogger(__name__)


class StorageRole(StrEnum):
    """What a store is FOR.

    BRONZE, SILVER and GOLD name the lakehouse tier a store's tables sit in; RAW is the external world (a IIIF
    endpoint, a drop bucket), never a governed tier. DERIVED covers artefacts produced from a tier but not governed
    as one (an export); OBSERVABILITY covers the telemetry store, which is operational rather than archival.
    """

    RAW = "raw"
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    DERIVED = "derived"
    OBSERVABILITY = "observability"


class Store(BaseModel):
    """One registered object store.

    ``endpoint`` exists because a deployment's raw material may live on another host than the lakehouse's store;
    without it a store could only say "bucket X on THE configured endpoint", and a bucket held elsewhere would list
    as empty. ``secret`` names the credentials for that host, resolved fail-closed by the object routes. A store
    that declares no secret is read with the deployment's own credentials, an operator's registered decision rather
    than a fallback a request can trigger.
    """

    name: str = Field(description="Stable identifier used by the object browser and the API.")
    bucket: str = Field(description="The bucket backing this store on `endpoint`.")
    role: StorageRole = Field(description="What this store is for.")
    endpoint: str | None = Field(
        default=None,
        description="S3 endpoint this bucket lives on. `None` means the deployment's configured default (RASK_S3_ENDPOINT_URL).",
    )
    insecure: bool = Field(
        default=False,
        description="Skip TLS verification for this endpoint. Needed for internal hosts with private CAs.",
    )
    secret: str | None = Field(
        default=None,
        description=(
            "Key in the Dapr secret store holding this store's credentials as `{access_key, secret_key}`. None = the deployment's own env credentials."
        ),
    )
    description: str = Field(default="", description="What a reader should expect to find here.")
    read_only: bool = Field(
        default=True,
        description="Whether the estate writes here through the UI. The browser is a READER.",
    )


class StoreRegistry(BaseModel):
    """Every store the viewer may browse, whole: the registry is configuration, not data, so it is never paged."""

    stores: list[Store]


#: The stores a deployment that registers none still browses: the lakehouse's own store, never a workload's bucket.
#: A workload's buckets belong in ``RASK_STORES`` for the deployment that has them.
DEFAULT_STORES: tuple[Store, ...] = (
    Store(
        name="lance-catalog",
        bucket="lance-catalog",
        role=StorageRole.BRONZE,
        description="The lakehouse warehouse — the governed medallion datasets.",
    ),
)


def registered_stores(raw: str) -> list[Store]:
    """The configured registry, falling back to :data:`DEFAULT_STORES`.

    ``raw`` is a JSON array of Store objects (``ViewerSettings.stores``). A MALFORMED value logs at error and
    falls back rather than raising: a typo in one env var should not take the browser down.

    The except clause is narrow on purpose. ``json.JSONDecodeError`` and pydantic's ``ValidationError`` are both
    ``ValueError``, and a non-iterable or non-mapping payload raises ``TypeError``: that is the whole failure surface
    of "the operator's JSON is wrong". A fault in this code (a renamed ``Store`` field, an attribute error in a
    validator) propagates, so a broken registry never looks like an unset one.
    """
    value = raw.strip()
    if not value:
        return list(DEFAULT_STORES)
    try:
        return [Store.model_validate(item) for item in json.loads(value)]
    except (ValueError, TypeError):
        log.exception("RASK_STORES is not a valid JSON array of stores; using defaults")
        return list(DEFAULT_STORES)


def store_by_name(name: str, raw: str) -> Store | None:
    """One store by its registered name, or None."""
    return next((s for s in registered_stores(raw) if s.name == name), None)
