"""Who may enumerate the estate's projects.

`GET /api/projects/` returns every operator Project CR in the cluster — slug, team, workload type,
k8s namespace and each tenant's LIVE INGRESS HOST. That is estate-wide tenant enumeration, and the
catalog gates the same class of read on `can_observe_events` rather than serving it open.

It shipped ungated under the "localhost / trusted network" posture `CLAUDE.md` documented until
2026-08-26. The chart has defaulted auth ON for a while and `ingress.yaml` publishes `/api`, so that
posture was already false; the owner ruling settled it and this module is the door.

**The relation is the READER tier on the estate root object**, matching the rung `flows` uses for
looking rather than spending. A per-project filter (`admin`/`member` of `project:<slug>`, so a tenant
sees only its own) is the better long-term shape and is deliberately NOT done here: it changes the
response for existing callers, and this change is meant to close an exposure without also changing
what an authorized caller sees. It is filed in `open_fastapi-audit.md`.

Everything mechanical — bearer → verified subject, the three-outcome checker — comes from
`service_kit.governed.deps`, shared with viewer, flows, annotator and notifications rather than
copied. With auth off (every knob defaults off) the subject is `anon` and the checker is permissive,
so a dev stack behaves exactly as before this module existed.
"""

from typing import Annotated

from fastapi import Depends

from controlplane.dependencies import ControlplaneSettingsDep
from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import ALLOW, DENY, audit
from service_kit.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(ControlplaneSettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: Enumerating projects is a READ, not a spend — the same rung `flows` uses to serve its palette.
#: `warehouse#reader` is defined `... or writer or member from project`, so every existing writer and
#: owner passes automatically and no authorized caller loses access.
READ = "reader"


async def require_read(subject: CurrentSubject, checker: CheckerDep, settings: ControlplaneSettingsDep) -> None:
    """Refuse a caller who may not enumerate the estate's projects.

    Mounted as a ROUTER dependency rather than per route, so a route added later cannot arrive
    ungated — the failure mode this whole change is about.
    """
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=READ, obj=obj)
    audit("controlplane_projects", ALLOW if allowed else DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{READ}' on '{obj}' — listing projects needs the estate reader tier")
