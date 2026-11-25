"""Structured compliance audit trail — a dedicated audit-event stream, separate from operational logs.

Every security-relevant action (authn success/failure, authz allow/deny, credential vending, admin ops)
is emitted as one structured event on the ``lance.audit`` logger: who (subject), what (action), the resource,
the outcome, and when (the record timestamp). A dedicated logger name keeps audit records a single,
filterable stream — routable and retainable independently of the noisy app INFO tier — and they export via
OTLP to GreptimeDB like every other log, queryable by ``audit.action`` / ``audit.outcome`` / ``audit.subject``
(the dedicated ``lance.audit`` logger name).

The whole trail is gated by one flag (:func:`configure_audit`, from a service's ``RASK_AUDIT_ENABLED``
setting): a deployment turns audit on or off for its compliance posture by setting the dedicated logger's
level. Fields are consistent across every call site, so a record never misses who / what / outcome.

The log path the records ride is not lossless, so each record emitted is also counted on the metric path
(``audit.records``, by action and outcome): the platform's store is whole over a window when it holds as many records
as were counted for it.
"""

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Final

from opentelemetry import metrics

from lakehouse.governed import fga_ids
from lakehouse.process.correlation import NO_TRACE, current_trace_id


#: The one well-known audit logger name (not a per-module logger) so audit records are a single stream.
AUDIT_LOGGER = "lance.audit"
_log = logging.getLogger(AUDIT_LOGGER)

#: Every record the trail emits, counted on the METRIC path, by action and outcome. The records themselves ride the log
#: path, which can lose one (a full export queue, a process killed before its batch left, a gateway's retry window run
#: out) without a word, and a record is the only account of who granted or was refused what. A store holding fewer
#: records over a window than this counts for it has lost some ([[LH-585]]). Counted where the record is handed to the
#: logger, so a trail that is switched off counts nothing.
_records = metrics.get_meter("lance.audit").create_counter(
    "audit.records",
    unit="{record}",
    description="Audit records this process emitted, by action and outcome: compare with the records the store holds.",
)

#: The record's format version, stamped on every record as ``audit.format``. A compliance query names the fields of
#: one format; a renamed or retyped field is a new format, so the query that read the old one can tell it is reading
#: records it was not written for instead of silently matching nothing.
AUDIT_FORMAT: Final = "2.0"


class AuditOutcome(StrEnum):
    """The closed outcome vocabulary a compliance query groups by: an authorization verdict, or what an action did."""

    ALLOW = "allow"
    DENY = "deny"
    SUCCESS = "success"
    FAILURE = "failure"


class AuditAction(StrEnum):
    """The lakehouse's closed action vocabulary: every action a lakehouse service records is one of these.

    An authorization verdict is ``AUTHZ`` and names the relation it decided in ``audit.relation``, so "every verdict on
    this table" is one filter however many relations the model grows. A listing that judges its own candidates records
    ``AUTHZ_LISTING`` once per page, with how many it judged and how many it served. A client of the lakehouse records under a closed
    vocabulary of its own: :func:`audit` takes any ``StrEnum``, so no free string reaches the trail from anywhere.
    """

    AUTHN = "authn"
    AUTHZ = "authz"
    AUTHZ_DECISION = "authz_decision"
    AUTHZ_LISTING = "authz_listing"
    READ_DATA = "read_data"
    VEND_CREDENTIALS = "vend_credentials"
    COMMIT_VERSION = "commit_version"
    COMMIT_VERSION_ANNOUNCED = "commit_version_announced"
    WRITE_JUDGE = "write_judge"
    EVENT_STREAM_OPENED = "event_stream_opened"
    LIST_PROJECTS = "list_projects"
    ACCESS_GRANT = "access_grant"
    ACCESS_REVOKE = "access_revoke"
    ACCESS_REVIEW = "access_review"
    ACCESS_SIMULATE = "access_simulate"
    ACCESS_SIMULATE_HYPOTHETICAL = "access_simulate_hypothetical"
    ACCESS_GRAPH = "access_graph"
    ACCESS_TUPLES_READ = "access_tuples_read"
    ACCESS_TUPLE_WRITE = "access_tuple_write"
    ACCESS_TUPLE_DELETE = "access_tuple_delete"
    ACCESS_LIST_OBJECTS = "access_list_objects"
    ACCESS_LIST_USERS = "access_list_users"
    ACCESS_EXPAND = "access_expand"
    MANAGED_ACCESS_SET = "managed_access_set"
    PROJECT_MEMBER_GRANT = "project_member_grant"
    PROJECT_MEMBER_REVOKE = "project_member_revoke"
    PRINCIPAL_DELETE = "principal_delete"
    ROLE_CREATE = "role_create"
    ROLE_DELETE = "role_delete"
    ROLE_ASSIGN = "role_assign"
    ROLE_UNASSIGN = "role_unassign"
    ERASURE_OBLIGATION_RECORDED = "erasure_obligation_recorded"
    ERASURE_REF_OBLIGED = "erasure_ref_obliged"
    ERASURE_HIDDEN_BRANCH_PURGED = "erasure_hidden_branch_purged"
    ERASURE_OBLIGATION_VERIFIED = "erasure_obligation_verified"
    EXPIRE_VERSIONS = "expire_versions"
    DLQ_REPLAY = "dlq_replay"
    DLQ_PARKED_REPLAY = "dlq_parked_replay"
    CLONE_ENDING_FINISH = "clone_ending_finish"
    TRASH_RELEASE = "trash_release"
    TRASH_END = "trash_end"
    IDEMPOTENCY_KEYS_RECLAIM = "idempotency_keys_reclaim"
    ANNOUNCEMENT_INTENTS_FINISH = "announcement_intents_finish"


def configure_audit(*, enabled: bool) -> None:
    """Enable or disable the audit stream at boot via the dedicated logger's level.

    Enabled → ``INFO`` (records emit through the OTLP handler on root). Disabled → a level above ``CRITICAL``
    so the ``INFO`` audit records are dropped at the logger, before any handler. Call once per service at
    startup. Without this call the logger follows the root's level, the one ``RASK_LOG_LEVEL`` sets, and the log level
    would decide the compliance trail.
    """
    _log.setLevel(logging.INFO if enabled else logging.CRITICAL + 1)


#: What a read record names as its columns when the read was not narrowed to named ones.
ALL_COLUMNS: Final = "all"


def about_table(name: str, uuid: str | None) -> dict[str, str]:
    """The fields a record of an act on a table names it by: ``resource``, the object its grants are held by
    (``table:<uuid>``, the spelling every act on the table is joined by), and ``table``, its name. A table the catalog has
    no uuid for is named by its name's label, as a judgement of an address is."""
    return {"resource": fga_ids.table_object(uuid) if uuid else fga_ids.addressed_object("table", name), "table": name}


def audit_read(
    *,
    subject: str,
    resource: str,
    version: int | None = None,
    columns: Sequence[str] | None = None,
    **fields: Any,
) -> None:
    """Record that a subject READ data — the question a lakehouse buyer expects answered (§ J1).

    The estate already authorizes every read and then forgot it happened, so "was this table ever read
    by that subject" had no answer even though allowing it was a deliberate decision. That is a
    zero-trust gap as much as a feature one.

    NOT A MIDDLEWARE, which is what § J1 first proposed. A middleware sees the path and the principal
    and nothing else, and `version` + `columns` are what make a read record worth keeping: only the
    route knows which version it served and which columns it returned. So the doors call this, and this
    fixes the action name and the field set — the part that makes the resulting log queryable.

    ``columns`` is always recorded: the names joined, or :data:`ALL_COLUMNS` when the read named none, because an
    absent field cannot be told apart from a door that forgot to say. It is joined rather than passed as a list
    because the audit record is a FLAT structure by design (`audit()`'s own contract), and a nested value would land
    differently in every sink.
    """
    audit(
        AuditAction.READ_DATA,
        AuditOutcome.SUCCESS,
        subject=subject,
        resource=resource,
        **({"version": version} if version is not None else {}),
        columns=",".join(columns) if columns else ALL_COLUMNS,
        **fields,
    )


def audit(
    action: StrEnum,
    outcome: AuditOutcome,
    *,
    subject: str | None = None,
    resource: str | None = None,
    **fields: Any,
) -> None:
    """Emit one structured audit event (dropped when audit is disabled — see :func:`configure_audit`).

    ``action`` is a member of a closed vocabulary (:class:`AuditAction` for the lakehouse); ``outcome`` one of
    :class:`AuditOutcome`; ``subject`` the verified principal; ``resource`` the object acted on, as the object its grants
    are held by (`fga_ids`; a table with its name beside it, :func:`about_table`), or, for a judgement made on an address
    before or without an object (a write judged by its name, a refusal of a name that holds nothing), the address's label
    (`fga_ids.addressed_object`). One spelling per object is what lets a query join every act on it. Every record carries
    ``audit.format`` (:data:`AUDIT_FORMAT`) and the id of the trace it was made under (``audit.trace_id``, ``-`` outside
    one, ADR 0209), so the records one request made are one filter, and the same id finds its log lines and its trace.
    Extra ``fields`` (relation, tier, reason) ride the same record under an ``audit.`` prefix so a compliance query sees a
    flat, consistent schema.
    """
    if not _log.isEnabledFor(logging.INFO):
        return
    _records.add(1, {"action": action.value, "outcome": outcome.value})
    _log.info(
        "audit",
        extra={
            "audit.format": AUDIT_FORMAT,
            "audit.action": action.value,
            "audit.outcome": outcome.value,
            "audit.trace_id": current_trace_id() or NO_TRACE,
            "audit.subject": subject or "",
            "audit.resource": resource or "",
            **{f"audit.{key}": value for key, value in fields.items()},
        },
    )


__all__ = [
    "ALL_COLUMNS",
    "AUDIT_FORMAT",
    "AUDIT_LOGGER",
    "AuditAction",
    "AuditOutcome",
    "about_table",
    "audit",
    "audit_read",
    "configure_audit",
]
