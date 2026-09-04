"""A stage run records WHICH SOURCE VERSIONS it consumed, not only which version it wrote.

Found while building the cascade lag detector (docs/DECISIONS.md "Cascade repair" (C3)) and it blocks it. The
detector's predicate is "the source's `published` version versus the highest source version the
destination has actually consumed". The first half is available (`publication.published_version`). The
second half had **no source at all**:

* the trigger carries `from_version`/`to_version`, then is acked and gone;
* `submit_stage_job` exports it as `BASE_VERSION` into the Ray job's environment, where it becomes a
  read predicate (`_row_created_at_version > base`) and nothing more;
* `PublishOutcome.from_version`/`to_version` are the DESTINATION's own published span — what this run
  wrote, never what it read;
* the `lance` run facet carries `operation`, `version`, `token`, `cascade_id`, `project`,
  `originator`, `promotion_status`, `duration_seconds`, `synthetic` — and no consumed range.

So the delta boundary that decides what a stage reads is, today, unauditable after the fact: nothing
can answer "did silver ever consume bronze version 7?" That is a gap in provenance before it is a gap
in monitoring — `stage=silver lane=delta` off `BASE_VERSION=106` is a claim no store can corroborate.

The facet is the right home: it already carries `version` (what was written), so the read range sits
beside the write it produced, keyed by the same run id. Bounded and low-cardinality — two integers per
run, never a per-row value.

OMITTED, never zero-filled. `from_version` is None on a first publication and means "everything up to
`to`"; writing 0 asserts a prior publication at version 0 that did not happen — the same rule
`build_stage_trigger` already applies on the wire.
"""

from __future__ import annotations

from typing import Any

from medallion.schemas.events import build_run_event


def _facet(*, from_version: int | None = None, to_version: int | None = None) -> dict[str, Any]:
    """The `lance` run facet of a stage COMPLETE event.

    Named parameters rather than a `**kwargs` splat: `build_run_event` is fully typed, and splatting an
    untyped mapping into it erases every one of those signatures — which is what made the first version
    of this helper need a suppression. Typing the two arguments the module actually varies costs one
    line and keeps the call checkable.
    """
    event = build_run_event(
        operation="embed_features",
        author="data_eng",
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="silver",
        output_name="silver$features",
        version=12,
        from_version=from_version,
        to_version=to_version,
    )
    facets = event["run"]["facets"]
    assert "lance" in facets, facets
    lance: dict[str, Any] = facets["lance"]
    return lance


def test_the_consumed_range_reaches_the_facet() -> None:
    lance = _facet(from_version=4, to_version=7)
    assert lance["from_version"] == 4
    assert lance["to_version"] == 7


def test_the_written_version_is_still_the_destination_s_own() -> None:
    """`version` and `to_version` answer different questions and must not be conflated: one is what
    this run WROTE, the other the last source version it READ."""
    lance = _facet(from_version=4, to_version=7)
    assert lance["version"] == 12


def test_a_first_publication_omits_the_floor_rather_than_zeroing_it() -> None:
    lance = _facet(from_version=None, to_version=7)
    assert "from_version" not in lance, "None means 'everything up to to'; 0 claims a prior publication"
    assert lance["to_version"] == 7


def test_a_run_with_no_range_carries_neither_key() -> None:
    """A full rescan and a promotion carry no range. Absent is the honest answer; a sentinel would be
    indistinguishable from a real boundary."""
    lance = _facet()
    assert "from_version" not in lance
    assert "to_version" not in lance


def test_the_stage_emit_actually_PASSES_the_range() -> None:
    """A facet that accepts the fields and a producer that never sends them is the estate's recurring
    'wired but inert' shape: every test green, the graph unchanged, the detector blind.

    Asserted structurally because the failure is a MISSING argument — an emit that stopped passing the
    range is indistinguishable, from the event alone, from a run that genuinely had none.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).parent.parent / "src" / "medallion" / "services" / "transform.py"
    tree = ast.parse(source.read_text())
    stage_emits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_run_event"
        and any(kw.arg == "column_map" for kw in node.keywords)  # the stage COMPLETE emit, not the FAIL one
    ]
    assert stage_emits, "no stage build_run_event call found — this gate would pass vacuously"
    passed = {kw.arg for call in stage_emits for kw in call.keywords}
    assert {"from_version", "to_version"} <= passed, (
        "the stage completion emit no longer passes the consumed range, so the delta boundary is unrecorded and the cascade lag detector has nothing to read"
    )
