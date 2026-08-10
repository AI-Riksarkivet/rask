"""The lander against REAL Lance — A4 and D6's creation two-step.

No mocked Lance. The claim under test is that this write path works on pylance 9.0.0, and a fake
would only prove the fake agrees with itself. §7.11 verified the mechanics standalone; this verifies
the lander's own composition of them.

The catalog is a structural fake, because it is a network service and its CONTRACT (create empty
server-side, register the committed version with the run id) is what matters here, not its wire
format — the estate's `_FakeRayClient` pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from ingest.lander import CREATION_FLAGS, Lander, create_empty, write_unit_fragments


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


def test_an_omitted_read_version_still_reads_the_current_one(lander: tuple[Lander, _FakeCatalog]) -> None:
    """Backwards-compatible by default: every existing caller passes nothing and keeps the old
    behaviour. `None` is the sentinel and not `0`, because 0 is a legal Lance version — a falsy
    default would silently mean "re-read" for a caller that meant "the empty dataset"."""
    land, _ = lander
    uri = land.ensure("p", "pages", SCHEMA)
    land.commit_fragments(uri, write_unit_fragments(uri, _batch([1])), run_id="run-A")

    result = land.commit_fragments(uri, write_unit_fragments(uri, _batch([2])), run_id="run-B")

    assert sorted(lance.dataset(uri).to_table(columns=["id"]).column("id").to_pylist()) == [1, 2]
    assert result.version > 1
