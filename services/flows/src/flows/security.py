"""Who may run a flow.

``POST /flows/runs`` executes a graph whose model nodes invoke LIVE Ray Serve endpoints — it spends
the estate's GPU compute on whatever the caller drew. It shipped ungated, which was the documented
"localhost / trusted network" posture and stopped being defensible the moment the gateway carried
``/api/flows`` to a browser. The catalog and validate routes stay open: they read a server-declared
registry and run graph hygiene, no cluster involved.

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

from flows.dependencies import FlowsSettingsDep
from service_kit.governed.deps import FgaChecker, make_auth_deps


_deps = make_auth_deps(FlowsSettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: The tier running a flow requires — see the module docstring for why it is a tier, not a verb.
EXECUTE = "writer"
