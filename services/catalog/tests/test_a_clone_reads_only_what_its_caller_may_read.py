"""A shallow clone is registered only by a caller who may tag its source (ADR 0198 §1, owner 2026-11-09).

A shallow clone reads its source's data files where they are, and its registration tags the version it reads on the source,
which keeps that version through the source's cleanup and refuses the source's destroying drop. So registering one asks
what the tag door asks on each source, ``can_create_tag`` (an owner or a publisher): a reader who could register a clone
would pin the owner's table. The question is asked before anything is attached, so a refused caller leaves no row, mark
or claim behind. Its refusal is one sentence whether a governed table is at the source's root or not, and names only that
root, which the caller's own manifest holds, never the table there ([[LH-436]]).

Every decision is the real model's, on a store of this test's own: alice owns ``db`` and its table ``db$src``, carol is a
writer on ``db2`` with no grant on ``db``, and bob is a writer on ``db2`` and a reader on ``db``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from lakehouse import location_claims
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed import fga


#: The Lance Namespace spec's code for a refusal on authorization (`spec.yaml`).
PERMISSION_DENIED = 15


def _source_root(clone: Path) -> str:
    """The root the clone's first transaction names as its source, as pylance reads it."""
    dataset = lance.dataset(str(clone))
    first = min(int(entry["version"]) for entry in dataset.versions())
    transaction = dataset.read_transaction(first)
    assert transaction is not None and isinstance(transaction.operation, lance.LanceOperation.Clone)
    return str(transaction.operation.ref_path)


@pytest.fixture
def cloned(governed_catalog: Any, tmp_path: Path) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``db$src`` and a shallow clone of it at ``<root>/clone``; a dataset no table holds and a shallow clone of it
    at ``<root>/stray``; carol a writer on ``db2``, bob a writer on ``db2`` and a reader on ``db``, and dave a writer on
    ``db2`` and a publisher of ``db$src`` who reads nothing there."""
    catalog = governed_catalog
    alice = catalog.bearer("alice")
    for namespace in ("db", "db2"):
        assert catalog.client.post(f"/v1/namespace/{namespace}/create", json={}, headers=alice).status_code == 200
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    created = catalog.client.post("/v1/table/db$src/create", content=encode_arrow_stream(rows), headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    source = lance.dataset(created.json()["location"])
    source.shallow_clone(str(tmp_path / "clone"), source.version)
    ungoverned = lance.write_dataset(rows, str(tmp_path / "raw"), data_storage_version="2.2", enable_stable_row_ids=True)
    ungoverned.shallow_clone(str(tmp_path / "stray"), ungoverned.version)
    for user, namespace, relation in (("carol", "db2", "writer"), ("bob", "db2", "writer"), ("bob", "db", "reader"), ("dave", "db2", "writer")):
        granted = catalog.client.post(f"/management/v1/namespace/{namespace}/access/grant", json={"user": f"dex~{user}", "relation": relation}, headers=alice)
        assert granted.status_code == 200, granted.text
    described = catalog.client.post("/v1/table/db$src/describe", json={}, headers=alice)
    catalog.store.write((catalog.person("dave"), "publisher", fga.table_object(described.json()["properties"]["rask.uuid"])))
    return catalog


@pytest.mark.parametrize(
    ("who", "dataset", "lacking"),
    [
        pytest.param("carol", "clone", "can_read_data", id="a-caller-with-no-grant-on-the-source"),
        pytest.param("bob", "clone", "can_create_tag", id="a-reader-of-the-source"),
        pytest.param("dave", "clone", "can_read_data", id="a-publisher-who-may-not-read-the-source"),
        pytest.param("carol", "stray", "can_read_data", id="a-source-no-table-holds"),
    ],
)
def test_a_clone_is_registered_only_by_a_caller_who_may_read_and_tag_its_source(cloned: Any, tmp_path: Path, who: str, dataset: str, lacking: str) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    refused = cloned.client.post(f"/v1/table/db2${dataset}/register", json={"location": dataset}, headers=cloned.bearer(who))

    assert (refused.status_code, refused.headers["content-type"], refused.json()["code"]) == (403, "application/problem+json", PERMISSION_DENIED), refused.text
    assert refused.json()["detail"] == f"{lacking} required on {_source_root(tmp_path / dataset)}"
    absent = cloned.client.post(f"/v1/table/db2${dataset}/describe", json={}, headers=cloned.bearer("alice"))
    assert absent.status_code == 404, f"the refused registration was left attached: {absent.text}"
    claims = location_claims.ClaimStore(control_root=str(tmp_path))
    assert location_claims.read(claims, str(tmp_path / dataset)) is None, "the refused registration claimed the clone's location before it was asked"


def test_a_clone_of_a_clone_is_refused_a_caller_who_holds_nothing_on_the_first_source(cloned: Any, tmp_path: Path) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """A clone of a clone reads the first source's files too, and its registration tags that source: a caller who owns
    only the clone between them is refused on the first source, by the root their own manifest names."""
    alice = cloned.bearer("alice")
    assert cloned.client.post("/v1/table/db2$clone/register", json={"location": "clone"}, headers=alice).status_code == 200
    granted = cloned.client.post("/management/v1/namespace/db2/access/grant", json={"user": "dex~carol", "relation": "owner"}, headers=alice)
    assert granted.status_code == 200, granted.text
    between = lance.dataset(str(tmp_path / "clone"))
    between.shallow_clone(str(tmp_path / "second"), between.version)
    first_source = cloned.client.post("/v1/table/db$src/describe", json={}, headers=alice).json()["location"]

    refused = cloned.client.post("/v1/table/db2$second/register", json={"location": "second"}, headers=cloned.bearer("carol"))

    assert (refused.status_code, refused.json().get("detail")) == (403, f"can_read_data required on {first_source}"), refused.text


def test_a_refusal_names_the_nearest_source_and_not_what_that_source_reads_through(cloned: Any, tmp_path: Path) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """The sources are asked in the order the clone reads through them: a caller who may not tag the governed clone
    between (carol, a writer on its namespace) is refused there, whatever stands at the root that clone reads through,
    so the refusal tells them nothing of what the clone between is a clone of ([[LH-436]])."""
    alice = cloned.bearer("alice")
    assert cloned.client.post("/v1/table/db2$clone/register", json={"location": "clone"}, headers=alice).status_code == 200
    between = lance.dataset(str(tmp_path / "clone"))
    between.shallow_clone(str(tmp_path / "second"), between.version)
    assert cloned.client.post("/v1/table/db$src/deregister", json={}, headers=alice).status_code == 200

    refused = cloned.client.post("/v1/table/db2$second/register", json={"location": "second"}, headers=cloned.bearer("carol"))

    assert (refused.status_code, refused.json().get("detail")) == (403, f"can_create_tag required on {_source_root(tmp_path / 'second')}"), refused.text


def test_a_location_a_clone_holds_is_refused_as_held_and_not_read_for_its_source(cloned: Any, tmp_path: Path) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """A location another table holds is refused as held before the dataset there is read, so a caller who may not read the
    clone there learns nothing of what it clones ([[LH-436]])."""
    assert cloned.client.post("/v1/table/db2$clone/register", json={"location": "clone"}, headers=cloned.bearer("alice")).status_code == 200

    refused = cloned.client.post("/v1/table/db2$copy/register", json={"location": "clone"}, headers=cloned.bearer("carol"))

    assert (refused.status_code, refused.headers["content-type"]) == (400, "application/problem+json"), refused.text
    assert _source_root(tmp_path / "clone") not in refused.text, f"the refusal names the source the held location clones: {refused.text}"
