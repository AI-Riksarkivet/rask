"""A table's unenforced primary key, held at every catalog door that lands rows ([[LH-243]]).

Lance records the key in field metadata (`lance-schema:unenforced-primary-key`,
`lance_docs/lance/format/table/index.md:81-104`) and checks it on no write. Measured on pylance 12.0.0 against a
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
- Every commit is judged before its finalize by :func:`refuse_repeats_in_version`: the rows of the version's new
  fragments within themselves and against the rows it carries over. ``version/create`` judges a client-staged version
  this way (``catalog.services.managed_versions``), and so does every catalog door's commit
  (``catalog.services.dataplane.commit_stamped``, [[LH-540]]): a ``/merge_insert`` whose ``on`` is not the key, or holds
  more than the key, inserts every source row its fields leave unmatched, whatever key that row carries, and an
  ``/update`` may set a key column. The ``/insert`` merge runs on a scalar index on the key where one exists (Lance plans
  the join on the scalar-index path).

A table that declares no key is append-only by the format's definition (`lance_docs/lance/format/index/system/mem_wal.md:7-8`)
and is not judged.

Two bounds, both measured on pylance 12.0.0:

- ``/insert`` judges a key on a field nested in a struct within its rows only: `merge_insert` cannot key on a
  nested field ("No field named target_k"), so Lance offers no join to judge it against the table. The commit is
  then judged by :func:`refuse_repeats_in_version`, which reads the nested key through the scanner.
- ``version/create`` judges the version that follows the one it was built on, and the backend's
  put-if-not-exists then decides the race: a writer that lost it re-stages against the newer version, and
  that version is judged again.
"""

from __future__ import annotations

import re
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, cast

import pyarrow as pa
import pyarrow.compute as pc
from lance.schema import LanceSchema
from lance_namespace import InvalidInputError


if TYPE_CHECKING:
    import lance
    from lance.dataset import Transaction
    from lance.fragment import LanceFragment


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


def _top_columns(keys: Sequence[str]) -> list[str]:
    """The top-level columns holding the key paths, each once, in key order."""
    return list(dict.fromkeys(key.split(".", 1)[0] for key in keys))


def _carries(rows: pa.Table, keys: Sequence[str]) -> bool:
    """Whether ``rows`` carry every key column: rows that do not are a schema mismatch, which the write refuses itself."""
    return bool(keys) and all(column in rows.column_names for column in _top_columns(keys))


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
def _refusing_held_keys(door: str) -> Generator[None]:
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


def plan_new_keys(dataset: lance.LanceDataset, rows: pa.Table, keys: Sequence[str], *, door: str) -> tuple[Transaction, dict[str, Any]]:
    """Plan the append of ``rows`` on ``dataset`` through Lance's ``when_matched_fail`` merge, uncommitted, with its row counts.

    Raises:
        InvalidInputError: ``dataset`` holds a row with one of the key values.
    """
    with _refusing_held_keys(door):
        return dataset.merge_insert(list(keys)).when_matched_fail().when_not_matched_insert_all().execute_uncommitted(rows)


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


def fragments_since(proposed: lance.LanceDataset, followed: lance.LanceDataset | None) -> tuple[list[LanceFragment], list[LanceFragment]]:
    """``proposed``'s new fragments and the ones it carries: a fragment is new when ``followed``, the version ``proposed``
    follows (``None`` for a table's first), holds no fragment of its id."""
    held = {fragment.fragment_id for fragment in followed.get_fragments()} if followed is not None else set()
    fragments = proposed.get_fragments()
    return [fragment for fragment in fragments if fragment.fragment_id not in held], [fragment for fragment in fragments if fragment.fragment_id in held]


def refuse_repeats_in_version(proposed: lance.LanceDataset, followed: lance.LanceDataset | None, *, door: str) -> None:
    """Refuse ``proposed`` when two rows of its new fragments, or one of them and a row it carries, share a key value.

    New and carried are :func:`fragments_since`'s. Measured on pylance 13.0.0, ``update`` and ``merge_insert`` write every
    row they change or insert to a new fragment and delete it from the fragment it left, so a key an update sets or a merge
    inserts is read among the new rows, and the row it replaced is not read among the carried ones. A key on a field nested
    in a struct is read through its struct, so the carried rows are narrowed by the key only where its first field is a
    column.

    Raises:
        InvalidInputError: a key value repeats; nothing was committed.
    """
    keys = key_columns(proposed.lance_schema)
    if not keys:
        return
    new, carried = fragments_since(proposed, followed)
    if not new:
        return
    rows = proposed.scanner(columns=_top_columns(keys), fragments=new).to_table()
    refuse_repeats_within(rows, keys, door=door)
    if not carried or rows.num_rows == 0 or not _carries(rows, keys):
        return
    written, first = _key_rows(rows, keys), keys[0]
    narrowed = pc.field(first).isin(written.column(first).unique()) if "." not in first else None
    kept = _key_rows(proposed.scanner(columns=_top_columns(keys), fragments=carried, filter=narrowed).to_table(), keys)
    if kept.num_rows == 0:
        return
    shared = written.join(kept, keys=list(keys), join_type="inner")
    if shared.num_rows:
        raise InvalidInputError(
            f"{door} refused: the table already holds a row with primary key {shared.slice(0, 1).to_pylist()[0]}, which this write would repeat. "
            "A repeated key makes every later merge on it ambiguous; change the existing row with merge_insert on the key, or update"
        )
