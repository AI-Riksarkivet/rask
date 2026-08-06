"""The ONE shared `WorkflowRuntime`, so `workflow.py` and `activities.py` both decorate against it.

Why a separate module and not `workflow.py`: the workflow body calls the activities, so if the
runtime lived beside the workflow definitions the activities would have to import the workflow
module to reach it, and `workflow → activities → workflow` is a cycle. One module below both breaks
it: `workflow.py → activities.py → runtime.py`.

**Lazy by IMPORT, and that is the whole mechanism.** Constructing a `WorkflowRuntime` builds a gRPC
worker aimed at the sidecar; `dapr.ext.workflow` also pulls grpc in at import. Neither belongs in
the cost of starting a service that, without a sidecar, will only ever run flows inline. So nothing
in the HTTP path imports this module — `lifespan.py` imports `flows.workflow` (which pulls this in,
and with it every `@wfr.workflow` / `@wfr.activity` registration) ONLY when `DAPR_GRPC_PORT` says a
sidecar exists. Import this module from the request path and you have made every process pay for the
durable lane, including the ones that cannot use it.
"""

import dapr.ext.workflow as wf


wfr = wf.WorkflowRuntime()
"""The runtime the decorators register into. Registration happens at import of `flows.workflow` and
`flows.activities`; `wfr.start()` — which actually connects to the sidecar — is the lifespan's call,
never a side effect of importing."""
