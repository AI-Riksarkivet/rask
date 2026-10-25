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

The `absent-on-the-tag-path` case pins that exemption so it stays a reasoned decision rather than
drifting back into an oversight.
"""

from __future__ import annotations

import re

import pytest

from annotator.annotations.commit import check_base_version_value
from service_kit.exceptions import ConflictError


@pytest.mark.parametrize(
    ("current", "base_version"),
    [
        pytest.param(7, 7, id="the-version-it-loaded"),
        # The falsy-vs-None trap: `if base_version:` would treat version 0 as "not supplied" and skip
        # the check on the first write of a table, exactly where two concurrent creators race.
        pytest.param(0, 0, id="version-zero"),
        # The tag path leaves `required` off: `merge_insert_only` cannot overwrite anyone's work, so
        # OMITTING the field is exempt there. A stale version is still refused on that path (below).
        pytest.param(99, None, id="absent-on-the-tag-path"),
    ],
)
def test_a_write_at_its_loaded_version_or_without_one_where_it_cannot_clobber_is_allowed(current: int, base_version: int | None) -> None:
    check_base_version_value(current=current, base_version=base_version)


@pytest.mark.parametrize(
    ("current", "base_version", "required", "message"),
    [
        # The table moved under the client between its read and its write. Both numbers are named, so
        # a lost race reads differently from a stale tab, and says how far behind.
        pytest.param(8, 7, False, "annotations changed on the server (loaded v7, now v8)", id="stale"),
        # Not `<` but `!=`: a version the table has never reached is a client reading a different
        # table, or a replayed request.
        pytest.param(7, 9, False, "annotations changed on the server (loaded v9, now v7)", id="ahead-of-the-server"),
        pytest.param(1, 0, False, "annotations changed on the server (loaded v0, now v1)", id="version-zero-is-a-version"),
        # Required where the write can clobber (`save.py`'s merge_upsert). A different mistake from a
        # stale version, so a different message, and it names the current version and the field: the
        # caller can act on the message alone.
        pytest.param(
            42,
            None,
            True,
            "this save did not state the version it was built from; the table is at v42. Re-read the annotations and send that version as base_version.",
            id="absent-where-the-write-can-clobber",
        ),
    ],
)
def test_a_write_at_another_version_or_without_a_required_one_is_refused_and_says_which(
    current: int, base_version: int | None, required: bool, message: str
) -> None:
    with pytest.raises(ConflictError, match=f"^{re.escape(message)}$"):
        check_base_version_value(current=current, base_version=base_version, required=required)
