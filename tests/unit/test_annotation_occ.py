"""Optimistic concurrency on the annotation write path.

`check_base_version_value` is the entire guarantee that two people editing the same page do not
silently overwrite each other, and it had NO unit coverage — only `tests/e2e-py`, which needs a live
stack and so never runs in the fast lane. A concurrency guard nobody exercises is a comment.

The last test is the uncomfortable one and it is deliberately written to PASS: it pins that a caller
omitting `base_version` opts out of the check entirely. That is the current contract, documented in
the function's own docstring ("`None` skips it — last-write-wins per id"), and stating it in a test
is the difference between a known trade-off and a hole nobody has looked at.
"""

from __future__ import annotations

import pytest
from annotator.annotations.commit import check_base_version_value

from service_kit.exceptions import ConflictError


def test_a_write_at_the_version_it_loaded_is_allowed() -> None:
    check_base_version_value(current=7, base_version=7)


def test_a_write_against_a_STALE_version_is_refused() -> None:
    """The whole point: the table moved under the client between its read and its write."""
    with pytest.raises(ConflictError):
        check_base_version_value(current=8, base_version=7)


def test_the_refusal_names_BOTH_versions() -> None:
    """ "annotations changed" alone leaves someone unable to tell a lost race from a stale tab. The
    two numbers say which, and how far behind."""
    with pytest.raises(ConflictError) as exc:
        check_base_version_value(current=8, base_version=7)

    assert "7" in str(exc.value)
    assert "8" in str(exc.value)


def test_a_version_AHEAD_of_the_server_is_also_refused() -> None:
    """Not `<`, but `!=`. A client claiming a version the table has never reached is not a fresh
    write — it is a client reading a different table, or a replayed request. Accepting it because
    it is not "behind" would let the one case nobody predicted through."""
    with pytest.raises(ConflictError):
        check_base_version_value(current=7, base_version=9)


def test_version_zero_is_a_VERSION_not_a_missing_value() -> None:
    """The falsy-vs-None trap. `if base_version:` would treat version 0 as "not supplied" and skip
    the check on the first write of a table — exactly where two concurrent creators race."""
    with pytest.raises(ConflictError):
        check_base_version_value(current=1, base_version=0)

    check_base_version_value(current=0, base_version=0)


def test_an_ABSENT_base_version_opts_OUT_of_concurrency_control() -> None:
    """The known hole, pinned so it is a decision rather than a surprise.

    `None` skips the check and the write becomes last-write-wins per id. The main client does send
    the field (`annotations-client.ts` passes `base_version: args.version`), so this is not the
    common path — but `tag-writer.ts` declares it optional, and any caller that omits it silently
    gets no protection at all.

    The task actor faced the same question for drafts and answered it the other way: "An OPTIONAL
    precondition is not a precondition" — it exempts only the FIRST write, when there is nothing to
    be stale against, and requires the field thereafter. Making this path match is a wire-visible
    change (`SaveAnnotations.base_version` is `int | None = None`), so it is the owner's call, not a
    silent tightening.
    """
    check_base_version_value(current=99, base_version=None)
