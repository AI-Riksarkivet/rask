"""The storage registry contract — every object store the estate knows, and its ROLE.

R28. Before this, the object browser addressed buckets through a hardcoded two-value union of one
deployment's bucket names, duplicated by hand in the frontend. Two independent copies of a fact that
belongs to the catalog, and neither said what either bucket was FOR: a reader could not tell which
held source material and which held derived output, nor which medallion tier a given store backs.
Adding a bucket meant editing Python, editing TypeScript, and hoping the two stayed in step.

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
    but not themselves governed as one (an export, a derived artefact); OBSERVABILITY covers the
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
#: RAW IS NOT NECESSARILY ON THE WAREHOUSE. The medallion tiers live on the RustFS this chart deploys,
#: but a deployment's raw material may live on an external store — a different host entirely. Before
#: this field, `Store` could only say "bucket X on THE configured endpoint", so the object browser
#: asked the warehouse for a bucket it does not hold, the warehouse correctly answered "no such
#: objects", and a populated store rendered as empty. The data was never missing; the registry could
#: not say where it was.
#:
#: `None` rather than a required field: the governed tiers genuinely do share the deployment default,
#: and forcing every entry to repeat it would invite four copies of one value that drift apart.
#:
#: SOLVED, by `secret` above — this note said the opposite until 2026-08-29 and the stale sentence was
#: load-bearing. It read "NOT YET SOLVED … a store on a different provider needs its own credentials and
#: there is nowhere to put them", which is false: `secret` names a Dapr/OpenBao bundle holding
#: `{access_key, secret_key}`, and both readers of this registry resolve it fail-closed (the object
#: browser's `_client_for`, and ingest's `objectstore.resolve_source_connection`). Left standing, it
#: reads as licence to reach an external endpoint with the deployment's own keys — which is either a
#: loud InvalidAccessKeyId or, when a bucket of that name exists here too, someone else's bytes served
#: under the caller's URI with no error at all.
#:
#: A store that declares NO secret still uses the deployment's env credentials. That is an OPERATOR's
#: registered decision (they wrote the entry), not a fallback a request can trigger.
class StoreRegistry(BaseModel):
    """Every store the catalog knows, newest contract first.

    Returned whole rather than paged: the registry is estate configuration, not data, and a
    frontend that has to page through it cannot render a tier->store view in one pass.
    """

    stores: list[Store]


#: The buckets the PLATFORM itself owns — nothing about any workload's data.
#:
#: This used to ship two more, ``images-batch`` (harvested page images) and ``images-batch-alto``
#: (exported ALTO XML), and every deployment inherited them because the chart ships
#: ``storage.stores: []`` and this is the fallback. A platform library asserting that an estate holds
#: page images and ALTO is the platform having an opinion about the workload — and it reached every
#: service, since `service-kit` is imported by all of them.
#:
#: A workload's buckets belong in ``RASK_STORES`` for the deployment that has them. The two below
#: stay because they are infrastructure the platform provisions and reads for itself, not a
#: statement about what anyone batch-processes.
DEFAULT_STORES: tuple[Store, ...] = (
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
    except (ValueError, TypeError):
        # NARROW, and deliberately so. `except Exception` also caught faults in OUR code — a `Store`
        # field renamed out from under this call site, an attribute error in a validator — and
        # answered them with the defaults, so a broken registry looked exactly like an unset env var
        # and the estate served the wrong stores with a log line nobody was looking for. The two
        # types here are the whole failure surface of "the operator's JSON is wrong":
        # `json.JSONDecodeError` and pydantic's `ValidationError` are both `ValueError`, and a
        # non-iterable or non-mapping payload raises `TypeError`.
        logging.getLogger(__name__).exception("RASK_STORES is not a valid JSON array of stores; using defaults")
        return list(DEFAULT_STORES)


def store_by_name(name: str, raw: str | None = None) -> Store | None:
    """One store by its registered name, or None — the membership check that replaced the Literal."""
    return next((s for s in registered_stores(raw) if s.name == name), None)


def normalise_endpoint(endpoint: str | None) -> str:
    """An endpoint reduced to what makes two of them the SAME store: no case, no trailing slash.

    `https://Objects.Example.org/` and `https://objects.example.org` are one host, and a registry
    lookup that treats them as two answers "not registered" for a store the operator did register.
    Only the scheme and host are case-folded in practice — S3 endpoints carry no path — so a plain
    lower-cased strip is the honest normalisation rather than a URL parse that invites more.
    """
    return (endpoint or "").strip().rstrip("/").lower()


def store_for_endpoint(endpoint: str, bucket: str, raw: str | None = None) -> Store | None:
    """The registered store holding `bucket` on `endpoint`, or None.

    The lookup a caller needs when the ENDPOINT is what it was handed — an ingest run declaring
    `{bucket, endpoint}` in its source options — rather than a store name from a URL. Both halves
    must match: an endpoint alone can host many buckets, and a bucket name alone is exactly the
    collision that makes an unresolved override dangerous (`pages` exists here too).
    """
    wanted = normalise_endpoint(endpoint)
    return next((s for s in registered_stores(raw) if s.bucket == bucket and normalise_endpoint(s.endpoint) == wanted), None)
