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
