"""The channel-preferences door — where a subject says how they want to be reached.

Per-user, like the watch door and for the same reason: this configures what interrupts YOU. No FGA
check, because there is no object — a subject may always change their own preferences, and the
subject is derived from the verified token with no header fallback, so there is no way to name
somebody else's.

OFF BY DEFAULT, every channel. The bell costs a reader nothing; an email is an interruption someone
has to ask for, and an estate that mailed everyone by default would be the badge-that-counts-other-
people's-work failure again, arriving somewhere worse.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from notifications.api.channels import EMAIL, SLACK
from notifications.api.security import CurrentSubject
from notifications.dependencies import ActorPlaneDep
from notifications.proxies import inbox_for
from service_kit.exceptions import ValidationError


log = logging.getLogger(__name__)

router = APIRouter(prefix="/prefs", tags=["prefs"])

#: What this build can actually send. A stored preference naming something else is kept (a channel
#: removed from the build must not make a stored document unreadable) but cannot be SET — the door is
#: where the vocabulary is enforced, so the store stays permissive and the input does not.
KNOWN_CHANNELS = frozenset({EMAIL, SLACK})


class ChannelPreferences(BaseModel):
    """Where this subject wants to be pushed, beyond the bell."""

    model_config = ConfigDict(frozen=True)

    channels: list[str] = Field(default_factory=list)
    #: Channel id → destination. A destination is something the subject SUPPLIED; nothing here is
    #: inferred from a token, because an address a plane guessed is an address it can guess wrong.
    destinations: dict[str, str] = Field(default_factory=dict)


@router.get("")
async def get_prefs(subject: CurrentSubject, _actor_plane: ActorPlaneDep) -> ChannelPreferences:
    """This subject's channel preferences. Absent state reads as OFF, which is the honest default."""
    result = await inbox_for(subject).get_prefs()
    return ChannelPreferences(
        channels=list(result.get("channels") or []),
        destinations=dict(result.get("destinations") or {}),
    )


@router.put("")
async def set_prefs(payload: ChannelPreferences, subject: CurrentSubject, _actor_plane: ActorPlaneDep) -> ChannelPreferences:
    """Replace this subject's preferences wholesale.

    Refuses an opt-in with no destination rather than storing one, because a channel that is on and
    unreachable is silently identical to one that is off — and the subject would have every reason to
    believe they had turned it on. The refusal names the channel.
    """
    unknown = sorted(set(payload.channels) - KNOWN_CHANNELS)
    if unknown:
        raise ValidationError(f"unknown notification channel(s): {', '.join(unknown)}")
    missing = sorted(channel for channel in payload.channels if not payload.destinations.get(channel))
    if missing:
        raise ValidationError(f"no destination for channel(s): {', '.join(missing)} — a channel with nowhere to send is silently off")
    result = await inbox_for(subject).set_prefs(payload.model_dump(mode="json"))
    log.info("channel_prefs_updated", extra={"channels": sorted(payload.channels)})
    return ChannelPreferences(
        channels=list(result.get("channels") or []),
        destinations=dict(result.get("destinations") or {}),
    )
