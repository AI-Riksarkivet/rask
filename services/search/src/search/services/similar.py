""" "More like this" — k-NN seeded by a row the caller already has.

Search answers a QUERY. The query for "another four hundred pages that look like this one" is the
page itself, and its embedding is already in the table — there was simply no way to reach it. This
is the missing step: a row key in, that row's own vector out, its neighbours back.

`open_browse.md` §3 calls it the highest value-per-unit-work selector, and the reason is that it
costs no new machinery: the vectors are declared (`Search.vectors`), the retrieval already exists
(`vector.vector_search`), and the atlas lasso becomes a second CALLER of this rather than a second
mechanism.

Pure by construction — key parsing, seed lookup and self-exclusion are the parts worth pinning, and
none of them need FastAPI or a live Lance DB to be exercised.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from service_kit.exceptions import NotFoundError, ValidationError
from service_kit.lancekit.descriptor import Declared
from service_kit.lancekit.keys import chunk_key_filter


logger = logging.getLogger(__name__)

#: The separator a key path uses. Matches the frontend's `view.keyPath(...).join('/')`, which is
#: what every surface already passes around as "the id of this row".
KEY_SEP = "/"

#: How many neighbours one call may ask for. A cap, not a page size: the answer is rendered as a
#: grid a person scans, and an uncapped `n` turns one click into a full-corpus scan.
MAX_N = 200


class SimilarSpec(BaseModel):
    """One "more like this" request."""

    #: The seed row, as its identity fields joined by `/`.
    key: str = Field(min_length=1, max_length=512)
    n: int = Field(default=24, ge=1, le=MAX_N)
    #: Which declared vector space to search — `semantic`, `visual`, `scene`, or any future key.
    #: None means the corpus's first declared binding.
    space: str | None = None
    #: A raw SQL filter, ANDed by the caller's own rules. Narrowing the neighbourhood is most of
    #: what makes this useful for building a labelling batch ("like this one, but only in 1897").
    where: str | None = None

    @property
    def fetch_n(self) -> int:
        """How many rows to ask Lance for.

        ONE MORE than requested, because the seed is always its own nearest neighbour (distance 0)
        and is dropped afterwards. Asking for exactly `n` quietly returns `n - 1`, which is
        invisible — the grid just looks one short.
        """
        return self.n + 1


def key_predicate(declared: Declared, key: str) -> str:
    """Compile a key path into an equality over EVERY identity field.

    The RENDERING is `service_kit.lancekit.keys.chunk_key_filter` — the estate's one key→predicate
    boundary, which viewer and annotator already run every identity filter through. This module used
    to render its own, quoting every segment as SQL text, and that duplication was the bug: identity
    is mixed-type by contract (a text doc key beside integer sub-keys), so `speech_id = '0'` against
    an int64 column is not a narrower filter but an expression Lance refuses outright. Every seed
    lookup on an integer-keyed corpus — the only kind `scripts/seed_demo_corpus.py` produces — came
    back 400 "seed lookup failed".

    The arity check stays HERE, because the shared helper deliberately pairs positionally with
    `strict=False` for the viewer's fixed-arity routes and cannot make it. `d1/2` against three key
    fields is a PREFIX, and a prefix matches every chunk of that speech — the seed would then be
    "whichever row Lance returned first", which is a different answer after every compaction and
    identical-looking every time.

    The key is user input reaching a SQL string; `chunk_key_filter` escapes the doc key through `eq`
    (the shared escaping boundary) and casts the rest to int, so neither segment can inject. Field
    names come from the descriptor and are trusted.
    """
    identity = declared.identity
    key_fields = identity.key_fields
    parts = key.split(KEY_SEP) if key else []
    if len(parts) != len(key_fields) or not all(parts):
        raise ValidationError(f"key {key!r} has {len(parts)} part(s); this corpus is identified by {len(key_fields)} ({KEY_SEP.join(key_fields)})")

    values = dict(zip(key_fields, parts, strict=True))
    if identity.doc_key not in values:
        # A descriptor whose identity omits its own doc key would make `chunk_key_filter` AND in a
        # clause over a column outside identity — a predicate that matches rows nobody asked for.
        raise ValidationError(f"this corpus's identity ({KEY_SEP.join(key_fields)}) does not include its doc key {identity.doc_key!r}")
    try:
        rest = [int(values[field]) for field in key_fields if field != identity.doc_key]
    except ValueError as e:
        raise ValidationError(f"key {key!r}: every identity field after {identity.doc_key!r} is an integer sub-key") from e
    return chunk_key_filter(declared, values[identity.doc_key], rest)


def seed_vector(table: Any, *, where: str, column: str) -> list[float]:
    """The seed row's own embedding, read from the declared column.

    Three refusals, each of which would otherwise be a plausible-looking wrong answer:

    * The column is absent (a stale descriptor, or a stage the pipeline has not run).
      `vector_search` degrades to `[]` in that case on purpose — for FUSION, where the other legs
      still rank. Here there is no other leg, so `[]` would be an empty answer to a question nobody
      asked.
    * No row matches. Searching from `None` returns the corpus's nearest to the ORIGIN: a list of
      unrelated rows with nothing about it that looks wrong.
    * The row exists but is not embedded. A row can land before its vectors do; that is not the same
      as the row being absent, and it is not a reason to search from nothing.
    """
    if column not in table.schema.names:
        raise ValidationError(f"this corpus declares a vector column {column!r} its table does not carry")

    try:
        rows = table.search().where(where).select([column]).limit(1).to_list()
    except Exception as e:
        logger.warning("seed lookup failed", exc_info=True)
        raise ValidationError("seed lookup failed") from e

    if not rows:
        raise NotFoundError("no row with that key")

    vec = rows[0].get(column)
    if vec is None or len(vec) == 0:
        raise NotFoundError("that row has no embedding yet — it has not been through the vector stage")
    return list(vec)


def drop_seed(hits: list[dict[str, Any]], key: str, key_fields: Sequence[str]) -> list[dict[str, Any]]:
    """Every neighbour except the seed itself.

    The seed's distance is 0, so it always ranks first — a "more like this" whose best answer is the
    thing you clicked wastes a row of the grid and reads as broken.

    Compared as STRINGS on purpose. Key fields are mixed types (a string doc id beside integer
    speech/chunk ids), Lance returns the integers as ints, and the key path is text — so `3 != "3"`
    would silently never match and the seed would survive every single time.
    """
    seed = tuple(key.split(KEY_SEP))
    return [hit for hit in hits if tuple(str(hit.get(f)) for f in key_fields) != seed]
