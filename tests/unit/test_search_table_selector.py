"""Searching ONE of a corpus's several searchable tables.

The descriptor can now declare several (`Declared.searches`). This is the half that lets a caller
pick one: `?table=<name>`, with the corpus's default when omitted.

The test that matters is the REFUSAL. An unknown table must be a 400 naming what is declared, never
a quiet fall-back to the default — a search that answered from a different table than the one asked
for is a wrong answer, and it would be invisible in the results.
"""

from __future__ import annotations

from typing import Any

import pytest
from search.services.spec import SearchSpec
from search.services.target import resolve_target

from service_kit.exceptions import ValidationError
from service_kit.lancekit.descriptor import DatasetDescriptor


def _descriptor(searches: list[dict[str, Any]]) -> DatasetDescriptor:
    return DatasetDescriptor.model_validate(
        {
            "id": "corpus",
            "tables": {
                name: {
                    "name": name,
                    "row_count": 1,
                    "version": 1,
                    "columns": [
                        {"name": "doc_id", "arrow_type": "string", "nullable": False},
                        {"name": "text", "arrow_type": "string", "nullable": True},
                    ],
                    "indexes": [],
                }
                for name in {s["row_table"] for s in searches}
            },
            "declared": {
                "identity": {"key_fields": ["doc_id"], "doc_key": "doc_id"},
                "searches": searches,
            },
        }
    )


class _Table:
    """A LanceDB table stand-in. `row_ds` is typed `Any` on `SearchTarget` and only carried through
    to the query layer, so a sentinel is enough — and naming it makes an accidental use obvious."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.version = 1

    def to_lance(self) -> str:
        return f"lance-dataset:{self.name}"


class _Handle:
    """The bits of `DatasetHandle` that `resolve_target` touches.

    A structural stand-in, not a subclass: building a real handle needs a Lance DB on disk, and the
    function under test only reads `descriptor`, `db.open_table` and `sync_table_info`. Call sites
    carry `# ty: ignore[invalid-argument-type]` for exactly that reason.
    """

    def __init__(self, descriptor: DatasetDescriptor) -> None:
        self.descriptor = descriptor
        self.opened: list[str] = []
        self.db = self

    def open_table(self, name: str) -> _Table:
        self.opened.append(name)
        if name not in self.descriptor.tables:
            raise FileNotFoundError(f"table {name} not found")
        return _Table(name)

    def sync_table_info(self, _name: str, _version: int) -> None:
        return None


PAGES_AND_LINES = [
    {"name": "pages", "row_table": "pages", "filterable": ["language"]},
    {"name": "lines", "row_table": "lines", "filterable": ["hand"]},
]


def test_omitting_the_table_selects_the_corpus_DEFAULT() -> None:
    handle = _Handle(_descriptor(PAGES_AND_LINES))

    target = resolve_target(handle, None)  # ty: ignore[invalid-argument-type]

    assert target.row_table_name == "pages"


def test_naming_a_table_searches_THAT_table() -> None:
    """The capability, stated directly: it used to be impossible."""
    handle = _Handle(_descriptor(PAGES_AND_LINES))

    target = resolve_target(handle, "lines")  # ty: ignore[invalid-argument-type]

    assert target.row_table_name == "lines"


def test_each_table_brings_its_OWN_filterable_fields() -> None:
    """`filterable` is declared per table. Carrying the default's list onto another table would
    accept filters for fields the searched rows do not have, and drop the ones they do."""
    handle = _Handle(_descriptor(PAGES_AND_LINES))

    pages = resolve_target(handle, "pages")  # ty: ignore[invalid-argument-type]
    lines = resolve_target(handle, "lines")  # ty: ignore[invalid-argument-type]

    assert pages.filterable == ["language"]
    assert lines.filterable == ["hand"]


def test_an_UNKNOWN_table_is_refused_and_names_what_IS_declared() -> None:
    """No fall-back. Answering from the default would be a wrong answer that looks like a right one,
    and a refusal that does not name the alternatives cannot be acted on."""
    handle = _Handle(_descriptor(PAGES_AND_LINES))

    with pytest.raises(ValidationError) as caught:
        resolve_target(handle, "ghost")  # ty: ignore[invalid-argument-type]

    message = str(caught.value)
    assert "ghost" in message
    assert "pages" in message and "lines" in message


def test_an_unknown_table_never_OPENS_the_default() -> None:
    """The refusal must happen before any table is opened — otherwise a typo silently costs a Lance
    open, and worse, a partially-built target could still be returned by a later refactor."""
    handle = _Handle(_descriptor(PAGES_AND_LINES))

    with pytest.raises(ValidationError):
        resolve_target(handle, "ghost")  # ty: ignore[invalid-argument-type]

    assert handle.opened == []


def test_a_corpus_with_NO_search_block_still_says_so() -> None:
    """The pre-existing message, unchanged — a different failure from naming an unknown table."""
    handle = _Handle(_descriptor([]))

    with pytest.raises(ValidationError) as caught:
        resolve_target(handle, None)  # ty: ignore[invalid-argument-type]

    assert "no search bindings" in str(caught.value)


def test_a_LEGACY_single_table_corpus_resolves_exactly_as_before() -> None:
    """Every descriptor on disk carries `search: {...}`. The selector must not have changed what
    they do."""
    descriptor = DatasetDescriptor.model_validate(
        {
            "id": "corpus",
            "tables": {
                "chunks": {
                    "name": "chunks",
                    "row_count": 1,
                    "version": 1,
                    "columns": [{"name": "doc_id", "arrow_type": "string", "nullable": False}],
                    "indexes": [],
                }
            },
            "declared": {
                "identity": {"key_fields": ["doc_id"], "doc_key": "doc_id"},
                "search": {"row_table": "chunks"},
            },
        }
    )

    target = resolve_target(_Handle(descriptor), None)  # ty: ignore[invalid-argument-type]

    assert target.row_table_name == "chunks"


def test_the_spec_carries_the_selector_on_the_wire() -> None:
    """`table` rides beside `dataset` on the same query string — the table is part of WHAT is being
    searched, not a filter on it."""
    assert SearchSpec().table is None
    assert SearchSpec.model_validate({"table": "lines"}).table == "lines"
