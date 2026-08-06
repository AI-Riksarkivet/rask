"""Read-side warehouse-registry resolver (#84) — project → active warehouse root, off the control bucket.

The catalog WRITES warehouse records (``services/catalog/services/warehouses.py``) as plain JSON at
``<control_root>/_warehouses/<id>.json`` with fields ``{id, bucket, root_uri, project, status, ...}``.
This module is the shared READ half for services that must route by *project* without a catalog client
(the established catalog↔compaction contract shape — see :mod:`service_kit.lakehouse.maintenance_policies`): the
medallion movers/producer resolve a project-carrying trigger to that project's warehouse root and lay the
stages out as ``<root_uri>/medallion/<namespace>``.

Records are immutable except ``status``, so resolution accepts short staleness through a per-process TTL
cache — positive hits only, meaning a freshly provisioned warehouse is visible immediately while a
deactivation is honored within one TTL window (default 5s; ``WAREHOUSE_REGISTRY_TTL_SECONDS``
overrides). All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import pyarrow.fs as pafs

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

_REGISTRY_PREFIX = "_warehouses"

#: One path-safe project id segment. Trigger ``project`` values become S3 key prefixes, Lance dataset
#: URIs, and lineage namespace qualifiers — anything outside this shape (traversal dots, separators,
#: whitespace) must be REJECTED at the boundary, never repaired. Same shape as the medallion train
#: head's name rule (``services/medallion/services/train.py``).
PROJECT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_PROJECT_RE = re.compile(PROJECT_PATTERN)

#: Env override for the positive-cache TTL — operators trading resolver load against how long a
#: DEACTIVATED warehouse may keep resolving (the stale-positive window).
_TTL_ENV_VAR = "WAREHOUSE_REGISTRY_TTL_SECONDS"
#: Small by design: the cache only ever serves stale POSITIVES, and the harm of one (routing a tenant
#: into a warehouse an admin just deactivated) outweighs the saved registry listing.
_DEFAULT_TTL_SECONDS = 5.0
#: ``(control_root, project, serving) → (expires_at_monotonic, root_uri)`` — positive resolutions only.
#: ``serving`` partitions the cache by warehouse class (``""`` = work, ``"gold"`` = serving), so a cached
#: work root can never answer a gold lookup or vice versa.
_cache: dict[tuple[str, str, str], tuple[float, str]] = {}


def _default_ttl_seconds() -> float:
    """The positive-cache TTL: ``WAREHOUSE_REGISTRY_TTL_SECONDS`` when set + parseable, else 5s."""
    raw = os.environ.get(_TTL_ENV_VAR)
    if raw:
        try:
            return float(raw)
        except ValueError:
            log.warning("warehouse_registry_ttl_invalid", extra={"value": raw})
    return _DEFAULT_TTL_SECONDS


class UnresolvableProjectError(RuntimeError):
    """A project-carrying request/trigger could not be routed to an active warehouse (fail closed).

    Raised by CALLERS when :func:`project_root` returns ``None`` (or resolution is disabled) and the
    flow must refuse rather than fall back to the shared default roots — falling back would silently
    read/write the wrong tenant's data while emitting real-looking lineage for it.
    """


def is_safe_project(value: object) -> bool:
    """True iff ``value`` is a path-safe project id (safe as an S3-key/URI/lineage-name segment)."""
    return isinstance(value, str) and _PROJECT_RE.fullmatch(value) is not None


def project_namespace(project: str, name: str) -> str:
    """Project-qualify a lineage namespace or dataset name — ``("acme", "bronze")`` → ``"acme-bronze"``.

    Empty ``project`` → ``name`` unchanged (the single-tenant default, byte-identical). Qualification
    keeps per-project lineage on DISTINCT graph nodes — the lineage repository MERGEs ``Dataset`` nodes
    on name alone, so two tenants both emitting ``bronze$events`` would otherwise collide onto one node
    (#84 risk 1) — and the ``-`` join keeps the result inside the established ``[A-Za-z0-9_-]`` segment
    shape (``acme-bronze$events`` is still a valid ``stage$name`` dataset id).

    LIVES HERE, beside :func:`is_safe_project`, because it is the SAME concern and more than one plane
    needs it. It began in ``medallion/core/config.py`` where only the medallion could reach it — and
    the ingest plane, which writes bronze and must therefore name it identically, composed
    ``f"{project}${dataset}"`` instead. That is not a near-miss: the cascade head matches on the
    qualified pair (``ingest_trigger.py``), so ingest's bronze writes could never fire it. A naming
    convention that two services must agree on cannot live inside one of them.
    """
    return f"{project}-{name}" if project else name


def clear_cache() -> None:
    """Drop every cached resolution (tests + explicit invalidation)."""
    _cache.clear()


def _resolve_root(
    control_root: str,
    storage_options: StorageOptions,
    project: str,
    *,
    serving: str,
    ttl_seconds: float | None,
) -> str | None:
    """Shared resolver behind :func:`project_root` / :func:`project_gold_root` — one serving class.

    ``serving=""`` matches WORK warehouses (no ``serving`` field on the record); ``serving="gold"``
    matches only records carrying ``"serving": "gold"``. A record with an UNKNOWN serving value matches
    neither class (fail closed: never route a tenant into a warehouse class this build does not know).
    """
    if ttl_seconds is None:
        ttl_seconds = _default_ttl_seconds()
    key = (control_root, project, serving)
    cached = _cache.get(key)
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]
    fs, base = fs_and_base(control_root, storage_options)
    matches: list[tuple[str, str]] = []
    selector = pafs.FileSelector(f"{base}/{_REGISTRY_PREFIX}", allow_not_found=True, recursive=False)
    for info in fs.get_file_info(selector):
        # bindings/ + any state prefixes are subdirectories — the non-recursive listing skips them.
        if info.type != pafs.FileType.File or not info.path.endswith(".json"):
            continue
        try:
            with fs.open_input_stream(info.path) as stream:
                record = json.loads(stream.readall().decode("utf-8"))
        except Exception as exc:
            log.warning("warehouse_record_unreadable", extra={"path": info.path, "error": str(exc)})
            continue
        if not isinstance(record, dict) or record.get("project") != project:
            continue
        if (record.get("status") or "active") != "active":
            continue
        if (record.get("serving") or "") != serving:
            continue
        root_uri = record.get("root_uri")
        if isinstance(root_uri, str) and root_uri:
            matches.append((str(record.get("id", "")), root_uri.rstrip("/")))
    if not matches:
        return None
    if len(matches) > 1:
        log.warning(
            "warehouse_project_ambiguous",
            extra={"project": project, "serving": serving, "count": len(matches)},
        )
    root = min(matches)[1]
    _cache[key] = (time.monotonic() + ttl_seconds, root)
    return root


def project_root(
    control_root: str,
    storage_options: StorageOptions,
    project: str,
    *,
    ttl_seconds: float | None = None,
) -> str | None:
    """The ACTIVE **work** warehouse ``root_uri`` for ``project``, or ``None`` when it has none.

    Scans ``<control_root>/_warehouses/*.json`` for records whose ``project`` matches and whose status
    is active (an ABSENT status counts as active — records written before the lifecycle feature are
    live, matching ``warehouse_status`` on the catalog side). One unreadable record is skipped with a
    warning, never allowed to void the rest. Multiple active warehouses resolve DETERMINISTICALLY to
    the lowest warehouse ``id`` (warned) so routing never flaps between roots.

    Only WORK records match — a record carrying ``"serving": "gold"`` (the project's gold SERVING
    warehouse, DECISIONS "Medallion tiers") is excluded here so registering a gold warehouse can never
    hijack the stage routing (its id sorting below the work warehouse's would otherwise win the
    lowest-id determinism); resolve it with :func:`project_gold_root`.

    Positive results are cached per process for ``ttl_seconds`` (``None`` → the
    ``WAREHOUSE_REGISTRY_TTL_SECONDS`` env value, default 5s); a miss is NOT cached, so a freshly
    provisioned warehouse resolves immediately. RESIDUAL STALENESS: because only ``status`` ever
    changes on a record, the cache re-checks it only on expiry — a warehouse deactivated after a
    positive hit keeps resolving for up to ``ttl_seconds`` per process (set the TTL to 0 to re-read
    the registry on every call). Blocking IO — callers threadpool it.
    """
    return _resolve_root(control_root, storage_options, project, serving="", ttl_seconds=ttl_seconds)


def project_gold_root(
    control_root: str,
    storage_options: StorageOptions,
    project: str,
    *,
    ttl_seconds: float | None = None,
) -> str | None:
    """The ACTIVE **gold serving** warehouse ``root_uri`` for ``project``, or ``None`` when it has none.

    The mirror of :func:`project_root` matching only records carrying ``"serving": "gold"`` (created via
    ``POST /v1/warehouses`` with the ``serving`` field — DECISIONS "Medallion tiers — hybrid physical
    layout"): the silver→gold mover's tenant TARGET root when ``MEDALLION_GOLD_WAREHOUSE_ENABLED`` is on.
    Same lowest-id determinism, same positive-only TTL cache (partitioned by serving class, so a cached
    work root never answers a gold lookup). ``None`` means the project has no gold warehouse — the caller
    falls back to the work root, byte-identically to the pre-gold behavior. Blocking IO — threadpool it.
    """
    return _resolve_root(control_root, storage_options, project, serving="gold", ttl_seconds=ttl_seconds)
