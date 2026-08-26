"""Who may run a flow.

``POST /flows/runs`` executes a graph whose model nodes invoke LIVE Ray Serve endpoints — it spends
the estate's GPU compute on whatever the caller drew. It shipped ungated, which was the documented
"localhost / trusted network" posture and stopped being defensible the moment the gateway carried
``/api/flows`` to a browser.

**``/catalog`` and ``/validate`` are gated too, since 2026-08-26, and this docstring used to say the
opposite** — "the catalog and validate routes stay open: they read a server-declared registry and run
graph hygiene, no cluster involved". That reasoning was about the EXECUTE tier and it was sound on its
own terms. It did not cover unmetered PARSING, which is what actually bit: ``/validate`` accepted an
arbitrary-size graph and built every node before the 256-node ceiling in ``graph.py`` was consulted —
measured at 3.00 s of event-loop block for a 23 MiB body, on a service with one replica and one loop,
which also serves ``/livez``. The bound now lives on ``FlowGraph.nodes`` where pydantic can refuse it,
and the owner ruling of 2026-08-26 ("the estate is authenticated") closes the door as well. They take
the READ rung rather than EXECUTE — looking at the palette is not spending compute.

**The relation is the ``writer`` tier on the estate root object**, not a ``can_*`` verb, and that is
a stated interim: the model (`service_kit/governed/auth/model.fga`) defines no flow-execution verb
yet, and this service has no per-project context to scope one to — a flow acts on Serve endpoints,
not on a table. Checking the tier keeps the WHO in tuples (an estate writer may spend estate
compute; the bootstrap owner passes because ``writer: ... or owner``), while the verb→tier mapping
lives here, in one named constant — the `_OWNER_SUFFIX_RELATION` precedent. The follow-up that
retires it is a model change (``define can_execute_flows: writer`` on ``warehouse``), which touches
no tuples because the grant stays tier-based.

Everything mechanical (bearer → verified subject, the three-outcome checker) comes from
``service_kit.governed.deps``, shared with the viewer and annotator rather than copied out of them.
With auth off (every knob defaults off) the subject is ``anon`` and the checker is permissive, so a
dev stack behaves exactly as before this module existed.
"""

from typing import Annotated

from fastapi import Depends

from flows.config import FlowsSettings
from flows.dependencies import FlowsSettingsDep
from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import ALLOW, DENY, audit
from service_kit.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(FlowsSettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: The tier running a flow requires — see the module docstring for why it is a tier, not a verb.
EXECUTE = "writer"

#: The tier LOOKING at the flow plane requires. Lower than EXECUTE on purpose: reading the node
#: palette or checking a graph for dangling edges spends nothing, so it should not need the rung that
#: spends GPU. `warehouse#reader` is defined `... or writer or member from project`, so everyone who
#: passes EXECUTE passes this automatically and no existing caller loses access.
READ = "reader"


async def require_read(checker: FgaChecker, settings: FlowsSettings, subject: str) -> None:
    """Refuse a caller who may not look at the flow plane.

    A helper rather than a fourth inline copy: `routes.py` already spells the same three lines out
    three times for EXECUTE, and the audit's own `DUP` findings are about exactly that shape. The
    denial keeps the estate's FGA format — `<subject> lacks <relation> on <object>` — so the fix is
    in the message.
    """
    obj = settings.fga_root_object
    allowed = await checker(user=subject, relation=READ, obj=obj)
    audit("flows_read", ALLOW if allowed else DENY, subject=subject, resource=obj)
    if not allowed:
        raise ForbiddenError(f"'{subject}' lacks '{READ}' on '{obj}' — reading the flow plane needs the estate reader tier")
