"""A branch name Lance refuses is the caller's mistake, never `Internal 18`.

[[LH-046]]. The spec's own vocabulary decides this: code **13 InvalidInput** is "malformed request or
invalid parameters" and code **18 Internal** is an "unexpected server/implementation error"
(`lance_docs/ns_catalog/spec.yaml`, error-code list). A name the caller typed is the first, and a
generated client dispatches on the code — so answering 18 tells it the server broke, which is
unretryable, unactionable, and indistinguishable from a real fault in an alerting pipeline.

`_classify_ref_error` already reads Lance's OWN validation (`Ref is invalid: …`), which covers most bad
names: spaces, `~ ^ : ? * [ ] @`, a trailing `/`, a `.lock` suffix and a literal backslash all arrive
with that prefix and map to InvalidInput. Measured against pylance 11.0.0 on 2026-09-15.

THREE NAMES DO NOT, AND THEY ARE THE ORDINARY ONES. Measured the same way:

* ``"main"`` — `branches.list()` does NOT contain `main` on any dataset (the default ref is implicit),
  so the door's collision pre-check misses it, pylance is called, and it answers
  ``OSError("Encountered internal error. Please file a bug report …")``.
* ``""`` — the spec marks `name` required but sets no `minLength`, so an empty string passes model
  validation and reaches pylance, which answers the same bug-report text.
* ``"../escape"`` — refused by the object-store PATH parser before Lance's ref validation runs, so the
  message is `LanceError(IO): Error parsing Path …` and carries no `Ref is invalid:` marker.

None of the three contains the marker, so all three fall through as Internal 18 today. They are
refused AT THE DOOR rather than by matching those messages, because the message for the first two is
pylance's bug-report text — not a stable discriminator, and the day upstream fixes that panic a matcher
keyed on it silently reports Internal again. That is the same reasoning `create_branch` already gives
for establishing a collision by READING rather than by matching.
"""

from __future__ import annotations

import pytest
from lance_namespace import InvalidInputError, TableBranchAlreadyExistsError

from catalog.services import dataplane
from catalog.services.dataplane import refuse_a_branch_name_the_backend_cannot_use


def test_the_reserved_main_ref_cannot_be_created() -> None:
    """`main` always exists implicitly, so asking to create it is the spec's 23, not a server fault."""
    with pytest.raises(TableBranchAlreadyExistsError, match="main"):
        refuse_a_branch_name_the_backend_cannot_use("main")


def test_an_empty_name_is_refused_as_invalid_input() -> None:
    """The spec marks `name` required but sets no minLength, so `""` reaches the backend unvalidated."""
    with pytest.raises(InvalidInputError):
        refuse_a_branch_name_the_backend_cannot_use("")


@pytest.mark.parametrize("name", ["../escape", "a/../b", ".."])
def test_a_traversal_segment_is_refused_as_invalid_input(name: str) -> None:
    """Refused before the object-store path parser sees it, which is where the message stops being readable."""
    with pytest.raises(InvalidInputError):
        refuse_a_branch_name_the_backend_cannot_use(name)


@pytest.mark.parametrize("name", ["work", "feature/x", "dot.name", "UPPER", "-lead", "v1.2.3"])
def test_a_name_the_backend_accepts_is_left_alone(name: str) -> None:
    """The guard must not become a second, stricter grammar than Lance's own.

    Every name here was driven against pylance 11.0.0 and CREATED successfully, including the ones a
    hand-written pattern would plausibly reject — a slash, a leading dash, an upper-case letter. Lance
    owns the grammar; this door only covers the three cases its errors are unreadable for.
    """
    refuse_a_branch_name_the_backend_cannot_use(name)


@pytest.mark.parametrize("name", ["a b", "tilde~1", "caret^", "colon:x", "star*"])
def test_a_name_lance_itself_rejects_is_left_to_lance(name: str) -> None:
    """These arrive as `Ref is invalid: …` and `_classify_ref_error` already maps them to InvalidInput.

    Pinned as a NEGATIVE so nobody widens this guard into a duplicate of Lance's grammar: two copies of
    that rule would drift, and the copy here would be the one nobody tests against a new pylance.
    """
    refuse_a_branch_name_the_backend_cannot_use(name)


def test_classify_ref_error_invalid_name_names_the_rejected_branch() -> None:
    # A branch create scopes its errors to the SOURCE (right for a missing version), so a malformed
    # new name was reported as `invalid branch name 'main'` — measured on S3 2026-09-20.
    err = dataplane._classify_ref_error(OSError("Ref is invalid: Branch segment 'has space'"), kind="branch", name="main", invalid_name="has space")

    assert "'has space'" in str(err)
    assert "'main'" not in str(err)


def test_classify_ref_error_invalid_name_defaults_to_the_scope_name() -> None:
    err = dataplane._classify_ref_error(OSError("Ref is invalid: bad"), kind="tag", name="v1")

    assert "'v1'" in str(err)
