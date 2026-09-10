"""Bronze is the cascade's ROOT tier, so the ingest plane does not ask the catalog to publish it.

Publication is the quality gate on a PROMOTION — a stage runner offering silver or gold for the tier
above. Bronze is promoted from nothing: the cascade ANNOUNCES it with an event (`/bronze-arrival`
reacts to the write, and that is what drives bronze -> silver), and nothing downstream waits on a
bronze publish.

MEASURED 2026-09-10, and the estate has been proving this the noisy way. A real lane run reached
COMPLETE with 4 rows at version 2 and carried
`publish_error: catalog refused the publish (400): version 2 carries some governed-tier columns but is
not a conforming tier: missing 'lineage'; missing 'source_rowid'`. The gate is right to refuse: bronze
descends from nothing, so `source_rowid` and `lineage` are meaningless on it, while `stage` legitimately
names which tier it is — the cascade's OWN bronze carries exactly the same three-column shape
(`['stage']`, read off `s3://lance-catalog/medallion/bronze`). So every successful ingest ended by
asking for something that cannot be granted and recording the refusal as if it were a fault.

`published=None`, NOT `False`, and the model says why in as many words: the tri-state's third value
means the gate never ran, and "collapsing it to `False` would report a quality gate that never ran" as
a refusal. For bronze the gate genuinely never runs. The reason travels with it so the record says
which of the three it is.

THIS FILE REPLACES `test_run_publishes.py`, which opened "A run does not merely commit — it asks the
catalog to PUBLISH what it committed" and pinned five behaviours of that call: the publish itself, the
range it reports, a refused gate, an unreachable catalog, and a seam with no publish door. Every one of
them asserted a call that no longer happens, so the file was deleted rather than patched — its premise
is what the owner's ruling reversed.

The one property in it worth carrying forward is that a landed commit is never turned into a failed run
by anything that happens at this seam. It is now structural rather than defended: there is no call to
fail. The tests below hold the two halves that remain — that nothing is asked, and that the record says
so honestly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ingest import runtime
from ingest.catalog import ServiceCatalogSeam
from ingest.runtime import PublishSpec


class _CatalogThatWouldPublish(ServiceCatalogSeam):
    """A REAL seam that records any attempt, not a bare stub.

    Subclassing the protocol rather than duck-typing is the protocol's own instruction — `PublishSpec`
    records what trusting a double cost once, and `ty` refuses a stand-in that does not satisfy the
    seam. A double that merely returned success would also let a call through unnoticed, which is the
    one thing these tests exist to detect.
    """

    def __init__(self) -> None:
        self.attempts: list[tuple[str, str, int]] = []

    def publish(self, namespace: str, dataset: str, version: int, *, key_column: str = "id", required_columns: Sequence[str] = ()) -> dict[str, Any]:
        self.attempts.append((namespace, dataset, version))
        return {"published": True, "from_version": 1, "to_version": version}


@dataclass(frozen=True)
class _Spec:
    """A stand-in CHECKED against `PublishSpec` rather than trusted — the protocol's own docstring
    records what trusting one cost: a double offering `project` where the plane had moved to
    `namespace` swallowed an AttributeError into the catalog-cannot-publish branch, and the run
    reported a plausible refusal while nothing raised."""

    run_id: str
    namespace: str
    dataset: str


def _spec() -> PublishSpec:
    return _Spec(run_id="r1", namespace="lane-bronze", dataset="lane-bronze$pages")


def test_the_catalog_is_never_asked_to_publish_bronze() -> None:
    catalog = _CatalogThatWouldPublish()

    runtime._publish(catalog, _spec(), 2)

    assert catalog.attempts == [], "the ingest plane asked the catalog to publish a root tier"


def test_the_record_says_the_gate_did_not_run_rather_than_that_it_refused() -> None:
    """`False` is a refusal and would be a lie: nothing refused this, nothing was asked."""
    result = runtime._publish(_CatalogThatWouldPublish(), _spec(), 2)

    assert result["published"] is None, "a gate that never ran was recorded as a refusal"
    assert result.get("publish_error") is None, "an error was recorded for a call that was never made"
    assert "bronze" in str(result.get("publish_reason", "")).lower(), "the record does not say WHY the gate did not run"
