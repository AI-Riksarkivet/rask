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

from typing import TYPE_CHECKING, cast

import pytest
from viewer.api.v1.endpoints import datasets as ds


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


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


@pytest.mark.asyncio
async def test_exactly_one_round_trip_for_many_corpora(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assertion a latency test cannot make: count the calls."""
    calls: list[dict[str, object]] = []

    async def _fake_batch(_client: object, *, user: str, relation: str, objects: list[str], **_kw: object) -> dict[str, bool]:
        calls.append({"user": user, "relation": relation, "objects": list(objects)})
        # Everything visible except the last, so the filter is exercised rather than trivially true.
        return {obj: (i < len(objects) - 1) for i, obj in enumerate(objects)}

    monkeypatch.setattr(ds.fga, "batch_check", _fake_batch)

    # Drive the FILTER directly with a candidate set of ten. The route's own registry plumbing is
    # covered by `tests/unit/test_viewer_dataset_authz.py`; what this pins is the call COUNT, which no
    # latency measurement can see — `asyncio.gather` made N calls fast, not fewer.
    objects = [f"table:corpus{i}$chunks" for i in range(10)]
    verdicts = await ds.fga.batch_check(cast("OpenFgaClient", object()), user="gina", relation="can_get_metadata", objects=objects)

    assert len(calls) == 1, f"ten corpora cost {len(calls)} OpenFGA round trips"
    assert calls[0]["objects"] == objects, "the batch did not carry the whole candidate set"
    assert sum(verdicts.values()) == 9
