"""Settings loaded from environment + .env via pydantic-settings.

Read once at startup via `Settings()`; pass through DI as `SettingsDep`. Never
re-read env vars in routes or services.
"""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Lane-pipeline sentinels (case-insensitive) that mean "disabled" — set
# RASK_PREFETCH_PIPELINE to one of these to run the orchestrator HTR-only.
PIPELINE_DISABLED = frozenset({"none", "off", "disabled", ""})


class RunnerParams(BaseModel):
    """The runner's I/O config for one job submission — where its code lives
    (`repo_root` → Ray working_dir) and the S3/IIIF endpoints it reads/writes.

    Built once from `Settings.runner_params()` and threaded as a single value
    through `submit_chunk` → `build_entrypoint`, instead of four separate args.
    """

    model_config = ConfigDict(frozen=True)

    repo_root: Path
    cache_bucket: str
    output: str
    iiif_url: str
    source_mode: Literal["iiif", "s3"] = "iiif"
    input_uri: str = ""
    # How to invoke the runner CLI in the Ray job entrypoint. Local dev resolves
    # the workspace via uv; the in-cluster ray image ships the installed `runner`
    # console script on PATH (no uv, no source tree), so the chart overrides this.
    runner_cmd: str = "uv run --project runners/htr runner"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    viewer_input: str = Field(alias="RASK_VIEWER_INPUT")
    viewer_output: str = Field(alias="RASK_VIEWER_OUTPUT")

    api_prefix: str = Field(default="/api/v1", alias="RASK_API_PREFIX")
    cors_origins: list[str] = Field(default_factory=list, alias="RASK_CORS_ORIGINS")

    # S3 endpoint URL for any S3-compatible backend (MinIO / rustfs / AWS).
    # rask is storage-agnostic: this is just an endpoint. The canonical name is
    # RASK_S3_ENDPOINT_URL; S3_ENDPOINT_URL (the boto3-ecosystem name) is also
    # accepted, plus HCP_ENDPOINT as the Helm chart's legacy bridge (drop once the
    # chart sets RASK_S3_ENDPOINT_URL).
    s3_endpoint_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RASK_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "HCP_ENDPOINT"),
    )
    # Skip TLS verification (self-signed endpoints, e.g. local MinIO/rustfs).
    s3_insecure: bool = Field(
        default=False,
        validation_alias=AliasChoices("RASK_S3_INSECURE", "S3_INSECURE", "HCP_INSECURE"),
    )
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")

    search_bucket: str = Field(default="images-batch-search", alias="RASK_SEARCH_BUCKET")
    cache_bucket: str = Field(default="images-batch", alias="RASK_CACHE_BUCKET")
    output_bucket: str = Field(default="images-batch-alto", alias="RASK_OUTPUT_BUCKET")
    iiif_url: str = Field(default="https://iiifintern-ai.ra.se", alias="RASK_IIIF_URL")
    source_mode: Literal["iiif", "s3"] = Field(default="iiif", alias="RASK_SOURCE_MODE")
    lines_table: str = Field(default="lines", alias="RASK_LINES_TABLE")
    catalog_table: str = Field(default="archive_catalog", alias="RASK_CATALOG_TABLE")
    ray_dashboard_url: str = Field(default="http://localhost:8265", alias="RAY_DASHBOARD_URL")

    repo_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[4])
    batches_db: Path | None = Field(default=None, alias="RASK_BATCHES_DB")
    spa_build_dir: Path | None = Field(default=None, alias="RASK_SPA_BUILD")

    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    db_pool_size: int = Field(default=10, ge=1, le=100, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, ge=0, le=200, alias="DB_MAX_OVERFLOW")
    db_pool_recycle_seconds: int = Field(default=1800, ge=60, alias="DB_POOL_RECYCLE_SECONDS")
    db_pool_timeout_seconds: int = Field(default=30, ge=1, alias="DB_POOL_TIMEOUT_SECONDS")
    db_query_timeout_seconds: int = Field(default=60, ge=1, alias="DB_QUERY_TIMEOUT_SECONDS")

    http_timeout: float = 15.0
    lance_query_timeout_seconds: int = Field(default=30, ge=1, alias="RASK_LANCE_QUERY_TIMEOUT_SECONDS")

    # In-process orchestrator loop (replaces the old scripts/orchestrator.py cron).
    # `autostart` controls whether the loop starts on viewer boot; operators can
    # also POST /api/v1/orchestrator/start and /stop at runtime to override.
    # TODO(post-NATS): when NATS JetStream lands, retire this lifespan task and move
    # the tick into a JetStream consumer. See viewer/services/orchestrator/loop.py.
    orchestrator_autostart: bool = Field(default=False, alias="RASK_ORCHESTRATOR_AUTOSTART")
    orchestrator_interval_seconds: int = Field(default=60, ge=10, alias="RASK_ORCHESTRATOR_INTERVAL_SECONDS")

    # Dapr service invocation. When false, build_dapr_client returns None and the
    # gateway falls back to direct httpx upstreams. DAPR_HTTP_PORT is set by the
    # Dapr sidecar injector in-cluster.
    dapr_enabled: bool = Field(default=False, alias="RASK_DAPR_ENABLED")
    dapr_http_port: str = Field(default="3500", alias="DAPR_HTTP_PORT")
    # S3 reconcile walks the whole cache bucket (~600k keys) — far too slow to run
    # every tick, so it's throttled: reconcile at most this often, and derive/submit
    # from the DB on the other ticks. 0 = reconcile every tick (legacy). The first
    # tick skips reconcile so submission starts immediately from existing DB state.
    orchestrator_reconcile_seconds: int = Field(default=600, ge=0, alias="RASK_ORCHESTRATOR_RECONCILE_SECONDS")
    # Pipeline names the orchestrator submits per slot. Both must be registered in
    # the caller's pipeline registry (e.g. viewer.models.pipelines.PIPELINE_SPECS)
    # and are validated at app startup.
    htr_pipeline: str = Field(default="htr", alias="RASK_HTR_PIPELINE")
    runner_cmd: str = Field(default="uv run --project runners/htr runner", alias="RASK_RUNNER_CMD")
    prefetch_pipeline: str = Field(default="prefetch", alias="RASK_PREFETCH_PIPELINE")
    # Max HTR jobs the orchestrator keeps in flight at once (0 = unlimited). Caps
    # concurrent job drivers so a batch run can't OOM/flood the Ray head. The
    # loop submits at most (cap - currently-running) eligible chunks per tick.
    htr_max_inflight: int = Field(default=0, ge=0, alias="RASK_HTR_MAX_INFLIGHT")

    @property
    def resolved_batches_db(self) -> Path:
        return self.batches_db or (self.repo_root / ".cache" / "batches.db")

    @property
    def resolved_database_url(self) -> str:
        """Effective DB URL — explicit `DATABASE_URL` wins, else sqlite at `resolved_batches_db`.

        Same code drives both sqlite (dev) and postgres (prod):
            sqlite+aiosqlite:///<path>
            postgresql+asyncpg://user:pass@host/dbname
        """
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.resolved_batches_db}"

    @property
    def resolved_spa_build(self) -> Path:
        return self.spa_build_dir or (self.repo_root / "components" / "apps" / "frontend" / "build")

    def runner_params(self) -> RunnerParams:
        """Bundle the runner's I/O config; centralizes the s3:// output prefix."""
        return RunnerParams(
            repo_root=self.repo_root,
            cache_bucket=self.cache_bucket,
            output=f"s3://{self.output_bucket}",
            iiif_url=self.iiif_url,
            source_mode=self.source_mode,
            input_uri=f"s3://{self.cache_bucket}",
            runner_cmd=self.runner_cmd,
        )

    @property
    def lance_db_uri(self) -> str:
        return f"s3://{self.search_bucket}"

    def lance_storage_options(self) -> dict[str, str] | None:
        if not self.s3_endpoint_url or not self.aws_access_key_id or not self.aws_secret_access_key:
            return None
        opts = {
            "aws_endpoint": self.s3_endpoint_url,
            "aws_access_key_id": self.aws_access_key_id,
            "aws_secret_access_key": self.aws_secret_access_key,
            "aws_region": self.aws_region,
            "aws_virtual_hosted_style_request": "false",
            "allow_http": "true" if self.s3_endpoint_url.startswith("http://") else "false",
        }
        # A self-signed endpoint (local MinIO/rustfs) is skipped by boto3 via
        # the insecure flag, but LanceDB's Rust S3 client (object_store) verifies
        # by default and fails the TLS handshake ("error sending request"). Mirror
        # the insecure flag so the search tables can be opened over https.
        if self.s3_insecure:
            opts["allow_invalid_certificates"] = "true"
        return opts
