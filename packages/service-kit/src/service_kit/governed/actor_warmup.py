"""Pay the Dapr SDK's BLOCKING sidecar handshake at startup, off the event loop.

MEASURED, not theorised. `ActorProxy.create` builds a process-global `ActorProxyFactory` on first
use, and that constructor reaches `DaprHealth.wait_for_sidecar()` — a synchronous
`urllib` + `time.sleep` loop with a 60 s default (`DAPR_HEALTH_TIMEOUT`). It is therefore the FIRST
actor call in a process that pays it, and it pays it **on the event loop**: driving the notifications
service against a stub sidecar, one `POST` to the reconcile cron hung the whole app for 60 s and the
pass's own `asyncio.timeout` never fired — because the loop the timer would fire on was the blocked
one. A wall-clock budget cannot bound work that blocks the scheduler.

Two properties make this the right place to solve it rather than each call site:

* **Startup is where blocking is legitimate.** A lifespan may take seconds; a request handler may not.
  Moving the handshake here converts a per-request cliff into a boot cost the readiness probe already
  covers.
* **It is process-global.** The factory is cached on the class, so warming it once serves every actor
  proxy the service ever opens — which is why a fix in one service's call path would not travel.

Non-fatal by construction: a service with no sidecar must still start and serve its read plane. The
actor routes refuse separately (`require_actor_plane`), so a failed warm-up degrades exactly one
capability instead of the pod.
"""

import asyncio
import logging
from typing import Final


log = logging.getLogger(__name__)

#: How long to let the handshake run at boot before giving up on it. Deliberately far below the SDK's
#: own 60 s default: this is a WARM-UP, and a sidecar that has not answered in this long is one the
#: actor routes should be refusing anyway. Overrunning costs a slow first actor call, never a wrong one.
WARMUP_TIMEOUT_SECONDS: Final = 10.0


def _build_factory() -> None:
    """Touch the SDK's proxy factory so its constructor runs HERE, on a worker thread.

    Imported lazily for the reason every other `ActorProxy` call site imports lazily: importing it at
    module scope makes merely importing this module require a daprd to talk to.
    """
    from dapr.actor.client.proxy import ActorProxyFactory

    ActorProxyFactory()


async def warm_actor_proxy_factory(*, timeout_seconds: float = WARMUP_TIMEOUT_SECONDS) -> bool:
    """Build the actor proxy factory off the loop. Returns whether it succeeded.

    The return value is reported, never raised: callers are lifespans, and a lifespan that raises
    takes the pod with it over a capability the service can honestly refuse instead.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.to_thread(_build_factory)
    except TimeoutError:
        # The sidecar did not answer in time. The first real actor call will retry the handshake on
        # its own — slowly, and on the loop — which is why the actor routes gate on registration.
        log.warning("actor proxy factory warm-up timed out after %ss — the first actor call will pay it", timeout_seconds)
        return False
    except Exception:
        log.exception("actor proxy factory warm-up failed — the actor plane will be refused, not silently slow")
        return False
    log.info("actor proxy factory warmed")
    return True
