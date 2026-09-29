"""Main is ONE ref whether it is spelled ``None`` or ``"main"``, and a ref is a str or nothing.

Lance records main as null — a tag's ``branch`` and a branch's ``parentBranch``
(``lance_docs/file_format.md`` "Tag File Format" / "Branch Metadata File Format") — while a request may
name it ``"main"``. ``dataplane.recorded_branch`` folds both spellings into Lance's, and the catalog
compares refs through it, so a second spelling of main can never read as a second branch. A value that
is neither a str nor None is the caller's type error, not a ref.
"""

from __future__ import annotations

import pytest

from catalog.services.dataplane import recorded_branch


@pytest.mark.parametrize(("spelled", "recorded"), [(None, None), ("main", None), ("work", "work")])
def test_a_ref_is_recorded_as_lance_records_it(spelled: str | None, recorded: str | None) -> None:
    assert recorded_branch(spelled) == recorded


@pytest.mark.parametrize("wrong", [b"main"])
def test_a_ref_that_is_neither_a_str_nor_None_is_a_TypeError(wrong: object) -> None:
    with pytest.raises(TypeError, match="str or None"):
        recorded_branch(wrong)
