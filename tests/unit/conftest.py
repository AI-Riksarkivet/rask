"""Shared REAL-Lance fixtures for the maintenance unit suites.

Only builders whose construction is non-obvious live here. A shallow clone or an `add_bases` call is
a one-liner and stays inline in the test that uses it; a committed DATA OVERLAY is not — it needs a
hand-written `LanceFileWriter` file plus a `LanceOperation.DataOverlay` commit, and three suites
(the flag detector, the compaction refusal, the orphan-scan refusal) each need one.

Nothing here is faked. The whole point of the #64 refusal gate is agreeing with what Lance actually
writes into a manifest, and a double built from my own assumptions would only prove I am
self-consistent.
"""

from __future__ import annotations

import pathlib
import uuid
from collections.abc import Callable

import lance
import pyarrow as pa
import pytest
from lance import LanceOperation
from lance.file import LanceFileWriter
from lance.fragment import DataFile


@pytest.fixture
def overlay_dataset() -> Callable[[pathlib.Path], str]:
    """Build a REAL dataset that uses data overlay files (manifest feature flag 64).

    Verified on pylance 9.0.0: the commit succeeds through the public
    ``LanceOperation.DataOverlay`` API, the manifest comes back with ``reader/writer_feature_flags``
    == 64, and ``lance.dataset(uri)`` then REFUSES to reopen it with
    ``ValueError: Not supported: This dataset cannot be read by this version of Lance… Flags: 64``.

    So the dataset is genuinely on disk and genuinely flag-64, and the refusal a caller sees comes
    from the open rather than from our own manifest read — which is exactly the shape the gate has to
    handle, and exactly the shape that used to be reported as generic ``open:`` noise.
    """

    def _build(root: pathlib.Path) -> str:
        uri = str(root / "overlaid.lance")
        lance.write_dataset(
            pa.table({"id": pa.array(range(20), pa.int64()), "v": pa.array([f"v{i}" for i in range(20)])}),
            uri,
        )
        ds = lance.dataset(uri)
        fragment = ds.get_fragments()[0]
        # The overlay rewrites ONE column of ONE row — `fields` names the schema field id of `v`, and
        # `offsets` the row within the fragment whose value the overlay supersedes.
        field_id = fragment.metadata.files[0].fields[1]
        name = f"overlay-{uuid.uuid4()}.lance"
        path = pathlib.Path(uri) / "data" / name
        with LanceFileWriter(str(path)) as writer:
            writer.write_batch(pa.table({"v": pa.array(["NEW0"])}).to_batches()[0])
        data_file = DataFile(
            path=name,
            fields=[field_id],
            column_indices=[0],
            file_major_version=2,
            file_minor_version=1,
            file_size_bytes=path.stat().st_size,
        )
        lance.LanceDataset.commit(
            uri,
            LanceOperation.DataOverlay(
                groups=[
                    LanceOperation.DataOverlayGroup(
                        fragment_id=fragment.fragment_id,
                        overlays=[LanceOperation.DataOverlayFile(data_file=data_file, offsets=[0])],
                    )
                ]
            ),
            read_version=ds.version,
        )
        return uri

    return _build
