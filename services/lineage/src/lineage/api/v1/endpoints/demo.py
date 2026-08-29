"""Demo-only data peek — reads the real Lance datasets on S3 so the UI can show *what is
changing in storage*: the schema at each Lance version, row counts, and gold's embedded JSONB
lineage.

This is **demo instrumentation, not core lineage**. It is mounted only when
``LINEAGE_DEMO_DATA_ENABLED`` is set, reads object storage directly with pylance (the same library
the catalog uses), and never touches the AGE graph. The handler is ``async def`` (it awaits the
batch ``DatasetFilter`` governance gate), so the blocking pylance/S3 reads are dispatched via
``run_in_threadpool`` — never run directly on the event loop.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import lance
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from lineage.api.dependencies import SettingsDep
from lineage.api.fga_deps import FilterDep
from lineage.core.config import get_settings, storage_options
from lineage.schemas import DemoDataset, DemoDatasets, DemoField, DemoVersion
from service_kit.lakehouse import schema


log = logging.getLogger(__name__)

router = APIRouter(prefix="/demo", tags=["demo"])

# The medallion datasets the demo writes — catalog id -> object path under the bucket.
_LAYOUT: list[tuple[str, str]] = [
    ("bronze$events", "bronze/events"),
    ("silver$features", "silver/features"),
    ("gold$catalog", "gold/catalog"),
]


def _storage_options() -> dict[str, str]:
    return storage_options(get_settings())


def _read_lineage_jsonb(ds: Any) -> dict[str, Any] | None:
    """Read gold's embedded ``lineage`` JSONB column (one row) back into a dict."""
    try:
        column = ds.to_table(columns=["lineage"]).column("lineage").to_pylist()
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        # pylance/pyarrow read faults: S3 unreachable → OSError, a corrupt manifest → ValueError, an
        # absent/renamed ``lineage`` column → ValueError (lance projection) or KeyError (pyarrow
        # ``.column``). Logged, not silent — this is the demo tier, so a fault degrades the lineage
        # panel to empty rather than failing the request, but it must leave a trace. A narrower catch
        # than `Exception` on purpose: an AttributeError/TypeError here is a bug in this code and should
        # surface, not read as "no lineage column".
        log.warning("demo_lineage_jsonb_read_failed error=%s", exc)
        return None
    if not column:
        return None
    value = column[0]
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class PeekCache(BaseModel):
    """Version-keyed peek cache (§2 perf, 2026-07-11) — ONE PER APP INSTANCE, on ``app.state``.

    The peek's cost grew LINEARLY with total versions: one S3 dataset-open PER VERSION per dataset,
    polled every 2s. Lance versions are IMMUTABLE, so a version's schema entry can never change once
    read → per-version entries cache until they scroll out of the newest-K window. Incarnation
    identity (review 2026-07-11): a delete-and-recreate at the same URI restarts version numbers, and
    the new incarnation can reach ANY version count while nobody polls — so a bare version number is
    NOT a valid cache key. Every entry is therefore validated against the live manifest TIMESTAMP from
    ``ds.versions()`` (already fetched each tick — no extra opens): same (version, timestamp) = same
    immutable manifest; a mismatch = a dead incarnation's entry, rebuilt. Steady-state tick cost: ONE
    dataset-open (the probe) + the versions() listing, zero per-version opens. Benign races only (demo
    tier, threadpool): a concurrent rebuild duplicates work, never corrupts.

    App-scoped rather than module-global (F-LIN-15): module-level mutables bleed across app instances
    and needed a test-only reset seam; a per-app object needs neither.
    """

    payloads: dict[str, tuple[int, str | None, DemoDataset]] = Field(default_factory=dict)  # uri -> (version, stamp, payload)
    version_fields: dict[str, dict[int, DemoVersion]] = Field(default_factory=dict)  # uri -> version -> entry (stamp inside)


def get_peek_cache(request: Request) -> PeekCache:
    """The app's peek cache, created lazily on first use (the demo router mounts conditionally, so the
    lifespan does not know about it). No await between the read and the write → no async race."""
    cache = getattr(request.app.state, "demo_peek_cache", None)
    if cache is None:
        cache = PeekCache()
        request.app.state.demo_peek_cache = cache
    return cache


PeekCacheDep = Annotated[PeekCache, Depends(get_peek_cache)]


def _stamp(entry: dict[str, Any]) -> str | None:
    timestamp = entry.get("timestamp")
    return timestamp.isoformat() if hasattr(timestamp, "isoformat") else None


def _read_dataset(cache: PeekCache, name: str, uri: str, opts: dict[str, str], max_versions: int) -> DemoDataset:
    try:
        ds = lance.dataset(uri, storage_options=opts)  # the ONE per-tick open: the change probe
    except (OSError, ValueError, RuntimeError) as exc:
        # The dataset genuinely not existing yet (the cascade has not written this tier) is the common
        # case and is what exists=False reports — but so is an S3 outage, so it is logged rather than
        # swallowed. Narrowed past `Exception`: a bug in this function must surface, not read as absent.
        log.warning("demo_dataset_open_failed name=%s uri=%s error=%s", name, uri, exc)
        return DemoDataset(name=name, uri=uri, exists=False)
    current = int(ds.version)
    entries = list(ds.versions())[-max_versions:]  # newest K — bounds the cold tick too
    head_stamp = _stamp(entries[-1]) if entries else None
    cached = cache.payloads.get(uri)
    if cached is not None and cached[0] == current and cached[1] == head_stamp:
        return cached[2]  # unchanged tick, same incarnation — zero further S3 work
    known = cache.version_fields.setdefault(uri, {})
    versions: list[DemoVersion] = []
    for entry in entries:
        number = int(entry["version"])
        stamp = _stamp(entry)
        hit = known.get(number)
        if hit is not None and hit.timestamp == stamp:  # same immutable manifest → reuse
            versions.append(hit)
            continue
        try:
            at_version = lance.dataset(uri, storage_options=opts, version=number)
            fields = [DemoField(name=f.name, type=schema.type_label(f)) for f in at_version.schema]
        except (OSError, ValueError, RuntimeError) as exc:
            # A single version failing to open (a pruned/compacted-away manifest, an S3 hiccup) drops
            # that version's schema to empty rather than failing the whole peek — but it is logged, and
            # the catch is narrow so a code bug surfaces instead of masquerading as an empty schema.
            log.warning("demo_version_schema_read_failed uri=%s version=%s error=%s", uri, number, exc)
            fields = []
        known[number] = DemoVersion(version=number, timestamp=stamp, fields=fields)
        versions.append(known[number])
    # Prune entries below the window floor — the loop above can never serve them again, so keeping
    # them is a slow leak (the cascade mints a version per tick, indefinitely; review 2026-07-11).
    floor = versions[0].version if versions else 0
    for number in [n for n in known if n < floor]:
        del known[number]
    payload = DemoDataset(
        name=name,
        uri=uri,
        exists=True,
        current_version=current,
        row_count=ds.count_rows(),
        versions=versions,
        lineage_jsonb=_read_lineage_jsonb(ds) if name == "gold$catalog" else None,
    )
    cache.payloads[uri] = (current, head_stamp, payload)
    return payload


@router.get("/datasets")
async def demo_datasets(settings: SettingsDep, datasets: FilterDep, cache: PeekCacheDep) -> DemoDatasets:
    """The medallion datasets as they currently exist on S3 — schema per Lance version + rows.

    GOVERNED (audit: this endpoint reads real medallion schemas/row-counts + gold's lineage JSONB from S3
    with the SERVICE root credentials, so it must not disclose a dataset the caller cannot see). Every
    other lineage read gates on ``can_get_metadata``; this does the same via the shared batch filter —
    ONE ``batch_check`` over the layout, not a per-dataset round-trip loop (F-LIN-13). The ladder is
    unchanged: 401 unauthenticated / 503 FGA unwired propagate; a denied dataset is dropped, not fatal.
    FGA off → pass-through (the dev demo)."""
    # Gate FIRST — 401/503 must fire before this handler touches any storage configuration or S3.
    visible = await datasets.visible([name for name, _ in _LAYOUT])
    opts = _storage_options()
    bucket = settings.s3_bucket
    out: list[DemoDataset] = []
    for name, path in _LAYOUT:
        if name not in visible:
            continue
        out.append(await run_in_threadpool(_read_dataset, cache, name, f"s3://{bucket}/{path}", opts, settings.demo_max_versions))
    return DemoDatasets(datasets=out)
