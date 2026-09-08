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
from typing import Any, Final


#: The one well-known audit logger name (not a per-module logger) so audit records are a single stream.
AUDIT_LOGGER = "lance.audit"
_log = logging.getLogger(AUDIT_LOGGER)

#: Closed outcome vocabulary — a compliance query groups by these.
ALLOW = "allow"
DENY = "deny"
SUCCESS = "success"
FAILURE = "failure"


def configure_audit(*, enabled: bool) -> None:
    """Enable or disable the audit stream at boot via the dedicated logger's level.

    Enabled → ``INFO`` (records emit through the OTLP handler on root). Disabled → a level above ``CRITICAL``
    so the ``INFO`` audit records are dropped at the logger, before any handler. Call once per service at
    startup. Without this call the logger inherits root (``WARNING``), so audit is OFF unless explicitly on.
    """
    _log.setLevel(logging.INFO if enabled else logging.CRITICAL + 1)


#: The one action string every data READ is recorded under. Fixed here rather than passed by callers,
#: because a read log is only useful if it can be FILTERED — four doors spelling the verb four ways is
#: the same as no log for anyone trying to answer "who read this table".
READ_ACTION: Final = "read_data"


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

    ``columns`` is joined rather than passed as a list because the audit record is a FLAT structure by
    design (`audit()`'s own contract), and a nested value would land differently in every sink.
    """
    audit(
        READ_ACTION,
        SUCCESS,
        subject=subject,
        resource=resource,
        **({"version": version} if version is not None else {}),
        **({"columns": ",".join(columns)} if columns else {}),
        **fields,
    )


def audit(
    action: str,
    outcome: str,
    *,
    subject: str | None = None,
    resource: str | None = None,
    **fields: Any,
) -> None:
    """Emit one structured audit event (dropped when audit is disabled — see :func:`configure_audit`).

    ``action`` is the security-relevant operation (``authn``, ``can_write_data``, ``vend_credentials``, …);
    ``outcome`` one of :data:`ALLOW` / :data:`DENY` / :data:`SUCCESS` / :data:`FAILURE`; ``subject`` the
    verified principal; ``resource`` the object acted on. Extra ``fields`` (request id, tier, reason) ride the
    same record under an ``audit.`` prefix so a compliance query sees a flat, consistent schema.
    """
    _log.info(
        "audit",
        extra={
            "audit.action": action,
            "audit.outcome": outcome,
            "audit.subject": subject or "",
            "audit.resource": resource or "",
            **{f"audit.{key}": value for key, value in fields.items()},
        },
    )
