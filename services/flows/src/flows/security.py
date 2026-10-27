"""Who may run a flow.

``POST /flows/runs`` executes a graph whose model nodes invoke LIVE Ray Serve endpoints — it spends
the estate's GPU compute on whatever the caller drew. It shipped ungated, which was the documented
"localhost / trusted network" posture and stopped being defensible the moment the edge carried
``/api/flows`` to a browser.

**``/catalog`` and ``/validate`` are gated too, since 2026-08-26, and this docstring used to say the
opposite** — "the catalog and validate routes stay open: they read a server-declared registry and run
graph hygiene, no cluster involved". That reasoning was about spending compute and it was sound on its
own terms. It did not cover unmetered PARSING, which is what actually bit: ``/validate`` accepted an
arbitrary-size graph and built every node before the 256-node ceiling in ``graph.py`` was consulted —
measured at 3.00 s of event-loop block for a 23 MiB body, on a service with one replica and one loop,
which also serves ``/livez``. The bound now lives on ``FlowGraph.nodes`` where pydantic can refuse it,
and the owner ruling of 2026-08-26 ("the estate is authenticated") closes the door as well.

**Every route checks the lakehouse's estate-admin verb, ``can_observe_events``, on the estate root object**, asked
through the catalog's decision door. A flow acts on Serve endpoints, not on a table, so this service has no lakehouse
object to scope a check to, and the lakehouse model carries no relation of this service's own: the estate-wide surface
takes the estate-wide verb, as `compute` does.

Everything mechanical (bearer → verified subject, the three-outcome checker) comes from
``service_kit.governed.deps``, shared with the viewer and annotator rather than copied out of them.
With auth off (every knob defaults off) the subject is ``anon`` and the checker is permissive, so a
dev stack behaves exactly as before this module existed.
"""

from typing import Annotated

from fastapi import Depends

from flows.audit_actions import FlowsAuditAction
from flows.config import FlowsSettings
from flows.dependencies import FlowsSettingsDep
from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import AuditOutcome, audit
from service_kit.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(FlowsSettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: The lakehouse verb every flows route requires (see the module docstring).
ESTATE_ADMIN = "can_observe_events"


async def require_read(checker: FgaChecker, settings: FlowsSettings, subject: str) -> None:
    """Refuse a caller who may not look at the flow plane.

    A helper rather than a fourth inline copy: `routes.py` already spells the same three lines out
    three times for the run routes, and the audit's own `DUP` findings are about exactly that shape. The
    denial keeps the estate's FGA format — `<subject> lacks <relation> on <object>` — so the fix is
    in the message.
    """
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=ESTATE_ADMIN, obj=obj)
    audit(FlowsAuditAction.FLOWS_READ, AuditOutcome.ALLOW if allowed else AuditOutcome.DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{ESTATE_ADMIN}' on '{obj}' — reading the flow plane needs the estate admin")
