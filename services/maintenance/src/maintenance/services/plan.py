"""What the winning maintenance policy says about ONE dataset: its retention, its cadence, and which work may be due.

#50/#84: a per-table, namespace or project record from the catalog's ``_policies/`` registry is resolved
winner-takes-all (``maintenance_policies.resolve_policy``): the record that matched supplies EVERY field, and a field it
leaves unset gets the default rather than the value of the record it shadowed. A dataset no record matches keeps the
global defaults. A dataset's tier is not read here: it is a namespace property the catalog judges (ADR 0160).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from pydantic import BaseModel

from lakehouse.records import maintenance_policies
from lakehouse.records.maintenance_policies import PolicyRecord
from lakehouse.storage.credential import StoreCredentialUnavailableError
from maintenance.core.config import MaintenanceStore
from maintenance.services.due import DueQuestion
from maintenance.services.expiry import ExpiryPolicy
from maintenance.services.naming import PolicyOwners


log = logging.getLogger(__name__)


class DatasetPlan(BaseModel):
    """What the resolved policy says to do with ONE dataset this tick. ``skipped`` set means not this tick."""

    #: ``policy_interval`` (the cadence has not elapsed), or ``None`` to maintain.
    skipped: str | None = None
    #: The matched policy record, or ``None`` when no record applies (or one could not be read).
    policy: PolicyRecord | None = None
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
        if self.policy.kind == "table":
            return table
        return self.policy.id or None


def _age_bound(settings: maintenance_policies.PolicySettings, *, default: timedelta) -> timedelta | None:
    """The age a version must reach before expiry may take it.

    ``retain_versions: N`` alone means exactly keep-last-N: an age bound on top would keep everything younger than the
    global default and make the policy a no-op on fresh datasets.
    """
    if settings.retention_days is not None:
        return timedelta(days=settings.retention_days)
    if settings.retain_versions is not None:
        return None
    return default


def plan_from_policy(policy: PolicyRecord | None, *, older_than: timedelta) -> DatasetPlan:
    """The plan one resolved record makes, ``None`` meaning the global defaults.

    Raises:
        MalformedPolicyError: The record's settings cannot be read (:attr:`lakehouse.records.maintenance_policies.PolicyRecord.settings`).
    """
    if policy is None:
        return DatasetPlan(older_than=older_than)
    settings = policy.settings
    return DatasetPlan(
        policy=policy,
        older_than=_age_bound(settings, default=older_than),
        retain_versions=settings.retain_versions,
        cleanup_enabled=settings.cleanup_enabled,
        compact_enabled=settings.compact_enabled,
        optimize_indices_enabled=settings.optimize_indices_enabled,
        target_rows_per_fragment=settings.target_rows_per_fragment,
    )


class PlanContext(BaseModel):
    """What resolving one dataset's plan reads: this tick's policies, its clock, and the default retention."""

    policies: list[PolicyRecord]
    now: datetime
    older_than: timedelta


def resolve_plan(uri: str, *, context: PlanContext, owners: PolicyOwners) -> DatasetPlan:
    """This dataset's plan, without the cadence: what the event lane asks between ticks.

    ``owners`` are the uuids of the dataset's table and of its namespaces, whose records may govern it. A record whose
    settings cannot be read raises rather than planning on the defaults, since it may be a legal hold; the pass reads
    each record strictly before it plans (:func:`maintenance.services.table_pass.read_for`) and parks a table whose
    record it cannot read.

    Raises:
        MalformedPolicyError: The winning record's settings cannot be read.
    """
    policy = maintenance_policies.resolve_policy(context.policies, uri, table_uuid=owners.table_uuid, namespace_uuids=owners.namespace_uuids)
    return plan_from_policy(policy, older_than=context.older_than)


def last_maintained(policy: PolicyRecord, *, settings: MaintenanceStore, table_uuid: str, uri: str) -> datetime | None:
    """When a pass last maintained this dataset under ``policy``, or ``None`` when none did or the store cannot say.

    A stamp the store fails to give maintains, never the other way, or a lost stamp would silence maintenance for
    ever; it is logged, so a persistently broken state prefix is visible.
    """
    try:
        return maintenance_policies.read_state(settings.resolved_policy_root, settings.storage_options(), policy, table_uuid=table_uuid, uri=uri)
    except (OSError, StoreCredentialUnavailableError) as exc:
        log.warning("compaction_policy_state_unreadable", extra={"policy": policy.id, "uri": uri, "error": str(exc)})
        return None


def cadence_skip(policy: PolicyRecord, *, settings: MaintenanceStore, table_uuid: str, uri: str, now: datetime) -> str | None:
    """``policy_interval`` while the policy's ``compact_interval_hours`` has not elapsed since this dataset's stamp.

    Raises:
        MalformedPolicyError: The policy's settings cannot be read.
    """
    interval = policy.settings.compact_interval_hours
    if interval is None:
        return None
    stamped = last_maintained(policy, settings=settings, table_uuid=table_uuid, uri=uri)
    return "policy_interval" if stamped is not None and now - stamped < timedelta(hours=interval) else None
