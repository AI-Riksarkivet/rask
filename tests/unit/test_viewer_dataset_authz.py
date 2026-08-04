"""The corpus list stops being public.

`GET /api/datasets` enumerated every corpus in the registry — ids, table stats, declared
capabilities — to any caller, and `/api/datasets/{id}/descriptor` handed out a corpus's full schema.
That was the documented "localhost / trusted network" posture, and it stops being defensible the
moment more than one person can reach the zone: a corpus LIST names data someone may not know exists.

A corpus is NOT a new kind of FGA object. `MediaSettings.catalog_table_id` already maps a media
dataset's table onto the catalog identifier, and the annotator reads and writes Lance through exactly
that mapping — so the object is the `table` the model already defines, with the rungs it already has.
A parallel `dataset` type would be a second thing to keep in step with the real one.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from viewer.api.security import READ_METADATA, corpus_object
from viewer.api.v1.endpoints import datasets as ds
from viewer.api.v1.endpoints.datasets import router
from viewer.core.config import ViewerSettings, get_viewer_settings

from service_kit.exceptions import register_handlers
from service_kit.lancekit.descriptor import DatasetDescriptor


def _descriptor(dataset_id: str, row_table: str | None) -> DatasetDescriptor:
    """A REAL descriptor, not a stand-in.

    The first version of this was a duck-typed fake, and the descriptor route rejected it with a
    `ResponseValidationError` — FastAPI validates the response against `DatasetDescriptor`. A fake
    that cannot survive the endpoint's own response model would have let the 200 case pass for the
    wrong reason, so it is built through the real model.
    """
    return DatasetDescriptor.model_validate(
        {
            "id": dataset_id,
            "tables": {},
            "declared": {
                "identity": {"key_fields": ["doc_id"], "doc_key": "doc_id"},
                **({"search": {"row_table": row_table}} if row_table is not None else {}),
            },
        }
    )


class _Registry:
    """Stands in for the on-disk DatasetRegistry."""

    def __init__(self, ids: dict[str, str]) -> None:
        self._ids = ids
        #: Ids whose descriptor declares NO search block at all — the `Search | None` case.
        self.strip_search: set[str] = set()

    def list_ids(self) -> list[str]:
        return list(self._ids)

    def get(self, dataset_id: str) -> Any:
        row_table = None if dataset_id in self.strip_search else self._ids[dataset_id]
        return type("H", (), {"descriptor": _descriptor(dataset_id, row_table)})()


def _app(registry: _Registry, *, allow: Any, seen: list[dict[str, Any]] | None = None, fga_enabled: bool = False) -> FastAPI:
    """The router with the registry faked and the checker overridden.

    `allow` is either a bool or a set of PERMITTED object strings, so a test can grant access to one
    corpus and not another — which is the case the endpoint exists for.
    """

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow if isinstance(allow, bool) else obj in allow

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    # Through `model_validate`, not alias kwargs: pydantic accepts the aliases at runtime but a type
    # checker cannot see them, and `uvx ty check` runs with error-on-warning.
    settings = ViewerSettings.model_validate(
        {
            "LANCE_FGA_ENABLED": fga_enabled,
            # FGA requires OIDC — the shared settings validator refuses the pair otherwise.
            "LANCE_OIDC_ENABLED": fga_enabled,
            "LANCE_OIDC_ISSUER": "https://issuer.test",
            "LANCE_OIDC_AUDIENCE": "rask",
        }
    )
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    deps = ds.CheckerDep.__metadata__[0].dependency  # the get_checker callable this module annotates with
    app.dependency_overrides[deps] = lambda: checker
    subject_dep = ds.CurrentSubject.__metadata__[0].dependency
    app.dependency_overrides[subject_dep] = lambda: "gina"

    # NOT `class _State: registry = registry` — a class body does not close over the enclosing
    # function's locals, so the right-hand `registry` resolves to the module-level FIXTURE and every
    # test fails with `'FixtureFunctionDefinition' object has no attribute 'list_ids'`.
    state = type("_State", (), {"registry": registry})()
    app.dependency_overrides[ds.StateDep.__metadata__[0].dependency] = lambda: state
    return app


@pytest.fixture
def registry() -> _Registry:
    return _Registry({"vasa": "chunks", "secret": "chunks"})


def _permitted(dataset_id: str) -> str:
    return corpus_object(ViewerSettings(), dataset_id, "chunks")


def test_the_listing_returns_ONLY_the_corpora_the_caller_may_see(registry: _Registry) -> None:
    """The defect, stated directly: it used to return both."""
    client = TestClient(_app(registry, allow={_permitted("vasa")}))

    body = client.get("/api/datasets").json()

    assert [d["id"] for d in body["datasets"]] == ["vasa"]


def test_a_caller_with_nothing_gets_an_empty_list_not_a_403(registry: _Registry) -> None:
    """Filtered, not refused. The honest answer to "what can I search" is a shorter list — a 403
    would also confirm that corpora exist, which is the thing being protected."""
    client = TestClient(_app(registry, allow=False))

    r = client.get("/api/datasets")

    assert r.status_code == 200
    assert r.json()["datasets"] == []


def test_the_check_names_the_CATALOG_object_not_an_invented_one(registry: _Registry) -> None:
    """The object must be the same identifier the annotator writes through and the catalog
    authorizes on — `table:<namespace>$<table>` — or the two planes disagree about one corpus."""
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(registry, allow=True, seen=seen))

    client.get("/api/datasets")

    assert {c["obj"] for c in seen} == {_permitted("vasa"), _permitted("secret")}
    assert all(c["obj"].startswith("table:") for c in seen)


def test_the_relation_is_METADATA_not_data_access(registry: _Registry) -> None:
    """Listing a corpus and reading its descriptor is metadata. Gating on `can_read_data` would hide
    corpora from someone allowed to know they exist."""
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(registry, allow=True, seen=seen))

    client.get("/api/datasets")

    assert {c["relation"] for c in seen} == {READ_METADATA}
    assert READ_METADATA == "can_get_metadata"


def test_the_check_uses_the_VERIFIED_subject(registry: _Registry) -> None:
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(registry, allow=True, seen=seen))

    client.get("/api/datasets")

    assert {c["user"] for c in seen} == {"gina"}


def test_the_descriptor_is_gated_too(registry: _Registry) -> None:
    """It reveals strictly MORE than the listing — every table, column and capability. Guarding the
    list and leaving this open would mean the list only had to be guessed."""
    client = TestClient(_app(registry, allow=False))

    r = client.get("/api/datasets/secret/descriptor")

    assert r.status_code == 403, r.text
    assert "can_get_metadata" in r.text


def test_a_permitted_descriptor_still_serves(registry: _Registry) -> None:
    client = TestClient(_app(registry, allow={_permitted("vasa")}))

    assert client.get("/api/datasets/vasa/descriptor").status_code == 200


def test_a_corpus_with_NO_search_declaration_is_denied_under_authz() -> None:
    """`Search | None` is a real shape — a corpus can be viewable without being searchable — and it
    leaves nothing to name as an FGA object.

    Denied rather than guessed. Inventing an identifier would authorize against something the catalog
    never governs, and a check against a nonexistent object reads as a grant.
    """
    registry = _Registry({"vasa": "chunks"})
    registry.strip_search.add("vasa")  # type: ignore[attr-defined]
    client = TestClient(_app(registry, allow=True, fga_enabled=True))

    assert client.get("/api/datasets").json()["datasets"] == []


def test_the_same_corpus_still_lists_when_authz_is_OFF() -> None:
    """The deny above must not become a behaviour change for a dev stack that never turned FGA on."""
    registry = _Registry({"vasa": "chunks"})
    registry.strip_search.add("vasa")  # type: ignore[attr-defined]
    client = TestClient(_app(registry, allow=True, fga_enabled=False))

    assert [d["id"] for d in client.get("/api/datasets").json()["datasets"]] == ["vasa"]


def test_the_descriptor_route_survives_a_corpus_with_no_search_block() -> None:
    """A 500 I shipped: `dataset_descriptor` read `declared.search.row_table` with no None guard.

    `Search | None` is a real shape, handled correctly in the LISTING's `_row_table` and forgotten
    one function below. `ty` could not catch it because `dataset_handle` returns a loose type, so
    the only thing that would have was a test for the branch — this one.

    Denied, matching the listing: with nothing to name as an FGA object, guessing an identifier
    would authorize against something the catalog never governs.
    """
    registry = _Registry({"vasa": "chunks"})
    registry.strip_search.add("vasa")
    client = TestClient(_app(registry, allow=True, fga_enabled=True))

    r = client.get("/api/datasets/vasa/descriptor")

    assert r.status_code == 403, r.text


def test_a_search_less_descriptor_still_serves_when_authz_is_OFF() -> None:
    """The mirror of the test above, and the reason the guard is conditional.

    A corpus that the LISTING shows on an FGA-off stack must also be openable from it. The first
    version of this guard denied unconditionally, which would have broken every dev stack the moment
    a corpus omitted its search block.
    """
    registry = _Registry({"vasa": "chunks"})
    registry.strip_search.add("vasa")
    client = TestClient(_app(registry, allow=True, fga_enabled=False))

    assert client.get("/api/datasets/vasa/descriptor").status_code == 200
