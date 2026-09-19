"""A door's refusal must name ITS reason, not a neighbour's.

[[LH-019]]. `refuse_a_branch_this_door_cannot_honour` bakes one sentence into every caller: "the
underlying implementation answers from the main branch regardless of `branch`, so honouring the
parameter here would return main's rows labelled as …". That is exactly true of the query and index
doors it was written for — verified live 2026-08-31, `/query?branch=work` returned main's three rows
twice, once for a branch that had never existed.

IT IS FALSE OF THE MAINTENANCE DOORS, and measured so. `maintenance/compact` does not answer from main:
`open_dataset` takes a `branch` and its sibling `maintenance/preview` now uses it. What stops compaction
on a branch is a different fact entirely — a branch manifest REGISTERS ITS PARENT AS A BASE (measured on
pylance 11.0.0: `manifest_base_paths` is `[]` on main and `['<parent>']` on the branch, with inherited
fragments at `base_id: 0`), so a rewrite there would materialise the parent's bytes into `tree/<branch>/`.
That is the flag-16 cost refusal the sweep already makes, and it is why this door declines rather than
being unfinished.

A refusal that gives the wrong cause is worse than a terse one: it sends the caller to the wrong remedy,
and it reads as a gap someone should close when the answer is settled.
"""

from __future__ import annotations

import pytest

from catalog.services import dataplane
from service_kit.lakehouse.ns_errors import UnsupportedOperationError


def _refusal(*, reason: str) -> str:
    """The message `maintenance/compact` raises for a named reason — typed, not splatted.

    An untyped `**kwargs` here would have needed a `# type: ignore` to pass, which is exactly the
    escape this estate forbids: the signature is the contract under test, so a test that erases it
    proves less than the one it replaced.
    """
    with pytest.raises(UnsupportedOperationError) as caught:
        dataplane.refuse_a_branch_this_door_cannot_honour("work", door="maintenance/compact", reason=reason)
    return str(caught.value)


def test_the_query_family_keeps_its_own_reason() -> None:
    """The control: the sentence is right for the doors it was written for, and must not be lost."""
    with pytest.raises(UnsupportedOperationError) as caught:
        dataplane.refuse_a_branch_this_door_cannot_honour("work", door="query_table")

    assert "answers from the main branch" in str(caught.value)


def test_a_door_may_state_a_DIFFERENT_reason() -> None:
    """The maintenance doors decline for a cause the shared sentence gets wrong."""
    message = _refusal(reason="a compaction on a branch would materialise the parent's bytes into it")

    assert "materialise the parent's bytes" in message
    assert "answers from the main branch" not in message, "the door's own reason was appended to a false one rather than replacing it"


def test_the_refused_branch_is_still_named() -> None:
    """Whatever the reason, the caller must see which ref was refused."""
    assert "work" in _refusal(reason="anything")
