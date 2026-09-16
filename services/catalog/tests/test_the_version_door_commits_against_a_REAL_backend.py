"""The version door, driven end to end against a real `dir` namespace — not a stub.

WHY THIS FILE EXISTS. [[LH-018]]'s manifest guard shipped 2026-09-11 with a full unit suite and was
deployed, and it had CLOSED the door it was meant to confine: it demanded a table-relative
`manifest_path` and the backend resolves that field ABSOLUTELY, so the only spelling that can commit
was the one being refused 400. Every test used a stub namespace that recorded the call and returned
`object()`, so "the guard let it through" was the whole of what could be asserted, and "the backend
then accepted it" was assumed. A stub cannot tell a working door from a shut one.

WHAT THE BACKEND ACTUALLY WANTS, measured 2026-09-16 by driving every spelling against ONE staged
manifest that was present on disk each time::

    '_versions/<n>.manifest-<uuid>'                  -> InvalidInput "Staging manifest not found"
    't.lance/_versions/<n>.manifest-<uuid>'          -> InvalidInput "Staging manifest not found"
    '/<root>/t.lance/_versions/<n>.manifest-<uuid>'  -> OK, version 2 committed

The staged shape is the spec's own (`lance_docs/file_format.md:5391`: stage at
`{dataset}/_versions/{version}.manifest-{uuid}`, finalise by copy), and the version file name is the
inverted u64 Lance writes on disk.

NOT MARKED SLOW: a table of three rows and one appended version, on local files.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
from lance_namespace import CreateTableVersionRequest, InvalidInputError, connect

from catalog.api.v1.endpoints import versions as versions_endpoint


lance = pytest.importorskip("lance")


class _Settings:
    delimiter = "$"


def _inverted(version: int) -> int:
    """Lance names a version's manifest by the u64 complement, so version 2 is `…613`."""
    return (1 << 64) - 1 - version


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 — the namespace type is runtime-only
    """A namespace holding `t` with a genuine version-2 manifest moved into the SPEC's staging shape.

    Moving the real manifest aside (rather than fabricating one) is what makes the commit leg real: the
    file the door moves is a manifest Lance itself wrote, so a success is a version the dataset can
    actually open.
    """
    root = tmp_path / "data"
    namespace = connect("dir", {"root": str(root)})
    schema = pa.schema([pa.field("i", pa.int64())])
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_table(pa.table({"i": pa.array([1, 2, 3], pa.int64())}, schema=schema))
    from lance_namespace import CreateTableRequest

    namespace.create_table(CreateTableRequest(id=["t"]), sink.getvalue().to_pybytes())
    namespace.create_table(CreateTableRequest(id=["victim"]), sink.getvalue().to_pybytes())

    table_dir = root / "t.lance"
    lance.write_dataset(pa.table({"i": pa.array([4], pa.int64())}, schema=schema), str(table_dir), mode="append")
    final = table_dir / "_versions" / f"{_inverted(2)}.manifest"
    staging = table_dir / "_versions" / f"{_inverted(2)}.manifest-{uuid.uuid4()}"
    shutil.move(final, staging)
    (table_dir / "_versions" / "latest_version_hint.json").write_text("1")

    # The emit trailer is patched at its SEAM. This file is about whether the backend accepts what the
    # door sends it; a stand-in emitter would only add a second thing that can fail.
    async def _no_emit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(versions_endpoint.lineage_deps, "emit_measured_write", _no_emit)
    return namespace, root, staging


def _create(namespace: Any, table: str, manifest_path: str) -> Any:  # noqa: ANN401 — the door's response model
    return asyncio.run(
        versions_endpoint.create_table_version(
            table,
            CreateTableVersionRequest(id=[table], version=2, manifest_path=manifest_path),
            namespace,
            cast(Any, {}),
            cast(Any, _Settings()),
            cast(Any, None),
            cast(Any, None),
            None,
        )
    )


def test_an_absolute_manifest_inside_this_table_ACTUALLY_COMMITS(staged) -> None:  # noqa: ANN001
    """THE REGRESSION LEG. Not "the guard allowed it" — the version exists afterwards and opens."""
    namespace, root, staging = staged

    _create(namespace, "t", str(staging))

    assert [v["version"] for v in lance.dataset(str(root / "t.lance")).versions()] == [1, 2]
    assert lance.dataset(str(root / "t.lance")).to_table().column("i").to_pylist() == [1, 2, 3, 4]


def test_a_replay_of_that_commit_answers_the_spec_s_CONCURRENT_MODIFICATION(staged) -> None:  # noqa: ANN001
    """Why no `Idempotency-Key` seam is wired here: the version CAS already converges the replay, with
    the code the spec declares. `lance_docs/namespace.md:1772` gives this operation exactly
    1 (NamespaceNotFound), 4 (TableNotFound), 14 (ConcurrentModification) — and 14 is what a replay
    gets, non-destructively, mapping to 409 rather than a bare 500."""
    from lance_namespace import ConcurrentModificationError

    namespace, root, staging = staged
    _create(namespace, "t", str(staging))

    with pytest.raises(ConcurrentModificationError):
        _create(namespace, "t", str(staging))

    assert [v["version"] for v in lance.dataset(str(root / "t.lance")).versions()] == [1, 2], "the replay moved something"


def test_a_sibling_s_manifest_is_refused_and_the_sibling_is_untouched(staged) -> None:  # noqa: ANN001
    """THE ATTACK, against the real backend. Unguarded this MOVES the file: the victim's slot takes the
    attacker's manifest and the victim's dataset then cannot be opened at all, because its manifest names
    data files in a directory it does not own. Asserting the staged file is still in place is the point —
    a refusal that let the move happen first would be no refusal."""
    namespace, root, staging = staged

    with pytest.raises(InvalidInputError):
        _create(namespace, "victim", str(staging))

    assert staging.exists(), "the manifest was moved out of `t` — the refusal came too late"
    assert lance.dataset(str(root / "victim.lance")).to_table().column("i").to_pylist() == [1, 2, 3]
