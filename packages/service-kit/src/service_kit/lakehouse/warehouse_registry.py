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
overrides). That cache is BOUNDED and self-evicting (:class:`_TtlCache`, ``MAX_CACHE_ENTRIES``): its
key is composed from caller-supplied strings, so an unbounded per-process map would grow for the life
of a pod. All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import logging
import os
import re
import time

from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import list_records
from service_kit.schemas.storage import GOVERNED_TIERS


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
#: The ceiling on resident cache entries. Bounded because the key is caller-supplied
#: (``control_root`` and ``project`` both arrive on a request/trigger), and a per-process dict with no
#: ceiling grows for the life of a service pod. Generous relative to any real estate — the estate's
#: tenants number in the tens — so eviction is the safety net, not the steady state.
MAX_CACHE_ENTRIES = 512


class _TtlCache:
    """A bounded, self-evicting TTL map — the resolver's positive cache.

    It was a bare module-level ``dict`` with no bound and no eviction: an entry was only ever
    overwritten by a lookup of the SAME key, so anything resolved once and never asked for again stayed
    resident for the life of the process. Three behaviours make that safe here:

    * **Expired entries are dropped on write**, not merely ignored on read. A read already refused a
      stale entry, but nothing ever freed it.
    * **A non-positive TTL is not stored at all.** :func:`project_root` documents ``ttl_seconds=0`` as
      "re-read the registry on every call"; it nevertheless wrote an entry that was stale the instant it
      landed, so the documented way to opt OUT of caching was the one that leaked fastest.
    * **Over the ceiling, the soonest-to-expire entry goes.** Evicting the entry closest to needing a
      re-read costs the least: it was about to be re-resolved anyway.

    Not a ``BaseModel``: this is a mutable per-process cache, not a validated value object crossing a
    boundary.
    """

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str, str], tuple[float, str]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: tuple[str, str, str], *, now: float) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if now >= entry[0]:
            del self._entries[key]
            return None
        return entry[1]

    def set(self, key: tuple[str, str, str], value: str, *, now: float, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            self._entries.pop(key, None)
            return
        self._entries = {held: entry for held, entry in self._entries.items() if entry[0] > now}
        self._entries[key] = (now + ttl_seconds, value)
        while len(self._entries) > self._max_entries:
            del self._entries[min(self._entries, key=lambda held: self._entries[held][0])]

    def clear(self) -> None:
        self._entries.clear()


#: ``(control_root, project, serving) → (expires_at_monotonic, root_uri)`` — positive resolutions only.
#: ``serving`` partitions the cache by warehouse class (``""`` = work, ``"gold"`` = serving), so a cached
#: work root can never answer a gold lookup or vice versa.
_cache = _TtlCache(MAX_CACHE_ENTRIES)


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


class AmbiguousProjectWarehouseError(UnresolvableProjectError):
    """A project has more than one ACTIVE warehouse in one serving class and none is marked primary.

    A SUBCLASS, deliberately: an ambiguous project is an unresolvable one — the resolver does not know
    the answer — so every path already written to fail closed on :class:`UnresolvableProjectError`
    covers this the day it is added, rather than depending on someone finding all of them. Both live
    handlers (`medallion/api/produce.py`, `medallion/services/transform.py`) catch it unchanged, and a
    caller that wants to tell the two apart still can.

    The refusal NAMES the candidates and the fix (``"primary": true`` on exactly one record, or
    deactivate the rest): an operator who is told only "ambiguous" has to go and read the registry to
    learn what to do about it.
    """


def is_safe_project(value: object) -> bool:
    """True iff ``value`` is a path-safe project id (safe as an S3-key/URI/lineage-name segment)."""
    return isinstance(value, str) and _PROJECT_RE.fullmatch(value) is not None


def tier_namespace(tier: str) -> str:
    """The namespace NAME a governed tier writes into — from the same env the medallion reads.

    Not a constant, for the reason `ingest/naming.py::bronze_namespace` already states:
    ``MEDALLION_<TIER>_NAMESPACE`` is a chart value, and a tier the writer and the cascade head
    disagree about is a write nothing downstream ever sees.
    """
    import os

    return os.getenv(f"MEDALLION_{tier.upper()}_NAMESPACE", tier)


def namespace_for(project: str, tier: str) -> str:
    """The namespace a write for ``project`` into ``tier`` lands in — ``acme-silver``, or ``silver``
    untenanted.

    LIVES HERE for the same reason :func:`project_namespace` does, and the case that forced it is the
    same one repeated a tier later: ingest qualifies bronze through this module while the annotator
    composed the bare literal ``"silver"`` in four places, so with two tenants annotating, both landed
    in one namespace — one FGA parent, one set of grants, both tenants' labels.

    The ``is_safe_project`` guard is applied HERE rather than by each caller, because applying it on
    only one side is how a garbage project produces a write that is refused for a reason invisible
    from the writer.
    """
    return project_namespace(project if is_safe_project(project) else "", tier_namespace(tier))


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


def lane_key(project: str, qualified: str) -> str:
    """The TENANT-FREE lane key for a project-qualified dataset id -- the inverse of
    :func:`project_namespace` when the project is KNOWN.

    ``("acme", "acme-bronze$events")`` -> ``"bronze$events"``. Empty project, or a name that does not
    carry that project's prefix, returns ``qualified`` unchanged.

    THIS IS NOT THE UNSOUND INVERSE the module warns about above. Recovering the project FROM a name
    is ambiguous -- ``PROJECT_PATTERN`` permits hyphens, so ``acme-bronze-silver`` could be project
    ``acme`` or ``acme-bronze``. Here the project is supplied by the caller (the trigger carries it,
    the record stores it), so the only judgement is a prefix test on the ``-`` BOUNDARY. Stripping on
    the bare project name instead would turn ``acmebronze$x`` into ``bronze$x`` and match a lane
    nobody declared.

    WHY IT EXISTS. A stage trigger's ``dataset`` is a LANE KEY: the same string for every tenant, with
    the tenant travelling separately on ``trigger.project``. That convention was already established
    -- ``publication_trigger`` published the CATALOG identifier once and every tenant's publication
    was dropped as another lane's -- but ``ingest_trigger``'s declared-lane branch still returned the
    catalog id verbatim, so ONE function returned two different kinds of thing depending on which
    branch fired. A lane declared through the door was then reachable from one head and not the other.

    Lives beside :func:`project_namespace` for the reason stated there: a naming convention two
    services must agree on cannot live inside one of them.
    """
    prefix = f"{project}-"
    return qualified[len(prefix) :] if project and qualified.startswith(prefix) else qualified


def namespace_tiers(qualified: str) -> frozenset[str]:
    """Which governed tiers a namespace name contains, as hyphen-delimited segments.

    THE COMPLEMENT of :func:`project_namespace`, and deliberately only its SOUND half. The obvious
    inverse — recovering the PROJECT — is ruled out by the estate's own canon
    (``catalog/core/lineage_emit.py``, the ``ProjectResolver`` note): ``PROJECT_PATTERN`` permits
    hyphens, so ``acme-bronze-silver`` is genuinely ambiguous between project ``acme`` and project
    ``acme-bronze``, "and guessing wrong notifies the wrong tenant's watchers. The registry binding is
    the only sound answer." The TIER half carries no such ambiguity because the vocabulary is CLOSED —
    ``GOVERNED_TIERS`` is exactly bronze/silver/gold (R23) — so membership is a fact rather than a guess.

    Returns a SET, not "the" tier, because a name may honestly contain more than one and this refuses
    to pick. That matters where the answer gates an authorization door: a caller asking
    ``namespace_tiers(ns) & {"silver", "gold"}`` fails CLOSED on the ambiguous shape, which is the
    direction a gate must fail. Picking the leftmost would let ``acme-bronze-gold`` (project
    ``acme-bronze``, tier ``gold``) read as bronze and skip a gate it must cross.

    Segment-wise, so ``goldfish`` is not gold. Lanes work by construction: the cascade names them
    ``<tier>-<lane>`` (``acme-gold-htr``, ``chart/values.yaml``), and reading such a name from the RIGHT
    yields the LANE instead — the failure ``maintenance/services/tiers.py`` documents hitting live on
    ``bronze-pages``.
    """
    tiers = {tier.value for tier in GOVERNED_TIERS}
    return frozenset(segment for segment in qualified.split("-") if segment in tiers)


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
    now = time.monotonic()
    cached = _cache.get(key, now=now)
    if cached is not None:
        return cached
    matches: list[tuple[str, str, bool]] = []
    # `list_records` is the shared registry primitive: non-recursive (so `bindings/` and any state
    # prefixes are skipped), one unreadable record warned and skipped rather than voiding the answer.
    for record in list_records(control_root, storage_options, _REGISTRY_PREFIX, event="warehouse_record"):
        if record.get("project") != project:
            continue
        if (record.get("status") or "active") != "active":
            continue
        if (record.get("serving") or "") != serving:
            continue
        root_uri = record.get("root_uri")
        if isinstance(root_uri, str) and root_uri:
            matches.append((str(record.get("id", "")), root_uri.rstrip("/"), _is_primary(record)))
    if not matches:
        return None
    root = _sole_root(matches, project=project, serving=serving)
    _cache.set(key, root, now=now, ttl_seconds=ttl_seconds)
    return root


def _is_primary(record: dict[str, object]) -> bool:
    """The record's primary marker, in the shape the registry actually stores.

    The catalog writes `"true"` (a string), matching `protected` — the record is a str->str map, and
    one boolean in it would be the only value the store round-trips differently from its siblings. A
    real `True` is accepted too, so a hand-written record or a test fixture that reaches for the
    obvious spelling is not silently ignored: a marker that is present and unread would leave the
    project refusing while its operator believes they have already fixed it.
    """
    value = record.get("primary")
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _sole_root(matches: list[tuple[str, str, bool]], *, project: str, serving: str) -> str:
    """The one root this project routes to, or a refusal that names the candidates and the fix.

    THIS USED TO BE `min(matches)`, warn, and carry on. That is deterministic, which is what the old
    test asserted — and deterministic is not correct. It routes a tenant's writes BY ALPHABET.
    Measured on the deployed estate 2026-09-06: project `acme` carried SIX active work warehouses,
    five of them e2e residue, and the cascade head resolved `acme-bucket` only because that string
    sorts first. A suite minting `aaa-wh` would have relocated a tenant's bronze silently; the sole
    signal was a warning line.

    The estate's own rule for this shape is fail-closed — `UnresolvableProjectError` exists precisely
    so an unroutable project refuses rather than falling back to a shared default and "silently
    read/write the wrong tenant's data while emitting real-looking lineage for it". An ambiguous
    project is unroutable in exactly that sense: the resolver does not know the answer, and picking
    one is indistinguishable from knowing.

    `primary` is the operator's answer. Exactly one wins; two is still ambiguous, because choosing
    between two DECLARED primaries would be the original defect wearing a better name.
    """
    if len(matches) == 1:
        return matches[0][1]
    primaries = [match for match in matches if match[2]]
    if len(primaries) == 1:
        return primaries[0][1]
    candidates = ", ".join(sorted(warehouse_id for warehouse_id, _, _ in matches))
    detail = "two records are marked primary" if primaries else "none is marked primary"
    log.error(
        "warehouse_project_ambiguous",
        extra={"project": project, "serving": serving, "count": len(matches), "primaries": len(primaries)},
    )
    raise AmbiguousProjectWarehouseError(
        f"project {project!r} has {len(matches)} active {serving or 'work'} warehouses and {detail}: {candidates}. "
        f"Mark exactly one with \"primary\": true, or deactivate the rest — routing a tenant's data by "
        f"alphabetical accident is not a decision this resolver may make."
    )


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
