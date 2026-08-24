"""Ask the sidecar whether this app-id can actually reach an ACTOR state store.

WHY THIS EXISTS, measured live on 2026-08-24. `lance-statestore` carries `actorStateStore: "true"`
and is SCOPED to a list of app-ids. `medallion-producer` was missing from the scope list on the
deployed Component, and nothing anywhere reported it:

* The sidecar logs ``Actor state store not configured - actor hosting disabled, but invocation
  enabled`` and then ``Workflow engine started``, in that order. The second line is the one an
  operator greps for, and it is TRUE — the engine really does start.
* `WorkflowRuntime.start()` and `ActorRuntime.register_actor` are PROCESS-LOCAL. Neither touches
  the sidecar, so a lifespan that guards them with try/except sees success and reports healthy.
* The pod's probes stay green, because nothing it serves depends on actors until someone uses them.

The failure then surfaces at the worst possible moment and in the worst possible place: the first
`StartInstance` answers INTERNAL, and the follow-up state read NIL-DEREFERENCES daprd itself
(`wfengine/state.LoadWorkflowState`, dapr 1.18.1) — so a held promotion did not merely fail to
schedule, it SEGFAULTED the cascade head's sidecar, and the subscription's correct RETRY re-drove
the crash on every redelivery.

The discriminator is exact and cheap: a component the app-id cannot see is absent from its own
sidecar's metadata entirely, and one it can see advertises its capabilities. So "is there a loaded
component with the ACTOR capability" answers precisely the question the log lines do not.

NON-FATAL, deliberately, and for the same reason as :mod:`actor_warmup`: the services that host a
runtime also serve doors that do not need one. The medallion producer's `/produce` and
`/ingest-media` are the cascade's only ingress and work perfectly without actors — refusing to boot
would turn one dead capability into a dead estate. The verdict is logged at ERROR and returned, so a
caller may also surface it on a health detail.
"""

import asyncio
import json
import logging
import os
import urllib.request
from typing import Final


log = logging.getLogger(__name__)

#: Dapr's own default sidecar HTTP port. Overridden by `DAPR_HTTP_PORT`, which daprd injects.
_DEFAULT_DAPR_HTTP_PORT: Final = "3500"

#: The capability a state store advertises when it is usable as an ACTOR state store. Dapr reports
#: it per loaded component on `/v1.0/metadata`; a component this app-id is not scoped for does not
#: appear in that list at all, so absence and unusability collapse to the same answer here.
_ACTOR_CAPABILITY: Final = "ACTOR"

#: Short on purpose. This runs in a lifespan beside `warm_actor_proxy_factory`, and a sidecar that
#: cannot answer its own metadata route in this long is one whose verdict we would not trust anyway.
PROBE_TIMEOUT_SECONDS: Final = 5.0


def _actor_state_stores() -> list[str]:
    """Names of loaded components this app-id may use as an actor state store. Blocking."""
    port = os.environ.get("DAPR_HTTP_PORT") or _DEFAULT_DAPR_HTTP_PORT
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1.0/metadata", timeout=PROBE_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    return [component.get("name", "") for component in payload.get("components", []) or [] if _ACTOR_CAPABILITY in (component.get("capabilities") or [])]


async def probe_actor_state_store(*, capability: str) -> bool:
    """Whether this app-id can reach an actor state store. Reports the verdict, never raises.

    Args:
        capability: What breaks without one, named the way an operator would recognise it (e.g.
            "held promotions cannot be reviewed"). It goes in the ERROR line, because the whole
            point is that the existing logs describe the mechanism and never the consequence.

    Returns:
        True when the sidecar reports at least one ACTOR-capable component. False when it reports
        none, and ALSO false when it could not be asked — an unanswerable probe is not evidence of
        health, and the caller's own runtime guards already tolerate a sidecar that is not up.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            stores = await asyncio.to_thread(_actor_state_stores)
    except Exception:
        # Deliberately not an ERROR: at lifespan time a sidecar that has not finished booting is
        # ordinary, and the runtime guards handle it. Only a definite "none" is a misconfiguration.
        log.warning("could not ask the sidecar for its actor state store — %s may not work", capability, exc_info=True)
        return False
    if not stores:
        log.error(
            "NO ACTOR STATE STORE is visible to this app-id — %s. The workflow/actor runtime will "
            "still report that it started, and the first call will fail. Scope a component carrying "
            'actorStateStore: "true" to this app-id (chart: stateStore.scopes) and RESTART this pod '
            "— daprd reads actor state stores only at boot, so a scope added under a running sidecar "
            "does not reach it.",
            capability,
        )
        return False
    log.info("actor state store available to this app-id: %s", ", ".join(stores))
    return True
