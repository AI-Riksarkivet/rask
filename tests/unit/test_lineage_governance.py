"""#49 governed business metadata — the writer gate, the endpoint contracts, and the tag semantics.

Three layers, mirroring ``test_lineage_auth``'s style: the ``require_write_access`` fail-closed ladder is
driven directly; the handlers run against a minimal fake repository; the repository's tag/description
methods (and the ingest tag-UNION) run against a scripted Cypher recorder, pinning the exact statements
and parameter shapes AGE receives (the comma-join, the attribution stamps, the never-clobber union).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from lance_namespace import (
    InvalidInputError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
    UnauthenticatedError,
)

from lineage.api import fga_deps
from lineage.api.v1.endpoints.governance import add_tag, get_governance, remove_tag, set_description
from lineage.core.config import LineageSettings
from lineage.schemas import DatasetGovernance, DescriptionUpdate
from lineage.services import repository as repository_module
from lineage.services.repository import LineageRepository
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


if TYPE_CHECKING:
    import psycopg


_FULL_AUTH = {
    "oidc_enabled": True,
    "oidc_issuer": "https://dex.example",
    "oidc_audience": "lance",
    "fga_enabled": True,
    "fga_store_id": "store",
    "fga_model_id": "model",
}


def _settings(**overrides: Any) -> LineageSettings:
    return LineageSettings.model_validate({"database_url": "postgresql://x/y", **overrides})


def _request(**state: object) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _token(sub: str = "alice") -> IDToken:
    return IDToken(iss="i", sub=sub, aud="lance", exp=0, iat=0)


# --------------------------------------------------------------------------- #
# The writer gate — the same fail-closed ladder as the reader gate, on can_write_data
# --------------------------------------------------------------------------- #


def test_write_gate_is_open_when_fga_off() -> None:
    asyncio.run(fga_deps.require_write_access("d", _request(), _settings(), None))


def test_write_gate_fails_closed_when_client_unwired() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(fga_deps.require_write_access("d", _request(fga=None), _settings(**_FULL_AUTH), _token()))


def test_write_gate_requires_authentication() -> None:
    with pytest.raises(UnauthenticatedError):
        asyncio.run(fga_deps.require_write_access("d", _request(fga=object()), _settings(**_FULL_AUTH), None))


def test_write_gate_denies_a_non_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def deny(*_a: object, **_k: object) -> bool:
        return False

    monkeypatch.setattr(fga, "check", deny)
    with pytest.raises(PermissionDeniedError, match="can_write_data"):
        asyncio.run(fga_deps.require_write_access("gold$catalog", _request(fga=object()), _settings(**_FULL_AUTH), _token("bob")))


def test_write_gate_passes_a_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    async def allow(_c: object, *, user: str, relation: str, obj: str, **_k: object) -> bool:
        captured.update({"user": user, "relation": relation, "obj": obj})
        return True

    monkeypatch.setattr(fga, "check", allow)
    asyncio.run(fga_deps.require_write_access("gold$catalog", _request(fga=object()), _settings(**_FULL_AUTH), _token()))
    assert captured == {"user": "alice", "relation": "can_write_data", "obj": "table:gold$catalog"}


# --------------------------------------------------------------------------- #
# Handlers — contracts the gate can't see (404, tag validation, attribution binding)
# --------------------------------------------------------------------------- #


class _FakeRepo:
    def __init__(self, known: bool = True) -> None:
        self.known = known
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _result(self, name: str) -> DatasetGovernance | None:
        return DatasetGovernance(name=name, tags=["pii"], tags_updated_by="alice") if self.known else None

    async def governance(self, name: str) -> DatasetGovernance | None:
        self.calls.append(("governance", (name,), {}))
        return self._result(name)

    async def set_tag(self, name: str, tag: str, **kwargs: Any) -> DatasetGovernance | None:
        self.calls.append(("set_tag", (name, tag), kwargs))
        return self._result(name)

    async def set_description(self, name: str, description: str, **kwargs: Any) -> DatasetGovernance | None:
        self.calls.append(("set_description", (name, description), kwargs))
        return self._result(name)


def test_add_tag_binds_the_verified_subject() -> None:
    repo = _FakeRepo()
    result = asyncio.run(add_tag("gold$catalog", "pii", cast(LineageRepository, repo), _token("alice")))
    assert result.tags == ["pii"]
    assert repo.calls == [("set_tag", ("gold$catalog", "pii"), {"present": True, "updated_by": "alice"})]


def test_remove_tag_binds_the_verified_subject() -> None:
    repo = _FakeRepo()
    asyncio.run(remove_tag("gold$catalog", "pii", cast(LineageRepository, repo), _token("carol")))
    assert repo.calls == [("set_tag", ("gold$catalog", "pii"), {"present": False, "updated_by": "carol"})]


def test_description_update_binds_subject_and_body() -> None:
    repo = _FakeRepo()
    asyncio.run(
        set_description(
            "gold$catalog",
            DescriptionUpdate(description="daily gold"),
            cast(LineageRepository, repo),
            _token(),
        )
    )
    assert repo.calls == [("set_description", ("gold$catalog", "daily gold"), {"updated_by": "alice"})]


def test_unknown_dataset_is_404() -> None:
    repo = _FakeRepo(known=False)
    with pytest.raises(TableNotFoundError):
        asyncio.run(get_governance("ghost", cast(LineageRepository, repo)))
    with pytest.raises(TableNotFoundError):
        asyncio.run(add_tag("ghost", "pii", cast(LineageRepository, repo), _token()))


@pytest.mark.parametrize("bad", ["a,b", "a/b", "-leading", "x" * 65, "sp ace"])
def test_invalid_tag_shapes_are_400(bad: str) -> None:
    # The comma is the storage JOIN separator — it above all must never enter the property.
    with pytest.raises(InvalidInputError):
        asyncio.run(add_tag("gold$catalog", bad, cast(LineageRepository, _FakeRepo()), _token()))


# --------------------------------------------------------------------------- #
# Repository semantics — the exact Cypher + params AGE receives
# --------------------------------------------------------------------------- #


class _CypherScript:
    """Records every (query, params) and returns scripted rows for the governance read."""

    def __init__(self, tags: str = "", exists: bool = True) -> None:
        self.tags = tags
        self.exists = exists
        self.writes: list[tuple[str, dict[str, Any]]] = []

    async def run_cypher(
        self,
        _conn: object,
        _graph: str,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        columns: int = 1,
    ) -> list[list[Any]]:
        del columns  # signature parity with the real helper; the script keys on the query text
        if "RETURN d.tags" in query:
            return [[self.tags, None, None, None, None, None]] if self.exists else []
        self.writes.append((query, params or {}))
        return [[1]]

    async def fetch(
        self,
        _pool: object,
        _graph: str,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        columns: int = 1,
    ) -> list[list[Any]]:
        return await self.run_cypher(None, "", query, params, columns=columns)


def _repo_with(script: _CypherScript, monkeypatch: pytest.MonkeyPatch) -> LineageRepository:
    @asynccontextmanager
    async def _conn():  # the pool/connection/transaction plumbing the methods traverse
        yield SimpleNamespace(transaction=lambda: _null())

    @asynccontextmanager
    async def _null():
        yield None

    monkeypatch.setattr(repository_module, "run_cypher", script.run_cypher)
    monkeypatch.setattr(repository_module, "fetch", script.fetch)
    pool = SimpleNamespace(connection=lambda: _conn())
    return LineageRepository(cast(Any, pool), "g")


def test_set_tag_appends_and_stamps_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _CypherScript(tags="layer=gold")
    repo = _repo_with(script, monkeypatch)
    asyncio.run(repo.set_tag("gold$catalog", "pii", present=True, updated_by="alice"))
    query, params = script.writes[0]
    assert "SET d.tags=$tags" in query and "d.tags_updated_by=$by" in query
    assert params["tags"] == "layer=gold,pii"  # producer order preserved, new tag appended
    assert params["by"] == "alice" and params["at"]  # attribution stamped


def test_remove_tag_filters_and_restamps(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _CypherScript(tags="layer=gold,pii")
    repo = _repo_with(script, monkeypatch)
    asyncio.run(repo.set_tag("gold$catalog", "pii", present=False, updated_by="carol"))
    assert script.writes[0][1]["tags"] == "layer=gold"


def test_set_tag_on_unknown_dataset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _CypherScript(exists=False)
    repo = _repo_with(script, monkeypatch)
    assert asyncio.run(repo.set_tag("ghost", "pii", present=True, updated_by="alice")) is None
    assert script.writes == []  # nothing written for a missing node


def test_ingest_unions_facet_tags_with_curated_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    # CONTRACT (#49): a producer's tags facet must never clobber human-curated tags — the ingest merge
    # UNIONs into the node's existing set (user tags survive every ingest).
    from lineage.models import Dataset

    script = _CypherScript(tags="pii,layer=gold")
    repo = _repo_with(script, monkeypatch)
    facet = {"tags": [{"key": "layer", "value": "gold"}, {"key": "team", "value": "ml"}]}
    ds = Dataset.model_validate({"namespace": "gold", "name": "gold$catalog", "facets": {"tags": facet}})
    # `_repo_with` monkeypatches `run_cypher`, so the connection is never dereferenced — the script
    # records the writes instead. Cast rather than widen the signature: production always holds a real
    # connection, and typing it optional would lose that.
    asyncio.run(repo._merge_dataset(cast("psycopg.AsyncConnection", None), ds))
    tag_writes = [p for q, p in script.writes if "SET d.tags=$tags" in q]
    assert tag_writes and tag_writes[0]["tags"] == "pii,layer=gold,team=ml"
