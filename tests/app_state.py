"""An app holding the state a lifespan would have put on it, and a request to it, both Starlette's own.

A door, a dependency or a relay tick reads what the lifespan installed off ``app.state`` (a request reaches it as
``request.app.state``). A test that wants one piece of that state without running the lifespan builds the real app and
request here and sets only that piece: a name the code reads that is not set raises the ``AttributeError`` Starlette's
``State`` raises in the pod, where a stand-in object would answer whatever its author gave it.
"""

from __future__ import annotations

from fastapi import FastAPI, Request


__all__ = ["app_holding", "request_on"]


def app_holding(**state: object) -> FastAPI:
    """An app whose state holds ``state`` and nothing else."""
    app = FastAPI()
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


def request_on(**state: object) -> Request:
    """A request to an app whose state holds ``state``."""
    return Request({"type": "http", "app": app_holding(**state), "method": "GET", "path": "/", "headers": []})
