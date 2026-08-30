"""A failed voice kNN is an outage, not a malformed request (VS-06).

open_python-audit VS-06's viewer site. ``_search_turns`` caught bare ``Exception`` and raised
``ValidationError("voice search failed")`` — HTTP 400 — so an unreachable object store, an expired
credential or a corrupt voice-embeddings manifest told the caller their request was wrong and told
the operator nothing was wrong on the server.

AND THERE IS NO CALLER INPUT HERE TO BE WRONG. Unlike the search service's six sites, this query
carries no caller-supplied SQL and no caller-chosen column: `voice.py` whitelists ``doc_id`` against
the descriptor's identity pattern before it is inlined, ``n`` is bounded by the route
(``Query(ge=1, le=MAX_N)``), and the embedding column comes from the descriptor. Every remaining
failure mode belongs to the estate, so the honest handler is no handler — the exception propagates
and the problem+json handler renders the 500.
"""

from __future__ import annotations

from typing import Any

import pytest

from service_kit.exceptions import ValidationError
from viewer.services import voice_service


OUTAGE = OSError("connection reset by peer while reading s3://warehouse/voice_embeddings.lance")


class _Raises:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, _name: str) -> Any:  # noqa: ANN401 — a stand-in for any builder method
        def _chain(*_a: object, **_k: object) -> _Raises:
            return self

        return _chain

    def to_list(self) -> list[dict[str, Any]]:
        raise self._exc


class _Table:
    def search(self, *_a: object, **_k: object) -> _Raises:
        return _Raises(OUTAGE)


def test_a_store_outage_reaches_the_handler_as_an_outage() -> None:
    try:
        voice_service._search_turns(_Table(), "embedding", [0.1, 0.2], n=5, exclude_doc_id=None)
    except ValidationError as exc:
        pytest.fail(f"a voice-table outage was reported to the caller as HTTP 400 {str(exc)!r} — nothing about the request was malformed")
    except OSError as exc:
        assert "connection reset" in str(exc)
        return
    pytest.fail("the outage was swallowed entirely — the call returned normally")
