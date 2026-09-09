"""The one component that REWRITES BYTES must appear in the compliance trail.

`governed/audit.py` exists so a security-relevant action is a filterable, retainable record: `audit_read`
opens with the argument — *"the estate already authorizes every read and then forgot it happened … that
is a zero-trust gap as much as a feature one"*. Compaction is the same argument with more at stake: it
merges fragments, reclaims versions and deletes files, and `services/maintenance` held **zero** `audit()`
call sites, so none of it reached the stream (§ Q17-36, and § Q17-27's stated residual).

WHY NOT EVERY DATASET. The sweep walks ~423 datasets a tick and most are already converged, so a record
per dataset per tick is thousands an hour that say nothing — the § Q17-26 flood, in the very stream that
flood was measured in. `_did_material_work` is the estate's existing answer to exactly this question and
is reused rather than restated: the lineage emit is gated on it for the same reason, so the audit trail
and the graph agree about what counted as work.

WHAT THIS DOES NOT CLOSE. § Q17-36 also wants attempt counts, duration and a per-object FGA-gated fact a
TENANT can query. This record is operator-global and lives on a 14-day trace TTL, so it answers "did the
estate rewrite anything, when, and to what" and not "how many times did MY table's compaction fail".
That half needs a durable record kind and is an owner decision, not a call site.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from maintenance.services.sweep import audit_material_work
from service_kit.governed.audit import AUDIT_LOGGER


if TYPE_CHECKING:
    from maintenance.services.optimize import DatasetResult


class _Result:
    """Only the fields the record names — a real `DatasetResult` needs a whole sweep to build.

    CAST rather than constructed: the subject here is which fields reach the record and when it fires,
    and a double carrying exactly those is what makes a missing field a failure rather than a default.
    """

    uri = "s3://lance-catalog/medallion/bronze"
    declared_table_id = "bronze$events"
    compaction_mode = "in_pod"
    fragments_removed = 3
    fragments_added = 1
    old_versions_removed = 5
    bytes_removed = 7567
    indices_optimized = 2


class _Idle(_Result):
    fragments_removed = 0
    old_versions_removed = 0


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == AUDIT_LOGGER]


def test_a_rewrite_is_recorded_against_the_object_it_rewrote(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_material_work(cast("DatasetResult", _Result()), subject="service-maintenance")

    got = _records(caplog)
    assert len(got) == 1, f"expected one audit record for a material rewrite, got {len(got)}"
    rec = got[0]
    assert getattr(rec, "audit.resource") == "table:bronze$events", "the record must name the FGA object, not the URI"
    assert getattr(rec, "audit.subject") == "service-maintenance"
    assert getattr(rec, "audit.outcome") == "success"
    # The numbers are the whole point: "it ran" is not an answer to "what did it do to my table".
    assert getattr(rec, "audit.fragments_removed") == 3
    assert getattr(rec, "audit.old_versions_removed") == 5
    assert getattr(rec, "audit.bytes_removed") == 7567


def test_a_converged_dataset_records_NOTHING(caplog) -> None:
    """~423 datasets a tick are already converged; a record each would drown the stream it joins."""
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        audit_material_work(cast("DatasetResult", _Idle()), subject="service-maintenance")

    assert not _records(caplog), "an idle dataset wrote an audit record — that is thousands an hour saying nothing"
