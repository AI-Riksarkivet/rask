"""diff2 F6(d) — the sweep must not maintain datasets that are IN THE TRASH.

A recoverable drop deregisters the table's ``__manifest`` row and files a trash record. It moves no
byte: measured against the shipped ``dir`` backend, the dataset directory keeps its ``_versions/``
child — and that marker is the ONLY thing ``discover_datasets`` consults to decide "this is a Lance
dataset". So a dropped table was rediscovered on the very next tick, indistinguishable from a live
one.

The consequence is not a wasted cycle, it is silent irreversible mutation of data the owner was told
is frozen:

    Karin drops `silver$htr_lines_2019` on a Friday. Grace is 7 days, so the UI tells her she can
    undrop until next Friday. The cron fires every 30 minutes (deploy/values-prod.yaml). Over the
    weekend the sweep runs `cleanup_old_versions` on that dataset, repeatedly. Monday she undrops and gets a table back — but not the one she dropped: the
    version history she was trying to recover to has been pruned, and the sweep's summary counted it
    as a successful maintenance pass.

These tests drive the REAL ``run_sweep`` rather than the partition helper, because the defect is
positional: the exclusion has to sit between the whole-estate base-ref pre-pass and the per-dataset
loop, and a test that called a helper directly would pass with the filter in the one place that
re-opens a different data-loss bug (see the shallow-clone test below).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import lance
import pyarrow as pa
import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services.discovery import Discovery
from service_kit.lakehouse import location_claims, trash
from service_kit.lakehouse.base_refs import normalise


def _dataset(path: Path) -> str:
    """A two-version Lance dataset — enough for expiry to have something to do."""
    uri = str(path)
    table = pa.table({"n": pa.array([1, 2], pa.int64())})
    lance.write_dataset(table, uri, mode="overwrite")
    lance.write_dataset(table, uri, mode="append")
    return uri


@pytest.fixture
def settings(tmp_path: Path) -> MaintenanceSettings:
    control = tmp_path / "control"
    control.mkdir()
    return MaintenanceSettings(
        MAINTENANCE_CONTROL_ROOT=str(control),
        MAINTENANCE_POLICY_ROOT=str(control),
    )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    settings: MaintenanceSettings,
    uris: list[str],
) -> tuple[list[Any], list[str]]:
    """Run the real sweep over ``uris``, each named its own table by the catalog's `__manifest`; return (results, the URIs
    whose expiry pass reached its reclamation decision)."""
    from maintenance.services import expiry
    from maintenance.services import sweep as sweep_mod
    from maintenance.services.due import TableIds

    reached: list[str] = []
    real = expiry.count_reclaimable

    def _spy(ds: lance.LanceDataset, uri: str, policy: expiry.ExpiryPolicy) -> int:
        reached.append(uri)
        return real(ds, uri, policy)

    monkeypatch.setattr(sweep_mod, "_s3fs", lambda _s: None)
    monkeypatch.setattr(sweep_mod, "discover_datasets", lambda _fs, _bucket, *, max_depth: Discovery(uris=list(uris)))
    # Each table carries a uuid as every catalog door gives one, in its location's claim, and its namespace one in its
    # `__manifest` row: maintenance refuses a table whose policy owners it cannot identify ([[LH-523]]).
    claims = location_claims.ClaimStore(control_root=settings.resolved_control_root, storage_options={})
    for i, uri in enumerate(uris):
        location_claims.take(claims, normalise(uri), f"db$t{i}", ["db", f"t{i}"], is_gone=lambda _claim: False, uuid=str(uuid4()))
    named = TableIds(by_location={normalise(uri): f"db$t{i}" for i, uri in enumerate(uris)}, namespace_uuids={"db": str(uuid4())})
    monkeypatch.setattr(sweep_mod, "table_ids", lambda *_a, **_k: named)
    monkeypatch.setattr(expiry, "count_reclaimable", _spy)
    return sweep_mod.run_sweep(settings), reached


def test_a_trashed_dataset_is_not_expired_and_says_so(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ACCEPTANCE CRITERION. The dropped dataset is left alone, and the tick reports why.

    So is each of its branches ([[LH-449]]): discovery lists a branch at ``<table>/tree/<branch>``, and the record
    names only the table, so an exclusion that compared whole locations would expire a recoverable table's branches.

    The table's name is one the catalog percent-encodes ([[LH-318]]): the record holds the location `describe_table`
    answers, ``…/r%C3%A4ksm%C3%B6rg%C3%A5s%25`` for ``räksmörgås%``, while the store keeps, and discovery lists, the
    directory that spelling opens (measured on pylance 13.0.0, `dir` backend, over moto and a local root).
    """
    live = _dataset(tmp_path / "live.lance")
    dropped = _dataset(tmp_path / "räksmörgås%.lance")
    branch = _dataset(tmp_path / "räksmörgås%.lance" / "tree" / "backfill")

    # Exactly what the catalog's recoverable drop writes: the bytes stay where they are, and a record
    # under the control root points at them by the URL the catalog describes the table at.
    location = f"file://{tmp_path}/r%C3%A4ksm%C3%B6rg%C3%A5s%25.lance"
    record = trash.make_record("silver$räksmörgås%", kind="table", uuid=str(uuid4()), location=location, dropped_by="user:karin", grace_days=7)
    trash.put(str(settings.resolved_control_root), {}, record)

    results, expired = _run(monkeypatch, settings, [live, dropped, branch])

    assert dropped not in expired, "the sweep reclaimed versions of a dataset the grace window promises is frozen"
    assert branch not in expired, "the sweep reclaimed versions of a trashed table's branch"
    assert live in expired, "the exclusion caught a LIVE dataset — the path comparison is too broad"

    excluded = [r for r in results if r.trashed]
    assert [r.uri for r in excluded] == [dropped, branch]
    # The reason names the record AND its deadline: an exclusion that has outlived its deadline is a
    # record the purge keeps refusing, i.e. a PERMANENT exclusion, and that must be readable from the
    # summary rather than inferred.
    assert "silver$räksmörgås%" in (excluded[0].trashed or "")
    assert "expires_at=" in (excluded[0].trashed or "")


def test_an_expired_but_unpurged_record_still_freezes_its_dataset(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A passed deadline is permission for the PURGE to reclaim, not permission for the sweep to expire.

    `undrop` deliberately does not consult the clock, so a record whose deadline has passed but which
    nothing has reclaimed still describes recoverable bytes. Keying the exclusion on `expired()`
    instead of on every record would put exactly those datasets — the ones most likely to be stuck —
    back under expiry.
    """
    dropped = _dataset(tmp_path / "long_expired.lance")
    record = trash.make_record("silver$old", kind="table", uuid=str(uuid4()), location=dropped, dropped_by="user:karin", grace_days=7)
    record["expires_at"] = "2020-01-01T00:00:00+00:00"
    trash.put(str(settings.resolved_control_root), {}, record)

    results, expired = _run(monkeypatch, settings, [dropped])

    assert expired == [], "an expired-but-unpurged record was reclaimed"
    assert [r.uri for r in results if r.trashed] == [dropped]


def test_a_trashed_clone_still_protects_its_live_source(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE POSITIONAL TRAP, and the reason this suite drives the real sweep.

    The exclusion must be applied AFTER `base_refs.protected_roots` runs over the FULL discovered list.
    A trashed shallow CLONE's manifest `base_paths` are the only evidence in the estate that its LIVE
    source must not be reclaimed (#114/#128d) — the source carries no flag of its own. Filter the
    trashed dataset out before the pre-pass and that protection disappears, so the sweep destroys the
    live source's data files: a worse bug than the one being fixed, reached by putting the right
    filter in the wrong place.

    Asserted through the pre-pass's own output rather than by constructing a real shallow clone, which
    needs a Lance feature this suite does not otherwise depend on.
    """
    from maintenance.services import sweep as sweep_mod
    from service_kit.lakehouse import base_refs

    source = _dataset(tmp_path / "source.lance")
    clone = _dataset(tmp_path / "clone.lance")
    trash.put(
        str(settings.resolved_control_root),
        {},
        trash.make_record("silver$clone", kind="table", uuid=str(uuid4()), location=clone, dropped_by="user:karin", grace_days=7),
    )

    seen: list[list[str]] = []
    real_protected = base_refs.protected_roots
    monkeypatch.setattr(
        sweep_mod.base_refs,
        "protected_roots",
        # Forwards **kw so the stub cannot fall behind the real signature: the sweep threads the
        # service's configured Lance session, and a stub that silently dropped it would keep passing
        # while the call it stands in for had moved on.
        lambda uris, options=None, **kw: (seen.append(list(uris)), real_protected(uris, options, **kw))[1],
    )
    _run(monkeypatch, settings, [source, clone])

    assert seen, "the base-ref pre-pass did not run"
    assert clone in seen[0], (
        "the trashed dataset was filtered out BEFORE the base-ref pre-pass. A trashed shallow clone's "
        "base_paths are the only thing protecting its live source from reclamation — removing it here "
        "re-opens #114 from the trash side."
    )


def test_an_unreadable_trash_index_aborts_the_tick(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail toward NOT reclaiming, exactly as the policy registry does.

    The read decides which datasets may be touched, so swallowing an error would mean an empty exclusion set, which
    means maintain everything, including the frozen data. A protective registry we cannot read at all stops the tick,
    and the next cron fire retries.
    """
    from maintenance.services import sweep as sweep_mod

    live = _dataset(tmp_path / "live.lance")

    def _boom(*_a: object, **_k: object) -> list[dict[str, Any]]:
        raise OSError("control root unreachable")

    monkeypatch.setattr(sweep_mod.trash, "list_all", _boom)
    with pytest.raises(OSError, match="control root unreachable"):
        _run(monkeypatch, settings, [live])


def test_a_namespace_record_carrying_no_location_excludes_nothing(tmp_path: Path, settings: MaintenanceSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A trashed NAMESPACE and a declared-only table both record `location=""`.

    Normalising the empty string yields the empty string, which is a prefix of every path — so an
    unguarded set comprehension would build an exclusion entry that matches nothing by equality but
    invites a future prefix-match refactor to exclude the entire estate. Pin the guard.
    """
    live = _dataset(tmp_path / "live.lance")
    trash.put(
        str(settings.resolved_control_root),
        {},
        trash.make_record("silver", kind="namespace", uuid=str(uuid4()), location="", dropped_by="user:karin", grace_days=7),
    )

    results, expired = _run(monkeypatch, settings, [live])

    assert expired == [live], "a location-less namespace record froze a live dataset"
    assert [r for r in results if r.trashed] == []
