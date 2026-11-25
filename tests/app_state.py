"""An app holding the state a lifespan would have put on it, a request to it, and the app a test client serves.

A door, a dependency or a relay tick reads what the lifespan installed off ``app.state`` (a request reaches it as
``request.app.state``). A test that wants one piece of that state without running the lifespan builds the real app and
request here and sets only that piece: a name the code reads that is not set raises the ``AttributeError`` Starlette's
``State`` raises in the pod, where a stand-in object would answer whatever its author gave it.
"""

from typing import cast

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


__all__ = ["app_holding", "app_of", "request_on"]


def app_holding(**state: object) -> FastAPI:
    """An app whose state holds ``state`` and nothing else."""
    app = FastAPI()
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


def request_on(**state: object) -> Request:
    """A request to an app whose state holds ``state``."""
    return Request({"type": "http", "app": app_holding(**state), "method": "GET", "path": "/", "headers": []})


def app_of(client: TestClient) -> FastAPI:
    """The FastAPI app ``client`` serves, whose ``state``, ``dependency_overrides`` and ``openapi()`` a test reaches.

    Starlette types ``TestClient.app`` as any ASGI callable, and every rask test client serves a FastAPI app.
    """
    return cast("FastAPI", client.app)
