"""Shared FastAPI dependencies (Annotated type aliases) for the lineage service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from lineage.core.config import LineageSettings, get_settings
from lineage.services.names import CatalogNames
from lineage.services.provenance import ProvenanceStore


SettingsDep = Annotated[LineageSettings, Depends(get_settings)]


def get_provenance(request: Request) -> ProvenanceStore:
    """The provenance store — the feed and the edge list the catalog keeps — built once in the lifespan."""
    return request.app.state.provenance


ProvenanceDep = Annotated[ProvenanceStore, Depends(get_provenance)]


def get_names(request: Request) -> CatalogNames:
    """The catalog's names resolved to uuids as the caller sees them, built once in the lifespan."""
    return request.app.state.names


NamesDep = Annotated[CatalogNames, Depends(get_names)]


def get_bearer(request: Request) -> str | None:
    """The caller's own bearer, which a name is resolved with (:mod:`lineage.services.names`); ``None`` when it sent none."""
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


BearerDep = Annotated[str | None, Depends(get_bearer)]


def get_publisher(request: Request) -> object | None:
    """The Dapr publisher that re-announces a recovered outbox event, or ``None`` without the outbox.

    ``None`` is a real answer, not a missing dependency: the publisher exists only to re-publish what the
    relay drains or an operator replays, and a deployment with no ``outbox_uri`` does neither. Returning it
    rather than raising keeps ``services.staged.republish_staged`` the single place that decides whether a
    re-publish is possible.
    """
    return getattr(request.app.state, "dapr", None)


PublisherDep = Annotated[object | None, Depends(get_publisher)]
