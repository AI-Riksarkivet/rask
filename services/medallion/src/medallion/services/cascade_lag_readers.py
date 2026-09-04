"""The two real reads behind the cascade-lag tick: the catalog's tag, and lineage's run board.

`run_lag_tick` is pure over two readers precisely so these can be swapped in a test; this module is the
production pair, and the only place that knows a catalog speaks HTTP and a lineage graph answers a
board query.

BOTH READERS MAY RAISE, and are meant to. `run_lag_tick` contains a failure to its own edge and counts
it, which is the behaviour that keeps one unreadable table from blanking the estate — so returning a
sentinel here would defeat a decision made one layer up.

THE EDGES COME FROM THE PRODUCER'S OWN LANE MAP. `transform_routes` is the declared source-namespace
set, which is why C3 is homed here: a mover knows only its own lane, and lineage can only infer an edge
from a run that already happened — so neither can see a first-ever hop, which is the case this detector
most needs to report.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx


log = logging.getLogger(__name__)

#: Short. A lag tick that blocks on one unreachable store delays every edge behind it, and the value is
#: a level that the next tick will re-measure anyway.
_TIMEOUT_SECONDS = 10.0

#: The tag the catalog moves on a successful publish. One string, matching `publication.PUBLISHED_TAG`.
_PUBLISHED_TAG = "published"


def declared_edges(settings: Any) -> Sequence[tuple[str, str]]:  # noqa: ANN401 — the settings seam
    """Every (edge, project) pair this deployment declares.

    An edge is named ``<from_namespace>-><to_namespace>`` from the lane map. The project comes from the
    medallion's configured tenant set; a single-tenant estate reports one row per lane.
    """
    routes = getattr(settings, "transform_routes", {}) or {}
    projects = [p for p in (getattr(settings, "lag_projects", None) or []) if p] or [""]
    return [(f"{source}->{_destination(settings, source)}", project) for source in sorted(routes) for project in projects]


def _destination(settings: Any, source: str) -> str:  # noqa: ANN401 — the settings seam
    """The tier a lane feeds. Read from the mover declaration rather than derived from the tier name,
    because a lane may fan out (``bronze-media`` -> ``silver-media``) and a naming convention would
    quietly mislabel it."""
    lanes = getattr(settings, "lane_destinations", {}) or {}
    return str(lanes.get(source) or "?")


def published_reader(settings: Any) -> Any:  # noqa: ANN401 — the settings seam
    """Read the source table's ``published`` tag version from the catalog."""

    def _read(edge: str, project: str) -> int | None:
        source = edge.split("->", 1)[0]
        table_id = f"{project}-{source}" if project else source
        # `GET /v1/table/{id}/tags/list` — the spec's ListTableTags, with a GET compat alias. There is
        # NO route exposing the published version directly: the publication router serves `POST
        # /{id}/publish` and nothing else, which the first live tick proved by failing every edge.
        url = f"{str(settings.catalog_url).rstrip('/')}/v1/table/{table_id}/tags/list"
        response = httpx.get(url, timeout=_TIMEOUT_SECONDS)
        if response.status_code == 404:
            return None  # no such table yet — an idle edge, not a failure
        response.raise_for_status()
        # `{"tags": {"<name>": {"version": N, "branch": ..., "manifest_size": ...}}}`, verified against
        # the installed client's `TagContents` rather than assumed.
        tag = (response.json().get("tags") or {}).get(_PUBLISHED_TAG)
        version = tag.get("version") if isinstance(tag, dict) else None
        return version if isinstance(version, int) and version >= 0 else None

    return _read


def consumed_reader(settings: Any) -> Any:  # noqa: ANN401 — the settings seam
    """The highest source version the destination has consumed, from lineage's run board.

    Reads ``consumed_to_version`` — the field `838a33c5` made queryable. A run that predates that
    commit carries none, so a freshly upgraded estate reports its edges UNKNOWN until each runs once;
    that is stated in the spec rather than smoothed over here.
    """

    def _read(edge: str, project: str) -> int | None:
        destination = edge.split("->", 1)[1]
        url = f"{str(settings.train_lineage_url).rstrip('/')}/api/v1/runs"
        response = httpx.get(url, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        seen = [
            run["consumed_to_version"]
            for run in response.json().get("runs", [])
            if isinstance(run.get("consumed_to_version"), int) and any(destination in str(output) for output in run.get("outputs", []))
        ]
        return max(seen) if seen else None

    return _read
