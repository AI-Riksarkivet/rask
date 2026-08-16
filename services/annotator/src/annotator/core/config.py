"""Per-service settings — the shared data-plane config plus annotator-local knobs.

lance-ns gives each service its own ``core/config.py`` with a service env prefix;
the shared MEDIA_* data-plane variables stay common (one Lance root serves all
three), and only service-local knobs (ANNOTATOR_*) are prefixed.

It also mixes in :class:`GovernedAuthSettings`, because the annotator now writes **per-subject**
state (annotation projects, tasks, claims, drafts) rather than only serving read-plane media. Every
entity in ``OPEN-WORK.md#design--annotation-projects`` is keyed on who owns or claims it, so the service
needs a VERIFIED subject: the trusted ``X-User`` header it used to accept becomes a cross-user leak
the moment anything is keyed on identity.
"""

from functools import lru_cache

from pydantic import Field

from service_kit.governed.settings import GovernedAuthSettings
from service_kit.media.config import Settings


class AnnotatorSettings(GovernedAuthSettings, Settings):
    service_name: str = "annotator"
    service_port: int = Field(default=8103, alias="ANNOTATOR_PORT")

    #: Control-plane change events (`catalog.control.v1`). The annotator is the third producer on that
    #: topic — it publishes `task_assigned`/`task_unassigned`, which NAME the annotator who must act, so
    #: the notifications plane can put the work in their inbox instead of making them look for it.
    #:
    #: Default OFF and same shape as the catalog's knobs on purpose: a half-configured transport must
    #: never pretend to emit. Off, `make_control_emitter` returns the no-op and assignment simply carries
    #: no notification — the assignment itself is unaffected, because the emit is best-effort and runs
    #: after the state change has already committed.
    #:
    #: In-cluster this needs the pubsub component's `scopes` to list this service's Dapr app-id, or the
    #: sidecar rejects every publish silently.
    control_emit_enabled: bool = Field(default=False, alias="ANNOTATOR_CONTROL_EMIT_ENABLED")
    control_emit_timeout_seconds: float = Field(default=5.0, ge=0.1, alias="ANNOTATOR_CONTROL_EMIT_TIMEOUT_SECONDS")
    control_pubsub: str = Field(default="catalog-control-pubsub", alias="ANNOTATOR_CONTROL_PUBSUB")


@lru_cache
def get_annotator_settings() -> AnnotatorSettings:
    return AnnotatorSettings()
