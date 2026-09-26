"""Main is ONE ref whether it is spelled ``None`` or ``"main"``, and a ref is a str or nothing.

Lance records main as null — a tag's ``branch`` and a branch's ``parentBranch``
(``lance_docs/file_format.md`` "Tag File Format" / "Branch Metadata File Format") — while a request may
name it ``"main"``. ``dataplane.recorded_branch`` folds both spellings into Lance's, and the catalog
compares refs through it, so a second spelling of main can never read as a second branch. A value that
is neither a str nor None is the caller's type error, not a ref.
"""

from __future__ import annotations

import pytest

from catalog.services import erasure
from catalog.services.dataplane import recorded_branch


@pytest.mark.parametrize(("spelled", "recorded"), [(None, None), ("main", None), ("work", "work"), ("feature/x", "feature/x")])
def test_a_ref_is_recorded_as_lance_records_it(spelled: str | None, recorded: str | None) -> None:
    assert recorded_branch(spelled) == recorded


@pytest.mark.parametrize("wrong", [3, b"main", ("main",)])
def test_a_ref_that_is_neither_a_str_nor_None_is_a_TypeError(wrong: object) -> None:
    with pytest.raises(TypeError, match="str or None"):
        recorded_branch(wrong)


@pytest.mark.parametrize(("branch_key", "version_key"), [("branch", "version"), ("parent_branch", "parent_version")])
def test_an_erasure_reference_names_main_None_by_construction(branch_key: str, version_key: str) -> None:
    """``_Reference`` names main ``None``, so ``"main"`` in a tag or fork record reads as main.

    Measured on pylance 12.0.0, Lance records main as null for a tag or a branch created from ``("main", n)``,
    ``(None, n)`` or a bare ``n``, so no real dataset reaches this spelling; this pins the alias's invariant.
    """
    meta = {branch_key: "main", version_key: 2}

    assert erasure._reference(meta, branch_key=branch_key, version_key=version_key) == (None, 2)
