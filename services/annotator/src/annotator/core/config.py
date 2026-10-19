"""Per-service settings — the shared media-library config plus the annotator's own knobs.

The annotator is a client of the lakehouse with its own release (ADR 0171): it reads and writes governed tables
through the catalog's doors, as the person on their own token, and publishes as its own ServiceAccount. So its
settings name the catalog and the token files it presents, and never a store credential: a corpus is opened on
the credential the catalog vends.

It mixes in :class:`GovernedAuthSettings` for the person it serves: every entity the annotator keeps (projects,
tasks, claims, drafts, member roles) is keyed on who owns or claims it, so a request needs a VERIFIED subject.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator

from service_kit.governed.settings import GovernedAuthSettings
from service_kit.media.config import Settings, TableBackend


class LineageSink(StrEnum):
    """Where a save the annotator commits to a LOCAL table (no catalog behind it) logs its OpenLineage RunEvent.

    Only the local mode emits: a write through the catalog is announced by the catalog, so emitting here too would
    count the run twice (`AnnotatorSettings.effective_lineage_sink`).
    """

    log = "log"
    none = "none"


class AssistBackend(BaseModel):
    """One registered assist producer: its endpoint plus its DECLARED contract.

    ``returns``/``inputs`` are what the backend itself claims (shape types out, prompt kinds
    in) — the registry surface, the panel's contract line and task compatibility all derive
    from these declarations rather than from a hardcoded family map in code. Empty means
    undeclared, and every surface must keep saying "unknown" rather than guess.
    A bare URL string is accepted as well: ``{"sam": "http://sam:9000"}``.
    """

    url: str
    returns: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_bare_url(cls, value: object) -> object:
        return {"url": value} if isinstance(value, str) else value


class AnnotatorSettings(GovernedAuthSettings, Settings):
    service_name: str = "annotator"
    service_port: int = Field(default=8103, alias="ANNOTATOR_PORT")

    #: The projected ServiceAccount token the publish saga presents to the catalog (audience `rask-catalog`), read on
    #: every call because the kubelet replaces it before it expires. The catalog verifies it like any service's, and a
    #: project admin grants that principal writer on the publish target through the catalog's grant door. Unset, the
    #: saga publishes with no bearer, which only a catalog authenticating nobody admits.
    catalog_token_file: Path | None = Field(default=None, alias="ANNOTATOR_CATALOG_TOKEN_FILE")

    #: Where a local-mode save logs its RunEvent; see `LineageSink`.
    lineage_sink: LineageSink = Field(default=LineageSink.log, alias="ANNOTATOR_LINEAGE_SINK")

    # Interactive AI-assist model endpoint — a Ray Serve deployment (GroundingDINO/SAM). Unset ⇒ a deterministic mock
    # so the draw/prompt→shapes round-trip is testable in-repo (drop-in for the Ray Serve HTTP endpoint).
    assist_url: str | None = Field(default=None, alias="ANNOTATOR_ASSIST_URL")
    # The producer REGISTRY: producer name/prefix → backend, as a JSON object. Routing is longest-prefix, so `"sam": …`
    # covers `sam-click`/`sam-box`; `assist_url` stays the fallback for anything unmapped. An entry is a bare URL string
    # or a STRUCTURED declaration: {"vlm": {"url": "http://vllm:8000", "returns": ["bbox"], "inputs": ["prompt"]}}.
    # `returns`/`inputs` are the backend's DECLARED contract; without them a registered producer renders "returns
    # unknown" and task compatibility cannot compute.
    assist_backends: dict[str, AssistBackend] = Field(default_factory=dict, alias="ANNOTATOR_ASSIST_BACKENDS")
    # Producer DISCOVERY from the Ray Serve control plane: a deployment that carries a `labeling` block in its
    # user_config is registered by being deployed. The discovery URL is the Ray dashboard base (:8265); the proxy URL is
    # the Serve HTTP ingress the discovered route_prefixes hang under (:8000). Under KubeRay set BOTH to the CR's stable
    # Services — discovery at `<rayservice>-head-svc:8265`, proxy at `<rayservice>-serve-svc:8000` — because the
    # serve-svc load-balances across every worker holding Serve replicas, while the derived fallback (dashboard host +
    # Serve's http_options port) reaches only the HEAD's proxy. The registry entries above win on a name conflict.
    serve_discovery_url: str | None = Field(default=None, alias="ANNOTATOR_SERVE_DISCOVERY_URL")
    serve_proxy_url: str | None = Field(default=None, alias="ANNOTATOR_SERVE_PROXY_URL")
    # A batch-labeling job's submit endpoint. Unset ⇒ a deterministic in-repo mock, so the submit/poll round-trip is
    # wired and testable. The annotator only enqueues; the deriver is the caller's own job.
    jobs_url: str | None = Field(default=None, alias="ANNOTATOR_JOBS_URL")

    @model_validator(mode="after")
    def _the_corpus_is_read_through_the_catalog(self) -> Self:
        """No store credential: a governed table is opened on what the catalog vends for the person (ADR 0171)."""
        if self.s3_endpoint:
            raise ValueError(
                "MEDIA_S3_ENDPOINT is set, and the annotator reads no store directly: point MEDIA_CATALOG_URI at the "
                "catalog, which vends each person the credential for the tables they may read"
            )
        return self

    @property
    def effective_lineage_sink(self) -> LineageSink:
        """The sink a save uses: ``none`` whenever a LIVE catalog sits behind the writes, which announces the write itself."""
        if self.write_backend is TableBackend.catalog and self.catalog_uri:
            return LineageSink.none
        return self.lineage_sink


@lru_cache
def get_annotator_settings() -> AnnotatorSettings:
    return AnnotatorSettings()
