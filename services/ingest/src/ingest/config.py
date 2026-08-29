"""THE operational settings of the ingest plane — one declaration per knob, in one place.

Before this, the plane read the environment in 44 places across 15 modules and there was no model at
all: `IngestAuthSettings` covered the AUTH half (`LANCE_OIDC_*` / `LANCE_FGA_*`) and everything
operational was a bare `os.getenv` at the point of use (ING-07, ingest-flow-09). Three consequences,
all of them observed in this tree rather than imagined:

* **ONE CONVENTION, THREE READERS.** `RASK_CATALOG_DELIMITER` was read by `naming.delimiter()`, by a
  dead `lineage._delimiter()`, and by a module-level `catalog_service.DELIMITER` — and the third was
  frozen at import while the first two were not. A delimiter the writers disagree about addresses a
  DIFFERENT TABLE rather than failing, which is the failure `naming.py`'s own header was written
  about.
* **KNOBS FROZEN AT IMPORT.** `fetch.HTTP_*`, `queue.MAX_ACK_PENDING`, `queue.PUBLISH_CONCURRENCY`,
  `catalog_service.SECRET_*` were computed at module import, so they were fixed per POD and — worse —
  invisible to any test or operator who changed the variable afterwards. `sizing.py` and
  `workflow.RunLimits` had already been dragged off that pattern for exactly this reason.
* **NO ROSTER.** Nothing enumerated what this service reads, so `chart/templates/fleet.yaml` and the
  code could only be compared by grep.

**DELIBERATELY NOT A CACHED SINGLETON**, which is the one place this departs from the estate's
`@lru_cache get_*_settings()` shape, and it is not an oversight. This plane has already RULED against
per-pod-frozen configuration twice, in writing: `sizing.py` ("Read at call time, not import time, so
a test — and `kubectl set env` — can move them without reimporting the module") and `workflow.py`'s
`RunLimits` (a module-level read "looks like a constant and behaves like a clock"). A process-lifetime
cache is import-time freezing with extra steps, so `settings()` builds the model per call: it reads
`os.environ` and validates ~30 fields, which is microseconds against the network call every caller of
it is about to make. `env_file` is deliberately absent for the same reason it is a defect on the auth
model — a per-call `.env` read WOULD be disk I/O on the hot path.

**REPLAY SAFETY IS UNCHANGED, and the gate was widened to keep it that way.** A Dapr workflow BODY
still may not read configuration: `replay_guard` now refuses `settings()` in workflow scope exactly
as it refuses `os.getenv`, because moving a read behind a function call must not move it out of the
gate's sight. Activities may read it freely — their results are recorded, so every replay sees what
the first execution saw.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.lakehouse.naming import CATALOG_DELIMITER


class IngestSettings(BaseSettings):
    """Every operational environment variable this service reads, and nothing else.

    NO `populate_by_name`: each field declares its variable outright, so the bare field name is never
    a second, undeclared lookup key. That is the defect `tests/unit/test_settings_env_namespace.py`
    exists for — `ray_address` silently answering to Ray's own `$RAY_ADDRESS` — and declaring the
    aliases explicitly is what keeps this class out of it.

    The AUTH half lives on `ingest.auth.IngestAuthSettings`, and stays there: `GovernedAuthSettings`
    is the estate's shared `LANCE_*` vocabulary and re-spelling it here would give this one service
    its own dialect for settings every governed service already reads.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, values: Any) -> Any:  # noqa: ANN401 — pydantic hands the raw source mapping
        """`kubectl set env FOO=` leaves an EMPTY STRING, and an empty string is not a value.

        Without this a blanked numeric knob takes the whole read down — `float("")` raises, and the
        read this replaces (`workflow.RunLimits.from_env`) carried its own `or 0` precisely to stop a
        config typo killing the activity that enforces the run ceilings. Doing it here means the
        guarantee holds for every field rather than for the three that remembered.
        """
        if isinstance(values, dict):
            return {k: v for k, v in values.items() if v != ""}
        return values

    # ── the source fetch policy (`fetch.py`) ──────────────────────────────────────────
    #: Retry budget for a transient HTTP failure. Three tries with exponential backoff turns a server
    #: hiccup into a slowdown rather than a lost unit — measured against a real rate-limited endpoint,
    #: which hands out RST under load at ~64 concurrent reads.
    http_attempts: int = Field(default=3, gt=0, validation_alias="RASK_INGEST_HTTP_ATTEMPTS")
    http_base_delay_seconds: float = Field(default=1.0, ge=0, validation_alias="RASK_INGEST_HTTP_BASE_DELAY")
    http_timeout_seconds: float = Field(default=60.0, gt=0, validation_alias="RASK_INGEST_HTTP_TIMEOUT")

    # ── how a run partitions its writes (`sizing.py`) ─────────────────────────────────
    #: The DEPLOYMENT defaults. A request may override any of them per run — see `IngestSizing`, and
    #: `sizing.py`'s header for why the row default collides with Lance's own guidance on purpose.
    fragment_rows: int = Field(default=1024, gt=0, validation_alias="RASK_INGEST_FRAGMENT_ROWS")
    fragment_bytes: int = Field(default=256 * 1024 * 1024, gt=0, validation_alias="RASK_INGEST_FRAGMENT_BYTES")
    fetch_batch: int = Field(default=16, gt=0, validation_alias="RASK_INGEST_FETCH_BATCH")
    fetch_concurrency: int = Field(default=8, gt=0, validation_alias="RASK_INGEST_FETCH_CONCURRENCY")

    # ── the work queue (`queue.py`, `runtime.py`) ─────────────────────────────────────
    nats_url: str = Field(default="nats://rask-nats:4222", validation_alias="RASK_NATS_URL")
    #: JetStream stops delivering past this many unacked messages, which is why `sizing.resolve`
    #: REFUSES a `fragment_rows` at or above it rather than accepting a number that deadlocks a drain.
    max_ack_pending: int = Field(default=2048, gt=0, validation_alias="RASK_INGEST_MAX_ACK_PENDING")
    publish_concurrency: int = Field(default=64, gt=0, validation_alias="RASK_INGEST_PUBLISH_CONCURRENCY")

    # ── the run ceilings (`workflow.resolve_limits`, an ACTIVITY) ─────────────────────
    #: Zero means "no ceiling". These are branched on inside the orchestrator, so they are resolved in
    #: an activity and pinned in history — never read from a workflow body. See `replay_guard`.
    max_run_hours: float = Field(default=0.0, ge=0, validation_alias="RASK_INGEST_MAX_RUN_HOURS")
    max_units: int = Field(default=0, ge=0, validation_alias="RASK_INGEST_MAX_UNITS")
    incremental_max_rows: int = Field(default=0, ge=0, validation_alias="RASK_INGEST_INCREMENTAL_MAX_ROWS")

    # ── where bytes come from and go (`adapters.py`, `staging.py`, `runtime.py`) ──────
    #: The ONE directory tree `local-dir` may read, and there is deliberately NO default. Unset means
    #: the kind is REFUSED, not "read anything": `options.root` is caller-supplied and reaches a reader
    #: that rglobs, so unconfined it is an arbitrary-file-read primitive aimed at the ingest pod's own
    #: filesystem — `{"root": "/proc/self", "pattern": "environ"}` lands the S3 credential as rows in a
    #: governed table.
    local_root: str = Field(default="", validation_alias="RASK_INGEST_LOCAL_ROOT")
    #: Same reasoning, same shape, for `lance-append`'s `options.uri`.
    lance_root: str = Field(default="", validation_alias="RASK_INGEST_LANCE_ROOT")
    #: The CATALOG's own root. `lance-append` refuses anything under it even when `lance_root` would
    #: allow it, because a copy between governed tiers is the cascade's operation, not ingest's.
    governed_root: str = Field(default="", validation_alias="LANCE_REST_ROOT")
    #: The dev/local fallback warehouse. Empty means "a temp dir", which is the only honest default for
    #: a value that names where governed bytes land.
    warehouse: str = Field(default="", validation_alias="RASK_INGEST_WAREHOUSE")
    s3_endpoint_url: str | None = Field(default=None, validation_alias="RASK_S3_ENDPOINT_URL")
    aws_region: str = Field(default="us-east-1", validation_alias="AWS_REGION")
    #: Comma-separated roots a dataset's external blob descriptors may point at.
    external_blob_bases: str = Field(default="", validation_alias="LANCE_EXTERNAL_BLOB_BASES")

    # ── the catalog (`catalog_service.py`, `naming.py`) ───────────────────────────────
    catalog_url: str = Field(default="http://rask-catalog:2333", validation_alias="RASK_CATALOG_URL")
    #: The table-id separator (`bronze$pages`). From env so it cannot drift from the catalog's own — a
    #: mismatch addresses a DIFFERENT table rather than failing.
    catalog_delimiter: str = Field(default=CATALOG_DELIMITER, validation_alias="RASK_CATALOG_DELIMITER")
    #: Explicit rather than inferred from a reachable URL: a catalog that is merely DOWN must fail the
    #: run loudly, not silently fall back to writing locally — which is how an estate ends up with
    #: governed data that no catalog knows about.
    #:
    #: A TYPO NOW REFUSES TO BOOT, and that is deliberate rather than incidental to the type. The read
    #: this replaces was `os.getenv(...).lower() in ("1", "true", "yes")`, so `RASK_INGEST_USE_CATALOG:
    #: "ture"` meant FALSE — the exact silent fallback to local writes the paragraph above forbids,
    #: reached by a one-character mistake with nothing anywhere reporting it. Pydantic's boolean
    #: parsing accepts the same words and raises on anything else, so the misconfiguration is loud.
    use_catalog: bool = Field(default=False, validation_alias="RASK_INGEST_USE_CATALOG")
    #: The catalog-SPECIFIC half of the service credential, if a deployment sets one. Read through
    #: `catalog_app_token` rather than directly — see that property for the fallback and why it is a
    #: property rather than an `AliasChoices` pair.
    catalog_app_token_override: str | None = Field(default=None, validation_alias="RASK_CATALOG_APP_TOKEN")
    catalog_service_identity: str | None = Field(default=None, validation_alias="RASK_CATALOG_SERVICE_IDENTITY")
    #: The field inside the secret bundle holding the catalog bearer — the pre-identity-door path.
    catalog_token_field: str = Field(default="catalog-token", validation_alias="RASK_CATALOG_TOKEN_FIELD")

    # ── the lineage graph (`provenance.py`) ───────────────────────────────────────────
    lineage_url: str = Field(default="http://rask-lineage:8000", validation_alias="RASK_LINEAGE_URL")
    lineage_app_token_override: str | None = Field(default=None, validation_alias="RASK_LINEAGE_APP_TOKEN")
    lineage_service_identity: str | None = Field(default=None, validation_alias="RASK_LINEAGE_SERVICE_IDENTITY")

    # ── the secret store (`catalog_service.py`, `objectstore.py`) ─────────────────────
    #: The Dapr secret store and key holding this plane's credentials. Same store the rest of the
    #: governed fleet reads (`lance-secrets` -> OpenBao), so there is one place a credential rotates.
    secret_store: str = Field(default="lance-secrets", validation_alias="RASK_SECRET_STORE")
    secret_key: str = Field(default="lance", validation_alias="RASK_SECRET_KEY")

    # ── the medallion handshake (`naming.py`) ─────────────────────────────────────────
    #: The bronze TIER's namespace name, read from the same chart value the medallion reads. A tier
    #: the writer and the cascade head disagree about is a write nothing downstream ever sees.
    bronze_namespace: str = Field(default="bronze", validation_alias="MEDALLION_BRONZE_NAMESPACE")

    # ── the incremental cron trigger (`cron.py`, `__init__.py`) ───────────────────────
    #: The Dapr input-binding component name. Unset means the cron route is NOT mounted at all — a
    #: door into starting ingest runs with no cron behind it exists for no reason.
    cron_binding_name: str = Field(default="", validation_alias="RASK_INGEST_CRON_BINDING_NAME")
    cron_kind: str = Field(default="", validation_alias="RASK_INGEST_CRON_KIND")
    cron_dataset: str = Field(default="", validation_alias="RASK_INGEST_CRON_DATASET")
    cron_options: str = Field(default="", validation_alias="RASK_INGEST_CRON_OPTIONS")

    # ── the estate's shared service token ─────────────────────────────────────────────
    #: What daprd injects into every fleet pod. Read here ONLY as the fallback under the two
    #: per-upstream overrides below; the ingest DOOR reads it straight from `os.environ` on purpose,
    #: because `get_auth_settings` is cached and a cached credential is a rotated secret nobody picks
    #: up (`auth.authorize_ingest`).
    app_api_token: str | None = Field(default=None, validation_alias="APP_API_TOKEN")

    @property
    def catalog_app_token(self) -> str | None:
        """The token presented to the catalog's service door: the specific one, else the shared one.

        A PROPERTY rather than an `AliasChoices` pair, and the difference is not cosmetic. Alias
        choices resolve to the first variable that is PRESENT, so a `RASK_CATALOG_APP_TOKEN` rendered
        EMPTY — a secret that resolved to nothing, which is the likely failure and the one ING-01 was
        about — would win and the estate token would never be tried. `or` treats blank as absent,
        which is what the two `os.getenv(...) or os.getenv(...)` call sites this replaces did.
        """
        return self.catalog_app_token_override or self.app_api_token

    @property
    def lineage_app_token(self) -> str | None:
        """The token presented to the lineage door. Same fallback, same reason, as the catalog's."""
        return self.lineage_app_token_override or self.app_api_token


def settings() -> IngestSettings:
    """The operational settings, read from the environment NOW.

    Not cached — see this module's header. Call it where the value is used, never at import: a
    module-level call would reintroduce the per-pod freezing this model exists to remove.
    """
    return IngestSettings()


def env_name(field: str) -> str:
    """The environment variable `field` is declared to answer to — for messages that must name it.

    Several refusals tell an operator which variable to set (`local-dir is not enabled here: set
    RASK_INGEST_LOCAL_ROOT ...`). Spelling the name a second time in the message is how a rename
    leaves a refusal pointing at a variable that no longer exists, so the message reads it off the
    declaration instead.
    """
    alias = IngestSettings.model_fields[field].validation_alias
    if isinstance(alias, AliasChoices):
        return str(alias.choices[0])
    return str(alias)
