"""Typed application settings (pydantic-settings).

Read once via :func:`get_settings`; routers read it off ``state.settings`` (the
injected :class:`~service_kit.media.state.AppState`). Only env-varying values live here —
algorithmic constants (RRF k, probe tokens, column-exclude sets) stay as module
constants in their feature packages.

Env vars are ``MEDIA_*`` (see aliases). ``cors_origins`` accepts either a JSON
list or a bare comma-separated string (``MEDIA_CORS_ORIGINS=https://a,https://b``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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
    s3_secret_store: str = Field(default="lance-secrets", alias="MEDIA_S3_SECRET_STORE")
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
    read_backend: str = Field(default="direct", alias="MEDIA_READ_BACKEND")
    write_backend: str = Field(default="direct", alias="MEDIA_WRITE_BACKEND")
    catalog_uri: str | None = Field(default=None, alias="MEDIA_CATALOG_URI")
    catalog_delimiter: str = Field(default="$", alias="MEDIA_CATALOG_DELIMITER")
    catalog_token: str | None = Field(default=None, alias="MEDIA_CATALOG_TOKEN")
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
    publish_secret_store: str = Field(default="lance-secrets", alias="MEDIA_PUBLISH_SECRET_STORE")
    publish_secret_key: str = Field(default="lance", alias="MEDIA_PUBLISH_SECRET_KEY")
    # The catalog namespace annotation tables live under; unset → the dataset id.
    catalog_namespace: str | None = Field(default=None, alias="MEDIA_CATALOG_NAMESPACE")

    # OpenLineage emission on annotation writes (pre-merge; lance-ns's mover emits at
    # merge). "stdout"/"log" write a spec-2-0-2 RunEvent per save; "none" disables.
    lineage_sink: str = Field(default="log", alias="MEDIA_LINEAGE_SINK")

    # Interactive AI-assist model endpoint — a Ray Serve deployment (GroundingDINO/SAM),
    # per the merge runtime stack (models = Ray Serve). Unset ⇒ a deterministic mock so
    # the draw/prompt→shapes round-trip is testable in-repo (drop-in for the Ray Serve
    # HTTP endpoint, like the catalog transport).
    assist_url: str | None = Field(default=None, alias="MEDIA_ASSIST_URL")
    # The producer REGISTRY (the CVAT-Nuclio-shaped seam): producer name/prefix → backend URL, as a
    # JSON object. Routing is longest-prefix, so `"sam": …` covers `sam-click`/`sam-box`;
    # `assist_url` stays the fallback for anything unmapped. One flat env var, because a registry
    # that needs a config FILE would be a second deployment surface for what is one map.
    assist_backends: dict[str, str] = Field(default_factory=dict, alias="MEDIA_ASSIST_BACKENDS")

    # Batch labeling job runner — a lance-ns RayJob submit endpoint (the silver-deriver
    # enqueue for bulk/auto-labeling over a read-plane selection). Unset ⇒ a deterministic
    # in-repo mock so the submit/poll round-trip is wired + testable (drop-in for the real
    # submitter, like the assist + catalog transports). We only enqueue — the deriver runs
    # in lance-ns (lance-ray + the catalog mover), never in this process.
    jobs_url: str | None = Field(default=None, alias="MEDIA_JOBS_URL")

    @field_validator("read_backend", "write_backend")
    @classmethod
    def _check_backend(cls, v: str) -> str:
        if v not in {"direct", "catalog"}:
            raise ValueError(f"read/write backend must be 'direct' or 'catalog', got {v!r}")
        return v

    @field_validator("lineage_sink")
    @classmethod
    def _check_lineage_sink(cls, v: str) -> str:
        if v not in {"stdout", "log", "none"}:
            raise ValueError(f"MEDIA_LINEAGE_SINK must be stdout|log|none, got {v!r}")
        return v

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
        return bool(self.catalog_uri) and self.read_backend == "catalog" and self.write_backend == "catalog"

    @property
    def effective_lineage_sink(self) -> str:
        """The sink the annotator's own emit uses: forced to ``none`` only when a
        LIVE catalog sits behind the writes (``catalog_uri`` set) — that catalog
        inline-emits a RunEvent for the same merge, so emitting here too would
        double-count the run. The in-process catalog fallback emits nothing, so
        our own sink stays active there."""
        if self.write_backend == "catalog" and self.catalog_uri:
            return "none"
        return self.lineage_sink

    @property
    def default_dataset_id(self) -> str:
        return self.db_path.stem

    @property
    def storage_options(self) -> dict[str, str] | None:
        """Lance ``storage_options`` for the object store (None = local filesystem).

        Path-style addressing is forced (``virtual_hosted_style_request=false``)
        because RustFS/MinIO reject virtual-hosted signing — verified live.
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
            opts["access_key_id"] = self.s3_access_key_id
            opts["secret_access_key"] = self.s3_secret_access_key
            return opts
        if not self.s3_access_key_id:
            raise RuntimeError("MEDIA_S3_ENDPOINT is set but MEDIA_S3_ACCESS_KEY_ID is not — the id is config, set it")
        secret = _store_secret(self.s3_secret_store, self.s3_secret_key, self.s3_secret_field)
        if not secret:
            raise RuntimeError(
                f"S3 secret {self.s3_secret_field!r} unavailable from Dapr store {self.s3_secret_store!r} — failing closed"
            )
        opts["access_key_id"] = self.s3_access_key_id
        opts["secret_access_key"] = secret
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
def get_settings() -> Settings:
    return Settings()
