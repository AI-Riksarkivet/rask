"""YAML config file with named profiles.

Config file: ~/.rahcp/config.yaml (or --config / RAHCP_CONFIG)

Example::

    default: dev

    profiles:
      dev:
        endpoint: http://localhost:8000/api/v1
        username: admin
        password: secret
        tenant: dev-ai
        verify_ssl: false
      prod:
        endpoint: http://localhost:8000/api/v1
        username: prod-user
        password: secret
        tenant: prod-archive
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".rahcp"
CONFIG_PATH = CONFIG_DIR / "config.yaml"


class Profile(BaseModel):
    """A named connection profile."""

    # Connection
    endpoint: str = "http://localhost:8000/api/v1"
    username: str = ""
    password: str = ""
    tenant: str = ""
    verify_ssl: bool = True
    timeout: float = 30.0

    # Multipart upload
    multipart_threshold: int = 100 * 1024 * 1024
    multipart_chunk: int = 64 * 1024 * 1024
    multipart_concurrency: int = 6

    # Bulk transfer defaults
    bulk_workers: int = 10
    bulk_progress_interval: float = 5.0
    bulk_queue_depth: int = 8
    bulk_tracker_flush_every: int = 500
    bulk_tracker_dir: str = ""
    bulk_tracker_prefix: str = ""
    bulk_presign_batch_size: int = 200
    bulk_chunk_size: int = 4 * 1024 * 1024  # 4 MB
    bulk_stream_threshold: int = 100 * 1024 * 1024  # 100 MB

    # IIIF
    iiif_url: str = "https://iiifintern-ai.ra.se"
    iiif_timeout: float = 60.0
    iiif_query_params: str = "full/max/0/default.jpg"
    iiif_workers: int = 4

    # Observability
    log_level: str = "warning"
    otel_endpoint: str = ""
    otel_protocol: str = "http/protobuf"
    otel_service_name: str = "rahcp-cli"


class CLIConfig(BaseModel):
    """Config with named profiles."""

    default: str = ""
    profiles: dict[str, Profile] = Field(default_factory=dict)

    def resolve(self, name: str | None = None) -> Profile:
        """Resolve a profile by name, falling back to default."""
        key = name or self.default
        if key and key in self.profiles:
            return self.profiles[key]
        if len(self.profiles) == 1:
            return next(iter(self.profiles.values()))
        return Profile()


def load_config(path: str | None = None) -> CLIConfig:
    """Load config from a YAML file.

    Resolution: explicit path > RAHCP_CONFIG env > ~/.rahcp/config.yaml
    """
    config_path = Path(path) if path else CONFIG_PATH
    if not config_path.exists():
        return CLIConfig()
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        log.warning("Failed to parse config file: %s", config_path)
        return CLIConfig()

    # Multi-profile format
    if "profiles" in raw:
        return CLIConfig(
            default=raw.get("default", ""),
            profiles={
                name: Profile(**vals)
                for name, vals in raw["profiles"].items()
                if isinstance(vals, dict)
            },
        )

    # Flat format (backwards compat) — single "default" profile
    return CLIConfig(
        default="default",
        profiles={"default": Profile(**raw)},
    )
