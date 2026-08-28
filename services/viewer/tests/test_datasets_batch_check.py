"""The corpus listing must ask OpenFGA once, not once per corpus.

open_fastapi-audit — "The viewer's corpus-list filter fans out N single `check` calls where
`fga.batch_check` is the estate's own pattern for the same question".

`list_datasets` is the FIRST call every zone makes on page load. It issued one `checker(...)` per
corpus through `asyncio.gather` — so latency was fine, and the inline comment reasons carefully about
exactly that — but the ROUND-TRIP COUNT is N per user per page load, N OpenFGA requests over the SDK's
aiohttp pool where one BatchCheck answers the same question. The comment never engages with
`batch_check` existing in the module it already imports.

`authz.md` is explicit: "Prefer `batch_check` over many `check`s when filtering — same network
round-trip cost as one call", and a filtered list is its named example. The estate agrees with itself
twice already — `lineage/api/fga_deps.py` batch-checks `can_get_metadata` for `DatasetFilter.visible`,
and `notifications/api/visibility.py` says "one round-trip over the whole candidate set, never one
check per object".

CONCURRENT IS NOT THE SAME AS ONE. That is the distinction this gate exists to hold: `asyncio.gather`
made N calls cheap in wall-clock while leaving them N calls, and a test that measured latency would
have called the old code correct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from viewer.api.v1.endpoints import datasets as ds


if TYPE_CHECKING:
    pass


def test_the_listing_batches_its_authorization() -> None:
    import inspect

    source = inspect.getsource(ds.list_datasets)
    assert "batch_check" in source, (
        "the corpus listing still makes one check per corpus — N OpenFGA round trips on the first call "
        "every zone makes, where one BatchCheck answers the same question"
    )
    # Comments stripped: the fix's own note EXPLAINS the gather it removed, and matching that text would
    # fail against the corrected code — the same false-positive class as grepping for a docstring claim
    # that a correction quotes.
    code = "\n".join(line for line in source.split("\n") if not line.strip().startswith("#"))
    assert "asyncio.gather" not in code, "the per-corpus gather is still there; concurrent is not the same as one — it makes N calls cheap, not fewer"


# `test_exactly_one_round_trip_for_many_corpora` lived here and is DELETED, not repaired. It
# monkeypatched `ds.fga.batch_check` and then awaited `ds.fga.batch_check` directly — a test of its
# own mock, structurally unable to go RED (found by the adversarial re-audit of this finding's
# closure). The property it claimed to pin — ten corpora, ONE OpenFGA round trip — is now pinned
# through the endpoint in `tests/unit/test_viewer_dataset_authz.py::
# test_ten_corpora_cost_ONE_openfga_round_trip`, whose fake counts INVOCATIONS of the module-level
# `batch_check` while a real request walks the route.
