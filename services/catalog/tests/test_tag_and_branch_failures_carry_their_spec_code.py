"""A tag or branch op that fails must mint its SPEC code, not collapse to Internal 18.

the lakehouse register, row A5 (drained 2026-09-10; in git history). The Lance Namespace spec defines a code per failure and a
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
    ConcurrentModificationError,
    CreateNamespaceRequest,
    CreateTableTagRequest,
    DeleteTableBranchRequest,
    DeleteTableTagRequest,
    GetTableTagVersionRequest,
    InvalidInputError,
    InvalidTableStateError,
    ListTableTagsRequest,
    TableBranchAlreadyExistsError,
    TableBranchNotFoundError,
    TableTagAlreadyExistsError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
    UpdateTableTagRequest,
    connect,
)
from tables_by_name import open_by_name

from catalog.services import table_claims
from catalog.services.dataplane import (
    create_branch,
    create_table,
    create_tag,
    delete_branch,
    delete_tag,
    get_tag_version,
    list_branches,
    list_tags,
    update_tag,
)
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse.history_fences import HistoryWork, TableHistory
from service_kit.lakehouse.location_claims import ClaimStore


lance = pytest.importorskip("lance")

from lance_namespace_urllib3_client.models.create_table_branch_request import CreateTableBranchRequest  # noqa: E402
from lance_namespace_urllib3_client.models.list_table_branches_request import ListTableBranchesRequest  # noqa: E402


TABLE_ID = ["db", "rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64())])

#: A version no fixture reaches — the dataset below commits once, so anything past that is absent.
#: Chosen far out so a future fixture adding versions cannot silently make it valid.
MISSING_VERSION = 999_999

LIVE_TAG = "published"
LIVE_BRANCH = "staging"
ABSENT = "no-such-ref"

_Call = Callable[[object], object]


def _table(ids: list[int]) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64())}, schema=SCHEMA)


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    """A table carrying one tag and one branch, so the missing and the colliding case are both real."""
    root = str(tmp_path / "data")
    namespace = connect("dir", {"root": root})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(namespace, {}, TABLE_ID, _table([1, 2, 3]), root=root, mode="create", registry=None)
    dataset = open_by_name(namespace, {}, TABLE_ID)
    dataset.tags.create(LIVE_TAG, 1)
    dataset.create_branch(LIVE_BRANCH, None)
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``, holding its location under a uuid as every table does (ADR 0173 §6)."""
    claims = ClaimStore(control_root=str(tmp_path / "control"))
    location = table_claims.door_table(ns, claims, TABLE_ID).location
    table_claims.claim_new_table(ns, claims, location, table="db$rows", segments=TABLE_ID)
    return table_claims.door_table(ns, claims, TABLE_ID)


@pytest.mark.parametrize(
    ("door", "call"),
    [
        ("tags/update", lambda table: update_tag(table, {}, UpdateTableTagRequest(id=TABLE_ID, tag=ABSENT, version=1))),
        ("tags/delete", lambda table: delete_tag(table, {}, DeleteTableTagRequest(id=TABLE_ID, tag=ABSENT))),
        ("tags/version", lambda table: get_tag_version(table, {}, GetTableTagVersionRequest(id=TABLE_ID, tag=ABSENT))),
    ],
)
def test_a_missing_tag_mints_TAG_NOT_FOUND(table: DoorTable, door: str, call: _Call) -> None:
    """Spec code 8. `tags/version` rides along as the regression guard — it answered 8 before A5."""
    with pytest.raises(TableTagNotFoundError) as caught:
        call(table)
    assert caught.value.code == 8, f"{door} minted code {caught.value.code} for a missing tag, not 8"


@pytest.mark.parametrize(
    ("door", "call"),
    [
        # The tag here EXISTS, so only the version can be the complaint — this is the case that proves
        # the classifier does not report 8 when the ref is fine and the version is not.
        ("tags/update", lambda table: update_tag(table, {}, UpdateTableTagRequest(id=TABLE_ID, tag=LIVE_TAG, version=MISSING_VERSION))),
        ("tags/create", lambda table: create_tag(table, {}, CreateTableTagRequest(id=TABLE_ID, tag="fresh", version=MISSING_VERSION))),
        # Reaches the object store rather than the ref API, so it raises OSError where the two above
        # raise ValueError — one classifier, two exception types, which is why both are driven.
        ("branches/create", lambda table: create_branch(table, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_version=MISSING_VERSION))),
    ],
)
def test_a_missing_version_mints_VERSION_NOT_FOUND(table: DoorTable, door: str, call: _Call) -> None:
    """Spec code 11. `from_version` is the spec's own snake_case wire spelling (`spec.yaml:4449`);
    `namespace.md`'s `fromVersion` is the Java generator's client-side naming, not the wire contract."""
    with pytest.raises(TableVersionNotFoundError) as caught:
        call(table)
    assert caught.value.code == 11, f"{door} minted code {caught.value.code} for a missing version, not 11"


@pytest.mark.parametrize(
    ("door", "call"),
    [
        # pylance validates the ref NAME before it looks anything up, and enforces five distinct rules.
        # One representative per rule, across both refs, because the wording differs per ref ("Branch
        # segment ... contains invalid characters" vs "Ref characters must be either alphanumeric") and
        # only the shared `Ref is invalid:` prefix is matched.
        # BACKEND-DEPENDENT, and the estate takes the other path — measured 2026-09-07. On this `dir`
        # dataset the ref-name validator runs first and says `Ref is invalid:`. On the DEPLOYED
        # S3-backed catalog the identical call reaches Lance's clone path first and dies with the
        # internal-error panic instead, which is the same text a collision produces, so a malformed
        # BRANCH name still answers 18 there. The tag cases below are correct on both backends. Kept
        # green here because the mapping is right and does fire wherever the validator runs; the
        # residual is upstream (an invalid name must not panic) and is recorded in §A5.
        ("branch, invalid character", lambda table: create_branch(table, {}, CreateTableBranchRequest(id=TABLE_ID, name="a b"))),
        ("tag, .lock suffix", lambda table: create_tag(table, {}, CreateTableTagRequest(id=TABLE_ID, tag="x.lock", version=1))),
    ],
)
def test_a_malformed_ref_name_mints_INVALID_INPUT(table: DoorTable, door: str, call: _Call) -> None:
    """Spec code 13 — a malformed parameter is the caller's to fix (400), never a server fault (500)."""
    with pytest.raises(InvalidInputError) as caught:
        call(table)
    assert caught.value.code == 13, f"{door} minted code {caught.value.code} for a malformed name, not 13"


def test_creating_a_tag_that_already_exists_mints_TAG_ALREADY_EXISTS(table: DoorTable) -> None:
    """Spec code 9 — a name collision the caller can fix, not a server fault."""
    with pytest.raises(TableTagAlreadyExistsError) as caught:
        create_tag(table, {}, CreateTableTagRequest(id=TABLE_ID, tag=LIVE_TAG, version=1))
    assert caught.value.code == 9, f"a tag collision minted code {caught.value.code}, not 9"


@pytest.mark.parametrize(
    ("door", "call"),
    [
        ("branches/delete", lambda table: delete_branch(table, {}, DeleteTableBranchRequest(id=TABLE_ID, name=ABSENT))),
        # THE SOURCE branch, not the one being created. With a `from_version`, pylance renders a missing source branch
        # as a missing VERSION (11), so the door reads the branch list before it creates; without one, the same read
        # answers first.
        (
            "branches/create from a missing source",
            lambda table: create_branch(table, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_branch=ABSENT, from_version=1)),
        ),
        # [[LH-406]] pylance resolves `(branch, version)` as one reference and reports a missing branch as a missing version.
        ("tags/create on a missing branch", lambda table: create_tag(table, {}, CreateTableTagRequest(id=TABLE_ID, tag="fresh", version=1, branch=ABSENT))),
        ("tags/update on a missing branch", lambda table: update_tag(table, {}, UpdateTableTagRequest(id=TABLE_ID, tag=LIVE_TAG, version=1, branch=ABSENT))),
    ],
)
def test_a_missing_branch_mints_BRANCH_NOT_FOUND(table: DoorTable, door: str, call: _Call) -> None:
    """Spec code 22 — added to the spec WITH the branch ops, and unreachable until now."""
    with pytest.raises(TableBranchNotFoundError) as caught:
        call(table)
    assert caught.value.code == 22, f"{door} minted code {caught.value.code} for a missing branch, not 22"


def test_a_branch_a_tag_names_is_refused_INVALID_TABLE_STATE_naming_the_tag(table: DoorTable) -> None:
    """Spec code 19, naming the tag, and nothing deleted: a tag pins its branch ("Ref conflict error: Branch staging is
    referenced by tags", measured on pylance 13.0.0), which the door asks before its first delete."""
    table.open_unchecked({}).tags.create("pin", (LIVE_BRANCH, None))

    with pytest.raises(InvalidTableStateError, match="tag 'pin'") as caught:
        delete_branch(table, {}, DeleteTableBranchRequest(id=TABLE_ID, name=LIVE_BRANCH))

    assert caught.value.code == 19
    assert LIVE_BRANCH in table.open_unchecked({}).branches.list()


def test_a_branch_cut_from_main_by_name_is_created(table: DoorTable) -> None:
    """`main` is a ref every table has and no branch list names, so a source spelled `main` is main itself."""
    create_branch(table, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_branch="main"))

    assert table.open_unchecked({}).branches.list()["fresh"].get("parent_branch") is None


@pytest.mark.parametrize(
    ("listing", "names"),
    [
        pytest.param(lambda table, token: list_tags(table, {}, ListTableTagsRequest(id=TABLE_ID, limit=1, page_token=token)), ["a", "b", LIVE_TAG], id="tags"),
        pytest.param(
            lambda table, token: list_branches(table, {}, ListTableBranchesRequest(id=TABLE_ID, limit=1, page_token=token)),
            ["a", "b", LIVE_BRANCH],
            id="branches",
        ),
    ],
)
def test_a_ref_listing_pages_by_its_limit(table: DoorTable, listing: Callable[[DoorTable, str | None], object], names: list[str]) -> None:
    """``limit`` bounds the page and ``page_token`` resumes after it, so a caller paging one at a time reads every ref once."""
    dataset = table.open_unchecked({})
    for name in ("a", "b"):
        dataset.tags.create(name, 1)
        dataset.create_branch(name, None)

    seen: list[str] = []
    token: str | None = None
    while True:
        page = listing(table, token)
        refs = getattr(page, "tags", None) or getattr(page, "branches", None) or {}
        assert len(refs) <= 1, refs
        seen.extend(refs)
        token = getattr(page, "page_token", None)
        if not token:
            break

    assert seen == names


def test_a_version_error_names_the_SOURCE_branch_not_the_one_being_created(table: DoorTable) -> None:
    """The detail a caller reads must name what was missing.

    Branching from a real branch at a version it does not have is correctly 11 — but the first fix
    rendered it as "no such version for branch '<the NEW name>'", naming a branch that exists nowhere
    yet and telling the caller nothing about which history lacked the version.
    """
    with pytest.raises(TableVersionNotFoundError) as caught:
        create_branch(table, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh", from_branch=LIVE_BRANCH, from_version=MISSING_VERSION))
    assert LIVE_BRANCH in str(caught.value), f"the version error named the wrong branch: {caught.value}"


def test_creating_a_branch_that_already_exists_mints_BRANCH_ALREADY_EXISTS(table: DoorTable) -> None:
    """Spec code 23.

    The one case that CANNOT be read off the exception: pylance answers a branch collision with
    ``OSError("Encountered internal error. Please file a bug report ... Clone operation should not
    enter build_manifest.")`` — its own bug-report text, which is neither a stable discriminator nor
    an honest thing to pattern-match. The door establishes the collision by READING instead.
    """
    with pytest.raises(TableBranchAlreadyExistsError) as caught:
        create_branch(table, {}, CreateTableBranchRequest(id=TABLE_ID, name=LIVE_BRANCH))
    assert caught.value.code == 23, f"a branch collision minted code {caught.value.code}, not 23"


@pytest.mark.parametrize(
    ("door", "call"),
    [
        ("tags/create", lambda table: create_tag(table, {}, CreateTableTagRequest(id=TABLE_ID, tag="fresh", version=1))),
        ("tags/update", lambda table: update_tag(table, {}, UpdateTableTagRequest(id=TABLE_ID, tag=LIVE_TAG, version=1))),
        ("branches/create", lambda table: create_branch(table, {}, CreateTableBranchRequest(id=TABLE_ID, name="fresh"))),
    ],
)
def test_a_ref_write_while_a_reclaim_holds_the_tables_history_mints_CONCURRENT_MODIFICATION(table: DoorTable, tmp_path: Path, door: str, call: _Call) -> None:
    """Spec code 14, and no ref written: Lance's reclaim reads the refs once, so a ref written while it runs can name a
    version it then deletes ([[LH-308]]). The caller retries once the reclaim ends."""
    dataset = table.open_unchecked({})
    refs = (dataset.tags.list(), dataset.branches.list())
    TableHistory(control_root=str(tmp_path / "control"), storage_options={}, table_uuid=table.resolved.require_uuid()).take(HistoryWork.RECLAIM)

    with pytest.raises(ConcurrentModificationError) as caught:
        call(table)

    assert caught.value.code == 14, f"{door} minted code {caught.value.code} while a reclaim held the history, not 14"
    reopened = table.open_unchecked({})
    assert (reopened.tags.list(), reopened.branches.list()) == refs, f"{door} wrote a ref while a reclaim held the history"
