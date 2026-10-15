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

from maintenance.core.config import MaintenanceSettings
from maintenance.services.due import DueQuestion
from maintenance.services.expiry import ExpiryPolicy
from service_kit.lakehouse import maintenance_policies, warehouse_records


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

    def expiry_policy(self, *, min_reclaimable: int) -> ExpiryPolicy:
        return ExpiryPolicy(
            older_than=self.older_than, retain_versions=self.retain_versions, cleanup_enabled=self.cleanup_enabled, min_reclaimable=min_reclaimable
        )

    def due_question(self, *, table: str, location: str, clones: list[str]) -> DueQuestion:
        return DueQuestion(
            table=table,
            location=location,
            policy=str(self.policy["id"]) if self.policy and self.policy.get("id") else None,
            compact_enabled=self.compact_enabled,
            optimize_indices_enabled=self.optimize_indices_enabled,
            target_rows_per_fragment=self.target_rows_per_fragment,
            clones=clones,
        )


def load_policies(settings: MaintenanceSettings, options: dict[str, str]) -> list[dict[str, Any]]:
    """The maintenance policies, loaded once per tick.

    A registry that cannot be read at all RAISES (fail toward not deleting, audit 2026-07-16): policies are the
    protective surface (retention extensions, legal holds), so expiring without them would reclaim history an owner
    explicitly kept. One bad record inside a readable registry is skipped-with-warning by ``list_policies``.
    """
    try:
        policy_records = maintenance_policies.list_policies(settings.resolved_policy_root, options)
    except Exception:
        log.error("compaction_policies_unreadable_tick_aborted")
        raise
    # The count distinguishes "no policies set" from "policies invisible" (a control root moved without
    # MAINTENANCE_POLICY_ROOT following lists cleanly as empty).
    log.info("compaction_policies_loaded", extra={"policies": len(policy_records)})
    return policy_records


def buckets_to_sweep(settings: MaintenanceSettings, options: dict[str, str]) -> list[str]:
    """#81 EVERY warehouse bucket the registry knows, beside the configured ones.

    A warehouse bucket is created by an API CALL at runtime, so a config-time list goes stale by construction. An
    unreadable registry degrades to the configured list and says so: what is known is still maintained.
    """
    buckets = list(settings.sweep_buckets)
    try:
        registry = warehouse_records.list_warehouse_records(settings.resolved_control_root, options)
        discovered = [b for b in warehouse_records.maintainable_buckets(registry) if b not in buckets]
        buckets.extend(discovered)
        log.info("sweep_registry_buckets", extra={"configured": len(settings.sweep_buckets), "from_registry": len(discovered)})
    except Exception as exc:  # noqa: BLE001 — a missing registry must not stop maintaining what we know
        log.warning("sweep_registry_unreadable", extra={"error": str(exc)})
    return buckets


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
        cleanup_enabled=bool(policy.get("cleanup_enabled", True)),
        compact_enabled=bool(policy.get("compact_enabled", True)),
        optimize_indices_enabled=bool(policy.get("optimize_indices_enabled", True)),
        target_rows_per_fragment=_int_field(policy, "target_rows_per_fragment"),
    )


class PlanContext(BaseModel):
    """What resolving one dataset's plan reads: this tick's policies, its clock, and the default retention."""

    policies: list[dict[str, Any]]
    now: datetime
    older_than: timedelta


def resolve_plan(uri: str, *, context: PlanContext, table_id: str | None, settings: MaintenanceSettings) -> DatasetPlan:
    """This dataset's plan, without the cadence: what the event lane asks between ticks.

    ``table_id`` is the catalog's id for the dataset where the caller knows it. A policy that cannot be resolved or
    parsed falls back to the GLOBAL defaults: the dataset is maintained, never left out.
    """
    try:
        policy = maintenance_policies.resolve_policy(context.policies, uri, logical_id=table_id, delimiter=settings.delimiter)
        return plan_from_policy(policy, older_than=context.older_than)
    except Exception as exc:  # noqa: BLE001 — unreadable tuning maintains on the defaults
        log.warning("compaction_policy_ignored", extra={"uri": uri, "error": str(exc)})
        return DatasetPlan(older_than=context.older_than)


def cadence_skip(policy: dict[str, Any], *, settings: MaintenanceSettings, uri: str, now: datetime) -> str | None:
    """``policy_interval`` while the policy's ``compact_interval_hours`` has not elapsed since this dataset's stamp.

    An unreadable, absent or malformed stamp maintains — never the other way, or a lost stamp would silence maintenance
    forever — and is logged, so a persistently broken state prefix is visible.
    """
    interval = policy.get("compact_interval_hours")
    if not interval:
        return None
    try:
        stamped = maintenance_policies.read_state(settings.resolved_policy_root, settings.storage_options(), policy, uri)
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
