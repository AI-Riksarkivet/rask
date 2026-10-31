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

**This lives in the CATALOG, not in any writer.** A user's pipeline, a Ray job, a backfill script and
a person with catalog credentials must all publish the same way, or each reimplements the contract
and they drift. It is also the one home for the race: a tag file carries no format-level CAS, and
`UpdateTableTag` declares no conflict (`lance_docs/ns_catalog/spec.yaml` lists no 409 for it, though
the error prose's table names code 14), so `set_tag` converges a first publication's create race on
Lance's create refusal, and a later move of the tag is last-writer-wins.

Deliberately shaped after `services/models.py::promote`, which has run this exact pattern for the
model registry since it was written — gate first, fail-closed, then create-or-update the tag. Models
gate on metric thresholds and data gates on assertions, but the control flow is the same one, and
having two spellings of it would be the drift this module exists to prevent.

**THE TAG IS THE GATE, AND A BREACH IS HELD (ADR 0156 C1, [[LH-466]]).** Readers of a governed tier read the
version tagged `published`, so a version reaches them only through this door. When the project's declared gate
has review on, a version whose failed assertions a validator may waive, or whose row count moves past the band
from the published version's, is HELD rather than refused: the tag stays where it is, and the door records a
hold that a `can_promote` holder answers (`catalog.services.promotions`). A structural finding is refused
whatever review says, because no approval makes broken data right.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, Literal

from lance_namespace import (
    CreateTableTagRequest,
    DeleteTableTagRequest,
    InvalidInputError,
    InvalidTableStateError,
    TableTagAlreadyExistsError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
    UpdateTableTagRequest,
)
from pydantic import BaseModel, ConfigDict

from catalog.core.config import shared_lance_session
from catalog.services import dataplane
from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.quality import NOT_NULL, STRUCTURAL_ASSERTIONS, Assertion, assert_quality, tier_contract_violations


if TYPE_CHECKING:
    from collections.abc import Sequence

    from catalog.services.table_claims import DoorTable
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

#: The prefix of the tags the platform pins versions with (`promotions.HOLD_TAG`), as `rask.*` is its metadata's.
CATALOG_TAG_PREFIX = "rask."


def is_catalog_tag(tag: str) -> bool:
    """Whether ``tag`` is one the catalog moves itself and no client may create, move or delete.

    `published` is what readers of a governed tier read (ADR 0156 C1), so only this door's gate and a
    `can_promote` holder's approval move it; a tag door that moved it would publish past both. `publishing` and
    the `rask.` tags pin versions for those doors.
    """
    return tag in (PUBLISHED_TAG, PUBLISHING_TAG) or tag.startswith(CATALOG_TAG_PREFIX)


#: The two authorities that can name a gate's key column, as the result reports them.
DECLARED_GATE = "declared"
REQUESTED_GATE = "request"

#: The band's two reasons to hold a version for a person. `FIRST_PUBLICATION`: nothing is published yet, or what
#: is published is empty, so there is no row count to compare against and the band cannot judge. `ROW_COUNT_DELTA`:
#: the row count moved further from the published version's than the band allows.
FIRST_PUBLICATION = "first_publication"
ROW_COUNT_DELTA = "row_count_delta"


class ReviewPolicy(BaseModel):
    """The review half of a project's declared gate, present only when the project turned review on."""

    model_config = ConfigDict(frozen=True)

    #: How far the row count may move from the published version's, as a ratio of it.
    band: float
    #: How long a held version waits for a decision before the sweep expires its hold.
    hours: float


def review_reasons(*, rows: int, published_rows: int | None, band: float) -> list[str]:
    """Why a person should look at a version whose row count is ``rows`` — empty when the band passes it.

    ``published_rows`` is ``None`` when nothing is published. A band of zero holds every change of row count
    and passes an unchanged one.
    """
    if not published_rows:
        return [FIRST_PUBLICATION]
    if abs(rows - published_rows) > band * published_rows:
        return [ROW_COUNT_DELTA]
    return []


class Verdict(BaseModel):
    """What a version's findings decide: publish it, hold it for a `can_promote` holder, or refuse it."""

    model_config = ConfigDict(frozen=True)

    outcome: Literal["publish", "hold", "refuse"]
    #: Failed assertions the request waived; a publish carrying any was an explicit acceptance.
    accepted: list[str] = []
    #: What decided a hold or a refusal: the failed assertions not waived, then the band's reasons.
    reasons: list[str] = []

    @property
    def reason(self) -> str | None:
        """The sentence the result carries, or ``None`` for a publish."""
        match self.outcome:
            case "refuse":
                return f"quality gate failed: {', '.join(self.reasons)}"
            case "hold":
                return f"held for review: {', '.join(self.reasons)}"
            case "publish":
                return None


def judge_findings(assertions: Sequence[Assertion], *, accept_assertions: Sequence[str] = (), band_reasons: Sequence[str] = (), review: bool) -> Verdict:
    """Decide a version from its assertions and the band's reasons, under the project's review setting.

    A structural failure refuses, whatever review says or the request accepts. With review off a failed assertion
    the request did not accept refuses, and the band is not consulted. With review on, such a failure or a band
    reason holds the version for a person.
    """
    failed = [a.assertion for a in assertions if not a.success]
    # An acceptance names exactly what it accepts, so it is a statement about known findings rather than a
    # blanket force that also waves through whatever appears later. Structural findings are never waived.
    waived = set(accept_assertions) - STRUCTURAL_ASSERTIONS
    unaccepted = [name for name in failed if name not in waived]
    if any(name in STRUCTURAL_ASSERTIONS for name in unaccepted) or (unaccepted and not review):
        return Verdict(outcome="refuse", reasons=unaccepted)
    held = [*unaccepted, *band_reasons] if review else []
    if held:
        return Verdict(outcome="hold", reasons=held)
    return Verdict(outcome="publish", accepted=sorted(set(failed)))


def gate_source_for(declared_by: str) -> str:
    """Name the authority behind a key column, from the project that declared it (or `""`).

    ONE definition, because every result on both paths reports this field and a second spelling is how
    the wire ends up claiming a source the assertions did not run under.
    """
    return DECLARED_GATE if declared_by else REQUESTED_GATE


class EffectiveGate(BaseModel):
    """The assertions this publish actually runs, and on WHOSE authority.

    Two parties describe the gate at this door and they are not equals. The project's `GateSpec` is
    POLICY — declared through an admin-gated door and readable by anyone. The request's `key_column`/`required_columns` are a
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
    #: The declaration's review half when it turned review on, else `None`: only a project decides that a
    #: breach waits for a person, so a request can neither turn review on nor off.
    review: ReviewPolicy | None = None

    @property
    def gate_source(self) -> str:
        """`"declared"` or `"request"` — which authority named the key column.

        A property rather than a field for the reason `GateSpec.gate_source` states: a source a caller
        could set is a source a caller could lie about, and the whole value of the field is that it
        cannot be.
        """
        return gate_source_for(self.declared_by)


def declared_gate(registry_root: str, storage_options: StorageOptions, project: str) -> GateSpec | None:
    """This project's declared gate settings, or `None` when nobody declared any.

    NEVER RAISES, and the stance is held deliberately: an unresolvable declaration
    falls back rather than refusing, because failing every publish for a tenant over a settings lookup
    takes an estate down for a config blip, while the request's own values remain a real gate. Logged
    at ERROR because it is still a defect — a declaration nobody can read is a policy nobody is under.

    Read through `service_kit.lakehouse.gate_specs`, the record's one format, rather than a reader of
    its own.
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
    review = ReviewPolicy(band=spec.review_band, hours=spec.review_hours) if spec.review_enabled else None
    return EffectiveGate(key_column=spec.key_column, required_columns=union, declared_by=spec.project, review=review)


def _open_for_contract(uri: str, storage_options: StorageOptions, version: int) -> tuple[Any, bool | None, int]:
    """The schema, stable-row-id property and row count of ``version`` at ``uri`` — the gate's reads off the data.

    `publish` already holds an opened dataset and reads them off it; `gate` is handed a URI and has to
    open one. Pinned to the same `version` the assertions ran against, never `latest`, for the reason
    `assert_quality` records: another writer may have committed since, and answering about a version
    this call is not gating is the failure the pin exists to prevent.
    """
    import lance

    dataset = lance.dataset(uri, storage_options=storage_options.in_clear(), version=version, session=shared_lance_session())
    # EVERY READING FROM ONE OPEN. The schema answers the column half of the contract,
    # `has_stable_row_ids` answers the half the columns cannot show, and the row count is the band's;
    # opening twice would let a concurrent commit put them on different versions.
    return dataset.schema, getattr(dataset, "has_stable_row_ids", None), int(dataset.count_rows())


def refuse_a_tier_without_provenance(schema: Any, *, version: int, has_stable_row_ids: bool | None = None) -> None:
    """Raise 400 when a table carries a `source_rowid` that cannot name the row it descends from.

    At this door because every publisher passes it. A table without the column is untouched: a governed row needs
    no column (ADR 0156 C9), and `quality.tier_contract_violations` owns the rule; this function owns only the
    door's answer to it.
    """
    problems = tier_contract_violations(schema, has_stable_row_ids=has_stable_row_ids)
    if not problems:
        return
    raise InvalidInputError(
        f"version {version} carries `source_rowid` that cannot trace a row to its source: {'; '.join(problems)}. "
        "Write it as uint64 stable row ids of the source table, or leave the column out."
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
    an exact row delta by the change-feed consumer rule (`catalog.services.changes`) and keeps no
    bookmark of its own. `from_version` is None the first time a dataset is published — there is no prior published
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
    #: The gate breached under the project's review, so `to_version` waits for a `can_promote` holder and
    #: `published` stays at `from_version`. Always false on a publish or a refusal.
    held: bool = False
    #: Why it is held: the failed assertions a validator may waive, then the band's reasons.
    hold_reasons: list[str] = []

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


def _main_version_of_tag(table: DoorTable, so: StorageOptions, tag: str) -> int | None:
    """The main version a tag points at, or None when unset.

    Resolved where `GetTableTagVersion` resolves a tag (`dataplane.tag_reference`), so there is ONE resolution path,
    and read with its branch: these tags move on main alone, so one on a branch is refused rather than read as main's
    version of the same number.

    Raises:
        InvalidTableStateError: The tag names a version of a branch.
    """
    return dataplane.main_version_of_tag(
        table.open_unchecked(so), tag, remedy="every tag door refuses it, so it was written at the store, and is put back on main there"
    )


def published_version(table: DoorTable, so: StorageOptions) -> int | None:
    """The version `published` points at, or None when nothing has been published yet."""
    return _main_version_of_tag(table, so, PUBLISHED_TAG)


def set_tag(table: DoorTable, so: StorageOptions, tag: str, version: int) -> None:
    """Create the tag, or move it if it already exists.

    The spec splits create and update into two operations and refuses the wrong one, so which to call
    depends on state the caller cannot assume — the first publication of every dataset needs create,
    every later one needs update.

    THE READ IS A HINT, NOT THE DECISION, because reading then branching is a TOCTOU: two concurrent
    FIRST publications of one dataset both see an absent tag, both call create, and the loser took a 409
    for a publish that should simply have moved the tag to the version it asked for.

    THE ARBITRATION IS THE FORMAT'S AND IT ALREADY EXISTS. Measured on the installed pylance:
    `tags.create` against an existing tag raises "Ref conflict error: tag <name> already exists", while
    `tags.update` offers NO conditional form at all — so the create's refusal is the only compare-and-set
    Lance gives for a tag, and a conditional put on `_refs/tags/<name>.json` would mean writing the
    format's internals by hand. Reading that refusal as "someone else won the race, now move it" is the
    same shape `records.create_json` uses for the registry, where a 409 means someone else won rather
    than that this failed.
    """
    if _main_version_of_tag(table, so, tag) is None:
        try:
            dataplane.create_tag(table, so, CreateTableTagRequest(id=table.segments, tag=tag, version=version))
            return
        except TableTagAlreadyExistsError:
            pass  # the racer created it between the read and here — converge below
    dataplane.update_tag(table, so, UpdateTableTagRequest(id=table.segments, tag=tag, version=version))


def advance(table: DoorTable, so: StorageOptions, version: int, *, tag: str = PUBLISHED_TAG) -> tuple[bool, int | None]:
    """Move ``tag`` forward to ``version``: whether it moved, and the version it named before.

    Read again right before the move, so a publication that landed after the caller's own read is never moved back
    over. The read and the move are two operations, because a Lance tag has no conditional update, so two
    publications inside that gap can still race.
    """
    current = _main_version_of_tag(table, so, tag)
    if current is not None and current >= version:
        return False, current
    set_tag(table, so, tag, version)
    return True, current


def drop_tag(table: DoorTable, so: StorageOptions, tag: str) -> None:
    """Delete the tag; one that is already gone is the outcome asked for, not an error."""
    with contextlib.suppress(TableTagNotFoundError):
        dataplane.delete_tag(table, so, DeleteTableTagRequest(id=table.segments, tag=tag))


def rows_at(table: DoorTable, so: StorageOptions, version: int | None) -> int | None:
    """The row count of ``version`` on main, or ``None`` for no version. Read off the manifest, not a scan."""
    return None if version is None else int(table.open(so, version=version).count_rows())


def gate(
    uri: str,
    *,
    key_column: str,
    version: int,
    required_columns: Sequence[str] = (),
    storage_options: StorageOptions | None = None,
    declared_by: str = "",
    review: ReviewPolicy | None = None,
    published_rows: int | None = None,
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
    two doors cannot resolve one differently. `review` is that declaration's review half and
    `published_rows` the published version's row count, so the answer says whether the act would HOLD.
    """
    assertions = assert_quality(
        uri,
        storage_options or StorageOptions(),
        key_column=key_column,
        required_columns=tuple(required_columns),
        version=version,
        session=shared_lance_session(),
    )
    refuse_a_gate_that_cannot_run(assertions, key_column=key_column, version=version, declared_by=declared_by)
    # THE SAME REFUSAL `publish` RAISES, and it belongs here for the reason the docstring above gives:
    # a gate that under-reports sends the promotion review an approval the ACT will then refuse, and
    # the reviewer has no way to see it coming. Opening the dataset a second time is the cost of
    # answering the same question — `assert_quality` above opened it for the assertions, and the
    # contract is a schema read, not a scan.
    contract_schema, stable_ids, rows = _open_for_contract(uri, storage_options or StorageOptions(), version)
    refuse_a_tier_without_provenance(contract_schema, version=version, has_stable_row_ids=stable_ids)
    band = review_reasons(rows=rows, published_rows=published_rows, band=review.band) if review is not None else []
    verdict = judge_findings(assertions, band_reasons=band, review=review is not None)
    return PublicationResult(
        table=uri,
        published=False,
        from_version=None,
        to_version=version,
        assertions=list(assertions),
        gate_source=gate_source_for(declared_by),
        reason=f"gate only: {verdict.reason}" if verdict.reason else None,
        held=verdict.outcome == "hold",
        hold_reasons=verdict.reasons if verdict.outcome == "hold" else [],
    )


def publish(
    table: DoorTable,
    storage_options: StorageOptions,
    *,
    version: int,
    key_column: str,
    required_columns: Sequence[str] = (),
    accept_assertions: Sequence[str] = (),
    tag: str = PUBLISHED_TAG,
    declared_by: str = "",
    review: ReviewPolicy | None = None,
) -> PublicationResult:
    """Gate `version`, then advance `published` to it. Returns the range the notification should carry.

    Fail-closed in every direction: an out-of-range version raises before anything is read, and a
    refused or held version returns `published=False` with the tag untouched, so the previously published
    version keeps serving. The assertions travel back either way — a blocked batch has to be
    auditable, not merely rejected. `review` is the project's declared review (see `judge_findings`); a
    HELD result is the caller's to record (`catalog.services.promotions`).

    The caller is already authorized; this decides only whether the DATA is good enough, which is the
    validator half of governance. FGA decides who MAY publish.

    `declared_by` names the project whose declared `GateSpec` supplied `key_column`, or `""` when the
    request did — see `EffectiveGate`. It is composed by the door, never resolved here, so `gate` and
    `publish` cannot end up under different policy.

    ``table`` is the table the door resolved: every read and every tag move is at its location ([[LH-530]]).
    """
    if version < 1:
        raise InvalidInputError(f"version must be >= 1, got {version}")

    table_id = table.segments
    dataset = table.open(storage_options)
    latest = int(dataset.version)
    if version > latest:
        raise TableVersionNotFoundError(f"version {version} not found (latest is {latest})")

    previous = _main_version_of_tag(table, storage_options, tag)
    if previous is not None and version < previous:
        # Publishing backwards is a rollback, and a rollback arriving by accident — a late retry of an
        # older run — silently un-publishes newer good data. Checked BEFORE the gate: there is no point
        # spending a full scan on a version that cannot be published either way.
        raise InvalidTableStateError(
            f"refusing to move {tag!r} backwards from {previous} to {version}; roll back explicitly via the tag API if that is intended"
        )

    try:
        # Pin the candidate for the gate's duration — see PUBLISHING_TAG.
        set_tag(table, storage_options, PUBLISHING_TAG, version)

        # Gate the version being published, not `latest`: they differ the moment another writer commits
        # while this gate runs, and publishing a version nobody checked is the whole failure this
        # prevents.
        candidate = table.open(storage_options, version=version)
        assertions = assert_quality(
            candidate.uri,
            storage_options,
            key_column=key_column,
            required_columns=tuple(required_columns),
            # The pin, carried. Passing only `candidate.uri` dropped it: `assert_quality` re-opened
            # the dataset bare and scanned `latest`, so this gate answered for a version it was not
            # publishing — in both directions, and silently in the one that matters.
            version=version,
            session=shared_lance_session(),
        )
        # BEFORE the verdict, so an unrunnable gate can never reach the tag move below.
        refuse_a_gate_that_cannot_run(assertions, key_column=key_column, version=version, declared_by=declared_by)
        # And before that verdict too: a tier with no provenance is not a weak publish, it is an
        # untraceable one, and no assertion in the list above can see a column that is absent.
        refuse_a_tier_without_provenance(candidate.schema, version=version, has_stable_row_ids=getattr(candidate, "has_stable_row_ids", None))

        # The band compares the row count readers would move to with the one they read now.
        band = (
            review_reasons(rows=int(candidate.count_rows()), published_rows=rows_at(table, storage_options, previous), band=review.band)
            if review is not None
            else []
        )
        verdict = judge_findings(assertions, accept_assertions=accept_assertions, band_reasons=band, review=review is not None)
        if verdict.outcome != "publish":
            return PublicationResult(
                table=candidate.uri,
                published=False,
                from_version=previous,
                to_version=version,
                assertions=assertions,
                gate_source=gate_source_for(declared_by),
                reason=verdict.reason,
                held=verdict.outcome == "hold",
                hold_reasons=verdict.reasons if verdict.outcome == "hold" else [],
            )

        set_tag(table, storage_options, tag, version)
        return PublicationResult(
            table=candidate.uri,
            published=True,
            from_version=previous,
            to_version=version,
            assertions=assertions,
            accepted=verdict.accepted,
            gate_source=gate_source_for(declared_by),
        )
    finally:
        # Suppressed on purpose: a failure to unpin must never mask the publish outcome, and the worst
        # case is one version exempt from GC until someone deletes a visible tag.
        with contextlib.suppress(Exception):
            dataplane.delete_tag(table, storage_options, DeleteTableTagRequest(id=table_id, tag=PUBLISHING_TAG))
