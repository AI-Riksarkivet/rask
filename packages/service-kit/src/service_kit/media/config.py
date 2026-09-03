"""Typed application settings (pydantic-settings).

Read once via :func:`get_settings`; routers read it off ``state.settings`` (the
injected :class:`~service_kit.media.state.AppState`). Only env-varying values live here —
algorithmic constants (RRF k, probe tokens, column-exclude sets) stay as module
constants in their feature packages.

Env vars are ``MEDIA_*`` (see aliases). ``cors_origins`` accepts either a JSON
list or a bare comma-separated string (``MEDIA_CORS_ORIGINS=https://a,https://b``).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.lakehouse.naming import CATALOG_DELIMITER


class TableBackend(StrEnum):
    """Which path a table read/write takes.

    A TYPE, not a `field_validator` over a set literal. The allowed values used to live inside a
    validator body, so the schema said "string", `ty` could not tell `read_backend == "catalouge"`
    from a real comparison, and every consumer re-spelled the literals. A `StrEnum` states the set
    once: pydantic rejects anything else at load with the members in the message, the members
    compare equal to their own strings so existing comparisons keep working, and the OpenAPI schema
    finally names the options.
    """

    direct = "direct"
    catalog = "catalog"


class LineageSink(StrEnum):
    """Where the annotation write path emits its OpenLineage RunEvent.

    ``stdout`` WAS a third member and is deliberately gone. `setup_logging()` already sends the root
    logger to stdout, so the option only distinguished itself by writing past the logging system —
    losing the request-id correlation filter, the level gate and the OTLP copy for the one record
    that describes a governed write. Nothing in the estate selected it (no chart value, no env in
    any manifest or script), and a value that must not be chosen is better removed than documented.
    """

    log = "log"
    none = "none"


class AssistBackend(BaseModel):
    """One registered assist producer: its endpoint plus its DECLARED contract.

    ``returns``/``inputs`` are what the backend itself claims (shape types out, prompt kinds
    in) — the registry surface, the panel's contract line and task compatibility all derive
    from these declarations rather than from a hardcoded family map in code. Empty means
    undeclared, and every surface must keep saying "unknown" rather than guess.
    A bare URL string is accepted for back-compat: ``{"sam": "http://sam:9000"}``.
    """

    url: str
    returns: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_bare_url(cls, value: object) -> object:
        return {"url": value} if isinstance(value, str) else value


class MediaSettings(BaseSettings):
    """The MEDIA plane's settings (``MEDIA_*``) — the viewer/search/annotator trio's config.

    NAMED `MediaSettings`, and it used to be a second class called `Settings`. One distribution held
    two of them: this one on `MEDIA_*` aliases, and `service_kit.config.Settings` on `RASK_*` with an
    `env_prefix`. They are not related by inheritance and share almost no field, so which one a
    `settings: Settings` annotation, a `SettingsDep` or a stack frame referred to depended entirely on
    which module the reader had open. `Settings` survives below as a back-compat alias for the
    services that still import it by that name.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Env vars are MEDIA_* (§4.4). Pre-rename env aliases were dropped in the
    # lance-media rename.
    embed_url: str = Field(default="http://127.0.0.1:8001", alias="MEDIA_EMBED_URL")
    rerank_url: str = Field(default="http://127.0.0.1:8002", alias="MEDIA_RERANK_URL")
    host: str = Field(default="127.0.0.1", alias="MEDIA_HOST")
    db_path: Path = Field(default=Path("transcripts_v2.lance"), alias="MEDIA_DB")
    # Multi-dataset serving (LANCE_MEDIA_MERGE §4.4): the registry root holds
    # one `<id>.lance` dir per dataset; `db_path`'s stem stays the default
    # dataset so the legacy single-DB routes keep their behavior.
    db_root: Path = Field(default=Path("."), alias="MEDIA_DB_ROOT")
    descriptor_dir: Path = Field(default=Path("config/descriptors"), alias="MEDIA_DESCRIPTOR_DIR")
    cors_origins: list[str] = Field(default_factory=lambda: ["*"], alias="MEDIA_CORS_ORIGINS")

    # Version-keyed search result cache (the read-fast tier). Number of result
    # sets to retain per app instance; 0 disables it entirely (then no per-request
    # version reads are paid). A write to any table a query reads bumps its Lance
    # version and strands the old entry (LRU-evicted). Entries are NOT uniformly
    # small — a hit row carries display.body (transcript text), so a full page of
    # hits can run to megabytes; the byte ceiling below is the real memory bound,
    # the entry count is just the lookup bound (#141).
    search_cache_size: int = Field(default=256, ge=0, alias="MEDIA_SEARCH_CACHE_SIZE")
    # Total bytes of cached results to retain (approximated as serialized JSON
    # length). 0 removes the byte bound (count-only, the pre-#141 behaviour).
    search_cache_bytes: int = Field(default=64 * 1024 * 1024, ge=0, alias="MEDIA_SEARCH_CACHE_BYTES")
    #: The MEMORY ceiling on the atlas `/points` cache. Its twin above has had one since someone
    #: measured the problem; this one evicted on entry count alone (12) while its own comment said
    #: "each is multi-MB". Three declared spaces across four corpora is already twelve keys, and at
    #: ~100 MB per Arrow payload that is 1.2 GB resident in a one-replica pod. `0` disables the byte
    #: bound only, exactly as above — the two must not diverge on the meaning of their settings.
    points_cache_bytes: int = Field(default=256 * 1024 * 1024, ge=0, alias="MEDIA_POINTS_CACHE_BYTES")
    #: The request-body ceiling for the media apps (viewer / search / annotator).
    #:
    #: These are the apps that accept MULTIPART UPLOADS, and they were the ones with no cap: they
    #: build through `service_kit.media.middleware`, not the fleet factory that grew one. A file part
    #: is spooled to a SpooledTemporaryFile in FULL before the handler is entered, so the handler's
    #: own `read(cap + 1)` bounds its memory and cannot bound the landing zone.
    #:
    #: 32 MiB, not the catalog's 256: that ceiling exists for bulk Arrow-IPC table writes, and reusing
    #: it here would make the bound meaningless for a voice snippet. It stays comfortably ABOVE
    #: `_MAX_UPLOAD_BYTES` so the handler's own 400 — which names the limit in the caller's terms —
    #: is still the message an oversize upload gets, rather than a bare 413 from the door.
    max_body_bytes: int = Field(default=32 * 1024 * 1024, ge=0, alias="MEDIA_MAX_BODY_BYTES")

    #: Whether this app serves `/docs`, `/redoc` and `/openapi.json`. OFF by default.
    #:
    #: viewer, search and annotator constructed `FastAPI(...)` with none of the three set, so FastAPI's
    #: defaults stood and all three were served — while four sibling services at least carried a flag.
    #: See `service_kit.config.Settings.docs_enabled` for why the default is closed.
    docs_enabled: bool = Field(default=False, alias="MEDIA_DOCS")

    # Optional S3 object-store backing (RASK_LANDING §4). Set MEDIA_S3_ENDPOINT to
    # serve datasets from RustFS / MinIO / AWS: the registry then lists + opens
    # under MEDIA_S3_DB_ROOT (an s3:// URI) with these storage_options. All unset
    # (the default) = the local-filesystem db_root path, byte-identical to before.
    s3_endpoint: str | None = Field(default=None, alias="MEDIA_S3_ENDPOINT")
    s3_access_key_id: str | None = Field(default=None, alias="MEDIA_S3_ACCESS_KEY_ID")
    s3_secret_access_key: str | None = Field(default=None, alias="MEDIA_S3_SECRET_ACCESS_KEY")
    # The SECRET half comes from the Dapr secret store, fail-closed — the MEDIA_PUBLISH_* precedent
    # below: coordinates are config, the secret is not. `rustfs-secret-key` is already seeded in the
    # `lance` bundle by the chart's infra-credentials plane. MEDIA_S3_SECRET_ACCESS_KEY exists for
    # tests and sidecar-less dev only; the chart never sets it.
    #: THE ONE STORE, NAMED ONCE (DUP-17). The estate runs a single Dapr secret-store component and
    #: seven env vars named it, each defaulting to the same literal and none of them set by the chart —
    #: so repointing the store meant finding all seven. `RASK_SECRET_STORE` is the estate-wide name
    #: (already what viewer and ingest read); the per-service alias stays FIRST so a single service can
    #: still be moved on its own.
    s3_secret_store: str = Field(default="lance-secrets", validation_alias=AliasChoices("MEDIA_S3_SECRET_STORE", "RASK_SECRET_STORE"))
    s3_secret_key: str = Field(default="lance", alias="MEDIA_S3_SECRET_KEY")
    s3_secret_field: str = Field(default="rustfs-secret-key", alias="MEDIA_S3_SECRET_FIELD")
    s3_region: str = Field(default="us-east-1", alias="MEDIA_S3_REGION")
    s3_db_root: str | None = Field(default=None, alias="MEDIA_S3_DB_ROOT")

    # Read-path backend (LANCE_NS_INTEGRATION §catalog-client). "direct" (default) =
    # open Lance ourselves, byte-identical to today. "catalog" = read through the
    # lance-ns catalog /v1/table/query contract (backend/lancekit/reader.py); when
    # MEDIA_CATALOG_URI is set it hits that live REST catalog, else an in-process
    # native-namespace transport. Host-agnostic + retry-friendly so the catalog path
    # drops into a Dapr service-invocation / Ray-Serve-fronted catalog unchanged.
    read_backend: TableBackend = Field(default=TableBackend.direct, alias="MEDIA_READ_BACKEND")
    write_backend: TableBackend = Field(default=TableBackend.direct, alias="MEDIA_WRITE_BACKEND")
    catalog_uri: str | None = Field(default=None, alias="MEDIA_CATALOG_URI")
    catalog_delimiter: str = Field(default=CATALOG_DELIMITER, alias="MEDIA_CATALOG_DELIMITER")
    # The publish saga's own OIDC identity (the catalog accepts only IdP bearers, and the saga
    # outlives any user request). A token is minted FRESH per publish via the password grant with a
    # dedicated service account — nothing long-lived is stored anywhere, so nothing can go stale
    # (the failure mode a hand-pinned MEDIA_CATALOG_TOKEN just demonstrated live). Coordinates are
    # config; the PASSWORD is not — it comes from the Dapr secret store, fail-closed, per the
    # estate's secrets rule. A set MEDIA_CATALOG_TOKEN still wins (prod may pin a token minted by
    # its own machinery).
    publish_token_url: str | None = Field(default=None, alias="MEDIA_PUBLISH_TOKEN_URL")
    publish_client_id: str | None = Field(default=None, alias="MEDIA_PUBLISH_CLIENT_ID")
    publish_client_secret: str | None = Field(default=None, alias="MEDIA_PUBLISH_CLIENT_SECRET")
    publish_username: str | None = Field(default=None, alias="MEDIA_PUBLISH_USERNAME")
    publish_secret_store: str = Field(
        default="lance-secrets", validation_alias=AliasChoices("MEDIA_PUBLISH_SECRET_STORE", "RASK_SECRET_STORE")
    )  # the one store; see s3_secret_store
    publish_secret_key: str = Field(default="lance", alias="MEDIA_PUBLISH_SECRET_KEY")
    # The catalog namespace annotation tables live under; unset → the dataset id.
    catalog_namespace: str | None = Field(default=None, alias="MEDIA_CATALOG_NAMESPACE")

    # OpenLineage emission on annotation writes (pre-merge; lance-ns's mover emits at
    # merge). "stdout"/"log" write a spec-2-0-2 RunEvent per save; "none" disables.
    lineage_sink: LineageSink = Field(default=LineageSink.log, alias="MEDIA_LINEAGE_SINK")

    # Interactive AI-assist model endpoint — a Ray Serve deployment (GroundingDINO/SAM),
    # per the merge runtime stack (models = Ray Serve). Unset ⇒ a deterministic mock so
    # the draw/prompt→shapes round-trip is testable in-repo (drop-in for the Ray Serve
    # HTTP endpoint, like the catalog transport).
    assist_url: str | None = Field(default=None, alias="MEDIA_ASSIST_URL")
    # The producer REGISTRY (the CVAT-Nuclio-shaped seam): producer name/prefix → backend, as a
    # JSON object. Routing is longest-prefix, so `"sam": …` covers `sam-click`/`sam-box`;
    # `assist_url` stays the fallback for anything unmapped. One flat env var, because a registry
    # that needs a config FILE would be a second deployment surface for what is one map.
    #
    # An entry is either a bare URL string (back-compat) or a STRUCTURED declaration:
    #   {"sam": "http://sam:9000",
    #    "vlm": {"url": "http://vllm:8000", "returns": ["bbox"], "inputs": ["prompt"]}}
    # `returns`/`inputs` are the backend's DECLARED contract — without them a registered producer
    # rendered "returns unknown" forever (the shape map in code only knows the built-in families)
    # and task compatibility could never compute. Declared here, the registry entry really is the
    # whole of adding a model.
    assist_backends: dict[str, AssistBackend] = Field(default_factory=dict, alias="MEDIA_ASSIST_BACKENDS")
    # Producer DISCOVERY from the Ray Serve control plane (the primary registry source —
    # model endpoints in this estate are Ray Serve deployments, and a deployment that carries
    # a `labeling` block in its user_config IS registered by being deployed; per the KubeRay
    # RayService guide, user_config edits are IN-PLACE reconfigurations, no new cluster).
    # The discovery URL is the Ray dashboard base (:8265); the proxy URL is the Serve HTTP
    # ingress the discovered route_prefixes hang under (:8000). Under KubeRay set BOTH
    # explicitly to the CR's stable Services — discovery at `<rayservice>-head-svc:8265`,
    # proxy at `<rayservice>-serve-svc:8000` — because the serve-svc load-balances across
    # every worker holding Serve replicas, while the derived fallback (dashboard host +
    # Serve's http_options port) reaches only the HEAD's proxy. Both svc names are stable
    # across zero-downtime upgrades (KubeRay repoints their selectors). Env entries above
    # stay the operator OVERRIDE on name conflicts. Unset discovery ⇒ config + mock only.
    serve_discovery_url: str | None = Field(default=None, alias="MEDIA_SERVE_DISCOVERY_URL")
    serve_proxy_url: str | None = Field(default=None, alias="MEDIA_SERVE_PROXY_URL")

    # Batch labeling job runner — a lance-ns RayJob submit endpoint (the silver-deriver
    # enqueue for bulk/auto-labeling over a read-plane selection). Unset ⇒ a deterministic
    # in-repo mock so the submit/poll round-trip is wired + testable (drop-in for the real
    # submitter, like the assist + catalog transports). We only enqueue — the deriver runs
    # in lance-ns (medallion-producer + the catalog mover), never in this process.
    jobs_url: str | None = Field(default=None, alias="MEDIA_JOBS_URL")

    def catalog_table_id(self, dataset_id: str, table: str) -> list[str]:
        """The catalog identifier for a dataset's table — settings-derived, never
        hardcoded: ``MEDIA_CATALOG_NAMESPACE`` when set, else the dataset id.

        Guards the catalog's id grammar: the delimiter inside a segment would
        silently split the identifier server-side into the wrong namespace/table.
        """
        namespace = self.catalog_namespace or dataset_id
        for segment in (namespace, table):
            if self.catalog_delimiter in segment:
                raise ValueError(
                    f"catalog id segment {segment!r} contains the delimiter {self.catalog_delimiter!r} — set MEDIA_CATALOG_NAMESPACE to a clean name"
                )
        return [namespace, table]

    @property
    def rest_catalog_mode(self) -> bool:
        """True when the annotations plane fully routes through a LIVE catalog —
        both backends flipped AND a URI set. Mixed configurations keep the local
        table in the loop (pre-merge safety), so paths gate on this, not on the
        individual flags."""
        return bool(self.catalog_uri) and self.read_backend is TableBackend.catalog and self.write_backend is TableBackend.catalog

    @property
    def effective_lineage_sink(self) -> LineageSink:
        """The sink the annotator's own emit uses: forced to ``none`` only when a
        LIVE catalog sits behind the writes (``catalog_uri`` set) — that catalog
        inline-emits a RunEvent for the same merge, so emitting here too would
        double-count the run. The in-process catalog fallback emits nothing, so
        our own sink stays active there."""
        if self.write_backend is TableBackend.catalog and self.catalog_uri:
            return LineageSink.none
        return self.lineage_sink

    @property
    def default_dataset_id(self) -> str:
        return self.db_path.stem

    def storage_options(self) -> dict[str, str] | None:
        """Lance ``storage_options`` for the object store (None = local filesystem).

        A METHOD, not a property: on the Dapr-secret path this performs a BLOCKING secret fetch and
        can raise, and a `@property` disguised that as a free attribute read — which is exactly how it
        reached an event loop (`viewer datasets.list_datasets` built its registry inline, on the loop).
        A call makes the cost visible at every site, and matches every other config in the estate
        (`MedallionSettings`, catalog, maintenance all expose `storage_options()`). The fetch is
        `_store_secret`-cached, so a boot-time warm (off the loop, in the lifespan) makes every
        request-path call a pure dict build.

        Path-style addressing is forced (``virtual_hosted_style_request=false``)
        because RustFS/MinIO reject virtual-hosted signing — verified live.

        The credential keys are ``aws_``-prefixed for the reason ``lance_storage_options`` records:
        every fleet pod exports ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY``, and the bare spellings
        do not displace them — object_store blends the two sources and signs with a pair belonging to
        neither identity (``403 SignatureDoesNotMatch``, measured in-cluster 2026-09-03). This builder
        goes to the trouble of fetching a secret from the store, fail-closed; being overridden by the
        environment afterwards would defeat the whole path.
        """
        if not self.s3_endpoint:
            return None
        opts = {
            "endpoint": self.s3_endpoint,
            "region": self.s3_region,
            "allow_http": "true" if self.s3_endpoint.startswith("http://") else "false",
            "virtual_hosted_style_request": "false",
        }
        # Credential resolution, per the estate's secrets rule (store only, fail-closed):
        #   1. BOTH static fields set — tests and sidecar-less dev. The chart never sets the secret.
        #   2. Otherwise the secret comes from the Dapr secret store (cached), the access-key id from
        #      config — the exact MEDIA_PUBLISH_* split (client_id is config, the password is not).
        #      A missing bundle or field RAISES rather than falling back to the AWS env chain: an
        #      env-borne secret is the shape the rule forbids, and a silent chain fallback would
        #      re-admit it while looking configured.
        if self.s3_access_key_id and self.s3_secret_access_key:
            opts["aws_access_key_id"] = self.s3_access_key_id
            opts["aws_secret_access_key"] = self.s3_secret_access_key
            return opts
        if not self.s3_access_key_id:
            raise RuntimeError("MEDIA_S3_ENDPOINT is set but MEDIA_S3_ACCESS_KEY_ID is not — the id is config, set it")
        secret = _store_secret(self.s3_secret_store, self.s3_secret_key, self.s3_secret_field)
        if not secret:
            raise RuntimeError(f"S3 secret {self.s3_secret_field!r} unavailable from Dapr store {self.s3_secret_store!r} — failing closed")
        opts["aws_access_key_id"] = self.s3_access_key_id
        opts["aws_secret_access_key"] = secret
        return opts

    @property
    def registry_root(self) -> str:
        """The root the registry lists/opens datasets under — the S3 URI when
        configured, else the local ``db_root`` path."""
        if self.s3_endpoint and self.s3_db_root:
            return self.s3_db_root
        return str(self.db_root)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache(maxsize=8)
def _store_secret(store: str, key: str, field: str) -> str | None:
    """One cached fetch per (store, key, field) — `storage_options` is read per dataset open, and
    the bundle is static for the pod's lifetime. Import inside so sidecar-less callers that never
    reach the store path (local roots, explicit test creds) pay nothing."""
    from service_kit.governed.secrets import fetch_dapr_secret

    return fetch_dapr_secret(store, key).get(field) or None


@lru_cache
def get_media_settings() -> MediaSettings:
    """The PROCESS-WIDE media settings, read once — for a service `main` that wants one object for
    the app's whole life.

    NAMED APART from `service_kit.dependencies.get_settings`, and the two are not interchangeable:
    that one takes a `Request` and returns whatever the app's own lifespan bound, this one takes
    nothing and returns an lru_cached singleton. Two functions with one name and incompatible
    signatures is how `AppState` came to default every instance in the process to a single shared
    object — see `service_kit.media.state.AppState.settings`, which no longer does.
    """
    return MediaSettings()


#: BACK-COMPAT ALIASES. `services/viewer`, `services/search` and `services/annotator` import these
#: names (and subclass `Settings` in their own `core/config.py`), so renaming the definitions without
#: them would be a contract change across three services. They are aliases, not second definitions:
#: `Settings is MediaSettings` and `get_settings is get_media_settings`.
Settings = MediaSettings
get_settings = get_media_settings
