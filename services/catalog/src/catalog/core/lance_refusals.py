"""Lance's failures read as the Lance Namespace spec's refusals.

pylance raises a caller's mistake as a bare ``ValueError``, ``OSError`` or ``RuntimeError`` whose only discriminator is
its text. These translations read that text and answer the spec's coded error (``lance_namespace``'s typed errors), so a
caller's malformed expression, a write whose schema does not match, a column op naming a column that is not there or a
ref failure is a 4xx and not a 500. Each marker is measured on the pylance it names.
"""

import logging
import re
from collections.abc import Generator, Sequence
from contextlib import contextmanager

from lance_namespace import (
    ConcurrentModificationError,
    InternalError,
    InvalidInputError,
    LanceNamespaceError,
    ServiceUnavailableError,
    TableBranchAlreadyExistsError,
    TableBranchNotFoundError,
    TableColumnNotFoundError,
    TableSchemaValidationError,
    TableTagAlreadyExistsError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
)

from lakehouse.lancekit.commit_verdict import CommitVerdict, classify_commit_failure
from lakehouse.problems.outage import reads_as_store_outage


__all__ = [
    "caller_query",
    "caller_sql",
    "clean_lance_message",
    "column_op",
    "raise_if_store_outage",
    "ref_errors",
    "write_schema_errors",
]

log = logging.getLogger(__name__)


#: Lance prefixes a caller-expression failure with this, whatever exception class it picks.
_USER_INPUT_MARKER = "Invalid user input"


#: The Rust source location Lance writes after each error it raises — ``, /home/runner/work/lance/lance/rust/…:150:14``.
#: Useful in a server log, noise in an API response, and it publishes the build path of a vendored library
#: to every caller. Removed from the detail wherever it sits: a nested error carries one per layer, and the outer
#: layer's text follows the inner one's (measured live on pylance 13.0.0, explain_plan's parse error). The log line
#: still carries the original exception.
_RUST_SOURCE_LOCATION = re.compile(r",\s*/\S+?\.rs:\d+:\d+")


def clean_lance_message(message: str) -> str:
    """Lance's own text, minus the two things a caller must never receive.

    The ``Invalid user input`` prefix is replaced by our own action wording, and the Rust source location
    (``, /home/runner/work/lance/…/planner.rs:1019:20``) is trimmed: it is useful in a server log, noise in
    an API response, and it publishes the build path of a vendored library to every caller. Everything else
    survives — Lance frequently names the columns that DO exist, which is the whole value of the message.
    """
    body = message.split(_USER_INPUT_MARKER, 1)[1].lstrip(": ") if _USER_INPUT_MARKER in message else message
    return _RUST_SOURCE_LOCATION.sub("", body).strip()


@contextmanager
def caller_sql(action: str) -> Generator[None]:
    """Translate Lance's expression-parse failures into a 4xx instead of letting them escape as a 500.

    ``updates`` and ``predicate`` are SQL fragments the CALLER wrote, and Lance validates them at execution
    time — an unknown column or a syntax slip surfaces as ``Invalid user input: Schema error: No field named
    "Z". Valid fields are id, v.``. Uncaught, that became a 500 ``InternalError``, which is wrong in three
    ways: it tells the caller the server broke when their expression was wrong, it hides Lance's genuinely
    useful message (it lists the valid fields) behind a generic body, and it spends the error budget — a 500
    is what alerting watches — on a client mistake. Found live 2026-07-26 driving
    ``POST /v1/table/{id}/update`` with an unquoted string literal, which Lance reads as a column reference.

    Discriminates on Lance's ``Invalid user input`` MARKER, not on the exception class, because the class is
    not consistent across operations: the same bad predicate is a ``ValueError`` out of ``update()`` and an
    ``OSError`` out of ``delete()`` (both observed here, identical message). Keying on the type would have
    fixed update and left delete throwing 500s — and keying on ``except Exception`` would flip a genuine
    object-store outage into "fix your query". Anything without the marker propagates untouched.
    """
    try:
        yield
    except (ValueError, OSError) as exc:
        message = str(exc)
        if _USER_INPUT_MARKER not in message:
            raise  # a storage / IO failure: a real 5xx, and it stays one
        # Keep Lance's text — it names the columns that do exist — minus its prefix, which we replace, and
        # minus the Rust source location, which the caller can do nothing with.
        detail = clean_lance_message(message)
        log.info("user_sql_rejected", extra={"action": action, "error": message})
        raise InvalidInputError(f"{action}: {detail}") from exc


#: The two shapes a query-plane refusal of the caller's expression arrives in (measured on pylance 13.0.0, through the
#: `dir` backend): ``query_table`` raises a typed ``InternalError`` whose text is Lance's Debug form,
#: ``Failed to execute query: InvalidInput { source: "Error parsing statement: …", location: … }``, while
#: ``explain_table_query_plan`` and ``analyze_table_query_plan`` raise a bare ``RuntimeError`` carrying
#: ``Failed to explain query plan for table at '<location>': Invalid user input: …``.
_QUERY_INPUT_MARKERS = (_USER_INPUT_MARKER, "InvalidInput {")


#: A column the caller named that the table lacks, in either shape: Display ``No field named nope.`` and Debug
#: ``FieldNotFound { field: Column { relation: None, name: "nope" }``.
_QUERY_MISSING_FIELD = re.compile(r'No field named (?P<display>[^\s.]+)|FieldNotFound \{ field: Column \{ relation: [^,]*, name: "(?P<debug>[^"]+)"')


#: The caller's own text in the Debug shape: ``InvalidInput { source: "<message>", location: … }``.
_QUERY_DEBUG_SOURCE = re.compile(r'InvalidInput \{ source: "(?P<source>(?:[^"\\]|\\.)*)"')


@contextmanager
def caller_query(action: str) -> Generator[None]:
    """Answer a query-plane refusal of the caller's request as the caller's mistake ([[LH-287]]).

    A filter naming an absent column is the spec's 12 ``TableColumnNotFound`` (404), and any other expression Lance
    refuses (a syntax slip, a literal of the wrong type) is 13 ``InvalidInput`` (400); everything without one of
    Lance's input markers propagates untouched, so a store outage stays a 5xx. The detail names the column or carries
    Lance's message with its location prefix and Rust source suffix removed: the raw text names the table's storage
    location and Lance's build paths, so it is logged and never answered.
    """
    try:
        yield
    except (InternalError, RuntimeError, ValueError, OSError) as exc:
        message = str(exc)
        if not any(marker in message for marker in _QUERY_INPUT_MARKERS):
            raise
        log.info("caller_query_rejected", extra={"action": action, "error": message[:1000]})
        if (missing := _QUERY_MISSING_FIELD.search(message)) is not None:
            raise TableColumnNotFoundError(f"{action}: the table has no column {missing.group('display') or missing.group('debug')!r}") from exc
        if (debug := _QUERY_DEBUG_SOURCE.search(message)) is not None:
            detail = debug.group("source").replace('\\"', '"').replace("\\\\", "\\")
        else:
            detail = message.rsplit(_USER_INPUT_MARKER, 1)[-1]
        raise InvalidInputError(f"{action}: {clean_lance_message(detail.lstrip(': '))}") from exc


#: Lance's schema check on a write, raised as a bare `OSError`. A stable Rust error-variant prefix, not
#: a path: "Append with different schema: `s` should have type string but type was int64" for a type
#: mismatch, "…: fields did not match, missing=[], unexpected=[zz]" for a column the table lacks.
_WRITE_SCHEMA_MARKER = "append with different schema:"


#: Lance giving up on a write it kept rebasing: "Too many concurrent writers. Attempted N times, but failed on
#: retry_timeout of 30.000 seconds." (pylance 13.0.0, `io/commit.rs`; measured 2026-11-16 with 32 clients appending to one
#: table). Nothing was committed, so the write is safe to send again.
_WRITE_GAVE_UP_MARKER = "too many concurrent writers"


@contextmanager
def write_schema_errors() -> Generator[None]:
    """Give the in-process write path the schema code the NATIVE path already answers.

    `insert_into_table` and `merge_insert_into_table` split on `branch`: without one they delegate to
    the native backend, which maps a schema mismatch to `TableSchemaValidationError` (20 -> 400); with
    one they run pylance in-process, where the same mismatch escapes as a bare `OSError` and is
    reported `Internal 18`. Measured 2026-09-07 across three payload shapes — wrong Arrow type, an
    extra column, a wholly unrelated schema — main answered 20 for all three and the branch answered
    500 for all three.

    So this is PARITY with a correct implementation next door, not a fresh judgement about which code
    a schema failure deserves: the branchless door already decided, and a caller must not get a
    different answer for the same mistake because they staged it on a branch.

    A write Lance gives up on because the table is too busy (`_WRITE_GAVE_UP_MARKER`) is answered here too, as
    `ConcurrentModificationError` (14 -> 409): it committed nothing and is safe to send again.
    """
    try:
        yield
    except OSError as exc:
        message = str(exc).lower()
        if _WRITE_GAVE_UP_MARKER in message:
            # Contention, not a fault of the server or the request: the caller's next attempt lands once the table is
            # less busy, and 500 would have a client stop and an operator paged.
            raise ConcurrentModificationError(
                "the table took too many concurrent writes for this one to commit; nothing was written, so send the write again"
            ) from exc
        if _WRITE_SCHEMA_MARKER not in message:
            raise  # a storage / IO failure: a real 5xx, and it stays one
        raise TableSchemaValidationError(clean_lance_message(str(exc))) from exc


#: A column op names a column that is not there. Lance says it four ways across three exception classes —
#: ``Column nope does not exist in the dataset`` (drop, ValueError), ``Invalid user input: Column "nope"
#: does not exist in the dataset`` (rename, OSError), ``Field 'nope' not found.\nAvailable fields: ['id']``
#: (update_field_metadata, OSError) — and the SPEC has a code for exactly this: 12 TableColumnNotFound (404).
#: Matched against pylance 9.0.0's real messages (probed, not guessed), so a pylance bump that rewords them
#: reds a test rather than silently reverting these paths to 500s.
_COLUMN_NOT_FOUND = re.compile(r"does not exist in the dataset|field '[^']*' not found", re.IGNORECASE)


#: Client mistakes Lance reports WITHOUT its ``Invalid user input`` marker. Each is a refusal of a
#: well-formed request the caller got wrong, i.e. 400 — never a server fault. The primary-key one is a
#: ``LanceError(Schema)`` OSError on pylance 12.0.0 for ``nullable=True`` on the key or an ancestor
#: ([[LH-208]]); it answered 500 Internal before this.
_COLUMN_BAD_REQUEST = re.compile(r"cannot drop all columns|primary key column and all its ancestors must not be nullable", re.IGNORECASE)


#: Messages that already enumerate the schema for the caller; appending our own list would just repeat it.
_LISTS_FIELDS = re.compile(r"valid fields|available fields", re.IGNORECASE)


@contextmanager
def column_op(action: str, fields: Sequence[str] = ()) -> Generator[None]:
    """Translate a schema-evolution failure the CALLER caused into the spec's 4xx, not a 500.

    The sibling of :func:`caller_sql` for the four column ops (add / alter / drop / update_field_metadata).
    It is a separate translator rather than a widening of ``caller_sql`` because ``caller_sql``'s single
    ``Invalid user input`` marker test is load-bearing for update/delete and would miss half of these:
    ``drop_columns`` raises an unmarked ``ValueError`` for both of its user errors. Seven distinct client
    mistakes answered 500 ``InternalError`` with detail "Internal Server Error" before this (#101) — the
    lakehouse UI renders that detail verbatim, so a user who typed a wrong column name read that the server
    had broken.

    Classification, in order:

    * ``LanceNamespaceError`` — already typed (``TableBranchNotFound`` out of the door's open, our own
      ``InvalidInput`` for an unsupported cast target): re-raised untouched.
    * a message naming a MISSING column → ``TableColumnNotFoundError`` (code 12 → 404). ``fields`` is the
      live schema, appended when Lance's own text does not already list it, so the answer always tells the
      caller both what was missing and what is there.
    * Lance's ``Invalid user input`` marker, or an unmarked message on the known-bad-request list →
      ``InvalidInputError`` (400).
    * anything else propagates — an object-store outage stays a 5xx instead of being blamed on the caller.
    """
    try:
        yield
    except LanceNamespaceError:
        raise
    except (ValueError, OSError) as exc:
        message = str(exc)
        detail = clean_lance_message(message)
        if _COLUMN_NOT_FOUND.search(message):
            if fields and not _LISTS_FIELDS.search(detail):
                detail = f"{detail}. Valid fields are {', '.join(fields)}"
            log.info("column_op_rejected", extra={"action": action, "error": message})
            raise TableColumnNotFoundError(f"{action}: {detail}") from exc
        verdict = classify_commit_failure(exc)
        if verdict is CommitVerdict.RETRYABLE_CONFLICT:
            # A LOST RACE, NOT A BAD REQUEST — and the difference is what the caller should do next.
            # Measured 2026-09-07: six concurrent `add_columns` on one table, five lose with
            # `OSError("Retryable commit conflict for version 2: This Merge transaction was preempted by
            # concurrent transaction ...")`. Reported as Internal 18 that reads as "the server broke", so
            # a client retries nothing and an operator is paged for contention that resolves itself.
            # Code 14 says re-read and re-commit, which is exactly what Lance calls it: retryable.
            #
            # AFTER the missing-column test on purpose: the conflict vocabulary carries the bare word
            # `concurrent`, so a column actually NAMED `concurrent` would match here — it is caught above
            # as the 12 it really is, and a genuine conflict never looks like a missing column (both
            # directions verified against the two regexes).
            raise ConcurrentModificationError(f"{action}: {detail} — re-read the table version and retry") from exc
        if verdict is CommitVerdict.INCOMPATIBLE:
            # The OTHER conflict, which this guard could not see while it matched a flat tuple: the table
            # was REPLACED underneath the op, so retrying is what corrupts it. Sharing the ordered verdict
            # is what makes the distinction reach every door at once rather than one at a time.
            raise InvalidInputError(
                f"{action}: {detail} — this is NOT retryable: the table changed underneath it (e.g. a concurrent overwrite). "
                "Re-read the current version and re-plan; do not retry this operation"
            ) from exc
        if _USER_INPUT_MARKER in message or _COLUMN_BAD_REQUEST.search(message):
            log.info("column_op_rejected", extra={"action": action, "error": message})
            raise InvalidInputError(f"{action}: {detail}") from exc
        raise  # not attributable to the request: a real 5xx, and it stays one


def raise_if_store_outage(exc: BaseException, *, table: str) -> None:
    """Raise 503 for a read the store did not answer, which says nothing about whether the window can be followed.

    Refusing it as unfollowable (409, "resynchronise from 0") would send a consumer to re-read the whole
    table because the store blinked.

    Raises:
        ServiceUnavailableError: ``exc`` is an object-store outage (`lakehouse.problems.outage`).
    """
    if reads_as_store_outage(exc):
        raise ServiceUnavailableError(f"the object store did not answer reading the change window on table {table}") from exc


#: Substrings that PROVE a ref operation failed on the VERSION rather than on the ref. Two spellings
#: because two layers answer: pylance's ref API raises `ValueError("Version not found error: version
#: main:999999 does not exist")`, while `create_branch` reaches the object store first and raises
#: `OSError("Dataset at path .../_versions/999999.manifest was not found")`. Measured on pylance 10.0.0.
_REF_VERSION_MISSING_MARKERS = ("version not found", ".manifest was not found")


#: The ref failure's own noun. pylance flattens every tag and branch failure into a bare
#: `ValueError` whose only discriminator is its text, because the Rust layer's typed variants do not
#: cross the pyo3 boundary.
#:
#: It captures the NOUN rather than testing `"branch" in message`, and that precision is load-bearing:
#: a tag literally named `branch` would make the substring test report code 22 for a missing tag. The
#: noun the message names also BEATS the caller's own kind, because a tag op scoped to a branch fails
#: on the BRANCH — answering 8 there sends the caller hunting a tag that was never the problem.
_REF_FAILURE_RE = re.compile(r"ref (?P<failure>not found|conflict) error:\s*(?P<noun>tag|branch)\b", re.IGNORECASE)


#: The ref-NAME validator's variant, raised before anything is looked up — a malformed name is a
#: malformed parameter, so it is `InvalidInput` (13 -> 400), never a not-found. Measured on pylance
#: 10.0.0 for both refs and every rule it enforces: invalid characters, a `.lock` suffix, a leading or
#: trailing `/`, consecutive `/`, and `..` inside a segment. The wording differs per ref ("Branch
#: segment ... contains invalid characters" vs "Ref characters must be either alphanumeric ..."), so
#: the shared PREFIX is what is matched.
_REF_INVALID_MARKER = "ref is invalid:"


def _classify_ref_error(exc: Exception, *, kind: str, name: str, invalid_name: str | None = None) -> Exception:
    """Map a pylance tag/branch failure onto the Lance Namespace spec's coded error.

    Returns ``exc`` UNCHANGED when nothing matches, so an unrecognised failure stays an honest
    Internal rather than being forced into a code that would misdirect the caller.
    """
    message = str(exc)
    if _REF_INVALID_MARKER in message.lower():
        # A branch create names its SOURCE for a missing version but the NEW name for a malformed one.
        subject = name if invalid_name is None else invalid_name
        return InvalidInputError(f"invalid {kind} name {subject!r}: {clean_lance_message(message)}")
    if any(marker in message.lower() for marker in _REF_VERSION_MISSING_MARKERS):
        # Lance's text names the manifest's storage path, so the caller is told the version, never the text.
        return TableVersionNotFoundError(f"no such version for {kind} {name!r}")
    match = _REF_FAILURE_RE.search(message)
    if match is None:
        return exc
    noun = match.group("noun").lower()
    if match.group("failure").lower() == "conflict":
        if noun == "branch":
            return TableBranchAlreadyExistsError(f"branch {name!r} already exists")
        return TableTagAlreadyExistsError(f"tag {name!r} already exists")
    if noun == "branch":
        return TableBranchNotFoundError(f"branch {name!r} not found")
    return TableTagNotFoundError(f"tag {name!r} not found")


@contextmanager
def ref_errors(kind: str, name: str, *, invalid_name: str | None = None) -> Generator[None]:
    """Translate the tag/branch failures inside the block into their spec-coded errors.

    Without this every one of them reaches `install_problem_handlers` as a bare exception and is
    reported as **Internal 18** — a generated client dispatches on `code`, so "your tag already
    exists" arrives as "the server broke": unretryable, unactionable, and indistinguishable from a
    real fault in an alerting pipeline.
    """
    try:
        yield
    except (ValueError, OSError) as exc:
        translated = _classify_ref_error(exc, kind=kind, name=name, invalid_name=invalid_name)
        if translated is exc:
            raise
        log.info("ref_operation_refused", extra={"kind": kind, "ref": name, "error": str(exc)[:500]})
        raise translated from exc
