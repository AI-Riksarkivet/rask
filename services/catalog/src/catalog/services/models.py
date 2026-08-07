"""Model-registry read + promotion ops for #17 (candidate→blessed).

The registry is one Lance dataset per model at ``<models_root>/<model>`` — the medallion trainer writes it
DIRECTLY there (``registry_uri_for``), so these ops open it by EXPLICIT URI, NOT the catalog's native
namespace resolver (which would drop the ``/medallion/`` prefix and miss it). Training publishes are
APPEND-only (each adds that publish's ``artifact``/``payload``/``meta`` rows, so the rows appended LAST
carry the newest publish's ``meta``) — but the compaction sweep also lands maintenance commits on the
registry (live 2026-07-15: train → v17, compact+GC → v18/v19 with identical rows), so the Lance version
can run AHEAD of the training count. That is safe by construction: compaction preserves rows and their
order, so ``metrics_at`` on any post-publish version still reads the newest publish's meta, and blessing a
maintenance version blesses identical content. "Candidate" = the latest version; "blessed" = a moving
Lance tag pinned to the promoted version.

Blocking pylance/S3 IO — callers run it in the threadpool. All errors are ``lance_namespace`` domain errors
so the catalog's exception handler maps them to RFC 9457 problem+json (404 missing / 409 gate-failed / 400).
"""

from __future__ import annotations

import json
import math
from typing import Any

import lance
import pyarrow.fs as pafs
from lance_namespace import (
    InvalidInputError,
    InvalidTableStateError,
    TableNotFoundError,
    TableVersionNotFoundError,
)

from service_kit.lakehouse.objectfs import fs_and_base


#: The reserved tag that names the promoted ("blessed") model version. A move of this tag IS the promotion.
BLESSED_TAG = "blessed"


def _reject_nonfinite(token: str) -> float:
    """``json.loads`` decodes the ``NaN``/``Infinity``/``-Infinity`` tokens to Python floats by default — a
    non-finite metric must NEVER silently clear the promotion gate (``NaN < threshold`` is always False, so a
    diverged or forged NaN metric would pass any bar), so refuse it at PARSE, fail-closed."""
    raise ValueError(f"non-finite JSON constant {token!r} in model metadata")


def _open(model_uri: str, storage_options: dict[str, str], *, version: int | None = None) -> lance.LanceDataset:
    """Open the registry Lance dataset by explicit URI (missing → 404, bad version → 404)."""
    try:
        return lance.dataset(model_uri, storage_options=storage_options, version=version)
    except (ValueError, OSError) as exc:
        if version is not None:
            raise TableVersionNotFoundError(f"model version {version} not found") from exc
        raise TableNotFoundError("model registry not found for this model") from exc


def metrics_at(model_uri: str, storage_options: dict[str, str], version: int) -> dict[str, Any]:
    """The training metrics recorded for model version ``version`` (``meta['metrics']``).

    The registry appends each publish's rows, so at Lance version N the rows added LAST are version N's
    artifacts and carry version N's ``meta`` — read the last row's meta (all of a publish's rows share it).
    """
    ds = _open(model_uri, storage_options, version=version)
    row_count = ds.count_rows()
    if not row_count:
        raise InvalidTableStateError(f"model version {version} has no rows")
    # Only the LAST row's meta is read (all of a publish's rows share it), so take exactly that row —
    # to_table() here materialised the meta of EVERY artifact ever published, for one value (#141).
    last_meta = ds.take([row_count - 1], columns=["meta"]).column("meta").to_pylist()[0]
    try:
        # parse_constant → refuse NaN/Infinity at parse (they would silently clear the gate — see below).
        meta = json.loads(last_meta, parse_constant=_reject_nonfinite)  # newest publish's rows appended last
    except (ValueError, TypeError) as exc:
        raise InvalidTableStateError(f"model version {version} metadata is unreadable or non-finite") from exc
    metrics = meta.get("metrics")
    if not isinstance(metrics, dict):
        raise InvalidTableStateError(f"model version {version} records no metrics to gate on")
    return metrics


def _safe_metrics(model_uri: str, storage_options: dict[str, str], version: int) -> dict[str, Any] | None:
    """``metrics_at`` for the describe read path: a version with no / unreadable metrics returns ``None``
    (metrics: null) rather than failing the whole describe — describe is a report, not the gate."""
    try:
        return metrics_at(model_uri, storage_options, version)
    except InvalidTableStateError:
        return None


def _apply_gate(metrics: dict[str, Any], min_metrics: dict[str, float] | None, *, version: int) -> None:
    """Fail-closed metrics gate: EVERY required metric must be present, numeric, and >= its threshold.

    A missing or non-numeric metric is a REFUSAL (409), never a silent pass — a candidate cannot be blessed
    on a metric the training run never recorded.
    """
    for name, threshold in (min_metrics or {}).items():
        actual = metrics.get(name)
        # bool is an int subclass, and NaN/Inf are float — both would slip past a bare numeric check and
        # (for NaN) clear every `<` comparison, so reject non-finite here too (defense in depth vs the parse).
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual):
            raise InvalidTableStateError(f"model version {version} cannot be promoted: metric {name!r} is absent, non-numeric, or non-finite")
        if actual < threshold:
            raise InvalidTableStateError(f"model version {version} cannot be promoted: {name}={actual} below required {threshold}")


def _tag_version(ds: lance.LanceDataset, tag: str) -> int | None:
    """The version ``tag`` points at, or ``None`` when unset (pylance 8 raises "Ref not found" for an unset
    tag — it does NOT return None)."""
    try:
        version = ds.tags.get_version(tag)
    except ValueError:
        return None
    return int(version) if version is not None else None


def blessed_version(model_uri: str, storage_options: dict[str, str], *, tag: str = BLESSED_TAG) -> int | None:
    """The version the ``blessed`` tag points at, or ``None`` if no version is blessed yet."""
    return _tag_version(_open(model_uri, storage_options), tag)


def describe(model_uri: str, storage_options: dict[str, str], *, tag: str = BLESSED_TAG) -> dict[str, Any]:
    """Registry summary: the latest (candidate) version, the blessed version (or None), and both metrics."""
    ds = _open(model_uri, storage_options)  # proves the registry exists (404 otherwise)
    latest = int(ds.version)
    blessed = _tag_version(ds, tag)
    return {
        "latest_version": latest,
        "blessed_version": blessed,
        "candidate_metrics": _safe_metrics(model_uri, storage_options, latest),
        "blessed_metrics": _safe_metrics(model_uri, storage_options, blessed) if blessed else None,
    }


def summarize(model_uri: str, storage_options: dict[str, str], *, tag: str = BLESSED_TAG) -> dict[str, Any]:
    """The list-view summary — latest (candidate) + blessed versions only, no metric reads.

    Deliberately lighter than :func:`describe`: metrics live in each version's ``meta`` rows, so reading
    them costs two extra versioned dataset opens per model — fine for one model, not for a listing.
    """
    ds = _open(model_uri, storage_options)
    return {"latest_version": int(ds.version), "blessed_version": _tag_version(ds, tag)}


def list_models(models_root: str, storage_options: dict[str, str]) -> list[str]:
    """Every model name under the registry root (one Lance dataset directory per model), sorted.

    A pure storage LIST (no dataset opens) — the caller filters names (shape + authz) before paying for
    per-model summaries. An absent root (nothing trained yet) yields ``[]``.
    """
    fs, base = fs_and_base(models_root, storage_options)
    infos = fs.get_file_info(pafs.FileSelector(base, allow_not_found=True))
    return sorted(info.base_name for info in infos if info.type == pafs.FileType.Directory)


def list_artifacts(artifacts_uri: str, storage_options: dict[str, str]) -> list[dict[str, Any]]:
    """Every artifact object under one model's plain-path tree (``<artifacts_root>/<model>/<token>/…``,
    the #17/#92 layout), sorted by path.

    A pure recursive storage listing (no dataset opens — the tree is OUTSIDE any Lance dataset by design):
    each entry is the path relative to the model's tree (``<token>/weights.json``), its size, and its mtime
    as ISO-8601 (``None`` when the filesystem reports none). An absent tree — nothing trained yet, or a
    registry-only model — is ``[]``, never an error.
    """
    fs, base = fs_and_base(artifacts_uri, storage_options)
    infos = fs.get_file_info(pafs.FileSelector(base, recursive=True, allow_not_found=True))
    files = sorted((info for info in infos if info.type == pafs.FileType.File), key=lambda i: i.path)
    return [
        {
            "path": info.path.removeprefix(base).lstrip("/"),
            "size_bytes": int(info.size),
            "updated_at": info.mtime.isoformat() if info.mtime is not None else None,
        }
        for info in files
    ]


def promote(
    model_uri: str,
    storage_options: dict[str, str],
    *,
    version: int,
    min_metrics: dict[str, float] | None = None,
    tag: str = BLESSED_TAG,
) -> int:
    """Bless model ``version`` (candidate→blessed): run the metrics gate, THEN move the ``blessed`` tag to it.

    The gate runs FIRST and fail-closed — a version that misses the bar raises ``InvalidTableStateError``
    (409) and the tag is never touched. The tag move is metadata-only (it mints NO new Lance version). Returns
    the newly-blessed version. The caller has already been authorized (validator-rung ``can_promote``).
    """
    if version < 1:
        raise InvalidInputError(f"model version must be >= 1, got {version}")
    ds = _open(model_uri, storage_options)  # latest — proves existence + bounds the target version
    latest = int(ds.version)
    if version > latest:
        raise TableVersionNotFoundError(f"model version {version} not found (latest is {latest})")
    _apply_gate(metrics_at(model_uri, storage_options, version), min_metrics, version=version)
    tags = ds.tags
    try:
        tags.get_version(tag)
    except ValueError:
        tags.create(tag, version)  # first bless of this model
    else:
        tags.update(tag, version)  # re-bless: move the pointer
    return version
