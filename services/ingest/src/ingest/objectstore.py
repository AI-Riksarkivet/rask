"""Per-run object-store connections — WHERE a run's bytes are, and whose credentials open them.

An `s3-prefix` run may declare an `endpoint` (`adapters.register_builtin_sources` advertises the
field and the compute zone renders it from `describe_sources()`). Until this module existed the
override reached the ENUMERATION half only, and even there it borrowed the deployment's own
credentials: `fetch._fetch_s3` built its client from `RASK_S3_ENDPOINT_URL` and `UnitTask` had
nowhere to carry the run's endpoint, so the option crossed the accept door and died at the queue.

**That is a silent-wrong-data hazard, not a missing feature.** Best case every unit parks on the DLQ
because the estate's own store has no such key. Worst case the estate holds a bucket of the same
name, the fetch succeeds, and rows land under an external `source_uri` carrying the estate's bytes —
a wrong dataset with nothing anywhere reporting an error.

**A declared endpoint must be REGISTERED, and its credentials come from the secret store.** The
estate's sanctioned place for "a bucket that is not on our endpoint" is the storage registry
(`RASK_STORES` -> `service_kit.schemas.storage.Store`): the entry carries the endpoint, the TLS
posture, and the NAME of a Dapr/OpenBao secret holding `{access_key, secret_key}`. Credentials are
never taken from the request, never published on a task, and never fall back to env for a declared
endpoint — env holds the WAREHOUSE's credentials, which is precisely how an external
`s3://pages/...` gets answered by the estate's own `pages` bucket. The object browser reached the
same conclusion from the same failure (`viewer/api/v1/endpoints/objects.py::_creds`).

Refusing is therefore the only other answer, and it is loud at BOTH doors — the accept-time adapter
build and the worker's fetch — for the same reason `local-dir`'s confinement is checked twice: a
unit key crosses the queue, and the run that admitted it is long gone by the time a worker reads it.

The two failure types are deliberately different, because `worker._is_permanent` reads them:
`UnregisteredSourceEndpointError` is a `ValueError` (a deployment gap no retry can close, so the
unit parks on its first delivery), while `SourceCredentialsUnavailableError` is not (a sidecar or a
seeding store can heal, so the unit is redelivered and only then parked).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field

from ingest.config import settings
from service_kit.governed.secrets import fetch_dapr_secret
from service_kit.schemas.storage import Store, normalise_endpoint, store_for_endpoint
from storage import configured_endpoint


log = logging.getLogger(__name__)


#: The Dapr secret store every governed service reads. Declared once on `IngestSettings`; read here
#: through a function so it is resolved when a secret is fetched rather than frozen at import.
def secret_store() -> str:
    return settings().secret_store


class UnregisteredSourceEndpointError(ValueError):
    """A run declared a source endpoint no registered store accounts for.

    A `ValueError` on purpose: `worker._is_permanent` classifies it as permanent, so the unit parks
    on its FIRST delivery. Retrying cannot register a store, and the alternative — letting the fetch
    proceed against the estate default — is the silent wrong-bucket read this whole module exists to
    make impossible.
    """


class SourceCredentialsUnavailableError(RuntimeError):
    """A registered store's secret could not be read — store down, secret missing, or seeded empty.

    `fetch_dapr_secret` returns `{}` for all three (it swallows its own failures), so this cannot
    distinguish them and does not pretend to. NOT a `ValueError`: a sidecar that is still coming up
    heals by waiting, so the unit is redelivered and parks only after exhausting its budget.
    """


class SourceConnection(BaseModel):
    """How to open a run's source store — resolved at the point of use, never carried on the wire.

    `endpoint is None` means the deployment's own store: `storage.s3_client` then resolves endpoint
    and credentials from env exactly as it always has, which is what keeps every run that declares
    no override byte-identical to before.
    """

    endpoint: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    insecure: bool = False
    #: Matches `storage.s3_client`'s own default, so the pyarrow filesystem and the boto3 client
    #: cannot sign against different regions for one bucket.
    region: str = Field(default_factory=lambda: settings().aws_region)

    @property
    def is_estate_default(self) -> bool:
        return self.endpoint is None


def resolve_source_connection(endpoint: str | None, bucket: str) -> SourceConnection:
    """The connection for `bucket` on the run's declared `endpoint`, or the estate default.

    Three answers, and no fourth:

    1. No endpoint declared, or one that IS the deployment's own — the estate default, env chain.
    2. A registered store for `(endpoint, bucket)` — its endpoint, its TLS posture, and either the
       credentials behind its `secret` or (a store that declares none) the deployment's env
       credentials, which is the OPERATOR's registered decision and not something a request chooses.
    3. Anything else — refused.

    Case 1's endpoint equality is why `storage.configured_endpoint` is public: a run that spells out
    the deployment's own endpoint is asking for the store we already use, and refusing it for want of
    a registry entry would be a refusal with no hazard behind it.
    """
    declared = normalise_endpoint(endpoint)
    if not declared or declared == normalise_endpoint(configured_endpoint()):
        return SourceConnection()

    store = store_for_endpoint(declared, bucket)
    if store is None:
        raise UnregisteredSourceEndpointError(
            f"source endpoint {endpoint!r} is not registered for bucket {bucket!r} — add it to RASK_STORES "
            f"(name, bucket, role, endpoint and the `secret` holding its credentials). Refusing rather than "
            f"reading bucket {bucket!r} from the deployment's own store, which would silently ingest the wrong bytes."
        )
    return _connection_for_store(store, declared)


def _connection_for_store(store: Store, endpoint: str) -> SourceConnection:
    if store.secret is None:
        # No secret declared: the operator registered this store as sharing the deployment's own
        # credentials. Env is reached because the REGISTRY says so, never as a fallback from a failed
        # lookup — which is the distinction the object browser's `_creds` docstring exists to hold.
        return SourceConnection(endpoint=endpoint, insecure=store.insecure)
    access_key, secret_key = _store_credentials(store.secret)
    return SourceConnection(endpoint=endpoint, access_key=access_key, secret_key=secret_key, insecure=store.insecure)


@lru_cache(maxsize=16)
def _store_credentials(secret: str) -> tuple[str, str]:
    """A registered store's `{access_key, secret_key}` from the Dapr secret store. FAIL-CLOSED.

    Cached because a secret is estate configuration, not per-unit data, and a sidecar round-trip per
    unit would put OpenBao on the fetch path of a million-unit run. `lru_cache` never stores a raised
    exception, so a transient outage cannot pin a failure for the process lifetime.

    `retries=1`: this runs inside a worker holding a JetStream ack, not at boot. The queue's own
    redelivery is the retry, and burning `fetch_dapr_secret`'s two-minute boot budget under an ack
    would expire `ack_wait` and have the unit redelivered underneath us.
    """
    store = secret_store()
    bundle = fetch_dapr_secret(store, secret, retries=1)
    access_key, secret_key = bundle.get("access_key"), bundle.get("secret_key")
    if not (access_key and secret_key):
        log.warning("source_store_secret_unavailable", extra={"secret": secret, "store": store})
        raise SourceCredentialsUnavailableError(
            f"credentials for this source could not be read: secret {secret!r} from the {store!r} secret store "
            f"yielded no access_key/secret_key pair — the store may be unreachable, the secret missing, or seeded "
            f"without the pair. Refusing rather than retrying an external endpoint with the deployment's own keys."
        )
    return (access_key, secret_key)


@lru_cache(maxsize=8)
def _memoized_client(endpoint: str | None, access_key: str | None, secret_key: str | None, insecure: bool) -> Any:  # noqa: ANN401 — boto3 has no public stub, same rule as storage.s3_client
    """One client per distinct connection. `storage.s3_client` opens a connection pool, and building
    one per unit throws that pool away every time — the measurement `staging._client_for` already
    made. Keyed on the resolved fields rather than the model because a pydantic model is unhashable."""
    import storage

    # Looked up on the MODULE rather than imported by name so a test can substitute the estate's one
    # S3 seam, and so `storage.s3_client` stays the single door every S3 connection goes through.
    return storage.s3_client(endpoint, access_key=access_key, secret_key=secret_key, insecure=insecure or None)


def source_s3_client(connection: SourceConnection) -> Any:  # noqa: ANN401 — boto3 has no public stub
    """A boto3 client for this connection — `storage.s3_client`, never boto3 directly.

    The estate's rule, and what keeps the CA bundle, the insecure flag and the region resolution in
    one place instead of re-derived per caller.
    """
    return _memoized_client(connection.endpoint, connection.access_key, connection.secret_key, connection.insecure)


def source_filesystem(connection: SourceConnection) -> Any:  # noqa: ANN401 — pyarrow.fs.FileSystem, kept loose so a caller needs no pyarrow import
    """A `pyarrow.fs` filesystem for this connection, through `service_kit.lakehouse.objectfs`.

    NOT a bare `pafs.S3FileSystem()`: the shared builder derives the scheme from the endpoint (an
    `https://` endpoint hardcoded to `http` is a silently downgraded connection) and passes the
    region, and a hand-rolled copy is exactly the drift `objectfs` was written to prevent.

    **The estate-default branch now honours the configured endpoint too, which it did not before.**
    `_s3_prefix` built a bare `pafs.S3FileSystem()` whenever the run declared no endpoint, so the
    pyarrow view of the bucket went to real AWS while the boto3 view went to `RASK_S3_ENDPOINT_URL` —
    two views of one bucket, the exact divergence that adapter's own comment claimed was impossible.
    Latent rather than fatal, because `iter_versioned_keys` prefers the boto3 client, but a source
    that ever falls back to the pyarrow listing would have listed a different store. Credentials on
    that branch come from pyarrow's own AWS_* env chain — the same environment `storage.s3_client`
    reads — so nothing is invented here for a run that declared no override.

    `insecure` reaches the boto3 client only: `pafs.S3FileSystem` exposes no verify-skip. A store
    needing one is listable but its enumeration will fail TLS — loudly, which is the right direction.
    """
    import pyarrow.fs as pafs

    from service_kit.lakehouse.objectfs import lance_storage_options, s3_filesystem

    if connection.is_estate_default:
        endpoint = configured_endpoint()
        if not endpoint:
            return pafs.S3FileSystem()
        scheme, _, host = endpoint.partition("://")
        return pafs.S3FileSystem(endpoint_override=host or endpoint, scheme=scheme or "http")
    options = lance_storage_options(connection.endpoint or "", connection.access_key or "", connection.secret_key or "", connection.region)
    if not (connection.access_key and connection.secret_key):
        # A registered store that declares no secret shares the deployment's credentials, so pyarrow's
        # own AWS_* chain supplies them. The keys are REMOVED rather than left empty: `s3_filesystem`
        # reads them with `.get`, and an empty string is a credential pyarrow will sign with.
        options.pop("access_key_id", None)
        options.pop("secret_access_key", None)
    return s3_filesystem(options)


def reset_connection_cache() -> None:
    """Drop the memoized credentials and clients.

    Exists for tests, and named as such rather than dressed up: both caches are process-wide by
    design (a boto3 client owns a connection pool), so a test asserting on how a client was BUILT
    would otherwise read the one another test memoized.
    """
    _store_credentials.cache_clear()
    _memoized_client.cache_clear()
