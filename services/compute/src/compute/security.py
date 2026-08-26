"""Who may look at the estate's compute.

Every route in this service reads the Ray plane: `{prefix}/ray/*` (jobs, actors, tasks, cluster,
logs, overview) and the root-mounted `/api/serve/*` proxy, which is a `{path:path}` catch-all onto
the Ray dashboard. The dashboard is itself token-protected — the chart turns `ray.auth.enabled` on and
injects `RAY_AUTH_TOKEN` — and this service holds that token and forwarded it on behalf of anyone who
could reach the port.

It shipped ungated under the "localhost / trusted network" posture `CLAUDE.md` documented until
2026-08-26. That posture was already false (the chart defaults auth ON, `ingress.yaml` publishes
`/api`) and the owner ruling settled it.

**The relation is the READER tier on the estate root object.** Reading cluster state is looking, not
spending — the same rung `flows` uses for its palette, and one tier below the `writer` it requires to
actually run a graph. `warehouse#reader` is defined `... or writer or member from project`, so every
existing writer and owner passes automatically and no authorized caller loses access.

Everything mechanical comes from `service_kit.governed.deps`, shared with viewer, flows, annotator and
notifications rather than copied. With auth off (every knob defaults off) the subject is `anon` and
the checker is permissive, so a dev stack behaves exactly as before this module existed.
"""

from typing import Annotated

from fastapi import Depends

from compute.dependencies import ComputeSettingsDep
from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import ALLOW, DENY, audit
from service_kit.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(ComputeSettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: Looking at the cluster is a READ. One rung below the `writer` tier `flows` requires to SPEND it.
READ = "reader"


async def require_read(subject: CurrentSubject, checker: CheckerDep, settings: ComputeSettingsDep) -> None:
    """Refuse a caller who may not look at the estate's compute.

    Mounted as a ROUTER dependency on both routers. Per route would not be enough: `proxy.py`
    registers a `{path:path}` catch-all, so anything the Ray dashboard serves is reachable through it
    and a per-route list could pass while the catch-all stayed open.
    """
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=READ, obj=obj)
    audit("compute_read", ALLOW if allowed else DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{READ}' on '{obj}' — reading the compute plane needs the estate reader tier")
