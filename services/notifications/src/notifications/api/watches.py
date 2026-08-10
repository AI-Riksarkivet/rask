"""The watch door — v2 targeting, and the one surface where a subject widens their own audience.

**Membership GATES watching; it never implies it.** No estate-wide auto-watch, ever: the failure this
plane exists to end is a badge that counts other people's work, and "everyone in the project is told
about everything in it" is that failure with a different justification. So watching is an explicit
act, gated on `project#member` against the PROJECT — the object that exists at authorization time
(create-on-parent doctrine, the same shape the annotator's project-create door uses).

**The permission is re-checked at DELIVERY**, not only here. Membership can be revoked between the
watch and the run, and a registry row is not an authorization. This door writes a preference; the
fan-out asks the model again.

**Two writes, both synchronous, and the request fails if either fails.** The subject's own list lives
on their inbox actor (what the settings surface renders) and the project's watcher list lives on the
`WatchIndexActor` (what a fan-out reads). Neither can answer the other's question. A best-effort
second write would produce the one outcome worse than a refusal: a watch the settings page shows and
the fan-out never reads, which looks exactly like a plane that quietly stopped notifying.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, ConfigDict, Field

from notifications.api.security import CheckerDep, CurrentSubject
from notifications.dependencies import ActorPlaneDep, NotificationsSettingsDep
from notifications.proxies import inbox_for, watch_index_for
from service_kit.exceptions import ForbiddenError


log = logging.getLogger(__name__)

# `/notifications/watches`, NOT `/watches`. The gateway forwards `{prefix}/notifications` UNREWRITTEN
# (services/gateway), so a router mounted one segment short is served by the app and unreachable through
# the front door — a 404 that looks like a missing feature while the route exists and answers locally.
# Found by driving the real service and reading its own openapi against the gateway's route table.
router = APIRouter(prefix="/notifications/watches", tags=["watches"])

#: The relation a watch requires, on `project:<id>`. Membership, not readership: the project is the
#: tenancy boundary, and being a member of it is what makes its runs your business.
WATCH_RELATION = "member"

_PROJECT_HELP = "The project id to watch. Estate-chosen and path-safe; not an encoded subject."


class WatchList(BaseModel):
    """Which projects this subject watches."""

    model_config = ConfigDict(frozen=True)

    projects: list[str] = Field(default_factory=list)
    total: int = 0


class WatchChange(BaseModel):
    """What one watch/unwatch changed. `changed: False` is the idempotent repeat, not a failure."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    watching: bool
    changed: bool
    total: int


async def _require_member(checker: CheckerDep, subject: str, project_id: str) -> None:
    """Fail CLOSED: any falsy check denies, and the refusal names the relation and the object.

    A 403 with a reason rather than an empty 200 — the estate's standing rule. An empty answer would
    make a permission problem read as "there is nothing here", which is the shape that gets debugged
    for an hour.
    """
    obj = f"project:{project_id}"
    if not await checker(user=subject, relation=WATCH_RELATION, obj=obj):
        raise ForbiddenError(f"{subject} lacks {WATCH_RELATION} on {obj}")


@router.get("")
async def list_watches(subject: CurrentSubject, _actor_plane: ActorPlaneDep) -> WatchList:
    """The projects this subject watches.

    Not membership-filtered, deliberately. A watch on a project you have since left is a row you
    should be able to SEE and remove — hiding it would leave a preference you cannot reach, and it
    delivers nothing either way because the fan-out re-checks membership.
    """
    result = await inbox_for(subject).get_watches()
    return WatchList(projects=list(result.get("projects") or []), total=int(result.get("total") or 0))


@router.put("/{project_id}", status_code=status.HTTP_200_OK)
async def watch_project(
    subject: CurrentSubject,
    checker: CheckerDep,
    _actor_plane: ActorPlaneDep,
    _settings: NotificationsSettingsDep,
    project_id: Annotated[str, Path(min_length=1, max_length=256, description=_PROJECT_HELP)],
) -> WatchChange:
    """Start watching a project. Requires `project#member`; idempotent on repeat."""
    await _require_member(checker, subject, project_id)
    # The PROJECT index first. If the second write fails the request fails, and the residue is a
    # watcher the fan-out will deliver to whose own list does not show it — the safe direction, since
    # delivery re-checks membership and the subject can always re-issue the watch. The reverse residue
    # (listed, never delivered) is the silent one.
    await watch_index_for(project_id).watch({"subject": subject})
    result = await inbox_for(subject).set_watch({"project_id": project_id, "watching": True})
    log.info("watch_added", extra={"project_id": project_id})
    return WatchChange(
        project_id=project_id,
        watching=True,
        changed=bool(result.get("changed")),
        total=int(result.get("total") or 0),
    )


@router.delete("/{project_id}", status_code=status.HTTP_200_OK)
async def unwatch_project(
    subject: CurrentSubject,
    _actor_plane: ActorPlaneDep,
    project_id: Annotated[str, Path(min_length=1, max_length=256, description=_PROJECT_HELP)],
) -> WatchChange:
    """Stop watching a project.

    NOT membership-gated, and that asymmetry is deliberate: someone removed from a project must still
    be able to clear a watch they can no longer create. Gating the removal on the permission the
    removal exists to stop needing is how a preference becomes unreachable.
    """
    await watch_index_for(project_id).unwatch({"subject": subject})
    result = await inbox_for(subject).set_watch({"project_id": project_id, "watching": False})
    log.info("watch_removed", extra={"project_id": project_id})
    return WatchChange(
        project_id=project_id,
        watching=False,
        changed=bool(result.get("changed")),
        total=int(result.get("total") or 0),
    )
