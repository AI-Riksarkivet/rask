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

import os
import struct
import threading
from collections.abc import Callable
from functools import partial
from typing import Any

import boto3
import httpx
import lance
import lance_namespace
import numpy as np
import pyarrow as pa
import pytest
from botocore.exceptions import ClientError

from lakehouse import manifest_wire
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


def _vended(catalog: RaceCatalog, table_id: list[str] = TABLE_ID) -> StorageOptions:
    """The write credential ``DescribeTable`` vends a stock client of the table."""
    described = httpx.post(f"{catalog.url}/v1/table/{'$'.join(table_id)}/describe", json={"vend_credentials": True}, timeout=60)
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


def _root_client() -> Any:  # noqa: ANN401 — boto3's client
    """The lane's root identity on the store, which plays what no vend can: a file never put, and the reads of the proof."""
    return boto3.client(
        "s3",
        endpoint_url=os.environ["RASK_LIFECYCLE_STORE_ENDPOINT"],
        region_name="us-east-1",
        aws_access_key_id=os.environ["RASK_LIFECYCLE_STORE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["RASK_LIFECYCLE_STORE_SECRET_KEY"],
    )


class _HookedCommits(lance_namespace.LanceNamespace):
    """pylance's REST namespace, with ``before`` called with each commit's request before it reaches the catalog: what a
    writer holding its staged files may do between staging a commit and asking for it."""

    def __init__(self, inner: lance_namespace.LanceNamespace, before: Callable[[lance_namespace.CreateTableVersionRequest], None]) -> None:
        self._inner, self._before = inner, before
        #: What the catalog answered each commit it refused; pylance reports a refused commit as of unknown outcome.
        self.refused: list[str] = []

    def namespace_id(self) -> str:
        return self._inner.namespace_id()

    def describe_table(self, request: lance_namespace.DescribeTableRequest) -> lance_namespace.DescribeTableResponse:
        return self._inner.describe_table(request)

    def list_table_versions(self, request: lance_namespace.ListTableVersionsRequest) -> lance_namespace.ListTableVersionsResponse:
        return self._inner.list_table_versions(request)

    def describe_table_version(self, request: lance_namespace.DescribeTableVersionRequest) -> lance_namespace.DescribeTableVersionResponse:
        return self._inner.describe_table_version(request)

    def create_table_version(self, request: lance_namespace.CreateTableVersionRequest) -> lance_namespace.CreateTableVersionResponse:
        self._before(request)
        try:
            return self._inner.create_table_version(request)
        except Exception as exc:
            self.refused.append(str(exc))
            raise


def _created(catalog: RaceCatalog, table_id: list[str], *, stable_row_ids: bool = True) -> lance.LanceDataset:
    namespace = "$".join(table_id[:-1])
    assert httpx.post(f"{catalog.url}/v1/namespace/{namespace}/create", json={}, timeout=60).status_code == 200
    lance.write_dataset(
        _rows(range(10), 16),
        namespace_client=_stock(catalog),
        table_id=table_id,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=stable_row_ids,
    )
    return lance.dataset(namespace_client=_stock(catalog), table_id=table_id)


@pytest.mark.parametrize("race_catalog", ["s3"], indirect=True)
@pytest.mark.parametrize("never_put", ["_deletions", "_transactions"])
def test_a_commit_naming_a_file_the_store_does_not_hold_is_refused(race_catalog: RaceCatalog, never_put: str) -> None:
    """ADR 0242 B1: a commit naming a file the store does not hold is refused 400 (InvalidInput), so no key a committed
    version names is left for a writer to put later, unsealed and unrecorded, and the table stays at its version. Played by
    a stock delete whose new deletion file, or whose staged transaction file, is gone from the store when its commit reaches
    ``version/create`` (the root identity removes it, as a writer that never put it leaves it). Before the fix the
    transaction file's commit landed (its manifest carries the transaction inline, which pylance 13.0.0 reads), the key
    left for the writer to put; the deletion file's answered 500 from the rule that reads the rows it deletes."""
    table_id = ["lance_catalog", f"unheld{never_put}", "t"]
    dataset = _created(race_catalog, table_id)
    bucket, _, root = dataset.uri.removeprefix("s3://").partition("/")
    store = _root_client()
    held = {entry["Key"] for entry in store.list_objects_v2(Bucket=bucket, Prefix=f"{root}/{never_put}/").get("Contents", [])}

    def remove_what_the_commit_staged(_request: lance_namespace.CreateTableVersionRequest) -> None:
        staged = [entry["Key"] for entry in store.list_objects_v2(Bucket=bucket, Prefix=f"{root}/{never_put}/").get("Contents", []) if entry["Key"] not in held]
        assert staged, f"the delete staged a file under {never_put}/"
        for key in staged:
            store.delete_object(Bucket=bucket, Key=key)

    hooked = _HookedCommits(_stock(race_catalog), remove_what_the_commit_staged)
    with pytest.raises(OSError, match="unknown"):
        lance.dataset(namespace_client=hooked, table_id=table_id).delete("id < 3")

    assert len(hooked.refused) == 1 and "Invalid input" in hooked.refused[0], hooked.refused
    assert "does not hold" in hooked.refused[0] and f"{never_put}/" in hooked.refused[0], hooked.refused
    after = lance.dataset(namespace_client=_stock(race_catalog), table_id=table_id)
    assert (after.version, after.count_rows()) == (1, 10)


def _without_inline_transaction(manifest: bytes) -> bytes:
    """``manifest`` with its ``transaction_section`` fields (21 and 23) removed, every other byte kept: a manifest Lance
    reads its version's transaction for from the file the manifest names (``lance_docs/lance/format/table/transaction.md``
    § Inline Transaction Sections)."""
    footer = struct.Struct("<QHH4s")
    offset, major, minor, magic = footer.unpack(manifest[-footer.size :])
    (length,) = struct.unpack_from("<I", manifest, offset)
    message = manifest[offset + 4 : offset + 4 + length]
    kept, at = bytearray(), 0
    while at < len(message):
        start = at
        number, _, at = manifest_wire.next_field(message, at)
        if number not in (21, 23):
            kept += message[start:at]
    return manifest[:offset] + struct.pack("<I", len(kept)) + bytes(kept) + footer.pack(offset, major, minor, magic)


@pytest.mark.parametrize("race_catalog", ["s3"], indirect=True)
@pytest.mark.parametrize("inline", [True, False], ids=["stock-manifest", "manifest-without-its-inline-transaction"])
def test_the_transaction_judged_is_the_one_committed(race_catalog: RaceCatalog, monkeypatch: pytest.MonkeyPatch, inline: bool) -> None:
    """ADR 0242 B2: the transaction a commit is judged on is the one it commits and announces. A writer stages an append
    and puts another commit's transaction over its ``.txn`` file while the catalog judges the commit (right before it
    reads the files the version adds). A stock manifest carries its transaction inline, which the catalog reads from its
    own copy of the manifest and finalizes: the version lands carrying the transaction the writer staged, and once it has
    landed the ``.txn`` file is sealed (a put over it 403). A manifest without its inline transaction, whose transaction
    Lance would read from that file, is refused 400 and the table stays at its version; before the fix it was admitted,
    and version 2, judged as an Append, landed carrying the earlier Overwrite transaction."""
    table_id = ["lance_catalog", f"judged{int(inline)}", "t"]
    dataset = _created(race_catalog, table_id)
    bucket, _, root = dataset.uri.removeprefix("s3://").partition("/")
    store, writer = _root_client(), object_client(_vended(race_catalog, table_id))
    other = dataset.read_transaction(1)
    assert other is not None
    other_bytes = store.get_object(Bucket=bucket, Key=f"{root}/_transactions/{other.read_version}-{other.uuid}.txn")["Body"].read()
    staged: dict[str, str] = {}

    def read_and_maybe_strip(request: lance_namespace.CreateTableVersionRequest) -> None:
        key = str(request.model_dump()["manifest_path"]).removeprefix(f"{bucket}/")
        manifest = store.get_object(Bucket=bucket, Key=key)["Body"].read()
        message = _manifest_message(manifest)
        staged["txn"] = next(value.decode() for number, value in manifest_wire.fields(message) if number == 12 and isinstance(value, bytes))
        if not inline:
            writer.put_object(Bucket=bucket, Key=key, Body=_without_inline_transaction(manifest))

    from catalog.services import committed_files

    judged = committed_files.added

    def put_over_the_transaction_while_judged(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the judge's own call
        assert _refused(lambda: writer.put_object(Bucket=bucket, Key=f"{root}/_transactions/{staged['txn']}", Body=other_bytes)) == 200
        return judged(*args, **kwargs)

    monkeypatch.setattr(committed_files, "added", put_over_the_transaction_while_judged)
    hooked = _HookedCommits(_stock(race_catalog), read_and_maybe_strip)
    append = partial(lance.write_dataset, _rows(range(10, 20), 16), namespace_client=hooked, table_id=table_id, mode="append")
    if not inline:
        with pytest.raises(OSError, match="unknown"):
            append()
        assert len(hooked.refused) == 1 and "carries no inline transaction" in hooked.refused[0], hooked.refused
        after = lance.dataset(namespace_client=_stock(race_catalog), table_id=table_id)
        assert (after.version, after.count_rows()) == (1, 10)
        return
    append()

    landed = lance.dataset(dataset.uri, version=2, storage_options=_vended(race_catalog, table_id).in_clear())
    transaction = landed.read_transaction(2)
    assert transaction is not None and f"{transaction.read_version}-{transaction.uuid}.txn" == staged["txn"]
    assert type(transaction.operation).__name__ == "Append" and landed.count_rows() == 20
    assert _refused(lambda: writer.put_object(Bucket=bucket, Key=f"{root}/_transactions/{staged['txn']}", Body=b"corrupt")) == 403


#: Lance's ``MAX_INLINE_TRANSACTION_BYTES``, and how far either side of it the two commits below hold.
_INLINE_LIMIT = 20 * 1024 * 1024
_MARGIN = 4096


@pytest.mark.parametrize("past", [False, True], ids=["inside-what-lance-inlines", "past-what-lance-inlines"])
def test_a_stock_commit_past_what_lance_inlines_is_refused_naming_its_size(race_catalog: RaceCatalog, past: bool) -> None:
    """A stock commit whose transaction Lance does not inline is refused 400 (InvalidInput) by ``version/create``, which
    judges a commit only on the transaction its manifest carries (ADR 0242 B2): a stated limit of the stock client's path,
    which the refusal names (the transaction's size, the 20 MiB Lance inlines, and to commit in smaller steps). Both sides
    of the limit are measured on the installed pylance, so a Lance that moves its threshold fails here: an append whose
    transaction properties hold 4 KiB less than 20 MiB stages its transaction inline and lands; one holding 4 KiB more
    stages a manifest with no inline section and is refused, the table left at its version. Before, the refusal named
    neither the size nor the limit."""
    table_id = ["lance_catalog", f"inline{int(past)}", "t"]
    assert httpx.post(f"{race_catalog.url}/v1/namespace/{'$'.join(table_id[:-1])}/create", json={}, timeout=60).status_code == 200
    stock = _stock(race_catalog)
    lance.write_dataset(_rows(range(10), 16), namespace_client=stock, table_id=table_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    hooked = _HookedCommits(stock, lambda _request: None)
    padding: dict[str, str] = {"padding": "x" * (_INLINE_LIMIT + (_MARGIN if past else -_MARGIN))}
    append = partial(lance.write_dataset, _rows(range(10, 20), 16), namespace_client=hooked, table_id=table_id, mode="append", transaction_properties=padding)

    if past:
        with pytest.raises(OSError, match="unknown"):
            append()
        assert len(hooked.refused) == 1, hooked.refused
        refusal = hooked.refused[0]
        assert "Invalid input" in refusal and "carries no inline transaction" in refusal, f"Lance moved its inline threshold: {refusal[:300]}"
        assert f"{_INLINE_LIMIT:,} bytes" in refusal and "transaction is 20," in refusal and "Commit in smaller steps" in refusal, refusal[:600]
    else:
        append()
        assert hooked.refused == [], f"Lance moved its inline threshold: {[refused[:300] for refused in hooked.refused]}"
    after = lance.dataset(namespace_client=_stock(race_catalog), table_id=table_id)
    assert (after.version, after.count_rows()) == ((1, 10) if past else (2, 20))


def _manifest_message(manifest: bytes) -> bytes:
    footer = struct.Struct("<QHH4s")
    offset = footer.unpack(manifest[-footer.size :])[0]
    (length,) = struct.unpack_from("<I", manifest, offset)
    return manifest[offset + 4 : offset + 4 + length]


#: The longest branch name a write credential's session policy fits within the 2,048 characters STS takes for this lane's
#: table (a 17-character bucket, a 36-character table prefix, the table's root the one base a branch's manifest declares,
#: no KMS key), counted as the store counts it (``vending.counted_length``): 97 with the seal's Deny in the store
#: identity's policy alone, 69 with it repeated in the session policy. Counted on the compact text alone the catalog
#: admitted 99, which the store refused (503).
_LONGEST_BRANCH = 97


@pytest.mark.parametrize("race_catalog", ["s3"], indirect=True)
def test_a_write_credential_fits_a_long_branch_name_and_a_longer_one_is_named(race_catalog: RaceCatalog) -> None:
    """ADR 0242 B3: the seal's Deny lives in the store identity's policy alone, so a write credential for a branch fits as
    long a branch name as before the seal, and is still refused a put over a sealed file; a name past what fits is
    refused 400 (InvalidInput) naming each part's length, never 500. The session the store's STS issues under the
    policy the catalog writes proves the policy fits: STS refuses one past its limit."""
    table_id = ["lance_catalog", "branched", "t"]
    dataset = _created(race_catalog, table_id)
    bucket, _, root = dataset.uri.removeprefix("s3://").partition("/")
    name = "$".join(table_id)
    sealed = f"{root}/data/{dataset.get_fragments()[0].data_files()[0].path}"
    answered: dict[int, httpx.Response] = {}
    for length in (_LONGEST_BRANCH, _LONGEST_BRANCH + 1):
        branch = "b" * length
        created = httpx.post(f"{race_catalog.url}/v1/table/{name}/branches/create", json={"name": branch}, timeout=60)
        assert created.status_code == 200, created.text
        answered[length] = httpx.post(f"{race_catalog.url}/management/v1/table/{name}/credentials", params={"tier": "write", "branch": branch}, timeout=60)

    fits, past = answered[_LONGEST_BRANCH], answered[_LONGEST_BRANCH + 1]
    assert fits.status_code == 200, fits.text
    options = StorageOptions.from_flat({k: v for k, v in fits.json()["credentials"]["storage_options"].items() if k != "expires_at_millis"})
    assert _refused(lambda: object_client(options).put_object(Bucket=bucket, Key=sealed, Body=b"corrupt")) == 403
    problem = past.json()
    assert (past.status_code, problem["code"], problem["title"]) == (400, 13, "SessionPolicyTooLong"), past.text
    assert problem["session_policy_names"]["branch"] == _LONGEST_BRANCH + 1 and "branch" in problem["detail"]


class _RefusesTagging:
    """The catalog's own object client on a store or role that grants no ``s3:PutObjectTagging``: every seal answered 403
    AccessDenied, every other call the real store's."""

    def __init__(self, inner: Any) -> None:  # noqa: ANN401 — boto3's client
        self._inner = inner

    def put_object_tagging(self, **_kw: Any) -> None:
        raise ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"RequestId": "", "HostId": "", "HTTPStatusCode": 403, "HTTPHeaders": {}, "RetryAttempts": 0},
            },
            "PutObjectTagging",
        )

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — the inner client's attribute
        return getattr(self._inner, name)


def _unkeyed_rows(ids: range) -> bytes:
    from lakehouse.arrow_ipc import encode_arrow_stream

    return encode_arrow_stream(pa.table({"id": pa.array(list(ids), pa.int64())}))


@pytest.mark.parametrize("race_catalog", ["s3"], indirect=True)
@pytest.mark.parametrize("writer", ["stock", "insert", "keyed-insert", "keyed-create-overwrite"])
@pytest.mark.parametrize("detection_only", [False, True], ids=["seal-required", "git-declares-detection-only"])
def test_a_seal_the_store_refuses_fails_the_commit_unless_git_declares_detection_only(
    race_catalog: RaceCatalog, monkeypatch: pytest.MonkeyPatch, detection_only: bool, writer: str
) -> None:
    """ADR 0242 B4: where the seal is required, a commit whose seal the store refuses fails before its version lands,
    answered 503 (ServiceUnavailable, code 17) for its writer to retry once the store's operator grants the tagging; never a
    landed version whose files no seal guards. Where git declares the warehouse's bucket detection-only
    (``LANCE_SEAL_DETECTION_ONLY_BUCKETS``), the commit lands with its files untagged and recorded, so maintenance's pass
    still checks them. The same at every path that adds files, through the one call that seals and records them
    (``committed_files.seal_and_record``, ADR 0242 § Found by review, second round): a stock client's commit, and an
    ``/insert`` and a ``/create`` mode=Overwrite Lance commits itself, with an Idempotency-Key and without. Before that call
    was the only one, a keyed append answered 500 on a detection-only warehouse and on a required one alike, its files left
    behind, and an unkeyed append on a required one LANDED with 200, sealed only after its version, which then failed."""
    from catalog.core.config import get_settings
    from lakehouse import stored_objects
    from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE

    table_id = ["lance_catalog", "untagged", "t"]
    table = f"{race_catalog.url}/v1/table/{'$'.join(table_id)}"
    if writer == "stock":
        dataset = _created(race_catalog, table_id)
    else:
        assert httpx.post(f"{race_catalog.url}/v1/namespace/lance_catalog$untagged/create", json={}, timeout=60).status_code == 200
        made = httpx.post(f"{table}/create", content=_unkeyed_rows(range(10)), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE}, timeout=60)
        assert made.status_code == 200, made.text
        dataset = lance.dataset(namespace_client=_stock(race_catalog), table_id=table_id)
    bucket, _, root = dataset.uri.removeprefix("s3://").partition("/")
    if detection_only:
        monkeypatch.setenv("LANCE_SEAL_DETECTION_ONLY_BUCKETS", bucket)
        get_settings.cache_clear()
    real = stored_objects.object_client
    monkeypatch.setattr(stored_objects, "object_client", lambda *a, **k: _RefusesTagging(real(*a, **k)))
    hooked = _HookedCommits(_stock(race_catalog), lambda _request: None)

    def write() -> httpx.Response | None:
        if writer == "stock":
            lance.write_dataset(_rows(range(10, 20), 16), namespace_client=hooked, table_id=table_id, mode="append")
            return None
        headers = {
            "Content-Type": ARROW_STREAM_MEDIA_TYPE,
            **({"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f6a"} if writer.startswith("keyed") else {}),
        }
        if writer == "keyed-create-overwrite":
            return httpx.post(f"{table}/create?mode=Overwrite", content=_unkeyed_rows(range(20)), headers=headers, timeout=60)
        return httpx.post(f"{table}/insert?mode=append", content=_unkeyed_rows(range(10, 20)), headers=headers, timeout=60)

    if detection_only:
        answered = write()
        assert answered is None or answered.status_code == 200, answered.text
    elif writer == "stock":
        with pytest.raises(OSError):  # noqa: PT011 — pylance reports the refused commit as of unknown outcome
            write()
    else:
        answered = write()
        assert answered is not None
        assert (answered.status_code, answered.json().get("code")) == (503, 17), answered.text

    after = lance.dataset(namespace_client=_stock(race_catalog), table_id=table_id)
    if detection_only:
        added = after.get_fragments()[-1].data_files()[0].path
        tags = _root_client().get_object_tagging(Bucket=bucket, Key=f"{root}/data/{added}")["TagSet"]
        assert (after.version, after.count_rows(), tags, hooked.refused) == (2, 20, [], [])
    else:
        assert writer != "stock" or (hooked.refused and all("unavailable" in refusal.lower() for refusal in hooked.refused)), hooked.refused
        assert (after.version, after.count_rows()) == (1, 10)


@pytest.mark.parametrize("race_catalog", ["s3"], indirect=True)
def test_stock_writers_racing_for_one_table_each_land_their_commit(race_catalog: RaceCatalog) -> None:
    """ADR 0242: a stock writer that loses a race puts its transaction file again, under the same name, and commits on the
    next version (measured on pylance 13.0.0), so the catalog seals that file only once the version lands. Four writers
    appending four times each to one table all land; with the file sealed before the finalize, as the seal's first
    version did, a writer's retry was refused 403 at that put and its commit failed (8 of 16 appends measured)."""
    table_id = ["lance_catalog", "racing", "t"]
    _created(race_catalog, table_id)
    failed: list[BaseException] = []

    def append(writer: int) -> None:
        for attempt in range(4):
            first = 1000 + 100 * writer + 10 * attempt
            try:
                lance.write_dataset(_rows(range(first, first + 10), 16), namespace_client=_stock(race_catalog), table_id=table_id, mode="append")
            except BaseException as exc:  # noqa: BLE001 — every append's outcome is the claim
                failed.append(exc)

    writers = [threading.Thread(target=append, args=(writer,)) for writer in range(4)]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()

    assert failed == [] and lance.dataset(namespace_client=_stock(race_catalog), table_id=table_id).count_rows() == 10 + 160, [str(e)[:200] for e in failed]
