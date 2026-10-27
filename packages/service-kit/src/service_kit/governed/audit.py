"""Structured compliance audit trail — a dedicated audit-event stream, separate from operational logs.

Every security-relevant action (authn success/failure, authz allow/deny, credential vending, admin ops)
is emitted as one structured event on the ``lance.audit`` logger: who (subject), what (action), the resource,
the outcome, and when (the record timestamp). A dedicated logger name keeps audit records a single,
filterable stream — routable and retainable independently of the noisy app INFO tier — and they export via
OTLP to GreptimeDB like every other log, queryable by ``audit.action`` / ``audit.outcome`` / ``audit.subject``
(the dedicated ``lance.audit`` logger name).

The whole trail is gated by one flag (:func:`configure_audit`, from a service's ``LANCE_AUDIT_ENABLED``
setting): a deployment turns audit on or off for its compliance posture by setting the dedicated logger's
level. Fields are consistent across every call site, so a record never misses who / what / outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Final

from service_kit.context import current_request_id


#: The one well-known audit logger name (not a per-module logger) so audit records are a single stream.
AUDIT_LOGGER = "lance.audit"
_log = logging.getLogger(AUDIT_LOGGER)

#: The record's format version, stamped on every record as ``audit.format``. A compliance query names the fields of
#: one format; a renamed or retyped field is a new format, so the query that read the old one can tell it is reading
#: records it was not written for instead of silently matching nothing.
AUDIT_FORMAT: Final = "1.0"


class AuditOutcome(StrEnum):
    """The closed outcome vocabulary a compliance query groups by: an authorization verdict, or what an action did."""

    ALLOW = "allow"
    DENY = "deny"
    SUCCESS = "success"
    FAILURE = "failure"


class AuditAction(StrEnum):
    """The lakehouse's closed action vocabulary: every action a lakehouse service records is one of these.

    An authorization verdict is ``AUTHZ`` and names the relation it decided in ``audit.relation``, so "every verdict on
    this table" is one filter however many relations the model grows. A client of the lakehouse records under a closed
    vocabulary of its own: :func:`audit` takes any ``StrEnum``, so no free string reaches the trail from anywhere.
    """

    AUTHN = "authn"
    AUTHZ = "authz"
    AUTHZ_DECISION = "authz_decision"
    READ_DATA = "read_data"
    VEND_CREDENTIALS = "vend_credentials"
    COMMIT_VERSION = "commit_version"
    COMMIT_VERSION_ANNOUNCED = "commit_version_announced"
    TIER_JUDGE = "tier_judge"
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
    ERASURE_OBLIGATION_RECORDED = "erasure_obligation_recorded"
    ERASURE_REF_OBLIGED = "erasure_ref_obliged"
    ERASURE_OBLIGATION_VERIFIED = "erasure_obligation_verified"
    EXPIRE_VERSIONS = "expire_versions"
    DLQ_REPLAY = "dlq_replay"
    DLQ_PARKED_REPLAY = "dlq_parked_replay"


def configure_audit(*, enabled: bool) -> None:
    """Enable or disable the audit stream at boot via the dedicated logger's level.

    Enabled → ``INFO`` (records emit through the OTLP handler on root). Disabled → a level above ``CRITICAL``
    so the ``INFO`` audit records are dropped at the logger, before any handler. Call once per service at
    startup. Without this call the logger inherits root (``WARNING``), so audit is OFF unless explicitly on.
    """
    _log.setLevel(logging.INFO if enabled else logging.CRITICAL + 1)


#: What a read record names as its columns when the read was not narrowed to named ones.
ALL_COLUMNS: Final = "all"


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
    :class:`AuditOutcome`; ``subject`` the verified principal; ``resource`` the object acted on. Every record carries
    ``audit.format`` (:data:`AUDIT_FORMAT`) and the request id it was made under (``audit.request_id``, ``-`` outside a
    request), so the records one request made are one filter. Extra ``fields`` (relation, tier, reason) ride the same
    record under an ``audit.`` prefix so a compliance query sees a flat, consistent schema.
    """
    _log.info(
        "audit",
        extra={
            "audit.format": AUDIT_FORMAT,
            "audit.action": action.value,
            "audit.outcome": outcome.value,
            "audit.request_id": current_request_id(),
            "audit.subject": subject or "",
            "audit.resource": resource or "",
            **{f"audit.{key}": value for key, value in fields.items()},
        },
    )
