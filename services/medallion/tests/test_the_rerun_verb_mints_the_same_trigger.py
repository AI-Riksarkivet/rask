"""A re-run re-mints the SAME trigger the publication head mints, from one implementation.

O2 (`open_lakehouse_diff_left.md`): *"No cascade reconciler and no re-run verb — a missed hop is
undetectable and unrepairable."* The failure it names is a hop that NEVER RAN: `table_published` was
published, the trigger was lost, no mover woke, and no workflow instance was ever created. That is why
the verb is addressed by the EDGE and not by an instance — there is no instance to address, nothing to
load, and no `serialized_input` to replay.

So the repair is to re-publish the trigger that went missing. Which makes the trigger's SHAPE the whole
contract, and a second hand-written copy of it the obvious way to get this wrong: the mover
discriminates on `dataset` being the tier-qualified lane (`bronze$events`, the same string for every
tenant), resolves the delta with `_row_created_at_version > from AND <= to`, and reads `from_uri` as
the catalog's vended location rather than composing a path. A re-run whose trigger differed in any of
those is not a repair — it drives the wrong lane, the wrong range, or the wrong bytes.

`build_stage_trigger` is therefore the one place the shape lives, and the publication head is its first
caller rather than its owner. This module pins that the two agree, because the estate has paid for a
hand-maintained mirror before: `stage_stamp.py` exists because the in-process and Ray copies of one
transform drifted into different SCHEMAS, and its docstring says so — *"a mirror maintained by hand is
a mirror that drifts."*
"""

from __future__ import annotations

import pytest

from medallion.services.publication_trigger import DELIMITER, build_stage_trigger


def _extra(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "project": "acme",
        "from_version": 4,
        "to_version": 7,
        "location": "s3://acme-bucket/aa3bed10_acme-bronze$events",
        "cascade_id": "cas-1",
    }
    base.update(over)
    return base


def test_the_lane_is_tier_qualified_not_tenant_qualified() -> None:
    """The mover compares `dataset` against its raw `from_dataset`, so the tenant must travel in
    `project` and NOT in the lane. Publishing the catalog identifier as the lane once made every
    tenant's publication DROP as another lane's."""
    trigger = build_stage_trigger(object_id=f"table:acme-bronze{DELIMITER}events", event_id="evt-1", extra=_extra())
    assert trigger is not None
    assert trigger["dataset"] == f"bronze{DELIMITER}events"
    assert trigger["namespace"] == "bronze"
    assert trigger["project"] == "acme"


def test_the_range_is_carried_verbatim_including_a_none_floor() -> None:
    """`from_version` is None on a dataset's FIRST publication and means "everything up to `to`".
    Coercing it to 0 asserts a different thing — that a prior publication existed at version 0."""
    trigger = build_stage_trigger(object_id=f"table:acme-bronze{DELIMITER}events", event_id="evt-1", extra=_extra(from_version=None))
    assert trigger is not None
    assert trigger["from_version"] is None
    assert trigger["to_version"] == 7


def test_the_vended_location_is_carried_so_the_mover_composes_no_path() -> None:
    trigger = build_stage_trigger(object_id=f"table:acme-bronze{DELIMITER}events", event_id="evt-1", extra=_extra())
    assert trigger is not None
    assert trigger["from_uri"] == "s3://acme-bucket/aa3bed10_acme-bronze$events"


def test_the_batch_identity_and_the_human_cross_the_tier_boundary() -> None:
    """The two fields this hop used to lose. `token` is minted per publication, so without
    `cascade_id` every tier is a fresh run with nothing joining it to the ingest that started it."""
    trigger = build_stage_trigger(
        object_id=f"table:acme-bronze{DELIMITER}events",
        event_id="evt-1",
        extra=_extra(originator="CiQwOGE4Njg0Yi1kYjg4"),
    )
    assert trigger is not None
    assert trigger["cascade_id"] == "cas-1"
    assert trigger["originator"] == "CiQwOGE4Njg0Yi1kYjg4"


@pytest.mark.parametrize("absent", ["project", "cascade_id"])
def test_an_absent_field_is_OMITTED_never_blanked(absent: str) -> None:
    """`""` is not the same claim as absent, and the difference is load-bearing at both sites: the
    mover reads a missing `project` as "no tenant" and would refuse `""` as garbage, and a blank
    originator addresses an inbox actor literally named "" ."""
    extra = _extra()
    del extra[absent]
    trigger = build_stage_trigger(object_id=f"table:acme-bronze{DELIMITER}events", event_id="evt-1", extra=extra)
    assert trigger is not None
    assert absent not in trigger


def test_a_table_outside_the_cascade_yields_no_trigger() -> None:
    """Undeclared namespaces are driven nowhere — a table outside the cascade is published all the
    time, and waking a lane for it fires compute no lane owns. The CALLER decides what to do with
    None; this function does not ack, retry, or log on the caller's behalf."""
    assert build_stage_trigger(object_id="not-a-table-id", event_id="evt-1", extra=_extra()) is None


def test_the_publication_head_uses_this_function_rather_than_its_own_copy() -> None:
    """The anti-drift gate. A second hand-built trigger dict in this module means the re-run and the
    event lane can disagree about the shape, which is the defect `stage_stamp.py` was created to end."""
    import ast
    import pathlib

    source = pathlib.Path(services_dir := __file__).parent.parent / "src" / "medallion" / "services" / "publication_trigger.py"
    assert source.exists(), services_dir
    tree = ast.parse(source.read_text())
    handler = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_publication")
    builds = [n for n in ast.walk(handler) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "build_stage_trigger"]
    assert builds, "handle_publication builds its own trigger dict instead of calling build_stage_trigger"
