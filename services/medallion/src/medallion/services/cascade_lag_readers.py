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
from urllib.parse import quote

import httpx

from medallion.core.config import dedicated_token_for
from medallion.services import catalog_register
from medallion.services.cascade_lag import ConsumedRange, EdgeNotMeasurable
from service_kit.lakehouse.record_store import list_records
from service_kit.lakehouse.warehouse_records import REGISTRY_PREFIX, measurable_projects
from service_kit.lakehouse.warehouse_registry import project_namespace


log = logging.getLogger(__name__)

#: Short. A lag tick that blocks on one unreachable store delays every edge behind it, and the value is
#: a level that the next tick will re-measure anyway.
_TIMEOUT_SECONDS = 10.0

#: The tag the catalog moves on a successful publish. One string, matching `publication.PUBLISHED_TAG`.
_PUBLISHED_TAG = "published"


def declared_edges(settings: Any) -> Sequence[tuple[str, str]]:  # noqa: ANN401 — the settings seam
    """Every (edge, project) pair this deployment declares.

    An edge is named ``<from_namespace>-><to_namespace>`` from the lane map. The PROJECTS come from the
    warehouse registry — the same authority `sweep.py::_buckets_to_sweep` asks — because a project is
    minted at runtime by `POST /v1/projects` with an operator-chosen id, so any statically configured
    list is stale the moment a tenant is onboarded, and an unmeasured tenant has no series at all
    rather than a visibly missing one. `lag_projects` remains as an explicit OVERRIDE for an estate
    that wants a subset; empty means "ask the registry".

    A registry that cannot be read falls back to the single-tenant row rather than measuring nothing:
    a detector that goes silent on a storage blip is the failure mode this whole module exists to
    avoid, and `run_lag_tick` reports each edge's own read failure anyway.
    """
    routes = getattr(settings, "transform_routes", {}) or {}
    projects = [p for p in (getattr(settings, "lag_projects", None) or []) if p] or _registry_projects(settings)
    return [(f"{source}->{_destination(settings, source)}", project) for source in sorted(routes) for project in projects]


def _registry_projects(settings: Any) -> list[str]:  # noqa: ANN401 — the settings seam
    """Live tenants from the warehouse registry, or ``[""]`` — the single-tenant row — when it has none
    or cannot be read.

    ``[""]`` rather than ``[]`` on the empty path is deliberate: a single-tenant estate has no registry
    records at all and its lane tables are unqualified, so an empty list would measure nothing and
    report a clean tick.
    """
    root = getattr(settings, "control_root", "") or ""
    if not root:
        return [""]
    try:
        records = list_records(root, settings.storage_options(), REGISTRY_PREFIX, event="warehouse_record")
        projects = measurable_projects(list(records))
    except Exception as exc:  # noqa: BLE001 — a registry blip must not blank the detector
        log.warning("cascade_lag_registry_unreadable", extra={"root": root, "error": str(exc)})
        return [""]
    return projects or [""]


def _destination(settings: Any, source: str) -> str:  # noqa: ANN401 — the settings seam
    """The tier a lane feeds. Read from the mover declaration rather than derived from the tier name,
    because a lane may fan out (``bronze-media`` -> ``silver-media``) and a naming convention would
    quietly mislabel it."""
    lanes = getattr(settings, "lane_destinations", {}) or {}
    return str(lanes.get(source) or "?")


def _service_headers(settings: Any) -> dict[str, str]:  # noqa: ANN401 — the settings seam
    """The credential these reads present, built by the SAME function every other medallion client uses.

    Measured live 2026-09-04, in two stages, and the second is why this delegates rather than
    reimplements. Both readers first sent a bare `httpx.get` with no headers at all → **401 on every
    edge**. Adding the obvious pair (`dapr-api-token` + `x-lance-service-identity`) still 401'd,
    because `service-medallion-producer` is a PRIVILEGED subject: the door binds it to
    `service-token-<identity>` and refuses the estate's shared token. `catalog_register._credential`
    has resolved that since 2026-08-26 — a second hand-written copy of a credential rule is exactly
    how one caller ends up refused while every other works.

    The gauge's own `known=False` path is what made this survive: a reader that cannot read publishes
    NOTHING and reports nothing wrong, so an empty series read as a healthy cascade.
    """
    return catalog_register.credential(
        token=None,
        app_token=getattr(settings, "app_api_token", "") or None,
        service_identity=getattr(settings, "catalog_service_identity", "") or None,
        dedicated_token=dedicated_token_for(settings),
    )


def _lane_table(settings: Any, lane: str, *, table_map: str) -> str | None:  # noqa: ANN401 — the settings seam
    """One lane's SOURCE or DESTINATION table id, project-unqualified, or ``None`` when undeclared.

    Read from the mover declarations (`movers[].fromDataset` / `.toDataset`, rendered by
    `chart/templates/medallion.yaml`) rather than composed from the tier name. A lane's table is a NAME
    the deployment chooses — `bronze$events`, `bronze-media$objects` — and nothing about the namespace
    predicts it, which is why the first version's `f"{project}-{source}"` could name no table at all.
    """
    return (getattr(settings, table_map, {}) or {}).get(lane) or None


def published_reader(settings: Any) -> Any:  # noqa: ANN401 — the settings seam
    """Read the source table's ``published`` tag version from the catalog.

    THE ID IS A TABLE, not a namespace. `fga_deps.require_parent` refuses a single-segment table id at
    every create door, so a bare `acme-bronze` names nothing that can exist and the route 404s forever —
    which this reader maps to "never published", i.e. lag 0 on an edge that may never have run.
    """

    def _read(edge: str, project: str) -> int | None:
        lane = edge.split("->", 1)[0]
        dataset = _lane_table(settings, lane, table_map="lane_sources")
        if dataset is None:
            # An undeclared lane is UNREADABLE, not idle: raising lets `run_lag_tick` count the edge
            # FAILED, where returning None would publish a confident 0 for a lane nobody described.
            raise LookupError(f"lane {lane!r} declares no source dataset; set MEDALLION_LANE_SOURCES")
        table_id = project_namespace(project, dataset)
        # `GET /v1/table/{id}/tags/list` — the spec's ListTableTags, with a GET compat alias. There is
        # NO route exposing the published version directly: the publication router serves `POST
        # /{id}/publish` and nothing else, which the first live tick proved by failing every edge.
        url = f"{str(settings.catalog_url).rstrip('/')}/v1/table/{quote(table_id, safe='')}/tags/list"
        response = httpx.get(url, timeout=_TIMEOUT_SECONDS, headers=_service_headers(settings))
        if response.status_code in (403, 404):
            # ONE answer for "absent" and "forbidden" — the catalog's no-existence-oracle rule, with
            # its authz gate running before existence resolution. Neither "idle" nor "broken" is
            # honest, so the edge is excluded from the tick rather than given a fabricated level.
            raise EdgeNotMeasurable(f"{table_id!r} is not visible to this subject")
        response.raise_for_status()
        # `{"tags": {"<name>": {"version": N, "branch": ..., "manifest_size": ...}}}`, verified against
        # the installed client's `TagContents` rather than assumed.
        tag = (response.json().get("tags") or {}).get(_PUBLISHED_TAG)
        version = tag.get("version") if isinstance(tag, dict) else None
        return version if isinstance(version, int) and version >= 0 else None

    return _read


def consumed_reader(settings: Any) -> Any:  # noqa: ANN401 — the settings seam
    """EVERY delta range the destination has consumed, from lineage's run board.

    RANGES, NOT A CEILING. `consumed_frontier` needs the lower bound to tell coverage from a gap, and a
    reader that reduced to `max(to_version)` discarded the only evidence of a lost hop before the
    detector saw the numbers.

    MATCHED ON THE FULL TABLE ID, project-qualified. A substring test over the destination NAMESPACE
    matches every tenant and every fan-out lane that shares the prefix — `silver` is inside
    `beta-silver$features` and `acme-silver-media$features` — and `(edge, project)` is the gauge's own
    key, so the project must reach the query or the answer belongs to somebody else.

    Reads the range `838a33c5` made queryable. A run predating it carries no ceiling and is skipped: it
    consumed no stated delta, and borrowing 0 would claim coverage it never had.
    """

    def _read(edge: str, project: str) -> list[ConsumedRange]:
        lane = edge.split("->", 1)[0]
        dataset = _lane_table(settings, lane, table_map="lane_destination_datasets")
        if dataset is None:
            raise LookupError(f"lane {lane!r} declares no destination dataset; set MEDALLION_LANE_DESTINATION_DATASETS")
        wanted = project_namespace(project, dataset)
        # `/runs`, NOT `/api/v1/runs`. Lineage mounts its run board at the root — measured live
        # 2026-09-04 by probing all three spellings against the deployed service: `/v1/runs` 404,
        # `/api/v1/runs` 404, `/runs` 401 (present, and asking for the credential below). This is the
        # SECOND instance of the same defect in this file's short history, the first being a catalog
        # route that did not exist; a route is not a thing to derive from a prefix convention.
        url = f"{str(settings.train_lineage_url).rstrip('/')}/runs"
        response = httpx.get(url, timeout=_TIMEOUT_SECONDS, headers=_service_headers(settings))
        response.raise_for_status()
        ranges: list[ConsumedRange] = []
        for run in response.json().get("runs", []):
            ceiling = run.get("consumed_to_version")
            if not isinstance(ceiling, int) or isinstance(ceiling, bool):
                continue
            if not any(str(output) == wanted for output in run.get("outputs", [])):
                continue
            floor = run.get("consumed_from_version")
            ranges.append(ConsumedRange(from_version=floor if isinstance(floor, int) and not isinstance(floor, bool) else None, to_version=ceiling))
        return ranges

    return _read
