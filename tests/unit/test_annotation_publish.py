"""Publish → the lakehouse: `DESIGN-annotation-projects.md` §7, as executable contract.

The publish decision is pure, so these can be exhaustive about the two things that would be expensive
to discover in a cluster: work reaching the lakehouse that should not have, and provenance that is
wrong or missing once it is there.

The sharpest case is `skipped`, and it cuts BOTH ways. It is terminal, so the project actor allows
the publish — but its draft shapes must not land (an annotator drew them, then decided the item did
not belong), *and* it must still leave one sentinel row, or the project's decisions are incomplete on
the record and a consumer cannot build an exclusion set. "No shapes" and "no row" are different
claims and only one of them is true.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from annotator.projects.models import AnnotationProject, Draft, LabelClass, LabelSchema, Shape, Task, TaskState
from annotator.projects.publish import (
    NO_SHAPE,
    PROJECT_FACET,
    PUBLISHED_COLUMNS,
    PublishRefusal,
    build_plan,
    project_facet,
    table_properties,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
PUBLISH_ID = "pub-1"


def _project(**kw: Any) -> AnnotationProject:
    base: dict[str, Any] = {
        "project_id": "p1",
        "tenant": "acme",
        "slug": "vasa-portraits",
        "review_required": True,
        "label_schema": LabelSchema(classes=[LabelClass(name="ship"), LabelClass(name="person")]),
    }
    base.update(kw)
    return AnnotationProject.model_validate(base)


def _task(task_id: str, state: TaskState, **kw: Any) -> Task:
    base: dict[str, Any] = {
        "task_id": task_id,
        "project_id": "p1",
        "state": state,
        "source": {"kind": "chunks", "keys": [f"key/{task_id}"], "where": "transcripts_v2"},
        "media": {"kind": "image", "image_url": f"s3://b/{task_id}.jpg"},
    }
    if state is TaskState.ACCEPTED:
        base.setdefault("submitted_by", "gina")
        base.setdefault("submitted_at", NOW)
        base.setdefault("reviewed_by", "carol")
        base.setdefault("reviewed_at", NOW)
        base.setdefault("review_action", "accepted")
    base.update(kw)
    return Task.model_validate(base)


def _draft(task_id: str, n: int, author: str = "gina") -> Draft:
    return Draft(
        task_id=task_id,
        project_id="p1",
        author=author,
        shapes=[Shape(shape_type="bbox", x=float(i), y=0.0, width=1.0, height=1.0, label="ship") for i in range(n)],
        revision=1,
    )


def _plan(pairs: list[tuple[Task, Draft | None]], project: AnnotationProject | None = None) -> Any:
    return build_plan(project or _project(), pairs, publish_id=PUBLISH_ID, published_at=NOW)


# --------------------------------------------------------------------------------------------------
# What lands — §7.1
# --------------------------------------------------------------------------------------------------


def test_an_accepted_task_contributes_one_row_per_shape() -> None:
    plan = _plan([(_task("t0", TaskState.ACCEPTED), _draft("t0", 3))])
    assert plan.shape_count == 3
    assert len(plan.rows) == 3
    assert {r["task_outcome"] for r in plan.rows} == {"accepted"}


def test_a_skipped_task_contributes_a_sentinel_row_and_none_of_its_shapes() -> None:
    """Both halves of the rule at once. The draft exists and holds five shapes; none may land, and
    exactly one sentinel must, or the project's decisions are incomplete on the record."""
    plan = _plan([(_task("t0", TaskState.SKIPPED), _draft("t0", 5))])

    assert len(plan.rows) == 1, "a skipped task's draft shapes reached the lakehouse"
    row = plan.rows[0]
    assert row["shape_type"] == NO_SHAPE
    assert row["task_outcome"] == "skipped"
    assert row["annotation_id"] == ""
    assert plan.shape_count == 0, "a sentinel was counted as a label"
    assert plan.skipped_count == 1


def test_shape_count_excludes_sentinels_so_a_dataset_is_not_overstated() -> None:
    """Counting sentinels as labels would inflate every published dataset by its skip rate."""
    plan = _plan(
        [
            (_task("t0", TaskState.ACCEPTED), _draft("t0", 2)),
            (_task("t1", TaskState.SKIPPED), _draft("t1", 9)),
            (_task("t2", TaskState.SKIPPED), None),
        ]
    )
    assert plan.shape_count == 2
    assert len(plan.rows) == 4  # 2 shapes + 2 sentinels
    assert plan.accepted_count == 1 and plan.skipped_count == 2


def test_an_accepted_task_with_no_shapes_is_recorded_as_a_decision() -> None:
    """ "Nothing to annotate here" is a real outcome. It leaves a row, not a hole."""
    plan = _plan([(_task("t0", TaskState.ACCEPTED), None)])
    assert len(plan.rows) == 1
    assert plan.rows[0]["shape_type"] == NO_SHAPE
    assert plan.rows[0]["task_outcome"] == "accepted"
    assert plan.shape_count == 0


@pytest.mark.parametrize("state", [TaskState.UNASSIGNED, TaskState.CLAIMED, TaskState.IN_REVIEW, TaskState.CHANGES_REQUESTED])
def test_a_non_terminal_task_refuses_the_publish_loudly(state: TaskState) -> None:
    """The project actor's precondition already refused this, so reaching here is a programming
    error. Skipping it quietly would publish a partial dataset that looks complete."""
    with pytest.raises(PublishRefusal, match="not terminal"):
        _plan([(_task("t0", state), _draft("t0", 1))])


def test_every_row_carries_exactly_the_published_schema_columns() -> None:
    """A row dict and the Arrow schema the workflow builds must not drift — a missing key becomes a
    null column and an extra key an unexpected-field error, both at write time in a cluster."""
    plan = _plan([(_task("t0", TaskState.ACCEPTED), _draft("t0", 1)), (_task("t1", TaskState.SKIPPED), None)])
    for row in plan.rows:
        assert tuple(row.keys()) == PUBLISHED_COLUMNS


def test_operational_state_never_reaches_the_lakehouse() -> None:
    """§7.1's deliberate absences: task state, assignee, leases, drafts, revisions, transitions. That
    is the annotator's own state, and the boundary is the point of the whole design."""
    plan = _plan([(_task("t0", TaskState.ACCEPTED, assignee="gina"), _draft("t0", 1))])
    forbidden = {"state", "assignee", "lease_expires_at", "revision", "transitions", "review_notes", "submitted_at"}
    assert not (set(plan.rows[0]) & forbidden)


# --------------------------------------------------------------------------------------------------
# Whose names travel with it
# --------------------------------------------------------------------------------------------------


def test_row_provenance_comes_from_the_task_not_the_payload() -> None:
    """The shape payload is client-supplied; `annotated_by` must not be."""
    draft = _draft("t0", 1)
    draft.shapes[0].attributes = {"annotated_by": "mallory"}

    row = _plan([(_task("t0", TaskState.ACCEPTED, submitted_by="gina", reviewed_by="carol"), draft)]).rows[0]

    assert row["annotated_by"] == "gina", "a client-supplied field overwrote server-recorded provenance"
    assert row["reviewed_by"] == "carol"
    assert row["review_action"] == "accepted"
    assert json.loads(row["attributes"]) == {"annotated_by": "mallory"}, "the client's claim belongs in attributes, not in provenance"


def test_the_drafter_is_carried_separately_from_the_submitter() -> None:
    """Under `fix_and_accept` the REVIEWER edits the shapes. One collapsed "who did this" field would
    credit the wrong person — in either direction."""
    task = _task("t0", TaskState.ACCEPTED, submitted_by="gina", reviewed_by="carol", review_action="fix_and_accept")
    plan = _plan([(task, _draft("t0", 1, author="carol"))])

    a = plan.attributions[0]
    assert (a.annotated_by, a.drafted_by, a.review_action) == ("gina", "carol", "fix_and_accept")


def test_an_accepted_task_with_no_submitter_refuses_the_publish() -> None:
    """Anonymous provenance in the lakehouse is worse than no publish: once written it is
    indistinguishable from the real thing."""
    with pytest.raises(PublishRefusal, match="anonymous"):
        _plan([(_task("t0", TaskState.ACCEPTED, submitted_by=None), _draft("t0", 1))])


def test_a_waived_review_writes_empty_string_not_null() -> None:
    """§7.1: `reviewed_by` is '' when review was waived. The column then says "no reviewer" — a fact —
    rather than "unknown", which is not what happened."""
    task = _task("t0", TaskState.ACCEPTED, reviewed_by=None, reviewed_at=None, review_action=None)
    row = _plan([(task, _draft("t0", 1))]).rows[0]

    assert row["reviewed_by"] == ""
    assert row["review_action"] == NO_SHAPE
    assert project_facet(_project(), _plan([(task, _draft("t0", 1))]))["tasksWithoutReview"] == 1


# --------------------------------------------------------------------------------------------------
# The run facet — §7.2
# --------------------------------------------------------------------------------------------------


def test_the_facet_reports_project_totals() -> None:
    plan = _plan(
        [
            (_task("t0", TaskState.ACCEPTED, submitted_by="zoe", reviewed_by="carol"), _draft("t0", 1)),
            (_task("t1", TaskState.ACCEPTED, submitted_by="gina", reviewed_by="carol"), _draft("t1", 2)),
            (_task("t2", TaskState.SKIPPED), None),
        ]
    )
    facet = project_facet(_project(), plan, frozen_at=NOW)

    assert facet["projectSlug"] == "vasa-portraits"
    assert facet["publishId"] == PUBLISH_ID
    assert (facet["taskCount"], facet["acceptedCount"], facet["skippedCount"]) == (3, 2, 1)
    assert (facet["annotatorCount"], facet["reviewerCount"]) == (2, 1)
    assert facet["labelClasses"] == ["person", "ship"], "label classes must be sorted, not schema-ordered"
    assert facet["frozenAt"] == NOW.isoformat()


def test_send_origins_are_counted_per_task_not_per_row() -> None:
    """A task with ten shapes is ONE send. Counting rows would report a project's origins in
    proportion to how densely each item happened to be annotated."""
    plan = _plan([(_task("t0", TaskState.ACCEPTED), _draft("t0", 10)), (_task("t1", TaskState.ACCEPTED), _draft("t1", 1))])
    facet = project_facet(_project(), plan)

    assert facet["sendOrigins"] == {"chunks": 2}
    assert facet["sourceDatasets"] == [{"dataset": "transcripts_v2", "items": 2, "version": None}]


def test_the_facet_is_spec_legal_and_does_not_collide_with_a_reserved_name() -> None:
    """A custom facet must carry `_producer`/`_schemaURL` or a strict consumer drops it — provenance
    that silently vanishes at the far end is not provenance. And the catalog REJECTS an emit whose
    facet name is reserved, so the name is part of the contract."""
    facet = project_facet(_project(), _plan([(_task("t0", TaskState.ACCEPTED), _draft("t0", 1))]))
    assert facet["_producer"] and facet["_schemaURL"]
    assert PROJECT_FACET not in {"lance", "author", "errorMessage", "progress", "parent"}


def test_a_replayed_publish_produces_an_identical_plan_and_facet() -> None:
    """The workflow replays this activity after a crash. Two replays that differ would read as two
    provenances for one publish — so everything derived is sorted, and nothing is timestamped here."""
    pairs = [
        (_task("t1", TaskState.ACCEPTED, submitted_by="zoe"), _draft("t1", 2)),
        (_task("t0", TaskState.ACCEPTED, submitted_by="gina"), _draft("t0", 1)),
        (_task("t2", TaskState.SKIPPED), None),
    ]
    first, second = _plan(list(pairs)), _plan(list(pairs))

    assert first.rows == second.rows
    assert project_facet(_project(), first) == project_facet(_project(), second)


def test_the_rows_and_the_facet_cannot_disagree() -> None:
    """Both are projected from one plan in one pass — which makes "the graph says X, the data says Y"
    impossible rather than merely unlikely."""
    plan = _plan(
        [
            (_task("t0", TaskState.ACCEPTED, submitted_by="gina"), _draft("t0", 3)),
            (_task("t1", TaskState.ACCEPTED, submitted_by="zoe"), _draft("t1", 2)),
            (_task("t2", TaskState.SKIPPED), _draft("t2", 7)),
        ]
    )
    facet = project_facet(_project(), plan)

    assert facet["shapeCount"] == plan.shape_count == 5
    assert facet["annotatorCount"] == len({r["annotated_by"] for r in plan.rows if r["task_outcome"] == "accepted"})
    assert facet["taskCount"] == len({r["task_id"] for r in plan.rows})


# --------------------------------------------------------------------------------------------------
# Table properties — §7.1
# --------------------------------------------------------------------------------------------------


def test_table_properties_are_all_strings() -> None:
    """Lance table properties are a string→string map. Rendering the numbers here means no serializer
    downstream gets to decide how a bool or an int is spelled."""
    plan = _plan([(_task("t0", TaskState.ACCEPTED), _draft("t0", 1)), (_task("t1", TaskState.SKIPPED), None)])
    props = table_properties(_project(), plan)

    assert all(isinstance(v, str) for v in props.values())
    assert props["annotation.task_count"] == "2"
    assert props["annotation.accepted_count"] == "1"
    assert props["annotation.skipped_count"] == "1"
    assert props["annotation.review_required"] == "true"
    assert props["annotation.label_classes"] == "person,ship"


# --------------------------------------------------------------------------------------------------
# The Arrow contract — §7.1
# --------------------------------------------------------------------------------------------------


def test_the_plan_converts_to_arrow_under_the_declared_schema() -> None:
    """The real proof of the row contract. Asserting key NAMES only catches a renamed column; this
    catches a wrong TYPE — a str where a float32 is declared, a naive datetime where a tz-aware one
    is — which otherwise surfaces as an opaque conversion error at write time in a cluster."""
    import pyarrow as pa
    from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA

    plan = _plan(
        [
            (_task("t0", TaskState.ACCEPTED), _draft("t0", 2)),
            (_task("t1", TaskState.SKIPPED), _draft("t1", 4)),
            (_task("t2", TaskState.ACCEPTED, reviewed_by=None, reviewed_at=None, review_action=None), None),
        ]
    )

    table = pa.Table.from_pylist(plan.rows, schema=PUBLISHED_LABELS_SCHEMA)

    assert table.num_rows == len(plan.rows) == 4  # 2 shapes + 1 sentinel + 1 empty-accepted
    assert table.schema == PUBLISHED_LABELS_SCHEMA
    # t0's two shapes and t2's sentinel are annotated; t1 was SKIPPED from a claim and so has no
    # submitter at all — '' rather than null, because "nobody submitted this" is a fact, and a null
    # would be readable as "unknown", which is a different and false claim.
    assert table.column("annotated_by").to_pylist() == ["gina", "gina", "", "gina"]
    assert table.column("task_outcome").to_pylist() == ["accepted", "accepted", "skipped", "accepted"]
    assert table.column("reviewed_by").to_pylist()[-1] == "", "a waived review must be '' in Arrow too, not null"


def test_an_all_sentinel_publish_still_produces_a_correctly_typed_table() -> None:
    """The reason the schema is declared rather than inferred. With inference, a project where every
    task was skipped yields null-typed columns, and a consumer's `x > 0.5` fails against a table that
    is supposed to have the same shape as every other publish."""
    import pyarrow as pa
    from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA

    plan = _plan([(_task("t0", TaskState.SKIPPED), None), (_task("t1", TaskState.SKIPPED), None)])

    table = pa.Table.from_pylist(plan.rows, schema=PUBLISHED_LABELS_SCHEMA)

    assert table.schema.field("x").type == pa.float32(), "an all-skipped publish inferred a null column"
    assert table.schema.field("polygon").type == pa.list_(pa.float32())
    assert table.column("shape_type").to_pylist() == [NO_SHAPE, NO_SHAPE]


def test_the_column_tuple_is_derived_from_the_schema_not_maintained_beside_it() -> None:
    """One source of truth. Two hand-written lists drift, and the drift shows up as a cluster-time
    conversion error rather than a test failure."""
    from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA

    assert tuple(PUBLISHED_LABELS_SCHEMA.names) == PUBLISHED_COLUMNS
    assert len(PUBLISHED_COLUMNS) == 34, "DESIGN §7.1 specifies 34 columns"


# --------------------------------------------------------------------------------------------------
# The reproducibility pin (the lineage READ edge) — captured at send, never fabricated
# --------------------------------------------------------------------------------------------------


def _pin_task(task_id: str, dataset: str | None, version: int | None) -> Task:
    return Task(
        task_id=task_id,
        project_id="p1",
        state=TaskState.ACCEPTED,
        submitted_by="gina",
        source={"kind": "chunks", "keys": [task_id], "where": dataset, "dataset_version": version},  # type: ignore[arg-type]
        media={"kind": "image"},  # type: ignore[arg-type]
    )


def test_source_pin_pins_exactly_one_dataset_at_one_version() -> None:
    """The dataset is a namespace-qualified CATALOG TABLE id, because that is what the pin becomes.

    This case used to assert a bare `"demo"`, which pinned the defect: the pin travels to the
    catalog as a table reference and a bare word is not one."""
    from annotator.projects.publish import source_pin

    project = AnnotationProject(project_id="p1", tenant="acme", slug="vasa")
    pairs = [(_pin_task("t0", "bronze$pages", 24), None), (_pin_task("t1", "bronze$pages", 24), None)]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)

    assert source_pin(plan) == ("bronze$pages", 24)


@pytest.mark.parametrize(
    ("specs", "why"),
    [
        ([("bronze$pages", 24), ("bronze$other", 3)], "two datasets — a single pin would lie about one of them"),
        ([("bronze$pages", 24), ("bronze$pages", 25)], "two VERSIONS of one dataset — same lie"),
        ([("bronze$pages", None), ("bronze$pages", 24)], "a missing capture poisons the pin — never fabricate"),
        ([("bronze$pages", None)], "no version captured at all"),
        ([(None, None)], "no dataset recorded"),
        (
            [(None, None), ("bronze$pages", 24)],
            "an item with NO recorded dataset mixed with a captured one — a pin would claim provenance some items never had",
        ),
        # Observed live 2026-08-03: `where` carries the MEDIA dataset name, and for an unregistered
        # corpus that is a bare word. Sent as a table reference it made the catalog authorize
        # `table:transcripts_v2` — an object that does not exist — and FGA denies before checking
        # existence, so the WHOLE publish failed with "can_get_metadata required" for a provenance
        # nicety. An unregistered corpus has no catalog node to draw a READ edge to.
        ([("transcripts_v2", 1)], "a bare media dataset name is not a catalog table reference"),
        ([("transcripts_v2", 1), ("transcripts_v2", 1)], "…however many items agree on it"),
    ],
)
def test_source_pin_refuses_anything_ambiguous(specs: list, why: str) -> None:
    from annotator.projects.publish import source_pin

    project = AnnotationProject(project_id="p1", tenant="acme", slug="vasa")
    pairs = [(_pin_task(f"t{i}", ds, v), None) for i, (ds, v) in enumerate(specs)]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)

    assert source_pin(plan) is None, why


def test_the_facet_source_datasets_carry_their_captured_versions() -> None:
    project = AnnotationProject(project_id="p1", tenant="acme", slug="vasa")
    pairs = [(_pin_task("t0", "demo", 24), None), (_pin_task("t1", "other", None), None)]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)

    facet = project_facet(project, plan)
    by_name = {d["dataset"]: d for d in facet["sourceDatasets"]}
    assert by_name["demo"]["version"] == 24
    assert by_name["other"]["version"] is None, "an uncaptured version must read as unknown, not invented"


# --------------------------------------------------------------------------------------------------
# Consensus v1 — replica groups in the facet (counts only; the merge is a named follow-up)
# --------------------------------------------------------------------------------------------------


def _replica_task(task_id: str, replica_of: str, labels: list[str]) -> tuple[Task, Draft]:
    task = Task(
        task_id=task_id,
        project_id="p1",
        state=TaskState.ACCEPTED,
        submitted_by=f"annotator-{task_id}",
        replica_of=replica_of,
        source={"kind": "chunks", "keys": ["k1"]},  # type: ignore[arg-type]
        media={"kind": "image"},  # type: ignore[arg-type]
    )
    draft = Draft(
        task_id=task_id,
        project_id="p1",
        author=f"annotator-{task_id}",
        shapes=[Shape(shape_type="bbox", x=1, y=1, width=2, height=2, label=lbl) for lbl in labels],
    )
    return task, draft


def test_the_facet_reports_consensus_counts_never_a_fabricated_merge() -> None:
    """Two groups of two replicas: one agrees (equal label multisets), one does not. The facet
    counts; every replica's rows still land (annotated_by distinguishes) — no invented merge."""
    project = AnnotationProject(project_id="p1", tenant="acme", slug="vasa", consensus_n=2)
    pairs = [
        _replica_task("g1-r1", "g1", ["person", "ship"]),
        _replica_task("g1-r2", "g1", ["ship", "person"]),  # same multiset, different order → agrees
        _replica_task("g2-r1", "g2", ["person"]),
        _replica_task("g2-r2", "g2", ["signature"]),  # disagrees
    ]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)
    facet = project_facet(project, plan)

    assert facet["consensus"] == {"n": 2, "groups": 2, "perfect_agreement_groups": 1}
    # All four replicas' shapes are rows — 2+2+1+1.
    assert len(plan.rows) == 6


def test_no_consensus_facet_when_the_task_has_no_replicas() -> None:
    project = AnnotationProject(project_id="p1", tenant="acme", slug="vasa")
    pairs = [(_pin_task("t0", "demo", 24), None)]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)

    assert "consensus" not in project_facet(project, plan)


# --------------------------------------------------------------------------------------------------
# Consensus v1 — adjudication in the publish (the facet carries the pick; a stale pick refuses)
# --------------------------------------------------------------------------------------------------


def _adjudicated_project(**picks: str) -> AnnotationProject:
    return AnnotationProject(
        project_id="p1",
        tenant="acme",
        slug="vasa",
        consensus_n=2,
        adjudications={g: {"task_id": t, "by": "meg", "at": NOW} for g, t in picks.items()},  # type: ignore[arg-type]
    )


def test_the_facet_carries_the_managers_picks() -> None:
    project = _adjudicated_project(g1="g1-r2")
    pairs = [_replica_task("g1-r1", "g1", ["ship"]), _replica_task("g1-r2", "g1", ["boat"])]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)

    facet = project_facet(project, plan)

    pick = facet["consensus"]["adjudications"]["g1"]
    assert pick["task_id"] == "g1-r2"
    assert pick["by"] == "meg", "the facet must carry WHO decided — a pick without attribution is not provenance"
    assert pick["at"], "…and WHEN"


def test_no_adjudications_key_when_nothing_was_picked() -> None:
    project = AnnotationProject(project_id="p1", tenant="acme", slug="vasa", consensus_n=2)
    pairs = [_replica_task("g1-r1", "g1", ["ship"]), _replica_task("g1-r2", "g1", ["ship"])]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)

    assert "adjudications" not in project_facet(project, plan)["consensus"], "an absent pick must stay absent — not an empty map pretending to be a decision"


def test_a_pick_naming_a_no_longer_accepted_replica_refuses_the_publish() -> None:
    """The group was adjudicated, then the winner was reopened and skipped. Publishing anyway would
    stamp a facet whose canonical pick points outside the accepted set — refused, never dropped."""
    project = _adjudicated_project(g1="g1-r1")
    skipped_task, _ = _replica_task("g1-r1", "g1", [])
    skipped_task = skipped_task.model_copy(update={"state": TaskState.SKIPPED})
    pairs = [(skipped_task, None), _replica_task("g1-r2", "g1", ["ship"])]

    with pytest.raises(PublishRefusal, match="g1-r1"):
        build_plan(project, pairs, publish_id="tok", published_at=NOW)


def test_a_pick_for_a_group_absent_from_the_publish_refuses() -> None:
    project = _adjudicated_project(g9="g9-r1")
    pairs = [_replica_task("g1-r1", "g1", ["ship"]), _replica_task("g1-r2", "g1", ["ship"])]

    with pytest.raises(PublishRefusal, match="g9"):
        build_plan(project, pairs, publish_id="tok", published_at=NOW)


def test_a_pick_naming_another_groups_member_refuses_the_publish() -> None:
    """The audit's membership hole: `g1-r1-r2` is a member of group `g1-r1`, but every string
    check on `g1` passes it. Membership is judged by `replica_of` — the authoritative grouping."""
    project = _adjudicated_project(g1="g1-r1-r2")
    pairs = [
        _replica_task("g1-r1", "g1", ["ship"]),
        _replica_task("g1-r2", "g1", ["ship"]),
        _replica_task("g1-r1-r1", "g1-r1", ["boat"]),
        _replica_task("g1-r1-r2", "g1-r1", ["boat"]),
    ]

    with pytest.raises(PublishRefusal, match="not a member"):
        build_plan(project, pairs, publish_id="tok", published_at=NOW)


def test_the_template_is_stamped_into_properties_and_facet() -> None:
    """§7.1: a downstream consumer must know what SHAPE of labels the table holds."""
    from annotator.projects.models import TaskTemplate
    from annotator.projects.publish import table_properties

    project = AnnotationProject(
        project_id="p1",
        tenant="acme",
        slug="vasa",
        template=TaskTemplate(kind="reading-order", tools=["bbox"], enforce=True),
    )
    pairs = [(_pin_task("t0", "demo", 24), None)]
    plan = build_plan(project, pairs, publish_id="tok", published_at=NOW)

    assert table_properties(project, plan)["annotation.template_kind"] == "reading-order"
    facet = project_facet(project, plan)
    assert facet["template"]["kind"] == "reading-order"
    assert facet["template"]["enforce"] is True
