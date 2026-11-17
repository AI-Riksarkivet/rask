"""A write credential the catalog vends cannot put over a file a commit added: the STORE refuses it, and the table still
reads (ADR 0242).

Lance's data, deletion, index and transaction files are immutable once a version names them, and nothing legitimate
writes one twice. The write tier puts what a commit stages under ``data/``, ``_deletions/``, ``_indices/`` and
``_transactions/`` (``table_layout.WRITER_PUT_GLOBS``), so a session policy scoped by prefix alone let any holder of a
table's write credential put over every committed file of it: measured on RustFS 1.0.3 (ADR 0227 §9), seven bytes put
over a referenced data file (200) left the table unreadable ("Invalid range 0..403 for object of size 7 bytes"). The
catalog now seals each file a commit adds before its version lands, and the write tier's session policy refuses a put
over a sealed key, which the store decides.

ONE TABLE, THE STOCK PATH OF ADR 0157, ON THE REAL STORE: the catalog served by uvicorn over a bucket of the test's own on
the pinned RustFS (``race_catalog``'s ``s3`` case, a session the store's STS issues under the catalog's session policy, to
a user holding the catalog's canned policy), and stock pylance 13.0.0 writing with the credential the catalog vends it and
no other key: a create below the multipart threshold (one PUT of the data file), an append above it (a data file past
5 MiB, which pylance writes as a multipart upload: measured behind a counting proxy, ``POST ?uploads``, ``PUT
?partNumber``, ``POST ?uploadId``), a delete (a deletion file), a merge-insert, a scalar index build (index files) and a
compaction, each committed through ``version/create``. Then a write credential the catalog vends on ``DescribeTable``
puts over each kind of file the versions name, as one PUT, a multipart upload and a copy onto it, and is refused 403 by
the store every time; it still lands a new data file, and the table reads every row.

Collected only where ``make lifecycle-rustfs-check`` names the store: on a local root no store policy exists to refuse
anything (``tests/integration/conftest.py``).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import lance
import lance_namespace
import numpy as np
import pyarrow as pa
import pytest
from botocore.exceptions import ClientError

from lakehouse.storage_options import StorageOptions, object_client
from tests.integration.conftest import RaceCatalog


NAMESPACE = "lance_catalog$sealed"
TABLE_ID = ["lance_catalog", "sealed", "t"]
#: Past the 5 MiB a pylance 13.0.0 object writer buffers before it starts a multipart upload (measured: a 24 MiB append
#: wrote its data file as one, a 10-row create as one PUT).
LARGE_ROW_BYTES = 8192
LARGE_ROWS = 1000


def _rows(ids: range, width: int) -> pa.Table:
    rng = np.random.default_rng(ids.start)
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata={"lance-schema:unenforced-primary-key": "true"}), pa.field("blob", pa.binary())])
    return pa.table({"id": pa.array(list(ids), pa.int64()), "blob": pa.array([rng.bytes(width) for _ in ids], pa.binary())}, schema=schema)


def _stock(catalog: RaceCatalog) -> lance_namespace.LanceNamespace:
    return lance_namespace.connect("rest", {"uri": catalog.url})


def _opened(catalog: RaceCatalog) -> lance.LanceDataset:
    return lance.dataset(namespace_client=_stock(catalog), table_id=TABLE_ID)


def _committed_files(dataset: lance.LanceDataset) -> dict[str, str]:
    """One file of each kind the table's versions name, relative to its root, by kind: version 1's data file, written
    below the multipart threshold, and the latest version's data file (the compaction's), deletion file, index file and
    transaction file."""
    latest = dataset.get_fragments()
    deletions = [fragment.metadata.deletion_file.path(fragment.fragment_id) for fragment in latest if fragment.metadata.deletion_file is not None]
    transaction = dataset.read_transaction(dataset.version)
    assert transaction is not None
    return {
        "small-data-file": f"data/{dataset.checkout_version(1).get_fragments()[0].data_files()[0].path}",
        "large-data-file": f"data/{latest[0].data_files()[0].path}",
        "deletion-file": deletions[0],
        "transaction-file": f"_transactions/{transaction.read_version}-{transaction.uuid}.txn",
    }


def _vended(catalog: RaceCatalog) -> StorageOptions:
    """The write credential ``DescribeTable`` vends a stock client of the table."""
    described = httpx.post(f"{catalog.url}/v1/table/{'$'.join(TABLE_ID)}/describe", json={"vend_credentials": True}, timeout=60)
    assert described.status_code == 200, described.text
    return StorageOptions.from_flat({k: v for k, v in described.json()["storage_options"].items() if k != "expires_at_millis"})


def _refused(put: Callable[[], object]) -> int:
    try:
        put()
    except ClientError as exc:
        return int(exc.response["ResponseMetadata"]["HTTPStatusCode"])
    return 200


@pytest.mark.parametrize("race_catalog", ["s3"], indirect=True)
def test_a_vended_write_credential_cannot_put_over_any_file_a_commit_added(race_catalog: RaceCatalog) -> None:
    assert httpx.post(f"{race_catalog.url}/v1/namespace/{NAMESPACE}/create", json={}, timeout=60).status_code == 200
    stock = _stock(race_catalog)
    lance.write_dataset(_rows(range(10), 16), namespace_client=stock, table_id=TABLE_ID, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    lance.write_dataset(_rows(range(10, 10 + LARGE_ROWS), LARGE_ROW_BYTES), namespace_client=stock, table_id=TABLE_ID, mode="append")
    _opened(race_catalog).delete("id < 3")
    _opened(race_catalog).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(_rows(range(5, 15), 16))
    _opened(race_catalog).create_scalar_index("id", index_type="BTREE", name="id_idx")
    _opened(race_catalog).optimize.compact_files(target_rows_per_fragment=1_000_000)
    _opened(race_catalog).delete("id = 3")
    dataset = _opened(race_catalog)
    expected = dataset.count_rows()
    assert expected == 10 + LARGE_ROWS - 4

    vended = _vended(race_catalog)
    s3 = object_client(vended)
    bucket, _, root = dataset.uri.removeprefix("s3://").partition("/")
    files = _committed_files(dataset)
    index_dir = f"_indices/{dataset.describe_indices()[0].segments[0].uuid}/"
    files["index-file"] = s3.list_objects_v2(Bucket=bucket, Prefix=f"{root}/{index_dir}")["Contents"][0]["Key"].removeprefix(f"{root}/")
    sizes = {kind: s3.head_object(Bucket=bucket, Key=f"{root}/{files[kind]}")["ContentLength"] for kind in ("small-data-file", "large-data-file")}
    assert sizes["small-data-file"] < 5 * 1024 * 1024 < sizes["large-data-file"], sizes
    own = f"{root}/data/new-by-the-writer.lance"
    assert _refused(lambda: s3.put_object(Bucket=bucket, Key=own, Body=b"new")) == 200, "the credential still writes a new file"

    answered: dict[str, dict[str, int]] = {}
    for kind, path in files.items():
        key = f"{root}/{path}"
        answered[kind] = {
            "put": _refused(lambda key=key: s3.put_object(Bucket=bucket, Key=key, Body=b"corrupt")),
            "multipart": _refused(lambda key=key: s3.create_multipart_upload(Bucket=bucket, Key=key)),
            "copy": _refused(lambda key=key: s3.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": own})),
            "untag": _refused(lambda key=key: s3.delete_object_tagging(Bucket=bucket, Key=key)),
        }
    assert answered == {kind: {"put": 403, "multipart": 403, "copy": 403, "untag": 403} for kind in files}

    reread = lance.dataset(dataset.uri, storage_options=vended.in_clear())
    assert reread.to_table().num_rows == expected
    assert _opened(race_catalog).to_table(columns=["id"]).num_rows == expected
