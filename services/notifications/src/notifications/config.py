"""notifications-only settings.

`service_kit.config.Settings` stays the shared base (api prefix, CORS, Dapr, OTel) — `make_service_app`
builds it and the lifespan puts it on `app.state.settings`. This class holds the fields that are
nobody else's business, in the same pydantic-settings shape as `flows`, so an inbox-only knob does not
widen the estate-wide config class.

**Every retention number is a SETTING, never a literal in the actor.** open_notifications §11 q3 leaves
the values UNVERIFIED — only real volume can set them — so what is settled is that measuring them
later has to be a config change rather than a code change. The defaults below are starting points and
are labelled as such, not measurements.

**One construction, two readers.** `get_notifications_settings` is cached because the InboxActor is
built by the Dapr runtime and never sees `app.state`: an actor turn cannot be handed a dependency. The
lifespan reads the same cached instance onto `app.state.notifications_settings` so routes keep going
through DI, and there is still exactly one object.
"""

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from notifications.models import INBOX_PAGE_LIMIT_MAX
from service_kit.governed.settings import GovernedAuthSettings


class NotificationsSettings(GovernedAuthSettings, BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    #: How long a pointer stays in an inbox. UNVERIFIED as a value (30 d is a starting point, not a
    #: measurement) and deliberately not load-bearing for correctness: it is the horizon the compaction
    #: reminder trims against. It is ALSO the state key's `ttlInSeconds` — but only when the sidecar
    #: will accept one; see `actor_state_ttl_enabled`.
    inbox_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=60, alias="RASK_NOTIFICATIONS_INBOX_TTL_SECONDS")

    #: Whether this pod's sidecar will ACCEPT a `ttlInSeconds` on an actor state write — i.e. whether
    #: its Dapr `Configuration` carries `features: [{name: ActorStateTTL, enabled: true}]`.
    #:
    #: **Default false because getting it wrong is not a silent no-op.** daprd REFUSES the whole
    #: transactional upsert when the metadata is present and the feature is off — `dapr/dapr` v1.18.1
    #: (the version this chart vendors), `pkg/actors/api/transactional.go`:
    #: `ttlInSeconds is not supported without the "ActorStateTTL" feature enabled`, reached from
    #: `pkg/runtime/runtime.go`'s `StateTTLEnabled: globalConfig.IsFeatureEnabled(config.ActorStateTTL)`.
    #: `chart/` declares exactly one Dapr `Configuration` (`lance-tracing`) and it carries no `features:`
    #: block, so writing the TTL unconditionally would fail EVERY inbox write on the shipped release —
    #: the opposite of the "belt, never braces" the TTL was there for.
    #:
    #: The compaction reminder is authoritative either way (open_notifications §11 q6). This flag only
    #: decides whether the belt is fastened, and it must be flipped in the SAME rollout that adds the
    #: feature to the `Configuration`: a sidecar reads that at BOOT, so the two cannot land apart.
    actor_state_ttl_enabled: bool = Field(default=False, alias="RASK_NOTIFICATIONS_ACTOR_STATE_TTL_ENABLED")

    #: The hard row cap per subject, applied by compaction after the age trim. A ceiling on storage per
    #: person, so one runaway producer cannot make one inbox unbounded between ticks.
    inbox_max_rows: int = Field(default=200, ge=1, alias="RASK_NOTIFICATIONS_INBOX_MAX_ROWS")

    #: How often the compaction reminder fires. It is the AUTHORITATIVE bound (see `inbox_ttl_seconds`),
    #: so this is the interval at which an inbox is actually trimmed — and, because the reminder lives
    #: in the Scheduler's etcd rather than beside the rows, it is also the window inside which a lost
    #: reminder is repaired from the read path.
    compaction_interval_seconds: int = Field(default=6 * 3600, ge=1, alias="RASK_NOTIFICATIONS_COMPACTION_INTERVAL_SECONDS")

    #: The default page size for `GET /inbox` when the caller states none.
    inbox_page_limit: int = Field(default=20, ge=1, le=INBOX_PAGE_LIMIT_MAX, alias="RASK_NOTIFICATIONS_INBOX_PAGE_LIMIT")

    @model_validator(mode="after")
    def _compaction_must_outpace_retention(self) -> Self:
        """Fail at boot rather than silently grow.

        Compaction is the only authoritative bound on an inbox. If it ticked no more often than the
        retention window, a row could outlive that window by up to a full interval with nothing
        noticing — and the TTL that would otherwise have caught it is not enabled on this estate.
        """
        if self.compaction_interval_seconds >= self.inbox_ttl_seconds:
            raise ValueError(
                f"RASK_NOTIFICATIONS_COMPACTION_INTERVAL_SECONDS ({self.compaction_interval_seconds}) must be shorter than "
                f"RASK_NOTIFICATIONS_INBOX_TTL_SECONDS ({self.inbox_ttl_seconds}) — the compaction reminder is the only thing that bounds an inbox"
            )
        return self


@lru_cache(maxsize=1)
def get_notifications_settings() -> NotificationsSettings:
    """Read once, at first use. `model_config.env_file` makes pydantic-settings read `.env` itself —
    the same mechanism `service_kit.build_settings` relies on — so there is no second dotenv load."""
    return NotificationsSettings.model_validate({})
