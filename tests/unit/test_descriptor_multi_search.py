"""A corpus can declare more than one searchable table.

`declared.search` was a single object, so a corpus exposed exactly one searchable table however many
it held — you could pick a DATASET but never a table within it. The bindings that make a table
searchable (fts, vectors, filterable) are per-TABLE, so they had to travel with the table rather than
sit beside one privileged one.

The shape mirrors `atlas`, which solved the same problem first: a named list, each entry bound to its
own table. The interesting tests are the COMPATIBILITY ones — descriptors are files written by hand
and by seeding scripts, and a contract change that needed all of them rewritten would have made this
feature's blast radius the whole corpus estate.
"""

from __future__ import annotations

from typing import Any

from service_kit.lancekit.descriptor import Declared, TableInfo, validate_descriptor
from service_kit.lancekit.introspect import ColumnInfo


IDENTITY: dict[str, Any] = {"key_fields": ["doc_id"], "doc_key": "doc_id"}


def declared(**extra: Any) -> Declared:
    return Declared.model_validate({"identity": IDENTITY, **extra})


def table(name: str, *columns: str) -> TableInfo:
    return TableInfo(
        name=name,
        row_count=1,
        version=1,
        columns=[ColumnInfo(name=c, arrow_type="string", nullable=True) for c in columns],
        indexes=[],
    )


# --------------------------------------------------------------------------------------------------
# Compatibility — every descriptor on disk keeps working
# --------------------------------------------------------------------------------------------------


def test_a_LEGACY_single_search_object_still_loads() -> None:
    """`search: {...}` is what every descriptor on disk carries. It becomes the one-entry list."""
    d = declared(search={"row_table": "chunks"})

    assert [(s.name, s.row_table) for s in d.searches] == [("default", "chunks")]


def test_the_search_PROPERTY_still_answers() -> None:
    """~15 call sites across the viewer, search service and atlas read `declared.search`. Renaming
    the field without keeping this would have broken all of them in one commit."""
    default = declared(search={"row_table": "chunks"}).search
    assert default is not None and default.row_table == "chunks"
    assert declared().search is None


def test_the_served_JSON_still_carries_search_beside_searches() -> None:
    """The frontend reads `declared.search` today. Dropping it from the wire to add a list would
    break every zone in the same commit that added the capability, so it is a COMPUTED field."""
    body = declared(search={"row_table": "chunks"}).model_dump()

    assert body["search"]["row_table"] == "chunks"
    assert [s["row_table"] for s in body["searches"]] == ["chunks"]


def test_the_legacy_key_does_not_survive_as_an_untyped_EXTRA() -> None:
    """`Declared` is `extra="allow"`. Without popping the key the translation would be cosmetic:
    `search` would linger as an unvalidated dict while the real list stayed empty."""
    d = declared(search={"row_table": "chunks"})

    assert (d.__pydantic_extra__ or {}).get("search") is None
    assert d.search is not None, "the property, not the leftover extra"


def test_an_explicit_searches_list_WINS_over_a_legacy_key() -> None:
    """A descriptor carrying both is stating the new shape; the legacy key is then a leftover."""
    d = Declared.model_validate({"identity": IDENTITY, "search": {"row_table": "old"}, "searches": [{"name": "new", "row_table": "new"}]})

    assert [s.row_table for s in d.searches] == ["new"]


# --------------------------------------------------------------------------------------------------
# The capability itself
# --------------------------------------------------------------------------------------------------


def test_several_tables_can_be_declared_and_the_FIRST_is_the_default() -> None:
    d = declared(searches=[{"name": "pages", "row_table": "pages"}, {"name": "lines", "row_table": "lines"}])

    assert [s.name for s in d.searches] == ["pages", "lines"]
    assert d.search is not None and d.search.name == "pages"


def test_a_table_resolves_BY_NAME() -> None:
    d = declared(searches=[{"name": "pages", "row_table": "pages"}, {"name": "lines", "row_table": "lines"}])

    lines, default = d.search_named("lines"), d.search_named(None)
    assert lines is not None and lines.row_table == "lines"
    assert default is not None and default.name == "pages", "None selects the default"


def test_an_UNKNOWN_name_resolves_to_None_rather_than_the_default() -> None:
    """The rule a caller must honour as a 404. Falling back to the default would answer from a
    DIFFERENT table than the one asked for — a wrong answer, not a lenient one."""
    d = declared(searches=[{"name": "pages", "row_table": "pages"}])

    assert d.search_named("lines") is None


def test_each_table_keeps_its_OWN_bindings() -> None:
    """The reason this is a list of `Search` and not a list of table names: fts and vector bindings
    are per-table, so a shared one would be wrong for every table but the first."""
    d = declared(
        searches=[
            {"name": "pages", "row_table": "pages", "fts": {"table": "pages", "column": "ocr"}},
            {"name": "lines", "row_table": "lines", "fts": {"table": "lines", "column": "text"}},
        ]
    )

    pages, lines = d.search_named("pages"), d.search_named("lines")
    assert pages is not None and pages.fts is not None and pages.fts.column == "ocr"
    assert lines is not None and lines.fts is not None and lines.fts.column == "text"


# --------------------------------------------------------------------------------------------------
# Validation covers every declared table, not just the default
# --------------------------------------------------------------------------------------------------


def test_a_SECOND_table_that_does_not_exist_is_a_violation() -> None:
    """It used to check only the default, so a bad second table loaded fine and 500'd the first time
    anyone selected it."""
    d = declared(searches=[{"name": "pages", "row_table": "pages"}, {"name": "lines", "row_table": "ghost"}])

    problems = validate_descriptor(d, {"pages": table("pages", "doc_id")})

    assert any("ghost" in p for p in problems), problems


def test_the_identity_fields_are_checked_on_EVERY_table() -> None:
    """A table you can search is a table whose rows must be addressable."""
    d = declared(searches=[{"name": "pages", "row_table": "pages"}, {"name": "lines", "row_table": "lines"}])

    problems = validate_descriptor(d, {"pages": table("pages", "doc_id"), "lines": table("lines", "text")})

    assert any("doc_id" in p and "lines" in p for p in problems), problems


def test_a_DUPLICATE_name_is_a_violation() -> None:
    """Names are the `table=` selector's keys. A duplicate makes the selector ambiguous, and which
    one it resolved to would depend on declaration order — invisibly."""
    d = declared(searches=[{"name": "pages", "row_table": "pages"}, {"name": "pages", "row_table": "lines"}])

    problems = validate_descriptor(d, {"pages": table("pages", "doc_id"), "lines": table("lines", "doc_id")})

    assert any("declared twice" in p for p in problems), problems


def test_a_valid_multi_table_corpus_has_NO_problems() -> None:
    d = declared(searches=[{"name": "pages", "row_table": "pages"}, {"name": "lines", "row_table": "lines"}])

    assert validate_descriptor(d, {"pages": table("pages", "doc_id"), "lines": table("lines", "doc_id")}) == []
