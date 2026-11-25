"""An app under a real uvicorn, for a test whose claim is about the wire (how a request's query string and body are
parsed, a client that speaks HTTP to a server, a stock client of the namespace doors), which `TestClient` cannot make:
it calls the ASGI app in-process, with no socket and no server between."""

import asyncio
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI


#: The event loop each served app runs on, by its base URL, while :func:`serve` serves it.
_LOOPS: dict[str, asyncio.AbstractEventLoop] = {}

#: How long a server may take to run its app's lifespan: a hang bound, far above a catalog's boot under a loaded suite.
_START_SECONDS = 150


@contextmanager
def serve(app: FastAPI) -> Generator[str]:
    """``app`` under uvicorn on a thread, its lifespan run; yields its base URL once it accepts connections."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _START_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    _LOOPS[url] = server.servers[0].get_loop()
    try:
        yield url
    finally:
        del _LOOPS[url]
        server.should_exit = True
        thread.join(timeout=30)


def loop_of(url: str) -> asyncio.AbstractEventLoop:
    """The event loop of the app :func:`serve` serves at ``url``, for running a coroutine on the app's own loop."""
    return _LOOPS[url]
