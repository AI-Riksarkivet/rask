"""Publish → the lakehouse: which annotations land, in what shape, and whose names travel with them.

The publish DECISION, implementing `OPEN-WORK.md#design--annotation-projects` §7. Deliberately pure — no
Dapr, no Lance, no catalog client — so the rules that would be expensive to get wrong in a cluster
are testable exhaustively here. **The annotator never writes Lance directly** (§7.1); a workflow
takes this plan and posts it through the catalog, which is what seeds FGA ownership and emits the
`CREATE` RunEvent.

Three rules carry the weight:

1. **Only an `accepted` task contributes SHAPES.** `skipped` is terminal, so it satisfies the publish
   precondition and the project actor correctly allows the publish — but a skipped task can still
   hold a draft, because the annotator drew shapes and only then decided the item did not belong.
   Terminal-therefore-publishable is the trap.
2. **A skipped task still contributes ONE SENTINEL ROW** (`shape_type="none"`,
   `task_outcome="skipped"`, §7.1). Dropping it entirely would be the opposite error: the project's
   *decisions* would be incomplete on the record and a consumer could not build an exclusion set.
   "No shapes" and "no row" are different claims, and only one of them is true.
3. **Attribution comes from the task, never from the payload.** `submitted_by` / `reviewed_by` are
   written by the actor from the OIDC-verified subject. The draft's `author` records who last SAVED
   — under `fix_and_accept` that is the reviewer — so the two are carried separately rather than
   collapsed into one "who did this", which would credit the wrong person in either direction.

Provenance is written in both directions, and they cannot disagree because both are projected from
one plan in one pass: the ROWS carry `annotated_by` / `reviewed_by` so a consumer answers "who
annotated this row" with no graph query, and ONE `annotationProject` run facet carries the project
totals so the graph answers "what produced this dataset".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

import pyarrow as pa
from lineage_kit.schemas import custom_facet
from pydantic import BaseModel, Field

from annotator.projects.models import AnnotationProject, Draft, Shape, Task, TaskState


#: The only state that contributes SHAPES. Every other terminal state contributes a sentinel row.
_SHAPES_FROM: Final[frozenset[TaskState]] = frozenset({TaskState.ACCEPTED})

#: The sentinel `shape_type` for a task that produced no shapes (§7.1). A real value in the column's
#: domain, not a null — a consumer filters `shape_type != 'none'` for shapes and reads `task_outcome`
#: for coverage, and neither query has to reason about nullability.
NO_SHAPE: Final[str] = "none"

#: The publish run facet (§7.2). Named to avoid the catalog's `_RESERVED_RUN_FACETS`
#: (`lance`, `author`, `errorMessage`, `progress`, `parent`) — a collision there is rejected at emit.
PROJECT_FACET: Final[str] = "annotationProject"

#: The published table's Arrow schema (§7.1) — the DATA CONTRACT, and the reason a publish can create
#: a correctly-typed table even when a project published nothing but skip sentinels. Without an
#: explicit schema, `pa.Table.from_pylist` infers from the rows, so an all-sentinel publish would
#: produce null-typed columns and a consumer's `x > 0.5` would fail against a table that is supposed
#: to have the same shape as every other publish.
#:
#: `polygon` is a list of float32 rather than a JSON string because it is the one repeated numeric
#: field a training consumer reads per row. `attributes` IS a JSON string — it is arbitrary
#: user-defined keys, which Arrow cannot type without freezing the taxonomy.
PUBLISHED_LABELS_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        # provenance of the project (never a join key into the corpus)
        ("project_id", pa.string()),
        ("project_slug", pa.string()),
        ("publish_id", pa.string()),
        ("task_id", pa.string()),
        ("task_outcome", pa.string()),  # accepted | skipped
        # the send capture — informational strings, copied at send time
        ("item_source_kind", pa.string()),
        ("item_dataset", pa.string()),
        ("item_key_path", pa.string()),
        # the label
        ("annotation_id", pa.string()),
        ("shape_type", pa.string()),  # bbox|polygon|mask|segment|tag|text|none
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("width", pa.float32()),
        ("height", pa.float32()),
        ("rotation", pa.float32()),
        ("polygon", pa.list_(pa.float32())),
        ("t_start", pa.float32()),
        ("t_end", pa.float32()),
        ("mask", pa.string()),
        ("label", pa.string()),
        ("text", pa.string()),
        # The ontology declares per-class attributes with REAL types (free/int/enum/bool), enforces
        # them at submit and publishes them — and while this was `pa.string()` no downstream consumer
        # could filter on one. Declared, enforced, unqueryable. `pa.json_()` makes
        # `json_get_int(attributes, 'order') > 3` a Lance filter.
        #
        # Verified end to end before changing it: the extension type survives Arrow IPC (the hop to
        # the catalog, which is what actually writes Lance here) and `lance.write_dataset` preserves
        # it. `_json_attributes` already emits `{}` rather than "" for a shapeless row, which matters
        # because an empty string is not valid JSON.
        ("attributes", pa.json_()),
        ("group", pa.string()),
        ("difficult", pa.bool_()),
        # who made it — server-stamped, never client-claimed
        ("source", pa.string()),  # human | model | propagated
        ("model_version", pa.string()),
        ("confidence", pa.float32()),
        ("annotated_by", pa.string()),
        ("annotated_at", pa.timestamp("us", tz="UTC")),
        ("reviewed_by", pa.string()),  # '' when review_required = False
        ("reviewed_at", pa.timestamp("us", tz="UTC")),
        ("review_action", pa.string()),  # accepted | fix_and_accept | none
        ("lead_time_seconds", pa.float32()),
        ("published_at", pa.timestamp("us", tz="UTC")),
    ]
)

#: DERIVED from the schema, never written twice. A hand-maintained second list is a second truth, and
#: the failure it produces — a row dict whose keys no longer match the Arrow schema — surfaces as an
#: opaque conversion error at write time in a cluster rather than as a test failure here.
PUBLISHED_COLUMNS: Final[tuple[str, ...]] = tuple(PUBLISHED_LABELS_SCHEMA.names)


class PublishRefusal(ValueError):
    """A publish that must not proceed. Raised rather than returning a partial plan — a publish that
    silently drops rows is worse than one that stops and says why."""


class Attribution(BaseModel):
    """Who produced one task's outcome, taken entirely from server-written fields."""

    task_id: str
    outcome: str
    annotated_by: str
    annotated_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_action: str | None = None
    #: Who last SAVED the shapes. Usually the annotator; under `fix_and_accept` it is the reviewer,
    #: which is exactly the case that makes collapsing these two fields a lie.
    drafted_by: str = ""
    shape_count: int = 0


class PublishPlan(BaseModel):
    """Everything the publish workflow needs, decided in one pass so the rows it writes and the
    lineage it emits cannot drift apart."""

    project_id: str
    publish_id: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    attributions: list[Attribution] = Field(default_factory=list)
    #: Per source dataset, the DISTINCT captured versions (sorted, `None` = uncaptured). Collected
    #: at plan time from the tasks' send captures, so the facet and the pin read one truth.
    dataset_versions: dict[str, list[int | None]] = Field(default_factory=dict)
    #: Items whose send recorded NO dataset at all. They appear nowhere in `dataset_versions`, so
    #: without this count a plan mixing captured and uncaptured items would still pin — stamping a
    #: READ edge that claims provenance for items that never recorded any (the A-1 audit finding).
    sources_uncaptured: int = 0
    #: Consensus v1: replica group id → its member task ids (sorted). Only groups that reached the
    #: publish appear; ordinary items are absent.
    replica_groups: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def accepted_count(self) -> int:
        return sum(1 for a in self.attributions if a.outcome == "accepted")

    @property
    def skipped_count(self) -> int:
        return sum(1 for a in self.attributions if a.outcome == "skipped")

    @property
    def shape_count(self) -> int:
        """Rows that are real shapes. Sentinels are decisions, not annotations, and counting them as
        labels would overstate every published dataset by its skip rate."""
        return sum(1 for r in self.rows if r["shape_type"] != NO_SHAPE)


def _attribution(task: Task, draft: Draft | None, outcome: str) -> Attribution:
    """Project one terminal task into an attribution row.

    `submitted_by` is required for an ACCEPTED task: accepted with nobody recorded as having
    submitted it means the audit trail is broken, and anonymous provenance in the lakehouse is worse
    than no publish — once written it is indistinguishable from the real thing. A SKIPPED task may
    legitimately have no submitter (skipped straight from a claim), so it is credited to the actor
    that skipped it, or to nobody.
    """
    if outcome == "accepted" and not task.submitted_by:
        raise PublishRefusal(f"task {task.task_id} is accepted but records no submitter — refusing to publish anonymous work")
    return Attribution(
        task_id=task.task_id,
        outcome=outcome,
        annotated_by=task.submitted_by or "",
        annotated_at=task.submitted_at,
        reviewed_by=task.reviewed_by,
        reviewed_at=task.reviewed_at,
        review_action=task.review_action,
        drafted_by=draft.author if draft else "",
        shape_count=len(draft.shapes) if draft else 0,
    )


def _row(
    project: AnnotationProject,
    task: Task,
    attribution: Attribution,
    shape: Shape | None,
    *,
    publish_id: str,
    published_at: datetime,
) -> dict[str, Any]:
    """One row of `PUBLISHED_LABELS_SCHEMA`. `shape=None` builds the sentinel."""
    source = task.source
    return {
        "project_id": project.project_id,
        "project_slug": project.slug,
        "publish_id": publish_id,
        "task_id": task.task_id,
        "task_outcome": attribution.outcome,
        "item_source_kind": source.kind,
        "item_dataset": source.where or "",
        "item_key_path": source.keys[0] if source.keys else "",
        "annotation_id": shape.shape_id if shape else "",
        "shape_type": shape.shape_type if shape else NO_SHAPE,
        "x": shape.x if shape else None,
        "y": shape.y if shape else None,
        "width": shape.width if shape else None,
        "height": shape.height if shape else None,
        "rotation": shape.rotation if shape else None,
        "polygon": list(shape.polygon) if shape else [],
        "t_start": shape.t_start if shape else None,
        "t_end": shape.t_end if shape else None,
        "mask": shape.mask if shape else None,
        "label": shape.label if shape else None,
        "text": shape.text if shape else None,
        "attributes": _json_attributes(shape),
        "group": shape.group if shape else None,
        "difficult": bool(shape.difficult) if shape else False,
        "source": (shape.source or "human") if shape else "",
        "model_version": shape.model_version if shape else None,
        "confidence": shape.confidence if shape else None,
        # Server-stamped provenance. Read off the TASK, so a client payload cannot reach it.
        # `reviewed_by` is '' rather than null when review was waived (§7.1) — the column says "no
        # reviewer", which is a fact, instead of "unknown", which is not what happened.
        "annotated_by": attribution.annotated_by,
        "annotated_at": attribution.annotated_at,
        "reviewed_by": attribution.reviewed_by or "",
        "reviewed_at": attribution.reviewed_at,
        "review_action": attribution.review_action or NO_SHAPE,
        "lead_time_seconds": task.lead_time_seconds,
        "published_at": published_at,
    }


def _json_attributes(shape: Shape | None) -> str:
    """The JSON text for one row's `attributes` (§7.1), landing in a `pa.json_()` column.

    Sorted keys so a replayed publish produces byte-identical rows — an activity that replays after a
    crash must not rewrite the dataset differently.

    It must always return VALID JSON, and that is now load-bearing rather than tidy: under
    `pa.string()` malformed text landed happily and only broke whoever parsed it later, whereas Lance
    refuses to encode it into a JSON column and fails the whole publish write. `{}` for a shapeless
    row is the reason this never returns `""`.
    """
    import json  # noqa: PLC0415 - only needed on this path

    return json.dumps(dict(sorted(shape.attributes.items())), separators=(",", ":")) if shape else "{}"


def build_plan(
    project: AnnotationProject,
    pairs: Sequence[tuple[Task, Draft | None]],
    *,
    publish_id: str,
    published_at: datetime,
) -> PublishPlan:
    """Decide the whole publish in one pass.

    `pairs` is every task in the project with its draft (or `None`). A non-terminal task is a
    programming error at this point — the project actor's precondition already refused the publish —
    so it is refused loudly rather than skipped quietly.
    """
    plan = PublishPlan(project_id=project.project_id, publish_id=publish_id)
    for task, _draft in pairs:
        if task.replica_of:
            group = plan.replica_groups.setdefault(task.replica_of, [])
            if task.task_id not in group:
                group.append(task.task_id)
                group.sort()
        # Version capture per dataset, from the SEND capture — one truth for the facet + the pin.
        if task.source.where:
            versions = plan.dataset_versions.setdefault(task.source.where, [])
            if task.source.dataset_version not in versions:
                versions.append(task.source.dataset_version)
                versions.sort(key=lambda v: (v is not None, v if v is not None else 0))
        else:
            plan.sources_uncaptured += 1

    # A pick that no longer names an ACCEPTED replica is refused, not silently dropped: the group
    # was adjudicated, then someone reopened the winner — publishing would stamp a facet whose
    # canonical pick points at work that is not in the accepted set, and provenance must not lie.
    states = {task.task_id: task.state for task, _ in pairs}
    for group, adjudication in sorted(project.adjudications.items()):
        if group not in plan.replica_groups:
            raise PublishRefusal(f"adjudication for group {group} names {adjudication.task_id}, but no replica of that group reached this publish")
        if adjudication.task_id not in plan.replica_groups[group]:
            # Membership by `replica_of` — the authoritative grouping — not by id shape: a
            # client-chosen id like `g1-r1-r2` (a member of group `g1-r1`) passes any string check
            # on `g1` (audit finding), and a facet naming another group's work as canonical lies.
            raise PublishRefusal(f"adjudication for group {group} names {adjudication.task_id}, which is not a member of that group — re-adjudicate")
        picked_state = states.get(adjudication.task_id)
        if picked_state is not TaskState.ACCEPTED:
            raise PublishRefusal(
                f"adjudication for group {group} names {adjudication.task_id}, which is {picked_state} rather than accepted — re-adjudicate the group"
            )
    for task, draft in pairs:
        if task.state is TaskState.ACCEPTED:
            attribution = _attribution(task, draft, "accepted")
            plan.attributions.append(attribution)
            shapes = draft.shapes if draft else []
            if not shapes:
                # Accepted with no shapes is a real decision ("nothing to annotate here"), and it is
                # recorded as one rather than vanishing.
                plan.rows.append(_row(project, task, attribution, None, publish_id=publish_id, published_at=published_at))
            for shape in shapes:
                plan.rows.append(_row(project, task, attribution, shape, publish_id=publish_id, published_at=published_at))
        elif task.state is TaskState.SKIPPED:
            # ONE sentinel row, and NONE of the draft's shapes (§7.1).
            attribution = _attribution(task, draft, "skipped")
            plan.attributions.append(attribution)
            plan.rows.append(_row(project, task, attribution, None, publish_id=publish_id, published_at=published_at))
        else:
            raise PublishRefusal(f"task {task.task_id} is {task.state}, not terminal — the publish precondition was not met")
    return plan


def project_facet(project: AnnotationProject, plan: PublishPlan, *, frozen_at: datetime | None = None) -> dict[str, Any]:
    """The `annotationProject` run facet (§7.2) — every key a fact the project store already holds.

    Counts rather than name lists: the NAMES are on every row (`annotated_by`, `reviewed_by`), so
    putting them here too would be a second copy that can disagree with the first. Sorted and derived
    so a replayed publish emits an identical facet — two replays producing different payloads would
    read as two provenances for one publish.
    """
    annotators = {a.annotated_by for a in plan.attributions if a.annotated_by}
    reviewers = {a.reviewed_by for a in plan.attributions if a.reviewed_by}
    # Counted per TASK, not per row: a task with ten shapes contributes ten rows and exactly one
    # send. Counting rows would report a project's origins in proportion to how densely each item
    # happened to be annotated, which is not what the number means.
    origins: dict[str, int] = {}
    datasets: dict[str, int] = {}
    for task_id in {r["task_id"] for r in plan.rows}:
        row = next(r for r in plan.rows if r["task_id"] == task_id)
        origins[row["item_source_kind"]] = origins.get(row["item_source_kind"], 0) + 1
        if row["item_dataset"]:
            datasets[row["item_dataset"]] = datasets.get(row["item_dataset"], 0) + 1
    return custom_facet(
        projectId=project.project_id,
        projectSlug=project.slug,
        publishId=plan.publish_id,
        taskCount=len(plan.attributions),
        acceptedCount=plan.accepted_count,
        skippedCount=plan.skipped_count,
        annotatorCount=len(annotators),
        reviewerCount=len(reviewers),
        reviewRequired=project.review_required,
        # ONE class list. This read `label_schema.classes` while the facet's `template` below came
        # from a separate object nothing cross-checked, so the run facet could carry a taxonomy the
        # enforcement had never heard of. Both now project from the same ontology.
        labelClasses=sorted(c.name for c in project.ontology.classes),
        shapeCount=plan.shape_count,
        sendOrigins=dict(sorted(origins.items())),
        # §7.2's per-dataset `version`, from the SEND capture (`ItemSource.dataset_version`). A
        # dataset whose captured versions are mixed reports None — "unknown" is a fact, an invented
        # number is not.
        sourceDatasets=[
            {
                "dataset": name,
                "items": n,
                # The captured version when it is UNAMBIGUOUS for this dataset; None otherwise —
                # "unknown" is a fact, an invented number is not (§7.2).
                "version": (plan.dataset_versions.get(name) or [None])[0] if len(plan.dataset_versions.get(name, [])) == 1 else None,
            }
            for name, n in sorted(datasets.items())
        ],
        leadTimeSecondsTotal=project.lead_time_seconds_total,
        frozenAt=frozen_at.isoformat() if frozen_at else None,
        # An accepted task with no reviewer is legal (the project can waive review), so it is
        # REPORTED rather than refused — and it is the number a governance reader most wants.
        tasksWithoutReview=sum(1 for a in plan.attributions if a.outcome == "accepted" and not a.reviewed_by),
        # The task definition travels whole — the facet is where "what was this task" lives, and
        # `labelClasses` above is now a projection OF this rather than a second opinion about it.
        ontology=project.ontology.model_dump(mode="json"),
        # Consensus v1 (only when replica groups exist): agreement COUNTS from label multisets per
        # group — every replica's rows still land, and no merged truth is invented here.
        **({"consensus": _consensus_counts(project, plan)} if plan.replica_groups else {}),
    )


def _consensus_counts(project: AnnotationProject, plan: PublishPlan) -> dict[str, Any]:
    """Agreement counts per replica group: a group agrees when every member's LABEL MULTISET is
    identical (order-free). Counts plus the manager's PICKS — never a fabricated merge, which would
    put words in annotators' mouths; a consumer wanting canonical rows filters by the picked ids."""
    labels_by_task: dict[str, list[str]] = {}
    for row in plan.rows:
        if row["shape_type"] == NO_SHAPE:
            continue
        labels_by_task.setdefault(row["task_id"], []).append(row["label"] or "")
    perfect = 0
    for members in plan.replica_groups.values():
        multisets = [sorted(labels_by_task.get(tid, [])) for tid in members]
        if len(set(map(tuple, multisets))) == 1:
            perfect += 1
    # The pick WITH its attribution (task_id, by, at) — a facet naming only the winner would drop
    # exactly the "who decided, when" half that makes an adjudication provenance (audit finding).
    picks = {
        group: {"task_id": adj.task_id, "by": adj.by, "at": adj.at.isoformat()}
        for group, adj in sorted(project.adjudications.items())
        if group in plan.replica_groups
    }
    return {
        "n": project.consensus_n,
        "groups": len(plan.replica_groups),
        "perfect_agreement_groups": perfect,
        **({"adjudications": picks} if picks else {}),
    }


def source_pin(plan: PublishPlan, *, delimiter: str = "$") -> tuple[str, int] | None:
    """The reproducibility pin (§7.2): the ONE (dataset, version) every published item came from.

    Pins only when EVERY published item names the same one dataset with the same one CAPTURED
    version. Two datasets, two versions of one dataset, any uncaptured version, or any item that
    recorded no dataset at all → None: the run facet still reports the per-dataset truth, but a
    single fabricated pin would be a lie — and the pin surfaces as the lineage READ edge, which
    downstream reproduction trusts.

    …and the same rule applies to the NAME. The pin travels to the catalog as a table reference, so
    it must BE one: a namespace-qualified id like ``bronze$pages``. `ItemSource.where` carries the
    MEDIA dataset name, which for an unregistered corpus (a Lance directory the catalog has never
    heard of) is a bare word. Sending it made the catalog authorize `table:transcripts_v2` — an
    object that does not exist — and FGA denies before it checks existence, so the ENTIRE publish
    failed with `can_get_metadata required on table:transcripts_v2` for the sake of a provenance
    nicety. Observed live, 2026-08-03.

    An unregistered corpus simply has no lineage READ edge to draw: there is no catalog node at the
    other end. Refusing to name one is the same discipline as refusing to fabricate a version — and
    the per-dataset truth still reaches the run facet either way."""
    if plan.sources_uncaptured or len(plan.dataset_versions) != 1:
        return None
    dataset, versions = next(iter(plan.dataset_versions.items()))
    if len(versions) != 1 or versions[0] is None:
        return None
    if delimiter not in dataset:
        return None
    return dataset, versions[0]


def table_properties(project: AnnotationProject, plan: PublishPlan) -> dict[str, str]:
    """The properties stamped on the table at create (§7.1). All strings — Lance table properties are
    a string→string map, so the numbers are rendered rather than left to a serializer's discretion."""
    return {
        "annotation.project_id": project.project_id,
        "annotation.publish_id": plan.publish_id,
        "annotation.task_count": str(len(plan.attributions)),
        "annotation.accepted_count": str(plan.accepted_count),
        "annotation.skipped_count": str(plan.skipped_count),
        "annotation.review_required": str(project.review_required).lower(),
        "annotation.label_classes": ",".join(sorted(c.name for c in project.ontology.classes)),
        # §7.1: a downstream consumer must know what SHAPE of labels this table holds. `kind` is a
        # free string now (aligned with Hugging Face pipeline ids by convention), so an empty one is
        # a real answer — an unconstrained project genuinely has no task type, and inventing
        # "bbox-detection" for it would be a claim nothing made.
        "annotation.task_kind": project.ontology.kind,
    }
