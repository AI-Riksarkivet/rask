"""Application settings (pydantic-settings).

Produces the ``properties`` map for the native ``lance_namespace`` backend and
the ``storage_options`` for direct pylance access. The catalog's own values come
from ``LANCE_*`` environment variables; the OIDC/FGA knobs are the estate's and
come from ``GovernedAuthSettings`` under ``RASK_OIDC_*`` / ``RASK_FGA_*``.
Object-store credentials are required (no silent fallback) so a misconfigured
deployment fails fast at boot.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.principal import PrincipalKey
from service_kit.governed.settings import GovernedAuthSettings, SignatureDoorSettings
from service_kit.governed.store_credential import S3KeyPair
from service_kit.governed.store_session import WebIdentity, held_session
from service_kit.lakehouse.endpoint_scheme import allow_http_for
from service_kit.lakehouse.objectfs import StoreTimeouts


if TYPE_CHECKING:
    # Type-only: `lance` is a heavy import and this module is read by every catalog process at boot.
    import lance


_STORAGE_PREFIX = "storage."

#: Which object-store budget a connection runs under (`CatalogReadBounds`).
StorePlane = Literal["metadata", "data"]


class LanceSessionCaps(BaseSettings):
    """The two Lance cache ceilings, and NOTHING else — so opening a dataset costs no credential.

    SPLIT OUT OF :class:`Settings` DELIBERATELY. `shared_lance_session` needs exactly these two
    integers, both defaulted, yet it built the whole catalog `Settings` to read them — and that model
    REQUIRES `LANCE_S3_ACCESS_KEY_ID` / `LANCE_S3_SECRET_ACCESS_KEY`. So every local `dir`-backend open
    was coupled to credentials it never uses, and a process that legitimately holds none could not open
    a dataset at all.

    MEASURED 2026-09-16, which is what made it a defect rather than an inefficiency: five tests in
    `services/maintenance/tests/test_compaction_runs_off_the_pod.py` and three in
    `tests/integration/test_create_properties_land_on_the_dataset.py` failed in isolation and passed
    only when an unrelated module had already put those variables in the environment — the suite's
    result depended on collection order. The alternative fix was to hand the suite fake credentials,
    which would have taught it to be comfortable with a credential arriving through the environment.
    The estate's rule is that a secret reaches a workload from ESO, the Dapr secret store or STS and
    never from env, so a test harness that needs one to open a local file is the wrong shape to
    normalise.

    Inherited by `Settings` rather than duplicated: two definitions of one default is the failure this
    estate keeps finding, and the aliases stay the single spelling operators already set.
    """

    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="LANCE_", extra="ignore")

    #: The bounded Lance caches this process is willing to hold (#102 / [[LH-096]]). Lance's own defaults
    #: are 1 GiB metadata + 6 GiB index, which dwarf the 512 Mi limit every lakehouse pod runs under —
    #: and a bare `lance.dataset()` mints those ceilings per open and discards them WITH the handle, so
    #: the cache never engages at all. Named settings rather than literals because a literal cannot
    #: track `resources.limits.memory`: raise the pod and these should follow, lower it and they must.
    #: `shared_lance_session` clamps them to the container on top of whatever is set here.
    lance_metadata_cache_mb: int = Field(default=128, ge=8, alias="LANCE_METADATA_CACHE_MB")
    lance_index_cache_mb: int = Field(default=256, ge=8, alias="LANCE_INDEX_CACHE_MB")


class CatalogStorageSettings(BaseSettings):
    """The object store this catalog reads and writes through.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    #: The scheme here is the ONLY source of `storage.allow_http` (see `namespace_properties`).
    #: It was a separate bool the chart pinned to "true" for every deployment, which let a TLS
    #: endpoint keep permission to fall back to plaintext — silent, because object_store consults
    #: the flag only when a request would otherwise be refused. One fact, so the two cannot disagree.
    s3_endpoint: str = Field(default="http://rask-rustfs:9000", alias="LANCE_S3_ENDPOINT")
    #: THE CATALOG'S IDENTITY AT THE OBJECT STORE ([[XC-083]], ADR 0170): the ServiceAccount token its pod projects for
    #: the store's audience. Set, the catalog's own IO and every vend sign with sessions STS exchanges it for
    #: (`AssumeRoleWithWebIdentity` at `s3_sts_endpoint`), and the catalog reads no key pair from anywhere.
    s3_web_identity_token_file: Path | None = Field(default=None, alias="LANCE_S3_WEB_IDENTITY_TOKEN_FILE")
    #: How long a session for the catalog's own IO lasts. It is renewed at half its life, so a request that took the
    #: old one still has half of it to finish. RustFS takes 900 to 43,200 s (`rustfs@1.0.3:rustfs/src/admin/handlers/sts.rs:64-83`).
    s3_session_seconds: int = Field(default=3600, ge=900, le=43200, alias="LANCE_S3_SESSION_SECONDS")
    #: The key pair the catalog signs with when it holds no token: the offline and Dagger lanes' fixture, with the secret
    #: store off. A missing id is refused here and a missing secret by the lifespan.
    s3_access_key_id: str = Field(default="", alias="LANCE_S3_ACCESS_KEY_ID")
    s3_secret_access_key: SecretStr = Field(default=SecretStr(""), alias="LANCE_S3_SECRET_ACCESS_KEY")
    s3_region: str = Field(default="us-east-1", alias="LANCE_S3_REGION")
    s3_virtual_hosted: bool = Field(default=False, alias="LANCE_S3_VIRTUAL_HOSTED")
    #: The role the token is exchanged for. AWS assumes it; a store that grants the policy the token's subject names
    #: (RustFS, measured in `docs/audits/2026-10-18/rustfs_web_identity_measure.py`) ignores it, and boto3 requires one.
    s3_assume_role_arn: str = Field(default="arn:aws:iam::000000000000:role/lance-vend", alias="LANCE_S3_ASSUME_ROLE_ARN")
    #: ENCRYPTION AT REST, carried INTO the vended credential (§ J5). A client vended credentials writes
    #: straight to object storage with the catalog out of the path, so whatever the estate intends must
    #: travel in the storage options or it does not happen.
    #:
    #: Key names and their contract are verified against `lance_docs/guide.md:2667-2669`, and
    #: `lance_storage_options` REFUSES an invalid pairing rather than passing it on: object_store drops
    #: an option it does not recognise, so a wrong value here would land plaintext under a config that
    #: claims encryption — the one failure with no later signal.
    #:
    #: Unset is every deployment's behaviour today: whatever the bucket does.
    s3_server_side_encryption: str | None = Field(default=None, alias="LANCE_S3_SERVER_SIDE_ENCRYPTION")
    s3_sse_kms_key_id: str | None = Field(default=None, alias="LANCE_S3_SSE_KMS_KEY_ID")
    s3_sse_bucket_key_enabled: bool | None = Field(default=None, alias="LANCE_S3_SSE_BUCKET_KEY_ENABLED")
    s3_sts_endpoint: str | None = Field(default=None, alias="LANCE_S3_STS_ENDPOINT")
    #: The object-store endpoint every vended credential names, for a client reaching the store from where it
    #: runs ([[LH-489]]). The catalog's own `s3_endpoint` may be an address only the cluster resolves
    #: (`rask-rustfs:9000`), and pylance applies a vended `endpoint` over the client's own (measured on pylance
    #: 13.0.0, both `endpoint` and `aws_endpoint` lose), so a client off the cluster writes only to what is
    #: vended here. Unset, the catalog vends its own `s3_endpoint`. In production the platform names the
    #: store's public address.
    s3_vended_endpoint: str | None = Field(default=None, alias="LANCE_S3_VENDED_ENDPOINT")

    @property
    def vended_store_endpoint(self) -> str:
        """The endpoint a vended credential names: `s3_vended_endpoint`, else the catalog's own."""
        return self.s3_vended_endpoint or self.s3_endpoint

    @property
    def web_identity(self) -> WebIdentity | None:
        """The catalog's identity at the store, or ``None`` when it signs with the environment's pair."""
        if self.s3_web_identity_token_file is None:
            return None
        return WebIdentity(token_file=self.s3_web_identity_token_file, endpoint=self.s3_sts_endpoint, region=self.s3_region, role_arn=self.s3_assume_role_arn)


class CatalogReadBounds(BaseSettings):
    """How long one object-store call, and how much memory one read, may cost this process ([[LH-247]]).

    The catalog serves requests, so a store that stops answering must cost the caller a prompt 503 and
    not a worker held for minutes, and a read whose answer is sized by the DATA must cost a bounded
    amount of the pod. Named settings rather than literals for the reason `LanceSessionCaps` gives: they
    must follow `resources.limits.memory` and the store's latency, which a literal cannot.
    """

    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="LANCE_", extra="ignore")

    #: Object-store call bounds, in TWO PLANES (`service_kit.lakehouse.objectfs.StoreTimeouts` holds the
    #: measurements). object_store's request timeout covers the WHOLE request, body included, so one
    #: budget cannot serve both a manifest read and a 5 MiB upload part.
    #:
    #: THE METADATA PLANE (`store_*`) is the native namespace connection: every describe, list, declare
    #: and version lookup, which is also the first store call every data door makes, so a store that has
    #: stopped answering is found here and answered 503. NO CLIENT RETRY: Lance re-issues a failed
    #: listing itself (six attempts per open, measured on pylance 12.0.0), so a client retry multiplies
    #: every outage by six more, and a 503 is retryable by the caller. Measured with these defaults on
    #: pylance 12.0.0: a dataset open against a store that drops packets failed in 14.5 s, against one
    #: that never answers in 32.8 s.
    store_connect_timeout_s: float = Field(default=2.0, gt=0, alias="LANCE_STORE_CONNECT_TIMEOUT_S")
    store_request_timeout_s: float = Field(default=5.0, gt=0, alias="LANCE_STORE_REQUEST_TIMEOUT_S")
    store_max_retries: int = Field(default=0, ge=0, alias="LANCE_STORE_MAX_RETRIES")
    store_retry_window_s: int = Field(default=5, ge=0, alias="LANCE_STORE_RETRY_WINDOW_S")
    #: THE DATA PLANE (`data_store_*`, with the same connect bound) is every handle that moves rows: the
    #: pylance opens the doors read and write through (`StorageOptionsDep`) and the native connection the
    #: native data operations run on (`DataNamespaceDep`). Its request bound is sized to the transfer, not
    #: to a metadata call: measured on pylance 12.0.0 through a proxy capping a moto store at 8 MB/s, a
    #: 100 MB write took 14.1 s and a 100 MB full read 13.3 s under these defaults, and BOTH FAILED under
    #: the metadata plane's (a 5 MiB part ran its 5 s and was cut off). The connect bound still finds a
    #: store that has dropped off the network in seconds.
    data_store_request_timeout_s: float = Field(default=120.0, gt=0, alias="LANCE_DATA_STORE_REQUEST_TIMEOUT_S")
    data_store_max_retries: int = Field(default=3, ge=0, alias="LANCE_DATA_STORE_MAX_RETRIES")
    data_store_retry_window_s: int = Field(default=300, ge=0, alias="LANCE_DATA_STORE_RETRY_WINDOW_S")

    #: Lance's process scan defaults, applied at boot by `catalog.main.bound_lance_scans` to every scan
    #: this process builds: the native backend's (`/query`, `analyze_plan`), which take no scanner
    #: option from Python, and the change feed's. Lance's own are a 2 GB IO buffer
    #: (`lance_docs/guide.md:3446-3448`) and 8,192-row batches whatever their width, which is 32 MiB of
    #: 1024-dim float32 vectors per batch (`guide.md:3453-3461`, which suggests 1,024 rows for those).
    scan_io_buffer_mb: int = Field(default=32, ge=1, alias="LANCE_SCAN_IO_BUFFER_MB")
    scan_batch_rows: int = Field(default=1024, ge=1, alias="LANCE_SCAN_BATCH_ROWS")
    #: The most bytes one `/query` answer may carry. The native backend materialises the whole answer
    #: in one buffer before the first byte is sent, so `k` (plus `offset`) is capped by the projected
    #: row width to stay inside this.
    query_max_response_mb: int = Field(default=16, ge=1, alias="LANCE_QUERY_MAX_RESPONSE_MB")

    @property
    def store_timeouts(self) -> StoreTimeouts:
        return StoreTimeouts(
            connect_s=self.store_connect_timeout_s,
            request_s=self.store_request_timeout_s,
            max_retries=self.store_max_retries,
            retry_window_s=self.store_retry_window_s,
        )

    @property
    def data_store_timeouts(self) -> StoreTimeouts:
        return StoreTimeouts(
            connect_s=self.store_connect_timeout_s,
            request_s=self.data_store_request_timeout_s,
            max_retries=self.data_store_max_retries,
            retry_window_s=self.data_store_retry_window_s,
        )


class CatalogControlBusSettings(BaseSettings):
    """The control lane: where governance events are published and staged.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    control_root: str = Field(default="", alias="LANCE_CONTROL_ROOT")
    # Control-plane change-events (opt-in, best-effort — the governance/metadata stream, distinct from the
    # OpenLineage data events above). When on, mutations publish a `CatalogControlEvent` onto the DEDICATED
    # `control_pubsub` component (below, NOT the shared `dapr_pubsub`) under the `catalog.control.v1` topic,
    # subscribed WITHOUT a queueGroupName so every replica buffers every event for the poll endpoint. Off by
    # default, like lineage.
    control_emit_enabled: bool = Field(default=False, alias="LANCE_CONTROL_EMIT_ENABLED")
    control_emit_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="LANCE_CONTROL_EMIT_TIMEOUT_SECONDS")
    # A DEDICATED Dapr pub/sub component (NOT the shared lineage one) with NO queueGroupName, so the
    # catalog's own subscription is a BROADCAST — every replica receives every event → each replica's ring
    # buffer stays complete. The chart renders it (dapr-component.yaml) + a NATS stream (nats-stream-job).
    control_pubsub: str = Field(default="catalog-control-pubsub", alias="LANCE_CONTROL_PUBSUB")
    # The bounded per-replica ring buffer size (events retained for `GET /management/v1/events`); a client whose cursor
    # fell off the end (overflow) gets `reset: true` → the console `invalidateAll()`s.
    control_buffer_size: int = Field(default=512, ge=1, alias="LANCE_CONTROL_BUFFER_SIZE")
    #: The CONTROL lane's outbox prefix — and it must NOT be the lineage one. Each prefix is drained
    #: by a lane-specific relay that re-ingests what it finds, so sharing would feed each relay the
    #: other lane's events.
    #:
    #: Why the control lane needs one at all: `DaprControlEmitter.emit` swallows a publish failure by
    #: design — it is called after the change is already made and audited, so raising would turn a
    #: delivered mutation into a 500 — so unstaged, a NATS blip loses the event with every pod green.
    #: Staged, the event survives to be re-published.
    control_outbox_uri: str = Field(default="", alias="LANCE_CONTROL_OUTBOX_URI")
    #: The cron binding that DRAINS `control_outbox_uri` — `catalog/api/control_relay.py`. Empty = the
    #: relay route is not mounted (staging still happens; nothing republishes it).
    #:
    #: THIS STRING IS THE ROUTE. Dapr delivers an input binding to `POST /<component name>` at the pod
    #: ROOT, so the Component's `metadata.name`, this value and the served path are one string; a
    #: component named one thing and a route mounted at another is a cron that ticks into a 404 while
    #: both halves look right in isolation. Pinned by `tests/unit/test_the_control_lane_relay_is_wired.py`.
    control_relay_binding_name: str = Field(default="", alias="LANCE_CONTROL_RELAY_BINDING_NAME")


class CatalogDaprSettings(BaseSettings):
    """Which Dapr components this catalog addresses, and where its secrets come from.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    # Secret consumption — when on, every secret the catalog reads is the Dapr secret store's (OpenBao): its signing key,
    # its app token and each data base's credential, never plaintext env, and a store miss FAILS CLOSED (no env
    # fallback). Its object-store identity is then its ServiceAccount token (`s3_web_identity_token_file`), which the
    # validator below requires: no key pair of the catalog's own is in the store either.
    secrets_from_dapr: bool = Field(default=False, alias="LANCE_SECRETS_FROM_DAPR")
    #: THE ONE STORE, NAMED ONCE (DUP-17). The estate runs a single Dapr secret-store component and
    #: seven env vars named it, each defaulting to the same literal and none of them set by the chart —
    #: so repointing the store meant finding all seven. `RASK_SECRET_STORE` is the estate-wide name
    #: (already what viewer and ingest read); the per-service alias stays FIRST so a single service can
    #: still be moved on its own.
    dapr_secret_store: str = Field(default="lance-secrets", validation_alias=AliasChoices("LANCE_DAPR_SECRET_STORE", "RASK_SECRET_STORE"))
    #: The field of a data base's credential secret (`LANCE_MULTIBASE_BASE_CREDENTIAL_REFS`) holding its secret key
    #: ([[LH-067]]); the key id rides the same secret (`catalog.services.base_credentials`).
    dapr_secret_s3_field: str = Field(default="secret_key", alias="LANCE_DAPR_SECRET_S3_FIELD")
    # Dapr pub/sub transport (used when lineage_transport == "dapr"). The component name is what the
    # sidecar resolves to NATS JetStream; the topic is versioned (a breaking schema change → a new .vN).
    dapr_pubsub: str = Field(default="lineage-pubsub", alias="LANCE_DAPR_PUBSUB")
    dapr_topic: str = Field(default="lineage.events.v1", alias="LANCE_DAPR_TOPIC")
    # Injected into every app container by the Dapr sidecar injector — read it rather than restate it, so a
    # non-default sidecar port cannot silently point us at a port nothing is listening on.
    dapr_http_port: int = Field(default=3500, ge=1, le=65535, alias="DAPR_HTTP_PORT")


class CatalogLineageSettings(BaseSettings):
    """How a write's provenance leaves this service.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    # Lineage emission (opt-in). When enabled, the catalog emits an OpenLineage event to the lineage
    # service on a table write — fire-and-forget + best-effort, so the lineage service being down can
    # never block or fail a catalog write. The catalog is the only component that knows the verified
    # principal, so it is the authoritative source of "who created/changed a table" (author = token.subject).
    # Transport: ``http`` (direct POST — dev / external producers) or ``dapr`` (publish to the Dapr
    # ``pubsub.jetstream`` component via the local sidecar — durable, decoupled, the production path).
    lineage_emit_enabled: bool = Field(default=False, alias="LANCE_LINEAGE_EMIT_ENABLED")
    # Literal (like vending_mode) so pydantic validates the allowed set — one enum-setting idiom in this file.
    lineage_transport: Literal["http", "dapr"] = Field(default="http", alias="LANCE_LINEAGE_TRANSPORT")
    lineage_url: str | None = Field(default=None, alias="LANCE_LINEAGE_URL")
    lineage_emit_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="LANCE_LINEAGE_EMIT_TIMEOUT_SECONDS")
    lineage_job_namespace: str = Field(default="lance-catalog", alias="LANCE_LINEAGE_JOB_NAMESPACE")
    #: Stage lineage events here before publishing (docs/RESILIENCE.md gap #1, the estate's #1
    #: weakness). EMPTY = today's behaviour exactly: `publish_lineage_with_outbox` degrades to a plain
    #: publish when this is unset, so the wiring is inert until a deployment opts in.
    #:
    #: Why it matters HERE specifically: the catalog's emit is inline-awaited and best-effort AFTER the
    #: Lance write commits, so a crash between the write and the publish loses the event. The data
    #: exists and the graph never learns of it — and because the catalog's write announcement is what a
    #: user's pipeline reads off lineage's `GET /events` to start its next hop, a lost one does not merely
    #: under-report provenance: the next tier's run silently never happens.
    lineage_outbox_uri: str = Field(default="", alias="LANCE_LINEAGE_OUTBOX_URI")
    #: The namespace this catalog keeps for provenance (ADR 0158): lineage's commit feed and edge list are tables
    #: in it, governed like any other. The catalog creates it at boot, protects it, and never announces a write
    #: under it on lineage, because lineage records every announcement as a row of those tables. Empty, the code
    #: default, keeps no such namespace; the chart names one whenever it deploys lineage.
    provenance_namespace: str = Field(default="", alias="LANCE_PROVENANCE_NAMESPACE")
    #: The principals that write the provenance namespace: lineage's service-account principal
    #: (`kubernetes~system:serviceaccount:<ns>:<sa>`). Each is granted `writer` on the namespace at boot, which
    #: reaches every table in it and nothing outside it. A bare name is refused when the settings are read.
    provenance_writers: list[PrincipalKey] = Field(default_factory=list, alias="LANCE_PROVENANCE_WRITERS")
    #: The principals that compact the provenance namespace's tables: a bring-your-own table-maintenance job's
    #: service-account principal. Each is granted `reader` and `maintainer` on the namespace at boot, what a compaction
    #: needs (it reads every row and rewrites how they are stored), and never `writer`, so no commit of its changes what
    #: the provenance says; the write tier it is vended can still put over a committed data file, as a writer's can
    #: (`vending.build_session_policy`). A principal no longer named loses both at the next boot.
    provenance_maintainers: list[PrincipalKey] = Field(default_factory=list, alias="LANCE_PROVENANCE_MAINTAINERS")
    #: The versions maintenance keeps of each provenance table: the namespace's maintenance policy (keep the last N),
    #: which the catalog writes at boot, so the history every open of those tables lists stays short ([[LH-529]]). No
    #: lineage read holds a version longer than one read. Unset, the catalog removes the policy it wrote while it stands as
    #: written, and the sweep's global retention applies; a hold set through the policy door stands.
    provenance_retain_versions: int | None = Field(default=None, ge=1, le=100_000, alias="LANCE_PROVENANCE_RETAIN_VERSIONS")


class CatalogMaintenanceSettings(BaseSettings):
    """The maintenance lane this catalog hands work to.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    # Maintenance: when true, reject mutating /v1 requests with 503 + Retry-After — for
    # zero-downtime model/schema migration windows. Default off (no-op).
    maintenance_read_only: bool = Field(default=False, alias="LANCE_MAINTENANCE_READ_ONLY")
    #: ADR 0138's grace period: how long after an erasure's notice its pinning tags and branches are released
    #: (`catalog.services.erasure_release`). An owner-set chart value; nothing cancels or extends one release.
    erasure_release_grace_days: int = Field(default=7, ge=0, alias="LANCE_ERASURE_RELEASE_GRACE_DAYS")
    #: How long after an erasure obligation is recorded it is due ([[LH-493]], ADR 0163). GDPR asks for erasure "without
    #: undue delay and in any event within one month" (Art. 12(3)). An owner-set chart value; an obligation still open past
    #: it is shown overdue, and nothing in the catalog acts on it.
    erasure_deadline_days: int = Field(default=30, ge=1, alias="LANCE_ERASURE_DEADLINE_DAYS")
    #: The cron binding that releases what is due (`catalog/api/erasure_release.py`). The Component's name,
    #: this value and the served path are one string, as for the control relay. Empty = no release route, so a
    #: deployment without the Component schedules releases that nothing performs: the chart renders both.
    erasure_release_binding_name: str = Field(default="", alias="LANCE_ERASURE_RELEASE_BINDING_NAME")
    #: The cron binding that expires promotion holds past their review deadline (`catalog/api/promotion_expiry.py`,
    #: ADR 0156 C1). One string with the Component's name and the served path, as for the erasure release. Empty =
    #: no expiry route, so a hold waits until it is decided: the chart renders both.
    promotion_expiry_binding_name: str = Field(default="", alias="LANCE_PROMOTION_EXPIRY_BINDING_NAME")
    #: The maintenance sweep's cleanup window (`maintenance.olderThanDays`), which an erasure report quotes as
    #: the last condition before the subject's bytes leave storage. Read here only to say it; the sweep owns it.
    erasure_cleanup_window_days: int = Field(default=7, ge=0, alias="LANCE_ERASURE_CLEANUP_WINDOW_DAYS")
    #: How old the maintenance sweep's clone list may be when ``version/create`` judges a compaction against it
    #: (``service_kit.lakehouse.maintenance_due``, ADR 0162). An older list means the sweep has stopped, and a
    #: dataset cloned since is unknown, so the compaction is refused; the sweep writes one every tick.
    clone_list_max_age_seconds: int = Field(default=3600, ge=60, alias="LANCE_CLONE_LIST_MAX_AGE_SECONDS")


class CatalogUserStateSettings(BaseSettings):
    """The per-user state store behind the estate's own UI preferences.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    # Per-subject user state (`GET/PUT/DELETE /management/v1/user-state/*`) on the Dapr state store. The default names
    # the component the chart already renders (`stateStore.name` in chart/values.yaml) and that the catalog
    # app-id is already in the `scopes` of, so this needs NO new chart value; tests/unit/test_invariants.py
    # asserts that agreement so a rename or a dropped scope reddens rather than 503ing in production.
    user_state_store: str = Field(default="lance-statestore", alias="LANCE_USER_STATE_STORE")
    user_state_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="LANCE_USER_STATE_TIMEOUT_SECONDS")
    # A per-document ceiling. `max_body_bytes` is sized for Arrow-IPC writes (256 MiB), which is four orders
    # of magnitude past anything a canvas or a saved-view list can legitimately be — and this is an
    # unmetered per-user write surface onto a SHARED Postgres, so it gets its own honest bound.
    user_state_max_bytes: int = Field(default=512 * 1024, ge=1, alias="LANCE_USER_STATE_MAX_BYTES")


class CatalogVendingSettings(BaseSettings):
    """Credential vending: which mode, and how long a vended credential lives.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    # Data-plane credential vending (see services/catalog/core/vending.py). Target = S3-compatible storage (RustFS in
    # development, AWS S3, Ceph RGW). Default "mode_b": server-mediated — no credential leaves the catalog.
    # "web_identity": every vend is a short-TTL session STS exchanges the catalog's OWN token for, narrowed to one table
    # and tier by an inline session policy (ADR 0170), so the vend's parent is the catalog's ServiceAccount and no key.
    # Client-DIRECT writes stage a manifest the catalog judges and finalizes (CreateTableVersion, ADR 0157); the vending
    # MODE is the separate CREDENTIAL mechanism. The chart turns `web_identity` on with the catalog's token mounted.
    vending_mode: Literal["mode_b", "web_identity"] = Field(default="mode_b", alias="LANCE_VENDING_MODE")
    vending_ttl_seconds: int = Field(default=900, ge=60, alias="LANCE_VENDING_TTL_SECONDS")


class CatalogAuthzSettings(BaseSettings):
    """Who this catalog trusts beyond the estate-wide rungs `GovernedAuthSettings` carries.

    Split out of the 464-line `Settings` ([[LH-113]]) so one domain's configuration can be read,
    changed and reasoned about without the other six in the way. Composed onto `Settings` exactly as
    `LanceSessionCaps` and the estate-wide `GovernedAuthSettings` already are, so `Settings` exposes
    every one of its fields, aliases and defaults.
    """

    #: THE IDENTITY THIS CATALOG SIGNS THE LINEAGE IT EMITS AS ([[LH-064]]), one variable on every signer
    #: (`RASK_SIGNING_IDENTITY`). With `secrets_from_dapr` it decides whether the catalog signs at all: its private
    #: key is `signing-key-<its key name>` in the store (`signing_key_name`). Empty is an estate with no store to sign from, and it emits
    #: unsigned. On the estate it is the catalog's own service-account subject
    #: (`kubernetes~system:serviceaccount:<ns>:<sa>`), the subject its signed events are authored as.
    signing_identity: str = Field(default="", alias="RASK_SIGNING_IDENTITY")
    #: The MAINTENANCE identities — the sweep, and anything else that rewrites HOW a dataset is stored. Each is a
    #: PRINCIPAL (on the estate, maintenance's service-account principal), refused when it is a bare name; the tuple
    #: names the user it is (`fga.user_object`).
    #:
    #: Maintenance compacts, optimizes indices and reclaims versions, and must hold ``maintainer`` and nothing
    #: else. The model keeps it apart from the data rungs in both directions (``can_maintain`` neither implies
    #: nor is implied by ``can_write_data``).
    #:
    #: Granted at the WAREHOUSE: ``namespace`` and ``table``
    #: both define ``maintainer ... or maintainer from parent``, so one tuple per tenant reaches every
    #: tier and every table below it, and the sweep's reach stays enumerable.
    #:
    #: EMPTY BY DEFAULT, on the same terms: an estate declaring nothing keeps exactly today's tuples.
    fga_maintainers: list[PrincipalKey] = Field(default_factory=list, alias="LANCE_FGA_MAINTAINERS")
    # When False (default), any authenticated caller may create a TOP-LEVEL namespace/
    # table and becomes its owner (the "users create their own workspaces" model). When
    # True, top-level creation also requires can_create_* on fga_root_object — an admin-gated
    # root that must be seeded to bootstrap. Nested creation always gates on the parent.
    fga_lock_root_create: bool = Field(default=False, alias="LANCE_FGA_LOCK_ROOT_CREATE")


class Settings(
    GovernedAuthSettings,
    SignatureDoorSettings,
    LanceSessionCaps,
    CatalogStorageSettings,
    CatalogReadBounds,
    CatalogControlBusSettings,
    CatalogDaprSettings,
    CatalogLineageSettings,
    CatalogMaintenanceSettings,
    CatalogUserStateSettings,
    CatalogVendingSettings,
    CatalogAuthzSettings,
    BaseSettings,
):
    """Catalog + object-store configuration sourced from ``LANCE_*`` env vars.

    The OIDC/FGA knobs are NOT declared here. They are the estate's shared vocabulary and arrive from
    `GovernedAuthSettings` under `RASK_OIDC_*` / `RASK_FGA_*`; this class once carried a byte-identical
    twin of them, which is precisely how the copies drifted. So do the control door's mode and signer sets, from
    `SignatureDoorSettings` (`RASK_SIGNATURE_DOORS`, `RASK_CONTROL_SIGNER_ROLES`): the subscription in
    `api/dapr.py` verifies each event against them before it acts ([[CTL-32]])."""

    # `populate_by_name` also teaches the env source the bare FIELD NAME as a second lookup, so
    # every alias below silently gained an un-namespaced twin (MedallionSettings.ray_address
    # answered to Ray's own $RAY_ADDRESS). `env_prefix` redirects that fallback onto the
    # namespace the aliases already declare; an explicit alias bypasses it, so the
    # deliberately-bare ones (DAPR_HTTP_PORT) still land.
    # See tests/unit/test_settings_env_namespace.py.
    model_config = SettingsConfigDict(populate_by_name=True, env_prefix="LANCE_", extra="ignore")

    # Catalog
    impl: str = Field(default="dir", alias="LANCE_REST_IMPL")
    root: str = Field(default="s3://lance-catalog", alias="LANCE_REST_ROOT")
    #: The path prefix the platform's Gateway publishes the catalog under and forwards unrewritten (the chart's
    #: `rask.catalogPublicPrefix`, `/api/catalog`), which the catalog strips itself from the bytes its client sent
    #: (`catalog.api.path_reading`, [[LH-526]]). A route rewriting the prefix instead rebuilds the path from its decoded
    #: form, so the `%2B` and `%2F` a name is judged by reach the catalog as `+` and `/` (measured on Traefik 3.7.8, whose
    #: URLRewrite sets `RawPath` from `EscapedPath()`). Empty, nothing is stripped: an in-cluster caller sends no prefix.
    public_path_prefix: str = Field(default="", pattern=r"^(/[^/]+)*$", alias="LANCE_PUBLIC_PATH_PREFIX")
    #: OFF by default. It defaulted to True and NO deployment path ever set it — `grep -rn DOCS
    #: chart/ .docker/ scripts/` matched nothing — so the flag documented a choice nobody was making
    #: and the schemas shipped openly. A security default every deployment must remember to disable is
    #: one nobody disables. Turn it on per-environment (the chart's dev values do).
    docs_enabled: bool = Field(default=False, alias="LANCE_REST_DOCS")
    # Allow blob-v2 columns to reference EXTERNAL objects outside the dataset root (Blob.from_uri) on
    # create. Default off for two reasons: (1) an external pointer's bytes live outside the lakehouse, so
    # Lance's version-aware GC can't protect them and a source delete dangles the pointer; (2) SSRF — the
    # client controls the pointer URI and the cascade's read_blobs would fetch it with the medallion
    # service's network position + creds, so an arbitrary URI (cloud metadata, internal host) becomes a
    # server-side read primitive. Enable only for a trusted producer; Lance's REGISTERED EXTERNAL BASES
    # (approved base paths) are the finer-grained alternative. Managed/inline blobs (bytes in) always work.
    allow_external_blobs: bool = Field(default=False, alias="LANCE_ALLOW_EXTERNAL_BLOBS")
    # The SAFER external-blob posture: a comma-separated allowlist of approved base URIs. A create that
    # names an external base (the door's ``external_blob_base`` parameter) registers that ONE base, and only
    # when it lies inside one of these and outside every governed root and bucket
    # (``table_bases.requested_external_blob_base``); a create that names none registers none. External
    # ``Blob.from_uri`` pointers then resolve only under that base, while
    # ``allow_external_blob_outside_bases`` stays False. Read beside that by the base judge: a plain base
    # declared inside one of these is sanctioned for reads and never granted by a vend ([[LH-209]]).
    # Empty = no external bases.
    external_blob_bases: str = Field(default="", alias="LANCE_EXTERNAL_BLOB_BASES")

    @property
    def external_blob_base_list(self) -> list[str]:
        """The registered external-blob base URIs (parsed from the comma-separated allowlist)."""
        return [b.strip() for b in self.external_blob_bases.split(",") if b.strip()]

    # #3-B Lance multi-base DATA distribution: a comma-separated ALLOWLIST of approved data-base URIs a
    # create may spread its fragments across (the Uber pattern — one table's data round-robins over N
    # buckets, manifest stays in the primary root, reads fan out). A per-request ``data_base`` MUST be on
    # this list — a caller can never point a base at an arbitrary bucket (data-exfil / rogue-write door).
    # Empty (default) = the feature is off and every create is byte-identical to today.
    # A table registers its OWN directory beneath each base it names (`<base>/<directory>`,
    # `dataplane.table_data_bases`), and a vend grants that directory and never the base ([[LH-252]]).
    # Every base here shares the catalog's S3 ENDPOINT: a base's own credential
    # (`LANCE_MULTIBASE_BASE_CREDENTIAL_REFS`) replaces the key pair and never the address.
    multibase_data_bases: str = Field(default="", alias="LANCE_MULTIBASE_DATA_BASES")

    @property
    def multibase_data_base_list(self) -> list[str]:
        """The approved multi-base data-distribution base URIs (parsed from the comma-separated allowlist)."""
        return [b.strip() for b in self.multibase_data_bases.split(",") if b.strip()]

    # [[LH-067]] Which SECRET each data base's credential comes from: `base-uri=secret-ref` pairs, comma
    # separated. A NAME, never material — the reference identifies a key in the Dapr secret store, which
    # is what lets this live in configuration at all under the estate's secrets rule.
    #
    # A base with a reference here, and every table directory beneath it, is written with its own
    # credential (`dataplane._write_blob`) and opened with it at every pylance open
    # (`core.namespace.open_location`, composed from the manifest's base paths). The native data doors
    # cannot carry it and refuse such a table (`core.namespace.judged_native_version`).
    # Empty (default) = every base keeps the estate's options and every write is byte-identical.
    multibase_base_credential_refs: str = Field(default="", alias="LANCE_MULTIBASE_BASE_CREDENTIAL_REFS")

    @property
    def multibase_base_credential_ref_map(self) -> dict[str, str]:
        """`{base_uri: secret_ref}`, parsed HERE so nothing else splits this string.

        A second parser is a second thing that can disagree about the separator, about whitespace, or
        about what an empty entry means — the reason `multibase_data_base_list` is a property too.

        BOTH MALFORMED SHAPES RAISE rather than dropping the entry, because a reference that silently
        vanishes leaves its base on the estate credential and looks correct — which is exactly the
        failure this whole axis exists to prevent. A repeated base raises for the same reason one step
        on: answering "which of these two secrets" by ordering is not an answer an estate should give.
        A reference for a base off ``LANCE_MULTIBASE_DATA_BASES`` raises too: no create can register
        that base, so the reference is configuration that does nothing.
        """
        parsed: dict[str, str] = {}
        for entry in (e.strip() for e in self.multibase_base_credential_refs.split(",")):
            if not entry:
                continue
            if "=" not in entry:
                raise ValueError(f"{entry!r} is not a base=secret-ref pair; LANCE_MULTIBASE_BASE_CREDENTIAL_REFS holds comma-separated `base=secret-ref`")
            base, _, ref = entry.partition("=")
            base, ref = base.strip(), ref.strip()
            if base in parsed:
                raise ValueError(f"base {base!r} is given a credential reference more than once; one base has one secret")
            parsed[base] = ref
        if unlisted := sorted(set(parsed) - set(self.multibase_data_base_list)):
            raise ValueError(f"{unlisted} is not on LANCE_MULTIBASE_DATA_BASES; a credential reference for a base no create can register does nothing")
        return parsed

    # #3-A per-warehouse physical multi-tenancy (admin control plane). When enabled, an admin API
    # (POST /management/v1/warehouses) provisions a physically separate S3 bucket per warehouse and binds top-level
    # namespaces to it, so a table under a bound namespace lands in that warehouse's bucket (not the shared
    # `root`). Default OFF: the request path is unchanged (no binding lookups) for single-bucket deployments;
    # the chart turns it on for the catalog. Additive + backward-compatible — an unbound namespace always
    # routes to the default `root`. The warehouse REGISTRY (records + bindings) lives under `control_root`.
    warehouses_enabled: bool = Field(default=False, alias="LANCE_WAREHOUSES_ENABLED")
    # #75 drop→undrop grace: how long a dropped table stays recoverable in `_trash/`. Stamped onto the
    # record AT DROP TIME (never read at expiry), so shortening this can't retroactively destroy a
    # window a user is still inside.
    #
    # DEFAULT 0 = OFF, and that is a deliberate choice rather than timidity. With a grace period a
    # drop DEREGISTERS instead of deleting, which silently changes what `drop_table` means for every
    # existing caller and diverges from the spec's own "drop removes the data". A deployment opts in
    # to recoverable drops; it is never opted in for them by an upgrade. Set it (7 is the sensible
    # value) on any estate where a fat-fingered drop costs harvested page images. Days, a fraction
    # allowed.
    trash_grace_days: float = Field(default=0, ge=0, le=365, alias="LANCE_TRASH_GRACE_DAYS")
    # Buckets NO warehouse may ever claim, beyond the always-reserved catalog root/registry buckets (audit
    # 2026-07-23: a project admin could register a warehouse over the SHARED bucket — or a medallion zone
    # bucket — and a later project-policy set would then govern every tenant's datasets in it). The chart
    # populates this from the `medallion.buckets` values map (the raw/gold zone buckets); comma-separated,
    # empty = only the root/registry buckets are reserved (the default, byte-identical for single-bucket).
    reserved_buckets: str = Field(default="", alias="LANCE_RESERVED_BUCKETS")

    # #17 model registry (candidate→blessed promotion). Each model's Lance registry dataset lives at
    # ``<models_root>/<model>`` — an explicit location rather than a namespace-resolved one, so the
    # catalog can NOT reach it through native namespace resolution (that would drop the ``/medallion/``
    # prefix): the promote/describe endpoints open it by EXPLICIT URI. Defaults to ``<root>/medallion/models``
    # so a single-bucket deploy needs no extra config; override only when the registry is zoned elsewhere.
    # INVARIANT (operator's): this root MUST share the catalog's S3 endpoint + creds — the read path vends
    # only ``storage_options()`` (same constraint as multibase_data_bases).
    models_registry_root: str = Field(default="", alias="LANCE_MODELS_REGISTRY_ROOT")

    # #17/#92 model ARTIFACTS — the plain-path weights/config objects the registry rows point at. The
    # trainer lays them out OUTSIDE any Lance dataset (``models/<model>/<token>/…`` at the BUCKET root on
    # s3; a ``model-artifacts`` sibling of the registry locally — medallion's ``artifact_base_for``), so
    # the describe read lists them by object listing, never through the registry. Empty = derive the
    # trainer's default layout from ``models_root``; override only when the artifact tree is zoned apart.
    model_artifacts_base: str = Field(default="", alias="LANCE_MODEL_ARTIFACTS_ROOT")

    @property
    def registry_root(self) -> str:
        """Where warehouse records + namespace bindings live (defaults to the catalog `root` bucket)."""
        return self.control_root or self.root

    @property
    def reserved_bucket_set(self) -> frozenset[str]:
        """Bucket names no warehouse may claim: the shared catalog root, the registry/control bucket, the
        model-registry root, every approved multi-base data bucket, and the configured medallion zone
        buckets (``LANCE_RESERVED_BUCKETS``). Platform-owned storage is reserved regardless of zoning —
        a zoned ``models_root`` or a multibase data bucket is exactly as claimable-looking as the shared
        root, and a warehouse over any of them would hand one project governance of every tenant's data
        there. Non-s3 roots (local dev/test ``file://`` registries) contribute nothing — there is no
        bucket to reserve."""
        named = {b.strip() for b in self.reserved_buckets.split(",") if b.strip()}
        for uri in (self.root, self.registry_root, self.models_root, *self.multibase_data_base_list):
            if uri.startswith("s3://") and (bucket := uri.removeprefix("s3://").split("/", 1)[0]):
                named.add(bucket)
        return frozenset(named)

    @property
    def models_root(self) -> str:
        """Root for per-model registry datasets (defaults to ``<root>/medallion/models``)."""
        return self.models_registry_root.rstrip("/") or f"{self.root.rstrip('/')}/medallion/models"

    def model_uri(self, model: str) -> str:
        """The explicit Lance-dataset URI for one model's registry: ``<models_root>/<model>``."""
        return f"{self.models_root}/{model}"

    @property
    def model_artifacts_root(self) -> str:
        """Root of the per-model artifact trees (``<root>/<model>/<token>/…``) — where a client writing the
        registry lays the artifacts its rows point at: on s3,
        ``s3://<bucket>/models`` (the registry's bucket root — pointer targets must never sit inside a
        Lance dataset, see #92); locally, a ``model-artifacts`` sibling under the registry's parent."""
        if self.model_artifacts_base:
            return self.model_artifacts_base.rstrip("/")
        base = self.models_root.rsplit("/", 1)[0]  # the medallion base the registry sits under
        if base.startswith("s3://"):
            bucket = "/".join(base.split("/", 3)[:3])  # s3://<bucket>
            return f"{bucket}/models"
        return f"{base}/model-artifacts"

    def model_artifacts_uri(self, model: str) -> str:
        """One model's artifact tree: ``<model_artifacts_root>/<model>``."""
        return f"{self.model_artifacts_root}/{model}"

    # OIDC authentication + OpenFGA authorization (both opt-in, both inherited from
    # `GovernedAuthSettings`). The app finds the store (or the pinned `RASK_FGA_STORE_ID`) at startup and
    # checks against the model its own image carries, writing that model when the store lacks it.

    # Compliance audit trail (#41): emit a structured event on the dedicated `lance.audit` logger for every
    # security-relevant action — authn success/failure, authz allow/deny, credential vending — carrying
    # who/what/resource/outcome. Default on so a governed deployment records an audit trail out of the box;
    # set false to silence the stream. The events only carry a real subject when OIDC is on.
    audit_enabled: bool = Field(default=True, alias="LANCE_AUDIT_ENABLED")

    # diff2 F10 item 3 — the FLOOR under #46's broadcast eviction of the warehouse-binding cache.
    # That cache is positive-forever (a binding is immutable), and the broadcast that invalidates it
    # on the three mutations which break that premise rides a pub/sub subscription with no
    # dead-lettering: a dropped event leaves an entry nothing will ever evict. Live status reads mean
    # a stale entry cannot route at a deleted bucket, so the residual is persistent 403s on a
    # since-re-bound namespace until restart, plus wrong-bucket routing under warehouse-id reuse — a
    # TTL bounds all of it to a window instead of the process lifetime. Minutes, because this is a
    # backstop for a lost event and not a consistency mechanism; 0 disables it.
    warehouse_binding_cache_ttl_seconds: float = Field(default=300.0, alias="LANCE_WAREHOUSE_BINDING_CACHE_TTL_SECONDS")

    # Max request-body size (bytes) accepted on any route. The Arrow-IPC write endpoints buffer the whole
    # body in memory, so an unbounded POST (e.g. a multi-GB media blob pushed through the catalog instead of
    # the direct storage/vending path) OOMs the worker. The body-limit middleware rejects oversized bodies
    # with 413 BEFORE buffering, steering large payloads to the direct-write path (claim-check). Default
    # 256 MiB — generous for tabular batches + small inline blobs, bounded well under a worker's memory.
    max_body_bytes: int = Field(default=256 * 1024 * 1024, ge=1, alias="LANCE_MAX_BODY_BYTES")

    # Load-shedding: cap CONCURRENT in-flight Arrow-IPC writes. Each buffers up to max_body_bytes, so N
    # concurrent = N × 256MiB — an OOM the memory tier only partly bounds. Over the cap → 429 (THROTTLING),
    # shed before the body is buffered. Generous default (rarely trips); 0 disables it entirely.
    max_concurrent_writes: int = Field(default=16, ge=0, alias="LANCE_MAX_CONCURRENT_WRITES")

    @model_validator(mode="after")
    def _validate_lineage(self) -> Self:
        """Fail fast on incomplete lineage configuration. The auth invariants are the shared mixin's."""
        if self.lineage_emit_enabled and self.lineage_transport == "http" and not self.lineage_url:
            raise ValueError("LANCE_LINEAGE_URL is required for the http lineage transport")
        return self

    @model_validator(mode="after")
    def _validate_store_identity(self) -> Self:
        """Fail closed on a store identity that is absent, doubled, or would send the token to the public AWS STS.

        Without ``LANCE_S3_STS_ENDPOINT`` boto3 resolves the PUBLIC AWS STS endpoint and would POST the catalog's token
        there (audit 2026-07-14), so the endpoint is named, never defaulted. A key pair beside the token is a second
        identity, and one with the secret store on would be a secret in the environment, which the store exists to keep
        out; a deployment reading its secrets from the store signs with its ServiceAccount's sessions alone.
        """
        if self.vending_mode == "web_identity" and self.s3_web_identity_token_file is None:
            raise ValueError("LANCE_VENDING_MODE=web_identity exchanges the catalog's own token: set LANCE_S3_WEB_IDENTITY_TOKEN_FILE")
        if self.s3_web_identity_token_file is not None:
            if not self.s3_sts_endpoint:
                raise ValueError(
                    "LANCE_S3_STS_ENDPOINT is required with LANCE_S3_WEB_IDENTITY_TOKEN_FILE (else the token would be sent to the public AWS STS endpoint)"
                )
            if self.s3_access_key_id or self.s3_secret_access_key.get_secret_value():
                raise ValueError(
                    "a key pair beside LANCE_S3_WEB_IDENTITY_TOKEN_FILE is a second identity: the catalog signs only with sessions its token is exchanged for"
                )
            return self
        if self.secrets_from_dapr:
            raise ValueError(
                "with LANCE_SECRETS_FROM_DAPR the catalog's object-store identity is its ServiceAccount token: set LANCE_S3_WEB_IDENTITY_TOKEN_FILE"
            )
        if not self.s3_access_key_id:
            raise ValueError("LANCE_S3_ACCESS_KEY_ID is required when the catalog holds no projected token (LANCE_S3_WEB_IDENTITY_TOKEN_FILE)")
        return self

    def s3_key_pair(self) -> S3KeyPair:
        """What the catalog signs object-store requests with: the session its token was exchanged for, or the environment's pair.

        Raises:
            StoreCredentialUnavailableError: No unexpired session is held (503).
        """
        if (identity := self.web_identity) is not None:
            return held_session(identity)
        return S3KeyPair(access_key_id=self.s3_access_key_id, secret_access_key=self.s3_secret_access_key)

    def namespace_properties(self, *, root: str | None = None, plane: StorePlane = "metadata") -> dict[str, str]:
        """Return properties for ``lance_namespace.connect(impl, properties)``, bounded for ``plane``.

        ``root`` lets a WAREHOUSE-rooted connection differ from the estate default; the ``storage.``
        property vocabulary is spelled here once, because a caller composing these names itself is how
        one of them drifts.

        THE ENDPOINT IS NOT OVERRIDABLE, and that is the [[LH-205]] invariant stated as a signature: these
        properties carry the estate's own credential, so the only store they may address is the estate's.
        A warehouse record naming another store is refused before a connection is built
        (``catalog.core.store_endpoint``); its credential would belong behind the Dapr secret store
        with the record naming a reference, and no door consumes one yet.

        ``allow_http`` is derived from the endpoint's scheme, never set on its own, so a TLS endpoint
        cannot keep permission to fall back to plaintext.

        The credential is the one in force when this is called (:meth:`s3_key_pair`), so a caller holding the result
        past a renewal holds a session the store will expire.
        """
        pair = self.s3_key_pair()
        session = {f"{_STORAGE_PREFIX}session_token": pair.session_token.get_secret_value()} if pair.session_token is not None else {}
        return {
            "root": root or self.root,
            f"{_STORAGE_PREFIX}endpoint": self.s3_endpoint,
            f"{_STORAGE_PREFIX}access_key_id": pair.access_key_id,
            f"{_STORAGE_PREFIX}secret_access_key": pair.secret_access_key.get_secret_value(),
            **session,
            f"{_STORAGE_PREFIX}region": self.s3_region,
            f"{_STORAGE_PREFIX}allow_http": allow_http_for(self.s3_endpoint),
            f"{_STORAGE_PREFIX}virtual_hosted_style_request": str(self.s3_virtual_hosted).lower(),
            **{f"{_STORAGE_PREFIX}{key}": value for key, value in self.timeouts_for(plane).storage_options().items()},
        }

    def timeouts_for(self, plane: StorePlane) -> StoreTimeouts:
        """The object-store call bounds of ``plane`` (see `CatalogReadBounds`)."""
        return self.store_timeouts if plane == "metadata" else self.data_store_timeouts

    @model_validator(mode="after")
    def _validate_outbox_prefixes(self) -> Self:
        """Refuse to boot with the two lanes sharing one outbox prefix.

        Not a tidiness rule. `lineage/api/reconcile_cron.py` drains the lineage prefix by parsing every
        staged object as an OpenLineage `RunEvent`; a `CatalogControlEvent` cannot validate, so that
        drain classifies it POISON and DELETES it. Sharing the prefix therefore does not give the
        control lane a relay — it hands the control lane's only durable copy to something that destroys
        it, which is strictly worse than staging nowhere. Fail loudly at boot rather than lose a
        `table_published` per outage in a log nobody reads.
        """
        if self.control_outbox_uri and self.control_outbox_uri.rstrip("/") == self.lineage_outbox_uri.rstrip("/"):
            raise ValueError(
                f"LANCE_CONTROL_OUTBOX_URI must not equal LANCE_LINEAGE_OUTBOX_URI ({self.control_outbox_uri!r}): "
                "the lineage relay parses every object in its prefix as an OpenLineage RunEvent and DELETES "
                "what it cannot parse, so a shared prefix destroys the staged control events instead of relaying them"
            )
        return self

    def storage_options(self, *, plane: StorePlane = "metadata") -> dict[str, str]:
        """Return the ``storage.*`` properties with the prefix stripped, for pylance, bounded for ``plane``."""
        properties = self.namespace_properties(plane=plane)
        return {key[len(_STORAGE_PREFIX) :]: value for key, value in properties.items() if key.startswith(_STORAGE_PREFIX)}


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()  # required fields are read from the environment


@lru_cache
def _session_caps() -> LanceSessionCaps:
    """The cache ceilings, read from the environment ONCE.

    CACHED FOR THE SAME REASON `get_settings` IS, and the omission was measurable: a `BaseSettings`
    constructor re-reads and re-validates the environment on every call, and `shared_lance_session` runs
    on every Lance open — which in the catalog is per request. Measured 2026-09-16 at **80.4 us per
    construction against ~0 us for the cached settings**, so leaving it uncached put an environment scan
    on the open path that the model it replaced had never had."""
    return LanceSessionCaps()


def shared_lance_session() -> lance.Session:
    """The process-wide bounded Lance session every catalog open threads ([[LH-096]]).

    THE CATALOG OPENS PER REQUEST, which is what makes this worth more here than anywhere else: a bare
    `lance.dataset(uri)` takes Lance's default 1 GiB metadata + 6 GiB index ceilings, serves one request
    and throws the cache away with the handle. Inside a 512 Mi pod that is both a cache that never warms
    and a set of caps that describe a container the process is not running in. `rask-maintenance` was
    OOMKilled (exit 137) on 2026-09-10 before it clamped, which is why this is the shape it already uses.

    NOT A HANDLE CACHE. Caching a DATASET pins a version; a `Session`'s keys carry `(uri, version, etag)`,
    so a compaction writes NEW keys and a new version is never served stale. An OLD version is: measured
    on pylance 12.0.0, a version this session has read, whose manifest cleanup kept, keeps answering
    after cleanup deletes its data file, which is why a read that must see storage opens on
    :func:`fresh_lance_session` instead.
    `service_kit.lakehouse.lance_session` records its thread-safety under concurrent opens.

    ONE OBJECT per cap pair: `lance_session` is `@cache`d on its arguments, so every caller here gets
    the same session and the opens actually share a cache rather than each minting one.
    """
    from service_kit.lakehouse.lance_session import lance_session

    return lance_session(*_affordable_caps())


#: Each cache of an evidence session, at the floor `LanceSessionCaps` admits. Fixed, not clamped: see
#: :func:`fresh_lance_session`.
_EVIDENCE_CACHE_BYTES = 8 << 20


def fresh_lance_session() -> lance.Session:
    """A session nothing else has read through, for a read that is EVIDENCE — 8 MiB per cache, fixed.

    The erasure's verification is the one such read today: it must report what storage holds, and the
    shared session can answer a version whose files are gone (see :func:`shared_lance_session`).

    OUTSIDE THE SHARED BUDGET, which the cgroup clamp sizes for ONE session: at those caps every
    concurrent erasure would add a whole budget. It reads each retained version once, so a cache buys
    it nothing, and the bound is 16 MiB per erasure in flight. Measured on pylance 12.0.0 over 301
    versions: 34.9 MB at the shared caps, 8.4 MB at these.
    """
    import lance

    return lance.Session(metadata_cache_size_bytes=_EVIDENCE_CACHE_BYTES, index_cache_size_bytes=_EVIDENCE_CACHE_BYTES)


def _affordable_caps() -> tuple[int, int]:
    """The configured cache caps, clamped from the cgroup rather than by lowering the defaults, so they
    track the pod's real limit instead of a literal somebody has to remember to change."""
    from service_kit.lakehouse.lance_session import affordable_cache_bytes

    caps = _session_caps()
    return affordable_cache_bytes(caps.lance_metadata_cache_mb << 20, caps.lance_index_cache_mb << 20)
