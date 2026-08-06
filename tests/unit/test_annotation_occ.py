"""Optimistic concurrency on the annotation write path.

`check_base_version_value` is the entire guarantee that two people editing the same page do not
silently overwrite each other, and it had NO unit coverage — only `tests/e2e-py`, which needs a live
stack and so never runs in the fast lane. A concurrency guard nobody exercises is a comment.

The hole this file used to pin OPEN is now closed on the path that needed it (#50). `base_version`
was optional everywhere, so any caller could opt out of concurrency control by omitting the field —
and the explorer's tag export did exactly that.

The fix is deliberately NOT uniform, because the two write paths cannot destroy the same things:

* `save.py` commits a `merge_upsert` — matched rows are UPDATED, so a save can overwrite another
  annotator's fields. The precondition is REQUIRED there.
* `tags.py` commits `merge_insert_only` — matched rows are left AS-IS, so an add cannot clobber
  anything, and its OCC is table-GLOBAL across a cross-unit batch. Requiring it would let any
  unrelated save fail an entire bulk tagging run for no safety gained, so it stays optional.

The last test pins that exemption so it stays a reasoned decision rather than drifting back into an
oversight.
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


def test_an_ABSENT_base_version_is_REFUSED_where_the_write_can_CLOBBER() -> None:
    """The hole is CLOSED (#50). It used to be pinned open, deliberately, as a documented
    trade-off — `None` skipped the check and the write became last-write-wins per id.

    An optional precondition is not a precondition: any caller could opt out of concurrency
    control by not sending the field, and one did. The explorer's tag export
    (`ExportNode.svelte`) posted `{adds, removes, dataset}` with no `base_version` at all, so that
    entire path ran unprotected — two annotators, or one person in two tabs, could silently
    overwrite each other exactly where this check was supposed to stop them.

    There is no first-write exemption to make here, unlike the draft actor's: `table_dataset` 404s
    when the annotations table is absent, so by the time this runs the table always exists and
    always has a version — and the client always has one, because the GET that served the
    annotations carried it.
    """
    with pytest.raises(ConflictError):
        check_base_version_value(current=99, base_version=None, required=True)


def test_the_missing_precondition_names_the_CURRENT_version_and_the_remedy() -> None:
    """A refusal that only says "missing base_version" makes the caller do a round trip to find out
    what to send. The current version is already in hand here, so the error carries it and names the
    field — the client can act on the message alone."""
    with pytest.raises(ConflictError) as exc:
        check_base_version_value(current=42, base_version=None, required=True)

    assert "42" in str(exc.value)
    assert "base_version" in str(exc.value)


def test_a_missing_precondition_is_distinguishable_from_a_STALE_one() -> None:
    """Both are 409 (one client error path, one remedy), but they are different mistakes: one caller
    never sent the field, the other sent a version that has since moved. Collapsing their messages
    would make a client bug that omits the field look like ordinary contention."""
    with pytest.raises(ConflictError) as missing:
        check_base_version_value(current=42, base_version=None, required=True)
    with pytest.raises(ConflictError) as stale:
        check_base_version_value(current=42, base_version=41)

    assert str(missing.value) != str(stale.value)


def test_the_TAG_path_keeps_the_precondition_optional_and_that_is_deliberate() -> None:
    """The reasoned exemption, pinned so it stays a decision.

    `tags.py` commits `merge_insert_only` — matched rows are left AS-IS, so an add cannot overwrite
    anyone's work by construction; that is the same property that makes a re-save idempotent. Its OCC
    is also table-GLOBAL across a cross-unit batch, so requiring the field would let any unrelated
    save anywhere in the table fail an entire bulk tagging run, for no safety gained.

    A STALE version is still refused on that path — omitting the field is exempt, lying about it is
    not.
    """
    check_base_version_value(current=99, base_version=None)

    with pytest.raises(ConflictError):
        check_base_version_value(current=99, base_version=98)
