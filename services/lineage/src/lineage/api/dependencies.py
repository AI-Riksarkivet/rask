"""Shared FastAPI dependencies (Annotated type aliases) for the lineage service."""

from __future__ import annotations

import logging
from typing import Annotated, Final, cast, overload

from fastapi import Depends, FastAPI, Request
from lance_namespace import InternalError

from lakehouse.events.bus import Bus
from lineage.core.config import LineageSettings, get_settings
from lineage.services.names import CatalogNames
from lineage.services.provenance import ProvenanceStore


log = logging.getLogger(__name__)

SettingsDep = Annotated[LineageSettings, Depends(get_settings)]

#: What ``getattr`` answers for a wire the lifespan never set, told apart from the ``None`` a lifespan sets for "off".
_UNSET: Final = object()


@overload
def app_wire[T](app: FastAPI, name: str, kind: type[T]) -> T: ...


@overload
def app_wire[T](app: FastAPI, name: str, kind: type[T], *, none_is_off: bool) -> T | None: ...


def app_wire[T](app: FastAPI, name: str, kind: type[T], *, none_is_off: bool = False) -> T | None:
    """What lineage's lifespan wired onto ``app.state`` as ``name``: the provenance store, the bus, the forward client.

    Off is what a setting says, and the lifespan sets the explicit off value for it (`lineage.main._build`), so a wire
    it did not set is a lineage that did not start the way it serves, never "off" ([[XC-219]]): read as off, a run's
    author check would be skipped, or a recorded event never forwarded, without a word. ``none_is_off`` names a wire
    whose off value the lifespan sets as ``None`` (the bus of a process named no bus, the forward client of one named
    no graph backend): there ``None`` is the answer, and only a wire never set is the fault. A request, a tick and a
    delivery all read it off the app. The ERROR line names the wire, since the caller's 500 is redacted.

    ``kind`` names what the lifespan wires there, for the reader's type; it is not checked.

    Raises:
        InternalError: Nothing is wired as ``name``, or ``None`` is, where ``None`` is not that wire's off value.
    """
    wire = getattr(app.state, name, _UNSET)
    if wire is _UNSET or (wire is None and not none_is_off):
        log.error("missing_wire", extra={"wire": name})
        raise InternalError(f"lineage has no {name} wired: its lifespan did not build it")
    return cast("T | None", wire)


def get_provenance(request: Request) -> ProvenanceStore:
    """The provenance store — the feed and the edge list the catalog keeps — built once in the lifespan."""
    return app_wire(request.app, "provenance", ProvenanceStore)


ProvenanceDep = Annotated[ProvenanceStore, Depends(get_provenance)]


def get_names(request: Request) -> CatalogNames:
    """The catalog's names resolved to uuids as the caller sees them, built once in the lifespan."""
    return app_wire(request.app, "names", CatalogNames)


NamesDep = Annotated[CatalogNames, Depends(get_names)]


def get_bearer(request: Request) -> str | None:
    """The caller's own bearer, which a name is resolved with (:mod:`lineage.services.names`); ``None`` when it sent none."""
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


BearerDep = Annotated[str | None, Depends(get_bearer)]


def get_publisher(request: Request) -> Bus | None:
    """This process's connection to the bus, which re-announces a recovered outbox event, or ``None`` without the outbox.

    ``None`` is a real answer, not a missing dependency: the publisher exists only to re-publish what the
    relay drains or an operator replays, and a deployment with no ``outbox_uri`` does neither. Returning it
    rather than raising keeps ``services.staged.republish_staged`` the single place that decides whether a
    re-publish is possible. It is the ``None`` the lifespan sets for a process named no bus; a lineage whose lifespan
    set nothing there faults (:func:`app_wire`).
    """
    return app_wire(request.app, "bus", Bus, none_is_off=True)


PublisherDep = Annotated[Bus | None, Depends(get_publisher)]
