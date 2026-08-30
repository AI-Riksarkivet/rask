"""THE media-service lifespan — one implementation for viewer, search and annotator.

The three are one shape wearing three names: a Lance media service over ``service_kit.media``. Each
one hand-wrote the same startup and the same teardown, and open_python-audit DUP-16 is that copy.
The copies had already drifted in the way copies do — not in what they did, but in the ORDER they did
it:

* viewer disposed the OpenFGA client, THEN disarmed the SIGTERM drain;
* search disarmed the drain, THEN disposed;
* annotator disposed, closed a Dapr channel the other two do not have, THEN disarmed.

None of those orders is wrong on its own, and that is exactly why three copies could hold three of
them: nothing compared them. One implementation makes the order a decision instead of an accident.

WHAT IS GENUINELY PER-SERVICE stays per-service, and is passed in rather than branched on here:
which resources this service opened and must therefore close (``closes``), and any extra startup a
service needs before it announces readiness (``setup``) with its matching teardown. The annotator's
actor plane is the only user of that hook, and it must run BEFORE ``startup_complete`` — a pod that
reports ready while its task actors are unregistered is a pod answering 503 to every task route.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

import httpx
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.auth_lifespan import _GovernedSettings, attach_auth
from service_kit.governed.fga import dispose as fga_dispose
from service_kit.media.config import MediaSettings
from service_kit.media.state import AppState, dataset_handle


log = logging.getLogger(__name__)

#: Extra startup for one service, run after `attach_auth` and BEFORE the readiness flags.
type MediaSetup = Callable[[FastAPI, AppState], Awaitable[None]]
#: Its matching teardown, run first in the `finally` — before anything shared is disposed.
type MediaTeardown = Callable[[FastAPI], Awaitable[None]]
#: The resources THIS service opened, named per service because `AppState` carries slots for all
#: three (the search service's model handles among them) and closing a slot you did not fill is a
#: no-op that reads as ownership.
type MediaCloses = Callable[[AppState], Sequence[Any]]


def make_media_state(settings: MediaSettings) -> AppState:
    """The media plane's `AppState`, with its one process HTTP pool.

    One line, and it was written in four places — the three lifespans plus `viewer.create_viewer_state`
    — which is enough for it to have been the thing DUP-16's grep found. It lives beside the lifespan
    that uses it so "what a media service opens at boot" has one answer.
    """
    return AppState(settings=settings, http=httpx.Client())


@asynccontextmanager
async def media_lifespan(
    app: FastAPI,
    settings: MediaSettings,
    *,
    service: str,
    closes: MediaCloses,
    setup: MediaSetup | None = None,
    teardown: MediaTeardown | None = None,
) -> AsyncIterator[AppState]:
    """Run one media service's whole lifecycle. Yields the ``AppState`` it built.

    Startup, in order:

    1. Build ``AppState`` onto ``app.state.resources`` — the shape every media router reads through
       ``StateDep``.
    2. Warm the DEFAULT dataset handle OFF THE STARTUP LOOP. ``settings.storage_options()`` is a
       blocking Dapr secret fetch on the cold path, so it runs in a thread; because ``_store_secret``
       is cached this also warms the secret before serving, making every request-path read a pure
       dict build. A failure here is logged and TOLERATED: ``/livez`` stays green and per-request
       resolution surfaces the problem as a domain 404, which is the honest answer for a service
       whose other datasets are fine.
    3. ``attach_auth``. Both halves default OFF and the service behaves exactly as it always did when
       they are. Built here, never at import — an ``OIDCVerifier`` fetches discovery, so module scope
       would make importing a main do I/O. A failure to BUILD is logged and non-fatal: the dependency
       then finds no verifier/client on ``app.state`` and answers 503, which is the honest reading.
       Falling back to a permissive checker would turn a broken authorization layer into an open one.
    4. The service's own ``setup``, if any — before readiness is announced.
    5. The lifecycle flags ``/readyz`` reads, then the SIGTERM drain.

    THE DRAIN IS ARMED AT SIGTERM, not at lifespan shutdown: the ``shutting_down`` flag below flips in
    the ``finally``, which uvicorn only reaches AFTER it has stopped accepting connections and
    drained — so the admission guards that read it refused nothing, ever. Kubernetes sends SIGTERM at
    the START of termination, and that window is exactly when the sidecar is still delivering. (Owner
    ruling 2026-08-25.)

    Teardown runs in a ``finally``, so an exception past the ``yield`` cannot skip it — the bare
    ``yield`` this replaces leaked the http client, the embedder and the reranker on exactly the path
    where leaking matters.
    """
    state = make_media_state(settings)
    app.state.resources = state
    try:
        handle = await run_in_threadpool(dataset_handle, state)  # fail-fast open of the default descriptor
        log.info("%s: default dataset %s ready (%d tables)", service, handle.id, len(handle.descriptor.tables))
    except Exception:
        log.exception("%s: default dataset failed to open — serving degraded", service)
    # CAST, not a widened parameter: `attach_auth` wants the structural `_GovernedSettings` (issuer,
    # audience, the FGA coordinates), and every settings object that reaches here is a `MediaSettings`
    # ALSO mixing in `GovernedAuthSettings` — `ViewerSettings`, `SearchSettings`, `AnnotatorSettings`
    # all do. Python has no intersection type to say that, and typing the parameter as the protocol
    # instead would lose the `MediaSettings` that `AppState` requires. The invariant is enforced where
    # it can be: `tests/unit/test_governed_services_wire_their_gate.py` refuses a governed service
    # whose settings do not carry the mixin.
    await attach_auth(app, cast(_GovernedSettings, settings), service=service)
    if setup is not None:
        await setup(app, state)
    app.state.startup_complete = True
    app.state.shutting_down = False
    _disarm_drain = arm_drain_on_sigterm(app)
    try:
        yield state
    finally:
        if teardown is not None:
            await teardown(app)
        # The OpenFGA client this lifespan opened. Its SDK is aiohttp-backed, so collecting it
        # unclosed leaves one half-open connection per replica until OpenFGA's own idle timeout, with
        # an "Unclosed client session" line as the only trace. Disposal lives beside the factory
        # (`service_kit.governed.fga.dispose`) rather than as a third copy of the same block.
        await fga_dispose(app)
        _disarm_drain()
        app.state.shutting_down = True
        for resource in closes(state):
            close = getattr(resource, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    log.warning("error closing %s on shutdown", type(resource).__name__)


def make_media_lifespan(
    settings_factory: Callable[[], MediaSettings],
    *,
    service: str,
    closes: MediaCloses,
    setup: MediaSetup | None = None,
    teardown: MediaTeardown | None = None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """The lifespan as FastAPI wants it: a one-argument factory over ``app``.

    ``settings_factory`` rather than a settings OBJECT because a main is imported long before it is
    run — resolving the settings at import would make the module's import order decide the process's
    configuration, which is the trap ``AppState``'s own per-state settings note records.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with media_lifespan(app, settings_factory(), service=service, closes=closes, setup=setup, teardown=teardown):
            yield

    return lifespan
