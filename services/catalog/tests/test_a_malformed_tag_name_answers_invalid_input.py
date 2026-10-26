"""A malformed TAG name answers the spec's 13: the grammar's through pylance, the store's bound through the door.

[[LH-096]] asks for tag names to be "refused at the door like branch names". Measured on pylance
13.0.0, the grammar needs no door: two of the three refusals `refuse_a_branch_name_the_backend_cannot_use`
makes are BRANCH-specific.

* ``main`` is the default BRANCH. It is an ordinary tag name and creating it succeeds.
* A ``.`` segment never reaches the object-store path parser through a tag: a tag is one file,
  ``_refs/tags/<name>.json`` (`lance_docs/lance/format/table/branch_tag.md` § "Tag Storage"), and its
  grammar rejects ``/`` outright (``feature/x`` is a legal BRANCH and an illegal tag) and a leading
  dot (``.`` and ``..`` answer ``Ref is invalid: Ref cannot begin with a dot``).

An empty tag answers ``Ref is invalid: Ref cannot be empty``, so every tag the grammar refuses carries
`_classify_ref_error`'s ``Ref is invalid:`` marker and maps to InvalidInput without help. A grammar
guard here would do what the branch guard's own docstring warns against: keep a second copy of a grammar
Lance owns, in the place nobody re-tests against a new pylance. Those cases are a tripwire, not a
tidy-up: a pylance that stops mapping one reds HERE, which is the day a door-side grammar guard becomes
the right answer.

The third refusal is the store's, not the grammar's, and it reaches a tag as it reaches a branch
([[LH-283]]): the tag's file is its name URL-encoded plus ``.json``, and measured on RustFS 1.0.3 a file
past one 255-byte key segment is refused with an error that carries no code, so the door refuses such a
name itself.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import CreateNamespaceRequest, CreateTableTagRequest, connect

from catalog.services import table_claims
from catalog.services.dataplane import create_table, create_tag
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.location_claims import ClaimStore


lance = pytest.importorskip("lance")

TABLE_ID = ["db", "rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64())])


def _registry(tmp_path: Path) -> base_registry.BaseRegistry:
    """The control root the doors read, so the table holds its location under a uuid as every table does (ADR 0173 §6)."""
    return base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={})


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    root = str(tmp_path / "data")
    namespace = connect("dir", {"root": root})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(
        namespace,
        {},
        TABLE_ID,
        pa.table({"id": pa.array([1], pa.int64())}, schema=SCHEMA),
        root=root,
        mode="create",
        registry=_registry(tmp_path),
        holder="db$rows",
    )
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID)


def _create(table: DoorTable, tag: str) -> int | None:
    """Create the tag and return the spec code it answered, or None if it succeeded."""
    try:
        create_tag(table, {}, CreateTableTagRequest(id=TABLE_ID, tag=tag, version=1))
    except Exception as exc:  # noqa: BLE001 — the CODE is the subject
        return getattr(exc, "code", None)
    return None


@pytest.mark.parametrize(
    ("shape", "tag"),
    [
        ("empty", ""),
        ("a separator, which a branch allows and a tag does not", "feature/x"),
        pytest.param("a 256-byte ref file, past one key segment", "a" * 251, id="ref-file-past-a-key-segment"),
    ],
)
def test_a_malformed_tag_name_answers_invalid_input(table: DoorTable, shape: str, tag: str) -> None:  # noqa: ANN001
    assert _create(table, tag) == 13, f"{shape}: answered something other than InvalidInput"


@pytest.mark.parametrize("tag", ["main"])
def test_a_legal_tag_name_is_accepted(table: DoorTable, tag: str) -> None:  # noqa: ANN001
    """`main` is the headline: it is reserved for BRANCHES only, and a guard copied across would break it."""
    assert _create(table, tag) is None
