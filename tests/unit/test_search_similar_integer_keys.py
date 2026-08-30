""" "More like this" must work on an INTEGER-keyed corpus — the only kind this repo can seed.

`scripts/seed_demo_corpus.py` declares `key_fields = [doc_id, speech_id, chunk_id]` with `doc_id`
utf8 and both sub-keys int64, and it says why in its own comment: "speech_id/chunk_id are INTEGERS:
the viewer builds its frame filter as `speech_id = 0 AND chunk_id = 19` with unquoted numeric
literals, so string columns" would not match. Every other service renders identity predicates
through `service_kit.lancekit.keys.chunk_key_filter`, which casts the sub-keys to int for exactly
that reason.

`search.services.similar` grew its OWN renderer and quoted every segment as SQL text, so
`/api/explorer/search/similar` compiled `speech_id = '0'` against an int64 column. Lance refuses the
expression outright ("Received literal Utf8(\"0\") and could not convert"), `seed_vector` catches it
and the wire answers 400 "seed lookup failed" — for every seed row, on the only corpus this repo can
produce. The duplication IS the defect, so these pin both halves: what the predicate renders, and
that a real Lance table actually returns the seed row for it.
"""

from __future__ import annotations

from typing import Any

import lancedb
import pyarrow as pa

from search.services.similar import key_predicate, seed_vector
from service_kit.lancekit.descriptor import Declared


#: The seeded corpus's identity, verbatim: a text doc key plus two int64 sub-keys.
KEY_FIELDS = ["doc_id", "speech_id", "chunk_id"]
DECLARED = Declared.model_validate({"identity": {"key_fields": KEY_FIELDS, "doc_key": "doc_id"}})

_DIM = 4


def _seeded_table(path: Any) -> Any:
    """A table shaped like the seeded corpus's `chunks`: utf8 doc key, int64 sub-keys, a vector."""
    schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("speech_id", pa.int64()),
            pa.field("chunk_id", pa.int64()),
            pa.field("embedding", pa.list_(pa.float32(), _DIM)),
        ]
    )
    rows = pa.table(
        {
            "doc_id": pa.array(["fe00cd746463ad2c", "fe00cd746463ad2c", "a7419c0b2d5e8f31"]),
            "speech_id": pa.array([0, 0, 1], pa.int64()),
            "chunk_id": pa.array([19, 20, 1], pa.int64()),
            "embedding": pa.array([[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8], [0.9, 1.0, 1.1, 1.2]], pa.list_(pa.float32(), _DIM)),
        },
        schema=schema,
    )
    return lancedb.connect(str(path)).create_table("chunks", rows)


def test_an_integer_key_field_is_rendered_as_a_NUMBER_not_as_quoted_text() -> None:
    """`speech_id = '0'` is not a narrower filter than `speech_id = 0` — it is an unresolvable
    expression against an int64 column, which is why this surfaced as a 400 rather than as an empty
    grid."""
    where = key_predicate(DECLARED, "fe00cd746463ad2c/0/19")

    assert "speech_id = 0" in where, f"the integer sub-key was quoted as text: {where}"
    assert "chunk_id = 19" in where, f"the integer sub-key was quoted as text: {where}"
    assert "'0'" not in where and "'19'" not in where, f"an integer key field is still SQL text: {where}"


def test_the_seed_row_is_actually_FOUND_in_a_real_lance_table(tmp_path: Any) -> None:
    """The end of the wire, against real Lance rather than a stand-in: the predicate this module
    renders must select the seed the caller named. A double cannot see this — only the engine
    refuses `Utf8` against `Int64`."""
    table = _seeded_table(tmp_path / "db")

    where = key_predicate(DECLARED, "fe00cd746463ad2c/0/19")
    vec = seed_vector(table, where=where, column="embedding")

    assert len(vec) == _DIM
    assert vec[0] == pa.scalar(0.1, pa.float32()).as_py(), "the predicate selected a row that is not the seed"
