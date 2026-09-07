"""A tag or branch op that fails must mint its SPEC code, not collapse to Internal 18.

`open_lakehouse_diff_left.md` § A5. The Lance Namespace spec defines a code per failure and a
generated client dispatches on that code, never on the HTTP status. Six of the catalog's tag and
branch failures answered **500 / code 18** instead, which tells a client "the server broke" for what
is really "your tag already exists" — unretryable, unactionable, and indistinguishable from a genuine
fault in an alerting pipeline.

MEASURED against the deployed catalog (k3s `rask-catalog:2333`, 2026-09-07), driving the doors rather
than reading them::

    POST /v1/table/{id}/tags/delete     tag missing      500 code 18   spec says 8
    POST /v1/table/{id}/tags/update     tag missing      500 code 18   spec says 8
    POST /v1/table/{id}/tags/create     tag exists       500 code 18   spec says 9
    POST /v1/table/{id}/tags/create     version missing  500 code 18   spec says 11
    POST /v1/table/{id}/branches/delete branch missing   500 code 18   spec says 22
    POST /v1/table/{id}/branches/create branch exists    500 code 18   spec says 23

`tags/version` was the ONE door that translated (`dataplane.get_tag_version` catches `ValueError`),
which is why the gap read as absent rather than systematic: the only tag op anyone had driven by hand
was the one already fixed. It is parametrized in beside the doors it should match, so a later
"simplification" that routes it through the shared helper has to keep answering 8.

THE CAUSE is that pylance raises bare `ValueError`/`OSError` and `install_problem_handlers` maps only
the `lance_namespace` typed hierarchy — everything else falls through to `Internal`. The status map
itself was never wrong: every code demanded here is already in `_STATUS`, and every typed class
already exists carrying the right `.code`. What was missing is translation where the raw exception is
raised.

Driven against a real `dir` namespace and real pylance calls, not a mock: the subject IS which
exception the library raises, so a double would only assert that this file and the fix agree with
each other. The assertions name the exception CLASS rather than a message, because the class carries
`.code` and the code is the contract.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import (
    CreateTableTagRequest,
    DeleteTableBranchRequest,
    DeleteTableTagRequest,
    GetTableTagVersionRequest,
    InvalidInputError,
    TableBranchAlreadyExistsError,
    TableBranchNotFoundError,
    TableTagAlreadyExistsError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
    UpdateTableTagRequest,
    connect,
)

from catalog.services.dataplane import (
    create_branch,
    create_table,
    create_tag,
    delete_branch,
    delete_tag,
    get_tag_version,
    open_dataset,
    update_tag,
)


lance = pytest.importorskip("lance")

from lance_namespace_urllib3_client.models.create_table_branch_request import CreateTableBranchRequest  # noqa: E402


TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64())])

#: A version no fixture reaches — the dataset below commits once, so anything past that is absent.
#: Chosen far out so a future fixture adding versions cannot silently make it valid.
MISSING_VERSION = 999_999

LIVE_TAG = "published"
LIVE_BRANCH = "staging"
ABSENT = "no-such-ref"

_Call = Callable[[object], object]


def _ipc(ids: list[int]) -> bytes:
    table = pa.table({"id": pa.array(ids, pa.int64())}, schema=SCHEMA)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    """A table carrying one tag and one branch, so the missing and the colliding case are both real."""
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, _ipc([1, 2, 3]), mode="create")
    dataset = open_dataset(namespace, {}, TABLE_ID)
    dataset.tags.create(LIVE_TAG, 1)
    dataset.create_branch(LIVE_BRANCH, None)
    return namespace


@pytest.mark.parametrize(
    ("door", "call"),
    [
        ("tags/update", lambda ns: update_tag(ns, {}, UpdateTableTagRequest(id=TABLE_ID, tag=ABSENT, version=1))),
        ("tags/delete", lambda ns: delete_tag(ns, {}, DeleteTableTagRequest(id=TABLE_ID, tag=ABSENT))),
        ("tags/version", lambda ns: get_tag_version(ns, {}, GetTableTagVersionRequest(id=TABLE_ID, tag=ABSENT))),
    ],
)
def test_a_missing_tag_mints_TAG_NOT_FOUND(ns, door: str, call: _Call) -> None:  # noqa: ANN001
    """Spec code 8. `tags/version` rides along as the regression guard — it answered 8 before A5."""
    with pytest.raises(TableTagNotFoundError) as caught:
        call(ns)
    assert caught.value.code == 8, f"{door} minted code {caught.value.code} for a missing tag, not 8"


@pytest.mark.parametrize(
    ("door", "call"),
    [
        # The tag here EXISTS, so only the version can be the complaint — this is the case that proves
        # the classifier does not report 8 when the ref is fine and the version is not.
        ("tags/update", lambda ns: update_tag(ns, {}, UpdateTableTagRequest(id=TABLE_ID, tag=LIVE_TAG, version=MISSING_VERSION))),
        ("tags/create", lambda ns: create_tag(ns, {}, CreateTableTagRequest(id=TABLE_ID, tag="fresh", version=MISSING_VERSION))),
        # Reaches the object store rather than the ref API, so it raises OSError where the two above
        # raise ValueError — one classifier, two exception types, which is why both are driven.
        ("branches/create", lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_version=MISSING_VERSION))),
    ],
)
def test_a_missing_version_mints_VERSION_NOT_FOUND(ns, door: str, call: _Call) -> None:  # noqa: ANN001
    """Spec code 11. `from_version` is the spec's own snake_case wire spelling (`spec.yaml:4449`);
    `namespace.md`'s `fromVersion` is the Java generator's client-side naming, not the wire contract."""
    with pytest.raises(TableVersionNotFoundError) as caught:
        call(ns)
    assert caught.value.code == 11, f"{door} minted code {caught.value.code} for a missing version, not 11"


@pytest.mark.parametrize(
    ("door", "call"),
    [
        # pylance validates the ref NAME before it looks anything up, and enforces five distinct rules.
        # One representative per rule, across both refs, because the wording differs per ref ("Branch
        # segment ... contains invalid characters" vs "Ref characters must be either alphanumeric") and
        # only the shared `Ref is invalid:` prefix is matched.
        ("branch, invalid character", lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="a b"))),
        ("branch, .lock suffix", lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="feat.lock"))),
        ("branch, leading slash", lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="/lead"))),
        ("branch, consecutive slashes", lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="a//b"))),
        ("branch, '..' inside a segment", lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="a..b"))),
        ("tag, invalid character", lambda ns: create_tag(ns, {}, CreateTableTagRequest(id=TABLE_ID, tag="a b", version=1))),
        ("tag, .lock suffix", lambda ns: create_tag(ns, {}, CreateTableTagRequest(id=TABLE_ID, tag="x.lock", version=1))),
    ],
)
def test_a_malformed_ref_name_mints_INVALID_INPUT(ns, door: str, call: _Call) -> None:  # noqa: ANN001
    """Spec code 13 — a malformed parameter is the caller's to fix (400), never a server fault (500)."""
    with pytest.raises(InvalidInputError) as caught:
        call(ns)
    assert caught.value.code == 13, f"{door} minted code {caught.value.code} for a malformed name, not 13"


def test_creating_a_tag_that_already_exists_mints_TAG_ALREADY_EXISTS(ns) -> None:  # noqa: ANN001
    """Spec code 9 — a name collision the caller can fix, not a server fault."""
    with pytest.raises(TableTagAlreadyExistsError) as caught:
        create_tag(ns, {}, CreateTableTagRequest(id=TABLE_ID, tag=LIVE_TAG, version=1))
    assert caught.value.code == 9, f"a tag collision minted code {caught.value.code}, not 9"


@pytest.mark.parametrize(
    ("door", "call"),
    [
        ("branches/delete", lambda ns: delete_branch(ns, {}, DeleteTableBranchRequest(id=TABLE_ID, name=ABSENT))),
        # THE SOURCE branch, not the one being created — and both spellings, because pylance renders
        # them differently and the first version of this fix got both wrong: with a `from_version` it
        # answered 11 (a missing VERSION, when the BRANCH is what is absent), and without one it
        # matched no marker at all and stayed Internal 18.
        (
            "branches/create from a missing source, with a version",
            lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_branch=ABSENT, from_version=1)),
        ),
        (
            "branches/create from a missing source, no version",
            lambda ns: create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_branch=ABSENT)),
        ),
    ],
)
def test_a_missing_branch_mints_BRANCH_NOT_FOUND(ns, door: str, call: _Call) -> None:  # noqa: ANN001
    """Spec code 22 — added to the spec WITH the branch ops, and unreachable until now."""
    with pytest.raises(TableBranchNotFoundError) as caught:
        call(ns)
    assert caught.value.code == 22, f"{door} minted code {caught.value.code} for a missing branch, not 22"


def test_a_version_error_names_the_SOURCE_branch_not_the_one_being_created(ns) -> None:  # noqa: ANN001
    """The detail a caller reads must name what was missing.

    Branching from a real branch at a version it does not have is correctly 11 — but the first fix
    rendered it as "no such version for branch '<the NEW name>'", naming a branch that exists nowhere
    yet and telling the caller nothing about which history lacked the version.
    """
    with pytest.raises(TableVersionNotFoundError) as caught:
        create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_branch=LIVE_BRANCH, from_version=MISSING_VERSION))
    assert LIVE_BRANCH in str(caught.value), f"the version error named the wrong branch: {caught.value}"


def test_creating_a_branch_that_already_exists_mints_BRANCH_ALREADY_EXISTS(ns) -> None:  # noqa: ANN001
    """Spec code 23.

    The one case that CANNOT be read off the exception: pylance answers a branch collision with
    ``OSError("Encountered internal error. Please file a bug report ... Clone operation should not
    enter build_manifest.")`` — its own bug-report text, which is neither a stable discriminator nor
    an honest thing to pattern-match. The door establishes the collision by READING instead.
    """
    with pytest.raises(TableBranchAlreadyExistsError) as caught:
        create_branch(ns, {}, CreateTableBranchRequest(id=TABLE_ID, name=LIVE_BRANCH))
    assert caught.value.code == 23, f"a branch collision minted code {caught.value.code}, not 23"
