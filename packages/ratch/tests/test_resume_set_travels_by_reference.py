"""A resume set must not be copied into every Ray task and every actor in the pool.

Found by the Ray design-patterns audit (2026-08-28) against two of ray-project's own docs, which
name the same hazard from two directions:

* `patterns/closure-capture-large-objects.rst` — a large object captured by a remote function is
  cloudpickled into EVERY task. `run_append_rows_stage` closed `_drop_done` over `done`, the set of
  every key tuple already in the OUTPUT table, and the inline comment asserted it was "small (key
  tuples only)". Nothing bounded it: it grows with the output table, not with the pending work.
* `patterns/pass-large-arg-by-value.rst` — anything over ~100 KB should be `ray.put` once and shared
  by reference. `_build_scan_column_by_rowid` and `run_blob_column_stage` both pass
  `done_ids=frozenset(value_by_row_id)` through `fn_constructor_kwargs` to a pool of actors, so the
  checkpoint of every row id already computed is copied once per actor.

Both are paid exactly when the job is already recovering from a failure, which is the worst moment
to add tens of MB of scheduling payload — Ray's own doc warns a large serialized function causes
slow scheduling or worker OOM.

The measurement here is the SERIALIZED SIZE of what gets shipped, because that is the thing the
patterns are about. The object-store hop is injected (`_object_store_put`) so the property can be
proven without standing up a cluster: what matters is that the payload stops carrying the set, not
which store holds it.
"""

from __future__ import annotations

import pytest
from ratch.core import driver

# `ray.cloudpickle` and not stdlib `pickle`: cloudpickle is what Ray actually serializes a task
# closure with, and stdlib pickle cannot serialize a closure at all — measuring with it would test
# the wrong serializer for exactly the case the finding is about.
from ray import cloudpickle


_BIG = 200_000
#: Comfortably above anything a pointer plus bookkeeping could weigh, far below what the set weighs.
_SMALL_ENOUGH = 64 * 1024


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stand in for the object store: `put` returns a handle, `get` resolves it."""
    kept: dict[str, object] = {}

    def put(value: object) -> str:
        handle = f"ref-{len(kept)}"
        kept[handle] = value
        return handle

    monkeypatch.setattr(driver, "_object_store_put", put)
    monkeypatch.setattr(driver, "_object_store_get", lambda ref: kept[ref])
    return kept


def test_a_large_resume_set_ships_as_a_pointer(fake_store: dict) -> None:
    """The core property: what crosses the wire must not scale with the output table."""
    resume = driver.Resume(range(_BIG))
    payload = cloudpickle.dumps(resume)

    assert len(payload) < _SMALL_ENOUGH, (
        f"the resume set serialized to {len(payload)} bytes — it is being copied into every task/actor rather than shared through the object store"
    )
    assert len(fake_store) == 1, "the set was put into the store once per serialization rather than once"


def test_it_still_answers_membership_after_travelling(fake_store: dict) -> None:
    """A pointer nobody can dereference would be a smaller wrong answer."""
    revived = cloudpickle.loads(cloudpickle.dumps(driver.Resume(range(_BIG))))
    assert 0 in revived
    assert _BIG - 1 in revived
    assert _BIG + 5 not in revived
    assert len(revived) == _BIG


def test_a_small_set_still_rides_inline(fake_store: dict) -> None:
    """A `ray.put` for a hundred keys costs more than it saves — and a fresh run has none at all."""
    revived = cloudpickle.loads(cloudpickle.dumps(driver.Resume(range(10))))
    assert not fake_store, "a small resume set was pushed to the object store for no gain"
    assert 3 in revived and 99 not in revived


def test_the_dereferenced_set_is_not_shipped_onward(fake_store: dict) -> None:
    """A worker that resolved the set must not then carry it in its own pickle — that would put the
    whole set back on the wire one hop later, which is the same defect with an extra step."""
    revived = cloudpickle.loads(cloudpickle.dumps(driver.Resume(range(_BIG))))
    assert 7 in revived, "dereference it, so the cache is populated"
    assert len(cloudpickle.dumps(revived)) < _SMALL_ENOUGH, "the resolved set was re-serialized instead of the pointer"


def test_the_append_stage_filter_carries_no_set(fake_store: dict) -> None:
    """closure-capture-large-objects, at the site the finding names: the map_batches function itself."""
    fn = driver.drop_done_rows(driver.Resume(range(_BIG)), ["doc_id"])
    assert len(cloudpickle.dumps(fn)) < _SMALL_ENOUGH, "the map_batches filter closes over the whole resume set"


def test_an_empty_resume_is_falsy() -> None:
    """`run_append_rows_stage` skips the filter stage entirely when nothing is done yet."""
    assert not driver.Resume([])
    assert driver.Resume([1])
