"""The lander against REAL Lance — A4 and D6's creation two-step.

No mocked Lance. The claim under test is that this write path works on pylance 9.0.0, and a fake
would only prove the fake agrees with itself. §7.11 verified the mechanics standalone; this verifies
the lander's own composition of them.

The catalog is a structural fake, because it is a network service and its CONTRACT (create empty
server-side, register the committed version with the run id) is what matters here, not its wire
format — the estate's `_FakeRayClient` pattern.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import lance
import pyarrow as pa
import pytest

from ingest.lander import CREATION_FLAGS, ForeignFileVersionError, Lander, create_empty, write_unit_fragments
from service_kit.lakehouse.features import manifest_feature_flags, mixes_data_file_versions, unsupported_features


SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string())])


class _FakeCatalog:
    """Records the contract calls so the test can assert the run id is registered, not just implied."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.registered: list[tuple[str, int, str]] = []

    def ensure_dataset(self, project: str, dataset: str, schema: pa.Schema) -> str:
        uri = str(self._root / f"{project}-{dataset}.lance")
        if not Path(uri).exists():
            create_empty(uri, schema)
        return uri

    def register_version(self, dataset_uri: str, version: int, run_id: str) -> None:
        self.registered.append((dataset_uri, version, run_id))


def _batch(ids: list[int]) -> pa.Table:
    return pa.table(
        {"id": ids, "source_uri": [f"iiif://vol/{i}" for i in ids]},
        schema=SCHEMA,
    )


@pytest.fixture
def lander(tmp_path: Path) -> tuple[Lander, _FakeCatalog]:
    cat = _FakeCatalog(tmp_path)
    return Lander(cat), cat


def test_creation_is_an_empty_dataset_carrying_the_creation_time_flags(tmp_path: Path) -> None:
    """D6 step 1: the catalog creates EMPTY, so no byte transits it — and the flags land at v1.

    `enable_stable_row_ids` is creation-time-only and a silent no-op afterwards
    (lance_docs/file_format.md:4011-4013), so if it is not set here it can never be set. CDF and
    every silver `source_rowid` reference depend on it.
    """
    uri = str(tmp_path / "empty.lance")
    version = create_empty(uri, SCHEMA)
    ds = lance.dataset(uri)

    assert version == 1
    assert ds.count_rows() == 0, "creation must carry zero rows — that is what keeps bytes out of the catalog"
    # Stable row ids are observable: a row written later gets a durable _rowid.
    ds.insert(_batch([1]))
    got = lance.dataset(uri).to_table(columns=["id"], with_row_id=True)
    assert "_rowid" in got.column_names
    assert CREATION_FLAGS["enable_stable_row_ids"] is True


def test_workers_fragments_land_in_one_commit(lander: tuple[Lander, _FakeCatalog]) -> None:
    """D6 step 2, and A4: three workers, ONE version bump, every row present."""
    land, _cat = lander
    uri = land.ensure("p", "pages", SCHEMA)
    before = lance.dataset(uri).version

    fragments: list[str] = []
    for worker in range(3):
        fragments.extend(write_unit_fragments(uri, _batch([worker * 2, worker * 2 + 1])))

    result = land.commit_fragments(uri, fragments, run_id="run-1")

    assert result.rows == 6
    assert result.fragments_committed == 3
    assert result.version == before + 1, "a run must produce exactly ONE data-visibility commit"


def test_the_run_id_is_registered_with_the_commit(lander: tuple[Lander, _FakeCatalog]) -> None:
    """The commit-metadata anchor: how a died-after-commit run is reconciled from storage truth."""
    land, cat = lander
    uri = land.ensure("p", "pages", SCHEMA)
    frags = write_unit_fragments(uri, _batch([1, 2]))
    land.commit_fragments(uri, frags, run_id="run-abc")

    assert cat.registered == [(uri, 2, "run-abc")]


def test_an_all_failed_run_leaves_no_version_behind(lander: tuple[Lander, _FakeCatalog]) -> None:
    """Empty fragments is a no-op, not an empty commit.

    A version nobody can explain is worse than no version: the cascade triggers on publication, and
    a run whose every unit failed must not look like data arrived.
    """
    land, cat = lander
    uri = land.ensure("p", "pages", SCHEMA)
    before = lance.dataset(uri).version

    result = land.commit_fragments(uri, [], run_id="run-empty")

    assert result.version == before
    assert result.rows == 0
    assert cat.registered == [], "nothing committed means nothing registered"


def test_a_second_run_appends_rather_than_overwriting(lander: tuple[Lander, _FakeCatalog]) -> None:
    """A4: ingesting B after A must not destroy A.

    The medallion's head wrote mode="overwrite" against one fixed per-lane URI, so ingesting volume
    B destroyed volume A (§1.2). Append is the whole fix, and it is asserted rather than assumed.
    """
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)

    land.commit_fragments(uri, write_unit_fragments(uri, _batch([1, 2])), run_id="run-A")
    land.commit_fragments(uri, write_unit_fragments(uri, _batch([3, 4])), run_id="run-B")

    ids = sorted(lance.dataset(uri).to_table(columns=["id"]).column("id").to_pylist())
    assert ids == [1, 2, 3, 4], "volume A's rows did not survive volume B"


def test_ensure_is_idempotent(lander: tuple[Lander, _FakeCatalog]) -> None:
    """Re-accepting a run must not recreate the dataset and lose what landed."""
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)
    land.commit_fragments(uri, write_unit_fragments(uri, _batch([1])), run_id="r")
    again = land.ensure("p", "pages", SCHEMA)

    assert again == uri
    assert lance.dataset(again).count_rows() == 1


# --------------------------------------------------------------------------- #
# read_version — carried, not re-read (the F12a residual)
# --------------------------------------------------------------------------- #


def test_the_carried_version_is_what_reaches_the_commit(lander: tuple[Lander, _FakeCatalog], monkeypatch: pytest.MonkeyPatch) -> None:
    """The property that is actually true, asserted on the CALL rather than on an outcome.

    An outcome assertion cannot see this: an Append COMMUTES, so Lance accepts a stale `read_version`
    and rebases (pinned below). That makes the carried version invisible in the resulting dataset —
    which is exactly why re-reading it survived undetected on this branch while the catalog branch
    carried it. So the pin is on what is handed to `lance`.
    """
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)
    land.commit_fragments(uri, write_unit_fragments(uri, _batch([1])), run_id="run-A")

    seen: dict[str, Any] = {}
    real = lance.LanceDataset.commit

    # `*args`/`**kw` rather than the real signature: the spy DELEGATES, so restating `commit`'s
    # parameter list here would be a second copy to keep in step with pylance for no benefit.
    def spy(*args: Any, **kw: Any) -> Any:
        seen["read_version"] = kw.get("read_version")
        return real(*args, **kw)

    monkeypatch.setattr(lance.LanceDataset, "commit", staticmethod(spy))
    land.commit_fragments(uri, write_unit_fragments(uri, _batch([2])), run_id="run-B", read_version=1)

    assert seen["read_version"] == 1, "the carried version was dropped and the current one re-read"


def test_an_append_commutes_so_a_stale_version_is_NOT_refused(lander: tuple[Lander, _FakeCatalog]) -> None:
    """The premise that makes the re-read survivable, pinned against pylance rather than assumed.

    Written because the opposite is the natural guess, and acting on it would mean claiming this
    change fixes a lost update. It does not: appends commute and Lance rebases them. If a future
    pylance makes an Append conflict-checked, THIS test goes red and the carried version stops being
    a consistency tidy-up and becomes a correctness fix — which is the moment to re-read the
    docstring in `lander.commit_fragments`.
    """
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)
    first = land.commit_fragments(uri, write_unit_fragments(uri, _batch([1])), run_id="run-A")

    land.commit_fragments(uri, write_unit_fragments(uri, _batch([2])), run_id="run-B", read_version=first.version - 1)

    assert sorted(lance.dataset(uri).to_table(columns=["id"]).column("id").to_pylist()) == [1, 2]


@pytest.mark.parametrize("read_version", [None, 0])
def test_no_read_version_commits_onto_the_current_one(lander: tuple[Lander, _FakeCatalog], read_version: int | None) -> None:
    """``LocalCatalog`` carries 0 (runtime.ensure_dataset_at); Lance has no version 0 to open, so both judge the latest."""
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)
    land.commit_fragments(uri, write_unit_fragments(uri, _batch([1])), run_id="run-A")

    result = land.commit_fragments(uri, write_unit_fragments(uri, _batch([2])), run_id="run-B", read_version=read_version)

    assert sorted(lance.dataset(uri).to_table(columns=["id"]).column("id").to_pylist()) == [1, 2]
    assert result.version > 1


# --------------------------------------------------------------------------- #
# File versions — a run inherits the table's, and a foreign set is refused
# --------------------------------------------------------------------------- #


def _file_versions(uri: str) -> set[tuple[int, int]]:
    return {(f.file_major_version, f.file_minor_version) for fragment in lance.dataset(uri).get_fragments() for f in fragment.metadata.files}


def _precreate(tmp_path: Path, version: Literal["2.1", "2.2"]) -> None:
    """At the path `_FakeCatalog` composes, so `ensure` finds it and skips `create_empty`."""
    lance.write_dataset(SCHEMA.empty_table(), str(tmp_path / "p-pages.lance"), mode="create", data_storage_version=version, enable_stable_row_ids=True)


@pytest.mark.parametrize("version", ["2.1", "2.2"])
def test_a_run_into_an_existing_table_inherits_its_version(lander: tuple[Lander, _FakeCatalog], tmp_path: Path, version: Literal["2.1", "2.2"]) -> None:
    """The writer names no version, so its files land at whatever the table holds and the flags stay clean."""
    _precreate(tmp_path, version)
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)

    land.commit_fragments(uri, write_unit_fragments(uri, _batch([1, 2])), run_id="r")

    assert _file_versions(uri) == {(2, int(version[-1]))}
    assert unsupported_features(lance.dataset(uri)) is None


def test_an_overwrite_between_the_judgement_and_the_commit_cannot_slip_unjudged_files_on(
    lander: tuple[Lander, _FakeCatalog], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """read_version 0 is judged at the latest version and committed AT it; committed at 0, Lance checks nothing."""
    import ingest.lander as lander_module

    _precreate(tmp_path, "2.1")
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)
    fragments = write_unit_fragments(uri, _batch([1]))
    judge = lander_module.describe_foreign_data_file_versions

    def _overwrite_then_judge(table_version: str, files: Any) -> str | None:
        lance.write_dataset(SCHEMA.empty_table(), uri, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
        return judge(table_version, files)

    monkeypatch.setattr(lander_module, "describe_foreign_data_file_versions", _overwrite_then_judge)

    with pytest.raises(OSError):
        land.commit_fragments(uri, fragments, run_id="r", read_version=0)
    assert unsupported_features(lance.dataset(uri)) is None


def test_a_run_into_a_fresh_table_lands_at_the_creation_version(lander: tuple[Lander, _FakeCatalog]) -> None:
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)

    land.commit_fragments(uri, write_unit_fragments(uri, _batch([1, 2])), run_id="r")

    assert lance.dataset(uri).data_storage_version == CREATION_FLAGS["data_storage_version"]
    assert _file_versions(uri) == {(2, 2)}
    assert unsupported_features(lance.dataset(uri)) is None


@pytest.mark.parametrize(("table_version", "fragment_version"), [("2.1", "2.2"), ("2.2", "2.1")])
def test_the_lander_REFUSES_fragments_at_another_file_version(
    lander: tuple[Lander, _FakeCatalog], tmp_path: Path, table_version: Literal["2.1", "2.2"], fragment_version: str
) -> None:
    """The LocalCatalog path never reaches the catalog's /commit door, so the lander judges the set itself.

    Refused BEFORE the commit because flag 256 cannot be removed in place.
    """
    _precreate(tmp_path, table_version)
    land, cat = lander
    uri = land.ensure("p", "pages", SCHEMA)
    base = lance.dataset(uri).version
    foreign = [json.dumps(f.to_json()) for f in lance.fragment.write_fragments(_batch([1]), uri, data_storage_version=fragment_version)]

    with pytest.raises(ForeignFileVersionError, match=rf"{re.escape(fragment_version)}.*{re.escape(table_version)}"):
        land.commit_fragments(uri, foreign, run_id="r")

    dataset = lance.dataset(uri)
    assert dataset.version == base
    assert not mixes_data_file_versions(manifest_feature_flags(dataset)[0])
    assert cat.registered == []
