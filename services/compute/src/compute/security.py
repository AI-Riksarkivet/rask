"""Who may look at the estate's compute.

Every route in this service reads the Ray plane: `{prefix}/ray/*` (jobs, actors, tasks, cluster,
logs, overview) and the root-mounted `/api/serve/applications` proxy onto the Ray dashboard. The
dashboard is itself token-protected — the chart turns `ray.auth.enabled` on and injects
`RAY_AUTH_TOKEN` — and this service holds that token and forwarded it on behalf of anyone who could
reach the port.

It shipped ungated under the "localhost / trusted network" posture `CLAUDE.md` documented until
2026-08-26. That posture was already false (the chart defaults auth ON, `ingress.yaml` publishes
`/api`) and the owner ruling settled it.

**The relation is the lakehouse's estate-admin verb, `can_observe_events`, on the estate root object**, asked through
the catalog's decision door. The Ray plane is no lakehouse object and the lakehouse model carries no relation of this
service's own, so the estate-wide surface takes the estate-wide verb, as `flows` does.

Everything mechanical comes from `service_kit.governed.deps`, shared with viewer, flows, annotator and
notifications rather than copied. With auth off (every knob defaults off) the subject is `anon` and
the checker is permissive, so a dev stack behaves exactly as before this module existed.
"""

from typing import Annotated

from fastapi import Depends

from compute.audit_actions import ComputeAuditAction
from compute.dependencies import ComputeSettingsDep
from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import AuditOutcome, audit
from service_kit.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(ComputeSettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: Looking at the cluster: the lakehouse's estate-admin verb (see the module docstring).
READ = "can_observe_events"


async def require_read(subject: CurrentSubject, checker: CheckerDep, settings: ComputeSettingsDep) -> None:
    """Refuse a caller who may not look at the estate's compute.

    Mounted as a ROUTER dependency on both routers, so it gates every route they register, the
    `/api/serve/applications` proxy included. Per route would not be enough for the Ray routes: a
    router-level gate cannot be skipped by a route added later without the dependency.
    """
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=READ, obj=obj)
    audit(ComputeAuditAction.COMPUTE_READ, AuditOutcome.ALLOW if allowed else AuditOutcome.DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{READ}' on '{obj}' — reading the compute plane needs the estate admin")
