"""A table's unenforced primary key, held at every catalog door that lands rows ([[LH-243]]).

Lance records the key in field metadata (`lance-schema:unenforced-primary-key`,
`lance_docs/file_format.md:2887-2910`) and checks it on no write. Measured on pylance 12.0.0 against a
table that declares `id` as its key: a create whose rows repeat a key commits both, an append re-using a
held key commits it, and a merge-insert whose UNMATCHED source rows repeat a key inserts every copy. The
format leaves enforcement to the writer ("Users can use specific workloads like merge-insert to enforce
it"), and a repeat is not harmless: once a table holds two rows for one key, every later merge whose
source carries that key and matches it is refused with `Ambiguous merge inserts are prohibited`, so a
single repeat wedges every downstream full-sync merge on the key for good.

So a door that writes rows to a table declaring a key refuses a write that would repeat one, judged
twice: within the rows the write carries, and against the rows the table holds when the write lands.

- ``/insert`` appends through Lance's own enforcement, `merge_insert(keys).when_matched_fail()
  .when_not_matched_insert_all().execute(rows)`: it raises on the first held key, and a merge that loses
  a commit race re-plans against the newer version, so two inserts racing one new key land it once
  (measured on 12.0.0: 20 of 20 races clean, where a check followed by an append landed both every time).
- ``/commit`` lands client-written fragments as an Append, which has no merge form. Its rows are judged
  against the version its detached commit rebased onto, through a key-only
  `merge_insert(keys).when_matched_fail()` planned with `execute_uncommitted`, which raises on the first
  held key and writes no file (measured on 12.0.0). Both merges run on a scalar index on the key where one
  exists (Lance plans the join on the scalar-index path).

A table that declares no key is append-only by the format's definition (`file_format.md:2250-2251`)
and is not judged.

Two bounds, both measured on pylance 12.0.0:

- A key on a field nested in a struct is judged within the rows only. `merge_insert` cannot key on a
  nested field ("No field named target_k"), so Lance offers no join to judge it against the table.
- ``/commit``'s judgement and its real commit are two steps. A writer landing the same NEW key between
  them lands too: an Append rebases over a concurrent append without comparing keys
  (`lance_docs/file_format.md` § Conflict Resolution).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, cast

import pyarrow as pa
from lance.schema import LanceSchema
from lance_namespace import InvalidInputError


if TYPE_CHECKING:
    import lance


#: How many repeated or held key values a refusal names. Enough to find the rows; a refusal that
#: enumerated every repeat of a large batch would be a payload of its own.
_SAMPLE: Final = 5

#: Lance's refusal of a matched row under `when_matched_fail`, on pylance 12.0.0:
#: `Merge insert failed: found matching row with key values: id = 3, <rust source location>`.
_HELD_KEY = re.compile(r"found matching row with key values: (?P<keys>.*?)(?:, /\S+:\d+:\d+)?$", re.DOTALL)


def key_columns(schema: LanceSchema) -> list[str]:
    """The declared primary key's column paths in key order, dotted for a field inside a struct; empty when none is declared."""
    paths: dict[int, str] = {}
    pending = [(field, "") for field in schema.fields()]
    while pending:
        field, prefix = pending.pop()
        path = f"{prefix}{field.name()}"
        paths[field.id()] = path
        pending.extend((child, f"{path}.") for child in field.children())
    return [paths[field.id()] for field in schema.unenforced_primary_key()]


def declared_key(schema: pa.Schema) -> list[str]:
    """The key an Arrow schema declares, as :func:`key_columns` names it.

    A schema Lance refuses to convert (a nullable key field is the one case) declares nothing judgeable
    here: the write that follows refuses the same schema with Lance's own message.
    """
    try:
        return key_columns(LanceSchema.from_pyarrow(schema))
    except ValueError:
        return []


def _column(rows: pa.Table, path: str) -> pa.Array | pa.ChunkedArray:
    head, *rest = path.split(".")
    column: pa.Array | pa.ChunkedArray = rows.column(head)
    for name in rest:
        column = cast(pa.StructArray, column.combine_chunks() if isinstance(column, pa.ChunkedArray) else column).field(name)
    return column


def _key_rows(rows: pa.Table, keys: Sequence[str]) -> pa.Table:
    return pa.table({key: _column(rows, key) for key in keys})


def _carries(rows: pa.Table, keys: Sequence[str]) -> bool:
    """Whether ``rows`` carry every key column: rows that do not are a schema mismatch, which the write refuses itself."""
    return bool(keys) and all(key.split(".", 1)[0] in rows.column_names for key in keys)


def refuse_repeats_within(rows: pa.Table, keys: Sequence[str], *, door: str) -> None:
    """Refuse ``rows`` when two of them carry one value of the key ``keys`` names.

    Raises:
        InvalidInputError: a key value repeats within ``rows``; nothing was written.
    """
    if rows.num_rows < 2 or not _carries(rows, keys):
        return
    counts = _key_rows(rows, keys).group_by(list(keys)).aggregate([([], "count_all")])
    if counts.num_rows < rows.num_rows:
        repeated = [{key: group[key] for key in keys} for group in counts.to_pylist() if group["count_all"] > 1]
        sample = repeated[:_SAMPLE]
        raise InvalidInputError(
            f"{door} refused: {len(repeated)} value(s) of the table's primary key ({', '.join(keys)}) repeat within the rows written, e.g. {sample}. "
            "A repeated key makes every later merge on it ambiguous; send each key once"
        )


def joinable(rows: pa.Table, keys: Sequence[str]) -> bool:
    """Whether Lance can join ``rows`` to the table on ``keys``: a declared key the rows carry, on no nested field."""
    return _carries(rows, keys) and not any("." in key for key in keys)


@contextmanager
def _refusing_held_keys(door: str) -> Iterator[None]:
    """Answer Lance's `when_matched_fail` refusal as the caller's mistake it is, naming the held key."""
    try:
        yield
    except OSError as exc:
        held = _HELD_KEY.search(str(exc))
        if held is None:
            raise
        raise InvalidInputError(
            f"{door} refused: the table already holds a row with primary key {held.group('keys')}. "
            "An append may only add new keys; change an existing row with merge_insert or update"
        ) from exc


def append_new_keys(dataset: lance.LanceDataset, rows: pa.Table, keys: Sequence[str], *, door: str) -> None:
    """Append ``rows`` through ``dataset``, refusing the write if the table holds one of their keys when it lands.

    ``dataset`` advances to the version this append committed. The caller checks :func:`joinable` first.

    Raises:
        InvalidInputError: the table holds a row with one of the key values; nothing was written.
    """
    with _refusing_held_keys(door):
        dataset.merge_insert(list(keys)).when_matched_fail().when_not_matched_insert_all().execute(rows)


def refuse_keys_held(dataset: lance.LanceDataset, rows: pa.Table, keys: Sequence[str], *, door: str) -> None:
    """Refuse ``rows`` when the table at ``dataset``'s version already holds one of their key values.

    A key on a nested field is not judged here (see the module docstring).

    Raises:
        InvalidInputError: ``dataset`` holds a row with one of the key values; nothing was written.
    """
    if rows.num_rows == 0 or not joinable(rows, keys):
        return
    with _refusing_held_keys(door):
        dataset.merge_insert(list(keys)).when_matched_fail().execute_uncommitted(_key_rows(rows, keys))
