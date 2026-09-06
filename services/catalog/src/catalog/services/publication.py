"""Publication — a committed version becomes CONSUMABLE only when the gate passes it.

Ruled by the owner 2026-08-04 (`c6c23407`).

**A commit is not a publication (D-R1).** Before this, a writer's commit was instantly visible to
every reader: whatever landed was consumable, a quality gate that ran afterwards could not un-publish
it, and there was no way to hold consumers at the last good version while a bad one sat above it. So
the writer commits version N, the gate reads N, and only then does the pointer move. A gate FAILURE
leaves the pointer at N-1 — version N stays committed and unreferenced, auditable, consumed by
nobody.

The gate runs AFTER the commit on purpose. The long-standing objection was that a gate must run
*before* the version exists or bad rows are readable; the answer is that "readable" and "published"
are different things once a pointer exists. And post-commit is the only position that works at all:
Lance's change-data-feed diffs two versions, so a pre-commit gate has nothing to diff.

**The tag is the truth; the event is only a notification (D-R2).** `published` answers "what is
ready?" durably, so a consumer that was down for a week can simply ask. An event is a wake-up to
avoid polling, and one missed while a consumer was offline costs nothing.

**This lives in the CATALOG, not in any writer.** The ingest plane, a Ray job, a backfill script and
a person with catalog credentials must all publish the same way, or each reimplements the contract
and they drift. It is also the only correct home mechanically: a tag file carries no format-level
CAS, while the namespace spec's `UpdateTableTag` does return `ConcurrentModification`, so the
catalog is the one door where a concurrent advance can be detected.

Deliberately shaped after `services/models.py::promote`, which has run this exact pattern for the
model registry since it was written — gate first, fail-closed, then create-or-update the tag. Models
gate on metric thresholds and data gates on assertions, but the control flow is the same one, and
having two spellings of it would be the drift this module exists to prevent.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from lance_namespace import (
    CreateTableTagRequest,
    DeleteTableTagRequest,
    GetTableTagVersionRequest,
    InvalidInputError,
    InvalidTableStateError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
    UpdateTableTagRequest,
)
from pydantic import BaseModel

from catalog.core.namespace import open_dataset
from catalog.services import dataplane
from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.quality import NOT_NULL, STRUCTURAL_ASSERTIONS, Assertion, assert_quality, passed, tier_contract_violations


if TYPE_CHECKING:
    from collections.abc import Sequence

    from lance_namespace import LanceNamespace

    from service_kit.lakehouse.gate_specs import GateSpec


log = logging.getLogger(__name__)


#: The pointer a consumer reads. One flat name per dataset — tag names cannot contain `/`, so a
#: branch cannot carry its own `published` and must encode the branch in the name if it ever needs
#: one. Matches `BLESSED_TAG` in the model registry: same concept, different subject.
PUBLISHED_TAG = "published"

#: Held on the candidate version for the DURATION OF THE GATE, then dropped.
#:
#: `cleanup_old_versions` exempts only TAGGED versions, and version N is untagged for exactly as long
#: as the gate takes to run — so a slow gate against a short `older_than` lets maintenance collect the
#: very version being gated, and the publish then advances a pointer at something that no longer
#: exists. The window is small and entirely real, and it widens with every assertion added to the
#: gate. A tag closes it: while `publishing` names N, maintenance cannot touch it.
#:
#: Removed in a `finally`, so a crashed gate leaves at most one stale `publishing` tag rather than
#: pinning a version forever. A stale one is visible in `ListTableTags` and blocks nothing except the
#: GC of one version.
PUBLISHING_TAG = "publishing"


#: The two authorities that can name a gate's key column, as the result reports them.
DECLARED_GATE = "declared"
REQUESTED_GATE = "request"


def gate_source_for(declared_by: str) -> str:
    """Name the authority behind a key column, from the project that declared it (or `""`).

    ONE definition, because every result on both paths reports this field and a second spelling is how
    the wire ends up claiming a source the assertions did not run under.
    """
    return DECLARED_GATE if declared_by else REQUESTED_GATE


class EffectiveGate(BaseModel):
    """The assertions this publish actually runs, and on WHOSE authority.

    Two parties describe the gate at this door and they are not equals. The project's `GateSpec` is
    POLICY — declared through an admin-gated door, readable by anyone, and the same record the
    medallion's movers resolve for themselves. The request's `key_column`/`required_columns` are a
    REQUEST, sent by whoever holds `can_update_tag`, up to and including an external writer the estate
    trusts least. A door that consulted only the request handed the least-trusted writer the weakest
    gate, which is exactly backwards.

    Composition follows from that, and the two fields compose differently ON PURPOSE:

    * `key_column` — only one column can carry the identity check, so the two authorities CONFLICT and
      policy wins whole. This is the medallion's own "a declared gate is not a merge" rule
      (`medallion/services/gate.py`), applied to the same record from the other side.
    * `required_columns` — these do not conflict. A declared column is a dependency the project
      mandates; a requested one is a dependency the caller's own consumer declares; both are real, and
      a gate satisfying both is what the union means. It is also the only composition that cannot
      WEAKEN an existing publish: every column either authority names is still asserted.
    """

    key_column: str
    required_columns: list[str]
    #: The project whose DECLARED record supplied `key_column`, or `""` when the request did. Not a
    #: free-text label: `gate_source` derives from it, so the attribution cannot disagree with the
    #: record that actually governed.
    declared_by: str = ""

    @property
    def gate_source(self) -> str:
        """`"declared"` or `"request"` — which authority named the key column.

        A property rather than a field for the reason `GateSpec.gate_source` states: a source a caller
        could set is a source a caller could lie about, and the whole value of the field is that it
        cannot be.
        """
        return gate_source_for(self.declared_by)


def declared_gate(registry_root: str, storage_options: dict[str, str], project: str) -> GateSpec | None:
    """This project's declared gate settings, or `None` when nobody declared any.

    NEVER RAISES, and the stance is the medallion's, held deliberately: an unresolvable declaration
    falls back rather than refusing, because failing every publish for a tenant over a settings lookup
    takes an estate down for a config blip, while the request's own values remain a real gate. Logged
    at ERROR because it is still a defect — a declaration nobody can read is a policy nobody is under.

    The two readers of this record (this door and `medallion.services.gate`) must not answer
    differently, which is why both go through `service_kit.lakehouse.gate_specs` rather than either
    growing its own reader.
    """
    if not project or not registry_root:
        return None
    try:
        return gate_specs.get_spec(registry_root, storage_options, project)
    except Exception:  # noqa: BLE001 — a settings lookup must not refuse an otherwise valid publish
        log.exception("gate_spec_unresolvable", extra={"project": project})
        return None


def effective_gate(spec: GateSpec | None, *, key_column: str, required_columns: Sequence[str]) -> EffectiveGate:
    """Compose the declared record with the request — see `EffectiveGate` for why each field composes
    the way it does."""
    if spec is None:
        return EffectiveGate(key_column=key_column, required_columns=list(required_columns))
    # Declared first, then the request's own additions — an order, not a set, so the assertions come
    # back in a stable sequence a caller can diff between runs.
    union = list(spec.required_columns) + [column for column in required_columns if column not in spec.required_columns]
    return EffectiveGate(key_column=spec.key_column, required_columns=union, declared_by=spec.project)


def _open_for_contract(uri: str, storage_options: dict[str, str], version: int) -> tuple[object, bool | None]:
    """The schema of ``version`` at ``uri`` — the gate's half of the provenance check.

    `publish` already holds an opened dataset and passes its schema straight in; `gate` is handed a URI
    and has to open one. Pinned to the same `version` the assertions ran against, never `latest`, for
    the reason `assert_quality` records: another writer may have committed since, and answering about a
    version this call is not gating is the failure the pin exists to prevent.
    """
    import lance

    dataset = lance.dataset(uri, storage_options=storage_options, version=version)
    # BOTH READINGS FROM ONE OPEN. The schema answers the column half of the contract and
    # `has_stable_row_ids` answers the half the columns cannot show; opening twice would let a
    # concurrent commit put the two halves on different versions.
    return dataset.schema, getattr(dataset, "has_stable_row_ids", None)


def refuse_a_tier_without_provenance(schema: Any, *, version: int, has_stable_row_ids: bool | None = None) -> None:
    """Raise 400 when a table CLAIMS to be a governed tier and does not carry the provenance to be one.

    Owner ruling D1, 2026-08-31: honest `source_rowid` provenance is mandatory. The case that decided
    it is impact analysis — one document is corrupted at ingest, and "which rows downstream are
    contaminated, so I re-run only those?" must not answer confidently and wrongly. A fabricated
    parent id is worse than an absent one, because it is queryable.

    AT THIS DOOR, because it is the one every publisher passes — the cascade's movers and an external
    writer alike — and because the job-side contract has a hole this closes: it counts parentless rows
    only `if SOURCE_ROWID_COLUMN in out.schema.names`, so a table that drops the column reports zero
    and passes the check that exists to catch it.

    Refuses on CLAIM, never on absence: a table carrying none of the three columns is not a governed
    tier and is untouched. `tier_contract_violations` owns that rule; this function owns only the
    door's answer to it.
    """
    problems = tier_contract_violations(schema, has_stable_row_ids=has_stable_row_ids)
    if not problems:
        return
    raise InvalidInputError(
        f"version {version} carries some governed-tier columns but is not a conforming tier: {'; '.join(problems)}. "
        "A tier that carries any of `stage`, `lineage` or `source_rowid` is claiming to be governed and must carry all "
        "three, so a row can always be traced to the source row it came from."
    )


def refuse_a_gate_that_cannot_run(assertions: Sequence[Assertion], *, key_column: str, version: int, declared_by: str = "") -> None:
    """Raise 400 when the key column names nothing in the data, instead of publishing without it.

    `assert_quality` SKIPS `not_null` when the column is absent — right for it, because a chart-wide
    key column legitimately does not exist in every tier it runs against. At THIS door it is wrong: the
    key column arrived from a request or a declaration that names this very table, so an absent one is
    a typo or a stale declaration, and honouring it publishes with the gate's identity assertion
    missing and a 200 that never mentions it. Silently applying a weaker gate than was asked for is the
    one outcome a gate must not have.

    Derived from the ASSERTIONS rather than from the schema on purpose: absence of `not_null` is
    exactly the condition being refused, so there is one definition of "the gate did not run" and no
    second schema read to disagree with it.
    """
    if not key_column or any(a.assertion == NOT_NULL for a in assertions):
        return
    authority = f"declared by project {declared_by!r}" if declared_by else "named by the request"
    raise InvalidInputError(
        f"key_column {key_column!r} ({authority}) is not a column of this table at version {version}, so the "
        f"{NOT_NULL} assertion cannot run and publishing would apply a WEAKER gate than was asked for. Name a "
        f"column the data carries, or change the gate that names it."
    )


class PublicationResult(BaseModel):
    """What a publish attempt did, and to what.

    `from_version`/`to_version` are the RANGE (D-R3) the notification carries, so a consumer resolves
    an exact row delta with `_row_created_at_version > from AND <= to` and keeps no bookmark of its
    own. `from_version` is None the first time a dataset is published — there is no prior published
    version, so the delta is "everything up to `to_version`".
    """

    table: str
    published: bool
    from_version: int | None
    to_version: int
    assertions: list[Assertion] = []
    reason: str | None = None
    #: Failed assertions a validator explicitly accepted. Empty on every ordinary publish — the field
    #: means "waved through", never "was asked for".
    accepted: list[str] = []
    #: Which authority named the key column these assertions ran on — see `EffectiveGate`. Carried on
    #: the RESULT, not merely logged, because two sources with one shape is how nobody can tell what
    #: governed their data: a caller whose requested key column was superseded by the project's
    #: declaration reads an identical body otherwise.
    gate_source: str = REQUESTED_GATE

    @property
    def advanced(self) -> bool:
        """Whether the tag actually MOVED — the question a readiness announcement should ask.

        `published` answers "is this version published", which is true again on a replay: re-publishing
        an already-published version succeeds and changes nothing. Emitting on that announced a second
        readiness with an empty range, and nothing downstream treats it as empty — `StageTrigger`
        declares neither version field, and the publication head mints a fresh token, so the
        instance-id dedupe never engages and a replay buys a full cascade.
        """
        return self.published and self.to_version != self.from_version


def _tag_version(ns: LanceNamespace, so: dict[str, str], table_id: Sequence[str], tag: str) -> int | None:
    """The version a tag points at, or None when unset.

    Goes through the catalog's own `GetTableTagVersion` rather than reading `ds.tags` directly, so
    there is ONE resolution path and it is the one the spec defines. The dataplane already converts
    pylance's raise-on-unset (`Ref not found` — it does NOT return None) into the spec's
    `TableTagNotFound`; this turns that into the absence it represents.
    """
    try:
        return int(dataplane.get_tag_version(ns, so, GetTableTagVersionRequest(id=list(table_id), tag=tag)).version)
    except TableTagNotFoundError:
        return None


def published_version(ns: LanceNamespace, so: dict[str, str], table_id: Sequence[str]) -> int | None:
    """The version `published` points at, or None when nothing has been published yet."""
    return _tag_version(ns, so, table_id, PUBLISHED_TAG)


def _set_tag(ns: LanceNamespace, so: dict[str, str], table_id: Sequence[str], tag: str, version: int) -> None:
    """Create the tag, or move it if it already exists.

    The spec splits create and update into two operations and refuses the wrong one, so which to call
    depends on state the caller cannot assume — the first publication of every dataset needs create,
    every later one needs update.
    """
    if _tag_version(ns, so, table_id, tag) is None:
        dataplane.create_tag(ns, so, CreateTableTagRequest(id=list(table_id), tag=tag, version=version))
    else:
        dataplane.update_tag(ns, so, UpdateTableTagRequest(id=list(table_id), tag=tag, version=version))


def gate(
    uri: str,
    *,
    key_column: str,
    version: int,
    required_columns: Sequence[str] = (),
    storage_options: dict[str, str] | None = None,
    declared_by: str = "",
) -> PublicationResult:
    """Run the publish gate's assertions and return the verdict WITHOUT touching the tag.

    A QUESTION, not a write. `published` is False on this path whatever the assertions say, and
    nothing about the dataset changes — which is what makes it safe to ask speculatively.

    It exists because the medallion cascade otherwise cannot have two properties it needs at once.
    Under `cascadeViaPublish` the publish IS the promotion (the tag move wakes the next stage), so
    deciding the promotion review BEFORE publishing lets the band hold but leaves the review unable to
    name the assertions it is reviewing, while publishing first preserves those names but has already
    promoted. Separating the verdict from the act dissolves that: a caller asks "would this pass, and
    is it unusual?", and only then decides to publish.

    The same `assert_quality` call the real publish makes, on the same pinned `version` — a gate that
    answered differently from the publish would be worse than no gate, because a caller would trust it.
    That equality includes the REFUSAL: an unrunnable key column is a 400 here too, or a caller could
    ask the question, be told the gate would pass, and then be refused by the act.

    `declared_by` names the project whose declared record supplied `key_column`; empty means the
    request did. It only describes the values — this function never reads a declaration itself, so the
    two doors cannot resolve one differently.
    """
    assertions = assert_quality(
        uri,
        storage_options or {},
        key_column=key_column,
        required_columns=tuple(required_columns),
        version=version,
    )
    refuse_a_gate_that_cannot_run(assertions, key_column=key_column, version=version, declared_by=declared_by)
    # THE SAME REFUSAL `publish` RAISES, and it belongs here for the reason the docstring above gives:
    # a gate that under-reports sends the promotion review an approval the ACT will then refuse, and
    # the reviewer has no way to see it coming. Opening the dataset a second time is the cost of
    # answering the same question — `assert_quality` above opened it for the assertions, and the
    # contract is a schema read, not a scan.
    contract_schema, stable_ids = _open_for_contract(uri, storage_options or {}, version)
    refuse_a_tier_without_provenance(contract_schema, version=version, has_stable_row_ids=stable_ids)
    failed = [a.assertion for a in assertions if not a.success]
    return PublicationResult(
        table=uri,
        published=False,
        from_version=None,
        to_version=version,
        assertions=list(assertions),
        gate_source=gate_source_for(declared_by),
        reason=f"gate only: {', '.join(failed)}" if failed else None,
    )


def publish(
    ns: LanceNamespace,
    storage_options: dict[str, str],
    *,
    table_id: Sequence[str],
    version: int,
    key_column: str,
    required_columns: Sequence[str] = (),
    accept_assertions: Sequence[str] = (),
    tag: str = PUBLISHED_TAG,
    declared_by: str = "",
) -> PublicationResult:
    """Gate `version`, then advance `published` to it. Returns the range the notification should carry.

    Fail-closed in every direction: an out-of-range version raises before anything is read, and a
    failed assertion returns `published=False` with the tag untouched, so the previously published
    version keeps serving. The assertions travel back either way — a blocked batch has to be
    auditable, not merely rejected.

    The caller is already authorized; this decides only whether the DATA is good enough, which is the
    validator half of governance. FGA decides who MAY publish.

    `declared_by` names the project whose declared `GateSpec` supplied `key_column`, or `""` when the
    request did — see `EffectiveGate`. It is composed by the door, never resolved here, so `gate` and
    `publish` cannot end up under different policy.
    """
    if version < 1:
        raise InvalidInputError(f"version must be >= 1, got {version}")

    table_id = list(table_id)
    dataset = open_dataset(ns, storage_options, table_id)
    latest = int(dataset.version)
    if version > latest:
        raise TableVersionNotFoundError(f"version {version} not found (latest is {latest})")

    previous = _tag_version(ns, storage_options, table_id, tag)
    if previous is not None and version < previous:
        # Publishing backwards is a rollback, and a rollback arriving by accident — a late retry of an
        # older run — silently un-publishes newer good data. Checked BEFORE the gate: there is no point
        # spending a full scan on a version that cannot be published either way.
        raise InvalidTableStateError(
            f"refusing to move {tag!r} backwards from {previous} to {version}; roll back explicitly via the tag API if that is intended"
        )

    try:
        # Pin the candidate for the gate's duration — see PUBLISHING_TAG.
        _set_tag(ns, storage_options, table_id, PUBLISHING_TAG, version)

        # Gate the version being published, not `latest`: they differ the moment another writer commits
        # while this gate runs, and publishing a version nobody checked is the whole failure this
        # prevents.
        candidate = open_dataset(ns, storage_options, table_id, version=version)
        assertions = assert_quality(
            candidate.uri,
            storage_options,
            key_column=key_column,
            required_columns=tuple(required_columns),
            # The pin, carried. Passing only `candidate.uri` dropped it: `assert_quality` re-opened
            # the dataset bare and scanned `latest`, so this gate answered for a version it was not
            # publishing — in both directions, and silently in the one that matters.
            version=version,
        )
        # BEFORE the verdict, so an unrunnable gate can never reach the tag move below.
        refuse_a_gate_that_cannot_run(assertions, key_column=key_column, version=version, declared_by=declared_by)
        # And before that verdict too: a tier with no provenance is not a weak publish, it is an
        # untraceable one, and no assertion in the list above can see a column that is absent.
        refuse_a_tier_without_provenance(candidate.schema, version=version, has_stable_row_ids=getattr(candidate, "has_stable_row_ids", None))

        accepted: list[str] = []
        if not passed(assertions):
            failed = [a.assertion for a in assertions if not a.success]
            # An override names EXACTLY what it accepts, so it is a statement about known findings
            # rather than a blanket force that also waves through whatever appears later. Structural
            # findings are excluded first and unconditionally: naming one cannot publish it.
            waved = set(accept_assertions) - STRUCTURAL_ASSERTIONS
            unaccepted = [name for name in failed if name not in waved]
            if unaccepted:
                return PublicationResult(
                    table=candidate.uri,
                    published=False,
                    from_version=previous,
                    to_version=version,
                    assertions=assertions,
                    gate_source=gate_source_for(declared_by),
                    reason=f"quality gate failed: {', '.join(unaccepted)}",
                )
            accepted = sorted(set(failed))

        _set_tag(ns, storage_options, table_id, tag, version)
        return PublicationResult(
            table=candidate.uri,
            published=True,
            from_version=previous,
            to_version=version,
            assertions=assertions,
            accepted=accepted,
            gate_source=gate_source_for(declared_by),
        )
    finally:
        # Suppressed on purpose: a failure to unpin must never mask the publish outcome, and the worst
        # case is one version exempt from GC until someone deletes a visible tag.
        with contextlib.suppress(Exception):
            dataplane.delete_tag(ns, storage_options, DeleteTableTagRequest(id=table_id, tag=PUBLISHING_TAG))
