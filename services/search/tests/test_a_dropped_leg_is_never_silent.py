"""A leg of a search that did not run must say so — no `except Exception: pass` (VS-07).

docs/DECISIONS.md "The Python estate audit" VS-07. Two wholly silent swallows survived the VS-06 pass, both in the search
plane, both turning a genuine failure into a normal-looking 200:

  * ``service._search_all``'s FTS leg — ``rankings.append(qb.to_list())`` under a bare
    ``except Exception: pass``. A store outage, a timeout or a malformed FTS query removed text
    ranking from a fused ``all`` search and NOTHING recorded it: the caller got vector-only hits
    presented as the fused answer, and the operator got no line to correlate. The vector legs
    fifteen lines below logged the identical failure class, so one function disagreed with itself.
  * ``postprocess.attach_captions`` — ``except Exception: return``, which drops the ``caption`` key
    off every hit. ABSENCE is already answered by the guard above it
    (``not hits or frame_tbl is None or caption_column not in frame_tbl.schema.names``), so the
    only thing this ``except`` can catch is a genuine fault, rendered as "this corpus has no
    captions".

The split applied here is the one `frames.py` and `vector.py` already carry
(:mod:`search.services.query_errors`): a failure the REQUEST caused — the marker phrase
``Invalid user input``, or a domain :class:`ValidationError` a leg's own classifier already
raised — drops that leg from the fusion and is LOGGED; anything else is the estate's fault and
propagates, so an outage is answered as an outage. Dropping every attempted leg is not "no hits"
either: with nothing left to fuse, ``all`` answers 400 rather than an empty 200.

Captions are the one deliberate difference and it is not a silent one: the hits are already
complete and correct when the caption scan runs, so a failure there is logged and the search still
answers — it degrades an enrichment, not the result set.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from search.services import service
from search.services.postprocess import attach_captions
from search.services.spec import SearchSpec
from search.services.target import SearchTarget
from service_kit.exceptions import ValidationError
from service_kit.lancekit.descriptor import FtsBinding, VectorBinding


#: What Lance raises for a malformed caller predicate, verbatim in shape (see `query_errors`).
CALLER_ERROR = RuntimeError("lance error: Invalid user input: Schema error: No field named nosuch. Valid fields are doc_id, v, body")

#: What an outage looks like: the object store went away mid-scan. Nothing about the request is wrong.
OUTAGE = OSError("connection reset by peer while reading s3://warehouse/corpus.lance")


class _Builder:
    """A lancedb query builder: every chained method returns self, the terminal call answers."""

    def __init__(self, exc: Exception | None, rows: list[dict[str, Any]]) -> None:
        self._exc = exc
        self._rows = rows

    def __getattr__(self, _name: str) -> Any:  # noqa: ANN401 — a stand-in for any builder method
        def _chain(*_a: object, **_k: object) -> _Builder:
            return self

        return _chain

    def to_list(self) -> list[dict[str, Any]]:
        if self._exc is not None:
            raise self._exc
        return self._rows


class _Schema:
    def __init__(self, names: list[str]) -> None:
        self.names = names


class _Table:
    """A row table whose FTS and vector legs fail independently.

    ``vector_search`` passes the query vector as a plain list positionally, the FTS legs pass a
    ``MatchQuery`` — that is the only discriminator either leg gives us, and it is enough to make
    one leg fail while the other answers.
    """

    def __init__(self, *, fts_exc: Exception | None = None, vec_exc: Exception | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._fts_exc = fts_exc
        self._vec_exc = vec_exc
        self._rows = rows if rows is not None else [{"doc_id": "a", "body": "hello"}]
        self.schema = _Schema(["doc_id", "v", "body"])

    def search(self, query: object, *_a: object, **_k: object) -> _Builder:
        is_vector = isinstance(query, list)
        return _Builder(self._vec_exc if is_vector else self._fts_exc, self._rows)


class _Vec:
    def tolist(self) -> list[float]:
        return [0.1, 0.2]


_SEMANTIC = VectorBinding(table="chunks", column="v", dim=2, query_encoder="text")


def _target(tbl: _Table, *, fts: FtsBinding | None, vectors: dict[str, VectorBinding]) -> SearchTarget:
    return SearchTarget(
        dataset_id="corpus",
        table_name="chunks",
        row_table_name="chunks",
        key_fields=["doc_id"],
        payload_columns=["doc_id", "body"],
        filterable=[],
        topic_columns=[],
        fts=fts,
        vectors=vectors,
        alignments_column=None,
        caption_column=None,
        body_column="body",
        row_tbl=tbl,
        row_ds=tbl,
        tables={"chunks": tbl},
    )


def _ctx(target: SearchTarget) -> service.SearchContext:
    return service.SearchContext(
        target=target,
        get_reranker=lambda: None,
        spec=SearchSpec(q="hello", mode="all"),
        where=None,
        rerank_query="hello",
        text_vec=_Vec(),
    )


_FTS = FtsBinding(table="chunks", column="body")


# ── SITE 1: the fused 'all' search ────────────────────────────────────────────────────────────────


class TestTheFtsLegOfAll:
    def test_an_outage_is_not_swallowed(self) -> None:
        """The finding. A store outage removed text ranking from the fused answer and said nothing;
        it must reach the global handler as the 500 it is."""
        ctx = _ctx(_target(_Table(fts_exc=OUTAGE), fts=_FTS, vectors={"semantic": _SEMANTIC}))

        with pytest.raises(OSError, match="connection reset"):
            service._search_all(ctx)

    def test_a_malformed_query_drops_the_leg_and_LOGS_it(self, caplog: pytest.LogCaptureFixture) -> None:
        """The half that must keep degrading: one unusable leg cannot 400 a fused search. But the
        drop is recorded, because a caller reading vector-only hits cannot tell that half the search
        did not run."""
        ctx = _ctx(_target(_Table(fts_exc=CALLER_ERROR), fts=_FTS, vectors={"semantic": _SEMANTIC}))

        with caplog.at_level(logging.WARNING, logger="search.services.service"):
            hits = service._search_all(ctx)

        assert [h["doc_id"] for h in hits] == ["a"], "the surviving vector leg must still be fused"
        assert caplog.records, "the FTS leg was dropped from the fused search and NOTHING recorded it"
        assert any(r.exc_info for r in caplog.records), "the drop was logged without the exception that caused it"


class TestTheVectorLegsOfAll:
    def test_an_outage_is_not_swallowed(self) -> None:
        """The same rule, so the two legs of one function agree: `vector_search` re-raises an outage
        untouched (VS-06) and `_search_all` must not re-swallow it."""
        ctx = _ctx(_target(_Table(vec_exc=OUTAGE), fts=None, vectors={"semantic": _SEMANTIC}))

        with pytest.raises(OSError, match="connection reset"):
            service._search_all(ctx)

    def test_a_malformed_query_drops_the_leg_and_LOGS_it(self, caplog: pytest.LogCaptureFixture) -> None:
        """A space the request cannot drive contributes nothing rather than 400ing the fusion —
        `vector_search` has already classified it as the caller's, so it arrives as ValidationError."""
        ctx = _ctx(_target(_Table(vec_exc=CALLER_ERROR), fts=_FTS, vectors={"semantic": _SEMANTIC}))

        with caplog.at_level(logging.WARNING, logger="search.services.service"):
            hits = service._search_all(ctx)

        assert [h["doc_id"] for h in hits] == ["a"], "the surviving FTS leg must still be fused"
        assert caplog.records, "the vector leg was dropped from the fused search and NOTHING recorded it"


class TestEveryLegDropped:
    def test_a_fusion_with_nothing_left_to_fuse_is_not_an_empty_200(self, caplog: pytest.LogCaptureFixture) -> None:
        """`[]` claims 'this corpus has nothing to show you'. When every leg was REJECTED, the true
        answer is that the query could not be run at all."""
        ctx = _ctx(_target(_Table(fts_exc=CALLER_ERROR, vec_exc=CALLER_ERROR), fts=_FTS, vectors={"semantic": _SEMANTIC}))

        with caplog.at_level(logging.WARNING, logger="search.services.service"), pytest.raises(ValidationError):
            service._search_all(ctx)


class TestAHealthyFusionIsUntouched:
    def test_both_legs_fuse(self, caplog: pytest.LogCaptureFixture) -> None:
        """The failure mode that would hide the fix: raising on everything also passes the tests
        above."""
        ctx = _ctx(_target(_Table(), fts=_FTS, vectors={"semantic": _SEMANTIC}))

        with caplog.at_level(logging.WARNING, logger="search.services.service"):
            hits = service._search_all(ctx)

        assert [h["doc_id"] for h in hits] == ["a"]
        assert not caplog.records, "a healthy fused search logged a dropped leg"


# ── SITE 2: the caption enrichment ────────────────────────────────────────────────────────────────


class _CaptionScan:
    def __init__(self, exc: Exception | None, rows: list[dict[str, Any]]) -> None:
        self._exc = exc
        self._rows = rows

    def to_table(self, *_a: object, **_k: object) -> _CaptionScan:
        return self

    def to_pylist(self) -> list[dict[str, Any]]:
        if self._exc is not None:
            raise self._exc
        return self._rows


class _FrameTable:
    def __init__(self, exc: Exception | None = None, *, names: list[str] | None = None, rows: list[dict[str, Any]] | None = None) -> None:
        self._exc = exc
        self._rows = rows if rows is not None else [{"doc_id": "a", "caption": "a cat"}]
        self.schema = _Schema(names if names is not None else ["doc_id", "frame_idx", "caption"])

    def to_lance(self) -> _CaptionScan:
        return _CaptionScan(self._exc, self._rows)


def _hits() -> list[dict[str, Any]]:
    return [{"doc_id": "a", "body": "hello"}]


class TestAttachCaptions:
    def test_a_failed_scan_is_LOGGED_not_rendered_as_no_captions(self, caplog: pytest.LogCaptureFixture) -> None:
        """The finding. Absence is answered by the guard above, so this can only be a real fault —
        and it left no trace at all."""
        hits = _hits()

        with caplog.at_level(logging.WARNING, logger="search.services.postprocess"):
            attach_captions(_FrameTable(OUTAGE), hits, caption_column="caption", key_fields=["doc_id"])

        assert caplog.records, "the caption scan failed and NOTHING recorded it"
        assert any(r.exc_info for r in caplog.records), "the failure was logged without the exception that caused it"

    def test_the_search_still_answers(self) -> None:
        """The deliberate asymmetry with the fused legs: the hits are already complete and correct,
        so a broken enrichment must not take the result set down with it."""
        hits = _hits()

        attach_captions(_FrameTable(OUTAGE), hits, caption_column="caption", key_fields=["doc_id"])

        assert hits == [{"doc_id": "a", "body": "hello"}]

    @pytest.mark.parametrize(
        ("frame_tbl", "hits"),
        [
            (None, _hits()),
            (_FrameTable(names=["doc_id", "frame_idx"]), _hits()),
            (_FrameTable(), []),
        ],
        ids=["no-frame-table", "no-caption-column", "no-hits"],
    )
    def test_genuine_ABSENCE_stays_a_silent_no_op(self, frame_tbl: Any, hits: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> None:  # noqa: ANN401
        """The case the docstring sanctions, and the only one that may be silent: a corpus that
        declares no captions is not a fault, so it must not page anyone."""
        with caplog.at_level(logging.WARNING, logger="search.services.postprocess"):
            attach_captions(frame_tbl, hits, caption_column="caption", key_fields=["doc_id"])

        assert not caplog.records, "absence was logged as a failure"
        assert all("caption" not in h for h in hits)

    def test_a_working_scan_still_attaches(self) -> None:
        """The failure mode that would hide the fix: never attaching also passes the tests above."""
        hits = _hits()

        attach_captions(_FrameTable(), hits, caption_column="caption", key_fields=["doc_id"])

        assert hits[0]["caption"] == "a cat"
