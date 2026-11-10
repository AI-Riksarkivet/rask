"""What the winning maintenance policy says about ONE dataset: its retention, its cadence, and which work may be due.

#50/#84: a per-table, namespace or project record from the catalog's ``_policies/`` registry is resolved
winner-takes-all (``maintenance_policies.resolve_policy``): the record that matched supplies EVERY field, and a field it
leaves unset gets the default rather than the value of the record it shadowed. A dataset no record matches keeps the
global defaults. A dataset's tier is not read here: it is a namespace property the catalog judges (ADR 0160).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from lakehouse import maintenance_policies
from maintenance.core.config import MaintenanceSettings
from maintenance.services.due import DueQuestion
from maintenance.services.expiry import ExpiryPolicy
from maintenance.services.naming import PolicyOwners


log = logging.getLogger(__name__)


class DatasetPlan(BaseModel):
    """What the resolved policy says to do with ONE dataset this tick. ``skipped`` set means not this tick."""

    #: ``policy_interval`` (the cadence has not elapsed), or ``None`` to maintain.
    skipped: str | None = None
    #: The matched policy record, or ``None`` when no record applies (or one could not be read).
    policy: dict[str, Any] | None = None
    older_than: timedelta | None = None
    retain_versions: int | None = None
    cleanup_enabled: bool = True
    compact_enabled: bool = True
    optimize_indices_enabled: bool = True
    target_rows_per_fragment: int | None = None

    def expiry_policy(self, *, min_reclaimable: int, staged_grace: timedelta) -> ExpiryPolicy:
        return ExpiryPolicy(
            older_than=self.older_than,
            retain_versions=self.retain_versions,
            cleanup_enabled=self.cleanup_enabled,
            min_reclaimable=min_reclaimable,
            staged_grace=staged_grace,
        )

    def due_question(self, *, table: str, uuid: str, location: str) -> DueQuestion:
        return DueQuestion(
            table=table,
            uuid=uuid,
            location=location,
            policy=self._winner(table),
            compact_enabled=self.compact_enabled,
            optimize_indices_enabled=self.optimize_indices_enabled,
            target_rows_per_fragment=self.target_rows_per_fragment,
        )

    def _winner(self, table: str) -> str | None:
        """Which record won, as a person reads it: the table's own is named as the table is named now."""
        if self.policy is None:
            return None
        if self.policy.get("kind") == "table":
            return table
        return str(self.policy.get("id") or "") or None


def _int_field(policy: dict[str, Any], name: str) -> int | None:
    value = policy.get(name)
    return int(str(value)) if value else None


def _age_bound(policy: dict[str, Any], *, default: timedelta) -> timedelta | None:
    """The age a version must reach before expiry may take it.

    ``retain_versions: N`` alone means exactly keep-last-N: an age bound on top would keep everything younger than the
    global default and make the policy a no-op on fresh datasets.
    """
    if (retention_days := _int_field(policy, "retention_days")) is not None:
        return timedelta(days=retention_days)
    if _int_field(policy, "retain_versions") is not None:
        return None
    return default


def plan_from_policy(policy: dict[str, Any] | None, *, older_than: timedelta) -> DatasetPlan:
    """The plan one resolved record makes, ``None`` meaning the global defaults."""
    if policy is None:
        return DatasetPlan(older_than=older_than)
    return DatasetPlan(
        policy=policy,
        older_than=_age_bound(policy, default=older_than),
        retain_versions=_int_field(policy, "retain_versions"),
        cleanup_enabled=not maintenance_policies.keeps_whole_history(policy),
        compact_enabled=bool(policy.get("compact_enabled", True)),
        optimize_indices_enabled=bool(policy.get("optimize_indices_enabled", True)),
        target_rows_per_fragment=_int_field(policy, "target_rows_per_fragment"),
    )


class PlanContext(BaseModel):
    """What resolving one dataset's plan reads: this tick's policies, its clock, and the default retention."""

    policies: list[dict[str, Any]]
    now: datetime
    older_than: timedelta


def resolve_plan(uri: str, *, context: PlanContext, owners: PolicyOwners) -> DatasetPlan:
    """This dataset's plan, without the cadence: what the event lane asks between ticks.

    ``owners`` are the uuids of the dataset's table and of its namespaces, whose records may govern it. A policy that
    cannot be parsed falls back to the GLOBAL defaults: the dataset is maintained, never left out.
    """
    try:
        policy = maintenance_policies.resolve_policy(context.policies, uri, table_uuid=owners.table_uuid, namespace_uuids=owners.namespace_uuids)
        return plan_from_policy(policy, older_than=context.older_than)
    except Exception as exc:  # noqa: BLE001 — unreadable tuning maintains on the defaults
        log.warning("compaction_policy_ignored", extra={"uri": uri, "error": str(exc)})
        return DatasetPlan(older_than=context.older_than)


def cadence_skip(policy: dict[str, Any], *, settings: MaintenanceSettings, table_uuid: str, uri: str, now: datetime) -> str | None:
    """``policy_interval`` while the policy's ``compact_interval_hours`` has not elapsed since this dataset's stamp.

    An unreadable, absent or malformed stamp maintains — never the other way, or a lost stamp would silence maintenance
    forever — and is logged, so a persistently broken state prefix is visible.
    """
    interval = policy.get("compact_interval_hours")
    if not interval:
        return None
    try:
        stamped = maintenance_policies.read_state(settings.resolved_policy_root, settings.storage_options(), policy, table_uuid=table_uuid, uri=uri)
    except Exception as exc:  # noqa: BLE001 — a lost stamp maintains
        log.warning("compaction_policy_state_unreadable", extra={"policy": policy.get("id"), "uri": uri, "error": str(exc)})
        return None
    if stamped is None:
        return None
    try:
        elapsed = now - datetime.fromisoformat(stamped)
    except (ValueError, TypeError):
        # TypeError covers a naive stamp: `now` is aware, and aware-minus-naive raises.
        log.warning("compaction_policy_state_malformed", extra={"policy": policy.get("id"), "uri": uri, "stamp": stamped})
        return None
    return "policy_interval" if elapsed < timedelta(hours=int(str(interval))) else None
