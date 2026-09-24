"""A dataset stamped with a relative ``source_uri`` can be corrected without inventing a run.

[[LH-141]]. 23 `:Dataset` nodes carry a RELATIVE `source_uri` — `'4750a5b9_acme-bronze$events'`,
`'silver/consensus-live-41965_…'`, `'transcripts_v2.lance/chunks.lance'` — and the reconcile reports each
one every tick as "names no storage location — a relative path cannot say whether the data is there".
Re-measured live 2026-09-23: still exactly 23, a closed historical population (`register_table`, the one
door that took a caller-supplied location, resolves before emitting since [[LH-187]]).

NOTHING CORRECTS THEM, which is the defect. The guard refuses the crossing on every pass and no writer
ever comes: `ensure_declared_dataset_id` runs only from the medallion's write paths, and a dataset whose
bytes are never rewritten is never re-stamped. The row is not "the guard is wrong" — the guard is right
and pinned — it is that a refusal with no repair path is a permanent warning.

THE REPAIR MUST NOT MINT A RUN, and that is the whole reason this needs its own builder.
`build_maintenance_event` produces a `RunEvent` with a per-table `job`, so restamping 23 datasets
through it would plant 23 `(:Run)` nodes for an operation nobody performed and 23 `(:Job)`s whose output
set the `/jobs` governance fold turns into an access handle. `DatasetEvent` is the OpenLineage static
metadata event -- "a dataset change that no job performed" -- and the lineage side already accepts it
end to end: `consumer._parse` discriminates on `dataset` without `run`, `enforce_bus_authz` authorizes
it, and `repository.ingest_dataset_event` -> `_merge_dataset` runs
``MATCH (d:Dataset) WHERE d.name = $name SET d.source_uri=$src`` -- an UNCONDITIONAL set, so a correct
absolute URI overwrites a relative one. Every hop exists except a producer.
"""

from __future__ import annotations

import pytest

from lineage.models import DatasetEvent, parse_event
from maintenance.core.lineage_emit import build_restamp_event


ABSOLUTE = "s3://acme-bucket/acme-bronze/events.lance"


def _event() -> dict[str, object]:
    return build_restamp_event(
        table_id="acme-bronze$events",
        namespace="lance",
        source_uri=ABSOLUTE,
        event_time="2026-09-23T19:00:00Z",
        author="service-maintenance",
    )


def test_the_restamp_is_a_STATIC_event_and_mints_no_run_or_job() -> None:
    """A run for an operation nobody performed is an access-control object, not untidiness."""
    payload = _event()
    assert "run" not in payload, f"the restamp carries a run, so it plants a phantom `(:Run)`: {payload}"
    assert "job" not in payload, f"the restamp carries a job, whose output set the `/jobs` fold makes an access handle: {payload}"


def test_the_restamp_PARSES_as_a_DatasetEvent_on_the_lineage_side() -> None:
    """The producer and the consumer must agree, and only the real discriminator can show that.

    `_parse` is the function the bus door actually calls, so this is the hop that decides whether the
    repair lands as a static change or is re-admitted as a run.
    """
    parsed = parse_event(_event())
    assert isinstance(parsed, DatasetEvent), f"the lineage consumer did not read this as a static change: {type(parsed).__name__}"


def test_the_restamp_CARRIES_the_absolute_location_where_the_repository_reads_it() -> None:
    """`_merge_dataset` reads `Dataset.source_uri`, which reads the standard `dataSource` facet.

    A builder that put the URI anywhere else would satisfy every other assertion here and correct
    nothing, because the SET would never run.
    """
    parsed = parse_event(_event())
    # NARROWED, not cast: `_parse` answers `RunEvent | DatasetEvent`, and a restamp that came back as a
    # run would fail here for the RIGHT reason rather than on a missing attribute.
    assert isinstance(parsed, DatasetEvent), f"the restamp did not parse as a static change: {type(parsed).__name__}"
    assert parsed.dataset.source_uri == ABSOLUTE, (
        f"the `dataSource` facet does not carry the absolute location, so `SET d.source_uri` never runs: {parsed.dataset.facets}"
    )
    assert parsed.dataset.name == "acme-bronze$events"


def test_a_RELATIVE_uri_is_REFUSED_at_the_builder() -> None:
    """The repair may only ever write an absolute location.

    Restamping one relative value with another is indistinguishable from success at every later hop --
    the node keeps failing the same guard on the same tick -- so the refusal belongs here, where the
    caller can still fix it, rather than in the graph where it is silent.
    """
    with pytest.raises(ValueError, match="absolute"):
        build_restamp_event(
            table_id="acme-bronze$events",
            namespace="lance",
            source_uri="acme-bronze/events.lance",
            event_time="2026-09-23T19:00:00Z",
        )
