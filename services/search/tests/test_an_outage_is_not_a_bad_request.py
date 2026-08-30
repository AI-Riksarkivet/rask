"""A store outage must not be reported to the caller as "you sent a bad request" (VS-06).

open_python-audit VS-06. Six retrieval sites caught bare ``Exception`` and re-raised
:class:`ValidationError`, which ``service_kit.exceptions`` maps to HTTP 400. An unreachable S3
endpoint, an expired credential, a corrupt Lance manifest and a genuinely malformed ``where`` all
came out as the same 400 — which tells the caller to fix their input, tells the operator nothing is
wrong on the server, and excludes the failure from every 5xx-based SLO and alert.

THE SPLIT IS EMPIRICAL, not a guess. Measured against the pinned lancedb/pylance in this venv
(``tbl.search().where("nosuch = 1")``, ``"n = = 1"``, ``"n = 'x'"``, a wrong-dim query vector, a
missing ``select`` column, and the same three through ``LanceDataset.to_table``): EVERY caller-input
failure arrives as ``RuntimeError``/``ValueError`` whose message contains **"Invalid user input"**,
and nothing else does. So that phrase — and only that phrase — may become a 400; every other
exception propagates and the global handler renders the 500 the operator needs to see.

Both halves are pinned per site on purpose: narrowing the catch is only correct if the genuine
client error still gets its 400, and keeping the 400 is only correct if the outage no longer does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from search.services import frames, service, similar, vector
from search.services.spec import SearchSpec
from search.services.target import SearchTarget
from service_kit.exceptions import ValidationError
from service_kit.lancekit.descriptor import FtsBinding, VectorBinding


if TYPE_CHECKING:
    from service_kit.lancekit.registry import DatasetHandle


#: What Lance raises for a malformed caller predicate, verbatim in shape (see the module docstring).
CALLER_ERROR = RuntimeError("lance error: Invalid user input: Schema error: No field named nosuch. Valid fields are doc_id, n, v")

#: What an outage looks like: the object store went away mid-scan. Nothing about the request is wrong.
OUTAGE = OSError("connection reset by peer while reading s3://warehouse/corpus.lance")


class _Raises:
    """A lancedb query builder whose terminal call raises — every chained method returns self."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, _name: str) -> Any:  # noqa: ANN401 — a stand-in for any builder method
        def _chain(*_a: object, **_k: object) -> _Raises:
            return self

        return _chain

    def to_list(self) -> list[dict[str, Any]]:
        raise self._exc

    def to_pylist(self) -> list[dict[str, Any]]:
        raise self._exc


class _Schema:
    def __init__(self, names: list[str]) -> None:
        self.names = names


class _Table:
    """A lancedb table / Lance dataset whose reads raise ``exc``."""

    def __init__(self, exc: Exception, names: list[str]) -> None:
        self._exc = exc
        self.schema = _Schema(names)

    def search(self, *_a: object, **_k: object) -> _Raises:
        return _Raises(self._exc)

    def to_table(self, *_a: object, **_k: object) -> _Raises:
        return _Raises(self._exc)


class _Vec:
    def tolist(self) -> list[float]:
        return [0.1, 0.2]


def _target(exc: Exception, **over: Any) -> SearchTarget:
    tbl = _Table(exc, ["doc_id", "v", "body"])
    target = SearchTarget(
        dataset_id="corpus",
        table_name="chunks",
        row_table_name="chunks",
        key_fields=["doc_id"],
        payload_columns=["doc_id", "body"],
        filterable=[],
        topic_columns=[],
        fts=None,
        vectors={},
        alignments_column=None,
        caption_column=None,
        body_column="body",
        row_tbl=tbl,
        row_ds=tbl,
        tables={"chunks": tbl},
    )
    for name, value in over.items():
        setattr(target, name, value)
    return target


def _ctx(target: SearchTarget) -> service.SearchContext:
    return service.SearchContext(
        target=target,
        get_reranker=lambda: None,
        spec=SearchSpec(q="hello"),
        where="n = 1",
        rerank_query="hello",
        text_vec=_Vec(),
    )


class _StubHandle:
    """`run_search` only passes the handle to `resolve_target`, which the browse test stubs."""


def _browse_args() -> dict[str, Any]:
    """`run_search`'s collaborators, cast at the seam: this test never reaches the embedder or the
    reranker (the browse leg has no query text), and the handle is consumed only by the stubbed
    `resolve_target`."""
    return {
        "get_embedder": cast("Any", lambda: None),
        "get_reranker": cast("Any", lambda: None),
    }


def _outage_propagates(call: Any) -> None:  # noqa: ANN401 — any zero-arg callable under test
    """The outage must arrive at the global handler AS ITSELF (→ 500), not wearing a 400."""
    try:
        call()
    except ValidationError as exc:
        pytest.fail(f"an object-store outage was reported to the caller as HTTP 400 {str(exc)!r} — the operator sees a client error, not an outage")
    except OSError as exc:
        assert "connection reset" in str(exc)
        return
    pytest.fail("the outage was swallowed entirely — the call returned normally")


# ── the six sites, each with both halves ──────────────────────────────────────────────────────────


class TestVectorSearch:
    def _search(self, exc: Exception) -> list[dict[str, Any]]:
        return vector.vector_search(_Table(exc, ["v"]), _Vec(), "v", payload_columns=["doc_id"], n=5, where="n = 1")

    def test_an_outage_is_not_a_400(self) -> None:
        _outage_propagates(lambda: self._search(OUTAGE))

    def test_a_malformed_predicate_is_still_a_400(self) -> None:
        with pytest.raises(ValidationError):
            self._search(CALLER_ERROR)


class TestFtsSearch:
    def _search(self, exc: Exception) -> list[dict[str, Any]]:
        return service._search_fts(_ctx(_target(exc, fts=FtsBinding(table="chunks", column="body"))))

    def test_an_outage_is_not_a_400(self) -> None:
        _outage_propagates(lambda: self._search(OUTAGE))

    def test_a_malformed_predicate_is_still_a_400(self) -> None:
        with pytest.raises(ValidationError):
            self._search(CALLER_ERROR)


class TestHybridSearch:
    def _search(self, exc: Exception) -> list[dict[str, Any]]:
        target = _target(
            exc,
            fts=FtsBinding(table="chunks", column="body"),
            vectors={"semantic": VectorBinding(table="chunks", column="v", dim=2, query_encoder="text")},
        )
        return service._search_hybrid(_ctx(target))

    def test_an_outage_is_not_a_400(self) -> None:
        _outage_propagates(lambda: self._search(OUTAGE))

    def test_a_malformed_predicate_is_still_a_400(self) -> None:
        with pytest.raises(ValidationError):
            self._search(CALLER_ERROR)


class TestBrowseScan:
    """`run_search`'s filter-only browse leg — a topic facet clicked with no query text."""

    def _run(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> list[dict[str, Any]]:
        monkeypatch.setattr(service, "resolve_target", lambda _handle, _table=None: _target(exc))
        return service.run_search(
            cast("DatasetHandle", _StubHandle()),
            spec=SearchSpec(where="n = 1"),
            filters={},
            image_bytes=None,
            **_browse_args(),
        )

    def test_an_outage_is_not_a_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _outage_propagates(lambda: self._run(monkeypatch, OUTAGE))

    def test_a_malformed_predicate_is_still_a_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError):
            self._run(monkeypatch, CALLER_ERROR)


class TestFrameJoin:
    def _join(self, exc: Exception) -> list[dict[str, Any]]:
        return frames.frames_to_row_hits(_target(exc), [{"doc_id": "a", "_distance": 0.1}], None)

    def test_an_outage_is_not_a_400(self) -> None:
        _outage_propagates(lambda: self._join(OUTAGE))

    def test_a_malformed_predicate_is_still_a_400(self) -> None:
        with pytest.raises(ValidationError):
            self._join(CALLER_ERROR)


class TestSeedLookup:
    def _seed(self, exc: Exception) -> list[float]:
        return similar.seed_vector(_Table(exc, ["v"]), where="doc_id = 'a'", column="v")

    def test_an_outage_is_not_a_400(self) -> None:
        _outage_propagates(lambda: self._seed(OUTAGE))

    def test_a_malformed_predicate_is_still_a_400(self) -> None:
        with pytest.raises(ValidationError):
            self._seed(CALLER_ERROR)
