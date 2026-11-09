"""A shallow clone is registered only by a caller who may read its source (ADR 0198 §1).

A shallow clone reads its source's data files where they are, so registering one asks what reading the source asks:
otherwise a caller could read any table by cloning it into a namespace it writes. Its readers then read it through the
catalog, since the source's root is no base a vended credential can address (``catalog.core.vending.unsanctioned_bases``).

Every decision is the real model's, on a store of this test's own: alice owns ``db`` and its table ``db$src``, and carol
is a writer on ``db2`` with no grant on ``db``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The Lance Namespace spec's code for a refusal on authorization (`spec.yaml`).
PERMISSION_DENIED = 15


@pytest.fixture
def cloned(governed_catalog: Any, tmp_path: Path) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``db$src``, a shallow clone of it written at ``<root>/clone``, and carol a writer on ``db2``."""
    catalog = governed_catalog
    alice = catalog.bearer("alice")
    for namespace in ("db", "db2"):
        assert catalog.client.post(f"/v1/namespace/{namespace}/create", json={}, headers=alice).status_code == 200
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    created = catalog.client.post("/v1/table/db$src/create", content=encode_arrow_stream(rows), headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    source = lance.dataset(created.json()["location"])
    source.shallow_clone(str(tmp_path / "clone"), source.version)
    granted = catalog.client.post("/management/v1/namespace/db2/access/grant", json={"user": "dex~carol", "relation": "writer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    return catalog


def _register(catalog: Any, who: str) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog; an httpx response
    return catalog.client.post("/v1/table/db2$clone/register", json={"location": "clone"}, headers=catalog.bearer(who))


def test_a_clone_is_registered_only_by_a_reader_of_its_source(cloned: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    refused = _register(cloned, "carol")

    assert (refused.status_code, refused.json()["code"]) == (403, PERMISSION_DENIED), refused.text
    assert refused.headers["content-type"] == "application/problem+json"
    absent = cloned.client.post("/v1/table/db2$clone/describe", json={}, headers=cloned.bearer("alice"))
    assert absent.status_code == 404, f"the refused registration was left attached: {absent.text}"
