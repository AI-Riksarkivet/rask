"""The storage registry contract — every object store the estate knows, and its ROLE.

R28. Before this, the object browser addressed buckets through a hardcoded two-value union
(``Literal["images-batch", "images-batch-alto"]``) duplicated by hand in the frontend. Two
independent copies of a fact that belongs to the catalog, and neither said what either bucket was
FOR: a reader could not tell that one holds source page images and the other derived ALTO, nor
which medallion tier a given store backs. Adding a bucket meant editing Python, editing TypeScript,
and hoping the two stayed in step.

A store is registered WITH A ROLE. The role is the governed fact — it says where a store sits in
the cascade (external raw, or one of the three governed tiers) — and it is what the UI groups by,
so the tier->store view is derived rather than transcribed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class StorageRole(StrEnum):
    """What a store is FOR.

    The three governed tiers are exactly bronze/silver/gold (R23): the medallion is
    bronze->silver->gold, and RAW is deliberately outside it — raw is the external world (a IIIF
    endpoint, a drop bucket), never a governed tier. DERIVED covers artefacts produced from a tier
    but not themselves governed as one (exported ALTO, thumbnails); OBSERVABILITY covers the
    telemetry store, which is operational rather than archival.
    """

    RAW = "raw"
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    DERIVED = "derived"
    OBSERVABILITY = "observability"


#: Roles that ARE the medallion, in cascade order. Everything else is adjacent to it, not part of
#: it — which is why this is a tuple here rather than a property of the enum.
GOVERNED_TIERS: tuple[StorageRole, ...] = (
    StorageRole.BRONZE,
    StorageRole.SILVER,
    StorageRole.GOLD,
)


class Store(BaseModel):
    """One registered object store."""

    name: str = Field(description="Stable identifier used by the object browser and the API.")
    bucket: str = Field(description="The bucket backing this store on `endpoint`.")
    role: StorageRole = Field(description="Where this store sits in the cascade.")
    endpoint: str | None = Field(
        default=None,
        description=(
            "S3 endpoint this bucket lives on. `None` means the deployment's configured default "
            "(RASK_S3_ENDPOINT_URL) — which is what every governed tier uses."
        ),
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
        description=(
            "Whether the estate writes here through the UI. The browser is a READER; a store the "
            "cascade owns must not be presented as editable just because S3 would allow it."
        ),
    )


#: Why `endpoint` exists at all, since every governed tier shares one:
#:
#: RAW IS NOT ON THE WAREHOUSE. The medallion tiers live on the RustFS this chart deploys, but the raw
#: page images live on an external store (HCP) — a different host entirely. Before this field, `Store`
#: could only say "bucket X on THE configured endpoint", so the object browser asked the warehouse for
#: `images-batch`, the warehouse correctly answered "no such objects", and a bucket full of images
#: rendered as an empty store. The data was never missing; the registry could not say where it was.
#:
#: `None` rather than a required field: the governed tiers genuinely do share the deployment default,
#: and forcing every entry to repeat it would invite four copies of one value that drift apart.
#:
#: NOT YET SOLVED, and deliberately not faked here: `storage.s3_client(endpoint)` resolves CREDENTIALS
#: from process env, so a store on a different provider needs its own credentials and there is nowhere
#: to put them. Attaching a bucket that shares the deployment's credentials works today; attaching one
#: that does not needs a secret reference resolved through the Dapr secret store (OpenBao), which is a
#: separate change. Until then such a store fails with an auth error, which is at least a loud and
#: explicable failure rather than a silently empty listing.
class StoreRegistry(BaseModel):
    """Every store the catalog knows, newest contract first.

    Returned whole rather than paged: the registry is estate configuration, not data, and a
    frontend that has to page through it cannot render a tier->store view in one pass.
    """

    stores: list[Store]


#: The estate's stores when a deployment declares none. These are the buckets rask actually runs
#: with, so an unconfigured stack behaves exactly as the old hardcoded union did — except the roles
#: are stated, and the list is data a deployment replaces rather than a Literal a reader edits in
#: two languages.
DEFAULT_STORES: tuple[Store, ...] = (
    Store(
        name="images-batch",
        bucket="images-batch",
        role=StorageRole.RAW,
        description="Source page images, as harvested. External input — never a governed tier.",
    ),
    Store(
        name="images-batch-alto",
        bucket="images-batch-alto",
        role=StorageRole.DERIVED,
        description="ALTO XML exported from the cascade. Produced from a tier, not itself one.",
    ),
    Store(
        name="lance-catalog",
        bucket="lance-catalog",
        role=StorageRole.BRONZE,
        description="The lakehouse warehouse — the governed medallion datasets.",
    ),
    Store(
        name="rask-observability",
        bucket="rask-observability",
        role=StorageRole.OBSERVABILITY,
        description="Telemetry retained by GreptimeDB. Operational, not archival.",
    ),
)


def registered_stores(raw: str | None = None) -> list[Store]:
    """The configured registry, falling back to the estate defaults.

    ``raw`` is a JSON array of Store objects (the ``RASK_STORES`` / ``LANCE_STORES`` env value). A
    MALFORMED value logs and falls back rather than raising: a typo in one env var should not take a
    service down, and a browser showing the default stores is a far better failure than a 500 on
    every page that lists them. Logged at error level precisely so it cannot pass unnoticed.

    Lives in service-kit, not in a service: the viewer validates bucket names against the SAME
    registry the catalog serves to the UI, and a service importing another service's endpoint module
    to share a fact would be a layering violation.
    """
    import json
    import logging
    import os

    value = (raw if raw is not None else os.environ.get("RASK_STORES", "")).strip()
    if not value:
        return list(DEFAULT_STORES)
    try:
        return [Store.model_validate(item) for item in json.loads(value)]
    except Exception:
        logging.getLogger(__name__).exception("RASK_STORES is not a valid JSON array of stores; using defaults")
        return list(DEFAULT_STORES)


def store_by_name(name: str, raw: str | None = None) -> Store | None:
    """One store by its registered name, or None — the membership check that replaced the Literal."""
    return next((s for s in registered_stores(raw) if s.name == name), None)
