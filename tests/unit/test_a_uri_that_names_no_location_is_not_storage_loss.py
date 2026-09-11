"""A relative `source_uri` is a malformed name, not evidence that data was destroyed.

`read_storage_version` answers `None` for a relative path — measured, both `'medallion/bronze'` and
`'t.lance'` do — and `None` classifies MISSING_ON_STORAGE, which the sweep reports as storage loss. So a
dataset whose graph URI was recorded relative is reported as DESTROYED on every tick, permanently,
because nothing rewrites the URI.

IT IS NOT HYPOTHETICAL AND IT IS NOT RARE. Counted over the live graph 2026-09-11: **60 of 1162**
Dataset nodes carry a relative `source_uri` — `silver/loop-1785786423_cae1f8ffb5a1`,
`transcripts_v2.lance/chunks.lance`, and one literally named `probe-relative-loc`. The producer that
made them is fixed forward (`cf040fff`, the register door echoed the caller's own path), but the nodes
keep the value they were given.

THE MODULE ALREADY RULES ON THIS EXACT QUESTION, one layer down: `read_storage_version`'s own docstring
says an unsupported feature flag, missing credentials or a bad endpoint must raise rather than answer
absent, "because the caller classifies a `None` as MISSING_ON_STORAGE and reporting 'we could not open
it' as 'it was destroyed' is a false alarm an operator acts on. Measured 2026-08-26: six live datasets
reported as storage loss while `services/maintenance` was reading their manifests in the same hour." A
URI that names no storage location at all is the same case, arriving one step earlier — the open never
had a chance to say anything about the data.

WHY NOT JUST FIX THE 60. Repairing them needs an authoritative location source and a place to put the
repair, which is an open design question (LH-141). Classifying honestly needs neither, cannot be wrong
about the data, and stops the alarm lying in the meantime.

WHAT IS DELIBERATELY NOT DONE HERE: guessing. A relative URI is NOT resolved against a configured root —
a warehouse-bound table belongs to its warehouse's root, and a join against the wrong one would produce
a confident, wrong absolute URI where today there is an honest failure.
"""

from __future__ import annotations

import pytest

from lineage.core.reconcile import StorageUnreadable, read_storage_version


@pytest.mark.parametrize("uri", ["medallion/bronze", "t.lance", "silver/loop-1785786423_cae1f8ffb5a1", "probe-relative-loc"])
def test_a_relative_uri_reads_as_unreadable_not_absent(uri: str) -> None:
    """THE GATE. `None` here means "the data is gone"; this URI cannot support that claim."""
    with pytest.raises(StorageUnreadable) as raised:
        read_storage_version(uri, {})

    assert uri in str(raised.value), "the reason must name the URI, since the URI is the defect"


@pytest.mark.parametrize("uri", ["s3://bucket/9f_ns$t", "file:///tmp/x.lance", "/tmp/x.lance"])
def test_a_uri_that_names_a_location_is_still_opened(uri: str) -> None:
    """The other half: this must not become a guard that refuses real locations.

    Each of these names somewhere storage could be. They go to the opener, which answers absent or
    unreadable on its own evidence — a missing dataset under a real root is still a genuine `None`.
    """
    try:
        read_storage_version(uri, {})
    except StorageUnreadable as exc:
        assert "names no storage location" not in str(exc), f"{uri} names a location and must reach the opener"
