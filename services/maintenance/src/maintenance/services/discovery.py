"""Where the estate's Lance datasets are: a bounded walk of one bucket, and the prefixes it could not reach.

Pure list-logic over a pyarrow filesystem, shared by the sweep, the orphan scan (``reconcile.py``) and the purge's
base-reference pre-pass, so all three cover the same datasets.
"""

from __future__ import annotations

import pyarrow.fs as pafs
from pydantic import BaseModel

from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER, BRANCH_REFS_DIR, branch_name_from_ref_file
from service_kit.lakehouse.control_prefixes import CONTROL_PREFIXES


class Discovery(BaseModel):
    """What one bucket's walk found — AND what it did not reach.

    ``truncated`` keeps the depth bound from being silent: a dataset nested deeper than ``max_depth`` is
    neither maintained by the sweep nor scanned by the orphan pass, and a report over the datasets the
    walk happened to reach would be the "0 that means we did not look" the orphan module forbids. A
    prefix the walk stopped at is DATA the caller must dispose of — the sweep logs it, the reconciler
    files an ``IncompleteScan``.
    """

    uris: list[str] = []
    #: Prefixes the walk stopped at because it hit ``max_depth`` — each may hide any number of datasets.
    truncated: list[str] = []


#: Control-plane bookkeeping the walk never enters. No dataset is ever written under one, so descending
#: costs S3 round-trips on the hot discovery path and, worse, MANUFACTURES coverage gaps: a subtree the
#: walk stops inside becomes an `IncompleteScan`, which blocks `purge.report_is_clean` for a gap that
#: cannot be filled. Measured 2026-09-08: 10 of the primary bucket's 59 truncated prefixes were
#: `_lineage_outbox/<event>.json/<id>/`, the only one of the five not listed here.
#:
#: The outbox is also the one that is DRAINED continuously, so walking it races the drain — a depth-4
#: walk died `FileNotFoundError` on an entry that was gone by the time it was opened.
#: `_staging` is where a DISTRIBUTED write lands its output before the one merge that converges it
#: into the tier (LH-007). It is a real Lance dataset while it
#: exists, so without this it would be discovered, maintained and counted among the estate's governed
#: tables. Once the destination exists the set is already unreachable — the walk descends a dataset
#: root's children only into `tree/` — but on the run that CREATES the destination the parent is still a
#: plain directory, and a crash inside that window leaves the set findable. One underscore, so the
#: `__`-prefix rule above does not cover it.
#: `_backups` earns its place for a reason the others do not have. The rest are skipped because probing
#: them is wasted round-trips; this one BLOCKS RECLAMATION. `control_root_backup.py` writes
#: `_backups/control/<timestamp>/…`, which nests past `discovery_max_depth`, so the walk would record an
#: `IncompleteScan` — and `report_is_clean` refuses to certify an estate with anything incomplete, which
#: gates the #79 purge. Measured on the deployed release 2026-09-16: three incomplete entries, all
#: backup snapshots, against 932 orphan files across 8 datasets the purge could not touch.
#: `_bases` holds the catalog's per-table base records ([[LH-279]]) and `_maintenance_due` the sweep's own due records
#: (ADR 0162): control-root JSON, never a dataset.
#: The set is the estate's one list (:data:`service_kit.lakehouse.control_prefixes.CONTROL_PREFIXES`), which the
#: catalog's destructive drop and the purge refuse a location crossing.


def _may_hide_a_dataset(fs: pafs.FileSystem, path: str) -> bool:
    """Could a Lance dataset exist somewhere below ``path``? One listing, and it answers by EVIDENCE.

    A dataset IS a directory with a ``_versions/`` child (`objectfs.is_lance_dataset_root`, the
    estate's one definition of the marker), so a directory whose children are all FILES cannot hide
    one at any depth. Saying otherwise is inventing a coverage gap — and an invented gap is not
    cosmetic: `purge.report_is_clean` blocks on any `IncompleteScan`, so a prefix that will never hold
    a dataset blocks reclamation exactly as hard as a manifest nobody could read, and blocks it
    forever because nothing about that prefix will ever change.

    Measured on the deployed estate 2026-09-10: 64 of 64 `incomplete` units were `depth limit
    reached`, the majority `s3://lance-catalog/models/<model>/<hash>` — model artefacts, which are
    files. Walking `models/` to depth 8 finds 0 datasets (recorded in
    `test_the_bucket_walk_does_not_invent_coverage_gaps.py`'s header), so raising the bound buys
    nothing and this buys all of it.

    ERRS TOWARD REPORTING. An unreadable or vanished prefix returns True — the walk genuinely does not
    know, and trading a false gap for a false CLEAN is strictly worse: the first blocks a purge, the
    second lets one run over ground nobody scanned. That is also why this asks the cheap question
    (are there directories) rather than recursing to look for a marker: one listing per truncated
    prefix, at the bound only, against a walk whose dominant cost is `_protected_roots` opening every
    dataset it found.
    """
    try:
        children = fs.get_file_info(pafs.FileSelector(path, recursive=False, allow_not_found=True))
    except OSError:
        return True
    return any(child.type == pafs.FileType.Directory for child in children)


def _is_a_prefix_of_an_existing_bucket(fs: pafs.FileSystem, root: str) -> bool:
    """Whether ``root`` is a key prefix inside an existing bucket, which S3 lists as absent until an object lands under it.

    A bucket is the store's own container and exists by itself; a key prefix exists only while some object sits under it.
    pyarrow reads an empty listing of a prefix as an absent path (measured on pyarrow 25.0.0 against moto: one
    ListObjectsV2, then `Path does not exist`), so a configured data base no table has written to yet reads exactly as an
    unreachable one. The bucket's own answer is what says the prefix is merely empty. A probe that fails raises, so a
    store that will not answer is reported rather than certified.
    """
    bucket, _, prefix = root.partition("/")
    return isinstance(fs, pafs.S3FileSystem) and bool(prefix) and fs.get_file_info(bucket).type == pafs.FileType.Directory


def _branch_refs(fs: pafs.FileSystem, root: str) -> list[str]:
    """The branches ``root``'s ``_refs/branches`` names, sorted ([[LH-203]]).

    THE REFS, NOT THE ``tree/`` DIRECTORY. A branch name may contain ``/`` and the format lays ``a/b`` at
    ``tree/a/b/`` (`lance_docs/lance/format/table/branch_tag.md` § "Branch Dataset Layout"), so a walk of ``tree/``'s children finds
    ``tree/a`` and never ``a/b`` inside it, and finds ``tree/a`` again after ``a`` is deleted, from
    the files Lance leaves behind while ``a/b`` exists. The refs are the format's own list of branches
    (`lance_docs/lance/format/table/branch_tag.md` § "Branch Metadata Path", one ``<name>.json`` per branch with ``/`` written
    ``%2F``), read with one listing, which is what the ``tree/`` probe cost.

    A listing that fails raises rather than answering "no branches": both callers catch per bucket and
    report it, where an empty answer would leave every branch of this dataset unmaintained in silence.
    """
    selector = pafs.FileSelector(f"{root}/{BRANCH_REFS_DIR}", recursive=False, allow_not_found=True)
    names = (branch_name_from_ref_file(info.base_name) for info in fs.get_file_info(selector) if info.type == pafs.FileType.File)
    return sorted(name for name in names if name is not None)


def discover_datasets(fs: pafs.FileSystem, bucket: str, *, max_depth: int = 3) -> Discovery:
    """Lance datasets under ``bucket`` — a directory IS a dataset iff it has a ``_versions/`` child
    (the Lance table-layout marker); any other directory is a namespace prefix and is recursed into
    (bounded by ``max_depth``). Every branch a dataset's ``_refs/branches`` names is listed as well, at
    ``<dataset>/tree/<branch>``, because a BRANCH is a full dataset the parent contains rather than part
    of it; none of a dataset's children is walked. Skips ``__`` bookkeeping dirs (the catalog's ``__manifest``) and the
    control-plane registries (``_warehouses``, ``_policies``, ``_protection``, ``_trash``) — no dataset ever
    lives under them, and probing them is wasted S3 round-trips on the hot discovery path.

    The catalog lays top-level tables out as ``<uuid>_<table_id>/``, but the medallion cascade nests
    its datasets one level down (``medallion/bronze``, ``medallion/silver-media`` …) — without the
    marker probe the sweep would report the ``medallion/`` prefix as a failed dataset AND never
    maintain the real ones under it.

    Returns a :class:`Discovery`, not a bare list, so the depth cut-off cannot be dropped on the floor.
    """
    found = Discovery()

    def _walk(prefix: str, depth: int) -> None:
        try:
            entries = fs.get_file_info(pafs.FileSelector(prefix, recursive=False))
        except FileNotFoundError:
            # A prefix named by one listing can be gone before the walk descends into it — the estate
            # writes and reclaims continuously, and BOTH callers catch per BUCKET, so a raise here would
            # cost a whole bucket its maintenance for the tick over one vanished sub-prefix.
            #
            # ONLY below the root, or at a root that is a prefix of a bucket that exists. A bucket that does
            # not exist must stay distinguishable from an empty one: both callers rely on that raise to report
            # the bucket rather than certify it as clean, and swallowing it here would be the "0 that means we
            # did not look" this module forbids.
            if depth == 1 and not _is_a_prefix_of_an_existing_bucket(fs, prefix):
                raise
            return
        for info in entries:
            if info.type != pafs.FileType.Directory:
                continue
            name = info.path.rstrip("/").split("/")[-1]
            if name.startswith("__") or name in CONTROL_PREFIXES:
                continue
            marker = fs.get_file_info(f"{info.path}/_versions")
            if marker.type == pafs.FileType.Directory:
                found.uris.append(f"s3://{info.path}")
                # A BRANCH IS A WHOLE DATASET THIS ONE CONTAINS, and stopping here would leave every
                # branch in the estate invisible: `tree/<name>/` carries its own `_versions/` and `_transactions/`,
                # so it accumulates versions with nothing ever reclaiming them. Measured 2026-09-07:
                # 85 of the deployed estate's 250 tables carry at least one.
                #
                # From the REFS, never by walking the dataset's children (`_branch_refs`). `data/`,
                # `_indices/`, `_deletions/` and `_transactions/` are not datasets, and `tree/`'s
                # children are directories, not branches: a branch named `a/b` sits two levels down.
                #
                # A branch sets `base_paths` (measured (16, 16), data files identical to the parent's at
                # `base_id` 0, no `data/` of its own), so the compaction gate and the orphan scan refuse
                # it on that flag while root-scoped `cleanup_old_versions` permits it via
                # `SUPPORTED_FOR_GC`.
                found.uris.extend(f"s3://{info.path}/{BRANCH_CONTAINER}/{name}" for name in _branch_refs(fs, info.path))
            elif depth < max_depth:
                _walk(info.path, depth + 1)
            elif _may_hide_a_dataset(fs, info.path):
                # Not a dataset, out of depth, and it CONTAINS DIRECTORIES — so something below may be
                # one and the walk cannot say. Record the prefix rather than returning silently.
                found.truncated.append(f"s3://{info.path}")

    _walk(bucket, 1)
    return found
