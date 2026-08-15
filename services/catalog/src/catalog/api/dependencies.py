"""Shared FastAPI dependencies (Annotated type aliases)."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    InvalidInputError,
    LanceNamespace,
    PermissionDeniedError,
    ServiceUnavailableError,
)
from openfga_sdk import OpenFgaClient

from catalog.core.config import Settings, get_settings
from catalog.core.control_emit import ControlEmitter, NoopControlEmitter
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import LineageEmitter, NoopEmitter
from catalog.core.namespace import build_namespace_for_root
from catalog.core.vending import CredentialVendor, ModeBVendor
from catalog.services import warehouses


log = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def _resolve_warehouse_root(request: Request, settings: Settings, top_ns: str) -> str | None:
    """The physical root_uri bound to top-level namespace ``top_ns`` (#3-A), or ``None`` → default root.

    FAILS CLOSED. A registry read ERROR raises ``ServiceUnavailableError`` (503) — it is NOT swallowed to
    ``None``. Swallowing it would route a possibly-BOUND tenant's table to the shared default bucket on a
    transient S3 blip: a tenant-isolation violation caused by a hiccup. Only a clean "not found" (an
    unbound namespace) resolves to ``None`` and the default root.

    DEACTIVATION GATE (P2.3): if the bound warehouse is deactivated, raises ``PermissionDeniedError`` (403) —
    a quarantined warehouse suspends EVERY op on its bound namespaces (the tenant-offboarding step). Status is
    read LIVE, so a deactivate/activate takes effect on the very next request.

    (This docstring previously asserted the opposite — "swallowed to None ... can NEVER break an existing
    flow" — describing the pre-audit behavior while the code below fail-closed. A docstring that lies about
    a fail-open/fail-closed decision is worse than none: it is the sentence a future reader trusts.)

    Cached in-process, POSITIVES ONLY (bindings are immutable, so a resolved root is safe forever; a
    negative must not be cached — see the comment below). Blocking S3 read runs in the threadpool."""
    # Cache POSITIVES only: a binding is immutable, so a resolved binding is safe to cache forever. A negative
    # (unbound) result must NOT be cached — else a namespace looked up while still unbound, then bound on
    # another replica, would keep routing to the default bucket on this one (a table meant for the tenant's
    # bucket would land in the shared one — an isolation violation). Unbound namespaces re-read the
    # authoritative binding each request; that read only happens when warehouses_enabled (default off).
    # The cache holds the FULL binding record ({warehouse_id, root_uri}) so the deactivation check below has
    # the warehouse_id without a second binding read.
    cache: dict[str, dict[str, str]] = request.app.state.warehouse_binding_cache
    binding = _fresh_cached_binding(cache, top_ns, settings.warehouse_binding_cache_ttl_seconds)
    if binding is None:
        try:
            binding = await run_in_threadpool(warehouses.binding_for_namespace, settings.registry_root, settings.storage_options(), top_ns)
        except Exception as exc:
            # FAIL CLOSED on a registry READ ERROR. A legitimately-unbound namespace returns a clean None (no
            # exception) from binding_for_namespace, so reaching here means the read itself failed (S3 blip,
            # timeout). Falling through to the default shared bucket would physically create/read a
            # MAYBE-bound tenant's table in the wrong bucket — an isolation break from a transient fault. 503
            # instead; the caller retries once the registry is reachable.
            log.warning("warehouse_binding_lookup_failed", extra={"top_ns": top_ns, "error": str(exc)})
            raise ServiceUnavailableError(f"warehouse binding lookup failed for {top_ns!r}") from exc
        if binding is not None:
            # The freshness stamp rides INSIDE the record under a reserved key rather than in a
            # parallel dict: `evict_stale_bindings` mutates this very dict in place and scans its
            # VALUES for a warehouse_id, so a second structure would be one more thing to keep in
            # sync — and the one that drifts is the one nothing reads on the hot path.
            cache[top_ns] = {**binding, _CACHED_AT: str(time.monotonic())}
    if binding is None:
        return None
    # Deactivation gate (P2.3 lifecycle): a deactivated warehouse quarantines EVERY op on its bound
    # namespaces. Status is MUTABLE, so it is read LIVE each request (never cached, unlike the immutable
    # root_uri) — one metadata GET, only for bound namespaces (the minority; the whole lookup is skipped when
    # warehouses are off). FAIL-CLOSED on BOTH failure kinds (audit #3/#5): a transient registry read error is
    # wrapped as 503 (NOT an unhandled 500) — symmetric with the binding read above; and a MISSING warehouse
    # record for a still-bound namespace (a clean status None) is NOT-active → 403, never silently allowed.
    try:
        status = await run_in_threadpool(
            warehouses.warehouse_status,
            settings.registry_root,
            settings.storage_options(),
            binding["warehouse_id"],
        )
    except Exception as exc:
        log.warning(
            "warehouse_status_lookup_failed",
            extra={"top_ns": top_ns, "warehouse_id": binding["warehouse_id"], "error": str(exc)},
        )
        raise ServiceUnavailableError(f"warehouse status lookup failed for {binding['warehouse_id']!r}") from exc
    if status != "active":
        raise PermissionDeniedError(
            f"warehouse {binding['warehouse_id']!r} is deactivated (quarantined); operations on namespace {top_ns!r} are suspended until it is reactivated"
        )
    return binding["root_uri"]


#: Reserved key stamping when a cached binding was resolved. Underscore-prefixed so it cannot collide
#: with a real record field (`top_ns` / `warehouse_id` / `root_uri`).
_CACHED_AT = "_cached_at"


def _fresh_cached_binding(cache: dict[str, dict[str, str]], top_ns: str, ttl_seconds: float) -> dict[str, str] | None:
    """The cached binding for ``top_ns`` if it is still fresh, evicting it if not (diff2 F10 item 3).

    The cache is POSITIVE-FOREVER on the premise that a binding is immutable, and #46 added
    broadcast eviction for the three mutations that break that premise. This is the FLOOR under that
    broadcast, because the broadcast is best-effort: it rides a pub/sub subscription with no
    dead-lettering, so a dropped event leaves an entry that nothing will ever evict.

    What that costs is narrower than it first looks and the finding's own text corrects it: warehouse
    STATUS is read live on every request and a missing record fails closed to 403, so a stale entry
    does NOT route at a deleted bucket. The residual is persistent 403s on a since-re-bound namespace
    until the process restarts, plus wrong-bucket routing during a partial-delete window or under
    warehouse-id reuse. A TTL bounds all three to the window rather than to the process lifetime.

    Minutes, not seconds: the read this avoids is one S3 GET on the request hot path, and bindings
    genuinely almost never change — the TTL is a backstop for a lost event, not a consistency
    mechanism. `0` disables it, restoring the pre-F10.3 forever-positive behaviour.
    """
    entry = cache.get(top_ns)
    if entry is None:
        return None
    if ttl_seconds <= 0:
        return entry
    if time.monotonic() - float(entry.get(_CACHED_AT) or 0.0) >= ttl_seconds:
        # pop by key, matching `evict_stale_bindings`' own concurrency argument: dict ops are atomic
        # under the GIL and a racing resolver simply re-reads the registry, which is the right outcome.
        cache.pop(top_ns, None)
        return None
    return entry


def _namespace_for_root(request: Request, settings: Settings, root_uri: str) -> LanceNamespace:
    """The (cached) namespace connection rooted at ``root_uri`` — one per warehouse bucket."""
    conns: dict[str, LanceNamespace] = request.app.state.warehouse_namespaces
    conn = conns.get(root_uri)
    if conn is None:
        conn = build_namespace_for_root(settings, root_uri)
        conns[root_uri] = conn
    return conn


async def get_namespace(request: Request) -> LanceNamespace:
    """The backend namespace for this request.

    Default: the single connection built once in the lifespan. With ``warehouses_enabled`` (#3-A) and a
    request that targets a warehouse-BOUND top-level namespace, returns that warehouse's bucket-rooted
    connection instead — so create/describe/commit/list/drop all operate on the physically isolated bucket.
    An UNBOUND namespace (the common case) always falls through to the default root: existing single-bucket
    behaviour is untouched, and the binding lookup is skipped entirely when the feature is off."""
    default_ns: LanceNamespace = request.app.state.namespace
    settings = get_settings()
    if not settings.warehouses_enabled:
        return default_ns
    object_id = request.path_params.get("id")
    if not object_id:  # collection routes (list/health) have no id to route by
        return default_ns
    top_ns = parse_identifier(object_id, settings.delimiter)[0]
    root = await _resolve_warehouse_root(request, settings, top_ns)
    if not root or root == settings.root:
        return default_ns
    return _namespace_for_root(request, settings, root)


async def namespace_for_top_ns(request: Request, settings: Settings, top_ns: str) -> LanceNamespace:
    """The rooted connection for ONE top-level namespace — the per-seed half of :func:`get_namespace`.

    Collection routes (``GET /v1/table``) carry no ``{id}`` for `get_namespace` to route by, so their
    request-scoped ``ns`` is ALWAYS the default root — structurally blind to every warehouse-BOUND
    namespace's tables (measured live: the estate listing showed 2 of 9 while the per-namespace routes
    saw everything, because those routes resolve the binding per id). A collection route that wants a
    bound namespace's contents resolves it explicitly through this helper, one seed at a time, using
    the SAME binding lookup + per-warehouse connection cache the id routes use.
    """
    default_ns: LanceNamespace = request.app.state.namespace
    if not settings.warehouses_enabled:
        return default_ns
    root = await _resolve_warehouse_root(request, settings, top_ns)
    if not root or root == settings.root:
        return default_ns
    return _namespace_for_root(request, settings, root)


NamespaceDep = Annotated[LanceNamespace, Depends(get_namespace)]


async def assert_no_warehouse_bound_namespace(request: Request, settings: Settings, id_segment_lists: Iterable[list[str] | None]) -> None:
    """#3-A guard for the BATCH routes. They carry no ``{id}`` path param for :func:`get_namespace` to route
    by (the tables are named in the body), so they run against the DEFAULT root — which would silently place
    a warehouse-BOUND tenant's table in the SHARED bucket (isolation break) and split the same id across two
    buckets vs. the per-table routes. Until batch routing resolves the body's namespaces, reject a batch that
    references a warehouse-bound top-level namespace; the per-table routes handle those. No-op when the
    feature is off."""
    if not settings.warehouses_enabled:
        return
    for segments in id_segment_lists:
        if not segments:
            continue
        root = await _resolve_warehouse_root(request, settings, segments[0])
        if root is not None and root != settings.root:
            raise InvalidInputError(f"batch operations are not supported for warehouse-bound namespace {segments[0]!r}; use the per-table routes")


def get_fga_client(request: Request) -> OpenFgaClient | None:
    """The wired OpenFGA client from catalog.state, or ``None`` when FGA isn't provisioned.

    One place that knows where the client lives (``app.state.fga``), injected like
    ``NamespaceDep`` instead of re-spelled as ``getattr(request.app.state, "fga", None)``
    in every create/seed/list handler.
    """
    return getattr(request.app.state, "fga", None)


FgaClientDep = Annotated[OpenFgaClient | None, Depends(get_fga_client)]


def get_storage_options(settings: SettingsDep) -> dict[str, str]:
    """Object-store options (S3/MinIO credentials, region) for direct pylance access."""
    return settings.storage_options()


StorageOptionsDep = Annotated[dict[str, str], Depends(get_storage_options)]


def get_lineage_emitter(request: Request) -> LineageEmitter:
    """The lineage emitter built in the app lifespan — a no-op when emission is disabled."""
    emitter = getattr(request.app.state, "lineage_emitter", None)
    return emitter if emitter is not None else NoopEmitter()


LineageEmitterDep = Annotated[LineageEmitter, Depends(get_lineage_emitter)]


def get_control_emitter(request: Request) -> ControlEmitter:
    """The control-plane change-event emitter built in the app lifespan — a no-op when emission is off."""
    emitter = getattr(request.app.state, "control_emitter", None)
    return emitter if emitter is not None else NoopControlEmitter()


ControlEmitterDep = Annotated[ControlEmitter, Depends(get_control_emitter)]


def get_vendor(request: Request) -> CredentialVendor:
    """The credential vendor built in the app lifespan — Mode B (no direct creds) by default."""
    vendor = getattr(request.app.state, "vendor", None)
    return vendor if vendor is not None else ModeBVendor()


VendorDep = Annotated[CredentialVendor, Depends(get_vendor)]
