"""A warehouse's grants, declared and converged: its project edge and the maintenance rung of the release's own
accounts."""

from __future__ import annotations

from openfga_sdk import OpenFgaClient

from catalog.api.authz.decisions import wired
from catalog.api.security import Caller
from catalog.core.config import CatalogAuthzSettings, CatalogLineageSettings, Settings
from lakehouse.governed import fga_ids, fga_writes
from lakehouse.governed.machine_identity import ServicePrincipal


__all__ = [
    "cascade_tuples",
    "converge_warehouse_grants",
    "maintainer_tuples",
    "own_users",
    "seed_warehouse",
]


async def seed_warehouse(
    client: OpenFgaClient | None,
    settings: Settings,
    token: Caller | None,
    *,
    warehouse_id: str,
    project: str,
) -> None:
    """Grant a creator who is a person ``owner`` on the new ``warehouse:<id>`` and link it to its ``project:<project>``
    parent, so the concentric cascade (project → warehouse → namespace → table) reaches everything created
    under it. No-op when FGA is off or unauthenticated; a 503 when FGA is on with no client wired.

    NOTE: the warehouse's parent-pointer relation is ``project`` (``define project: [project]`` in
    ``lakehouse/governed/auth/model.fga``) — NOT the ``parent`` name that namespaces/tables use. So this writes
    the two tuples directly rather than via :func:`fga_writes.grant_on_create` (whose parent edge is hardcoded to
    ``parent``, a relation the ``warehouse`` type does not define — writing it makes OpenFGA reject the whole
    seed → 503). Idempotent: ``write_tuples`` swallows duplicate-tuple writes on a create retry.

    The project-admin bootstrap tuple this used to optionally carry moved to ``seed_project_admin``,
    written by ``POST /management/v1/projects`` — a tenant's admin is minted with the tenant, not with its
    first warehouse."""
    if token is None or (client := wired(settings, client)) is None:
        return
    obj = f"warehouse:{warehouse_id}"
    # The project edge and the sweep's grant, granted here and not per namespace, at the WAREHOUSE because
    # `namespace` and `table` both define `... or ... from parent`: one tuple reaches every namespace and every
    # table under it. Written in the SAME call as the creator's grant so there is no window in which the
    # warehouse exists and is unreachable from its project or unmaintained.
    # A person owns the warehouse they create. A machine holds nothing on it by creating it, as with a table or a
    # project ([[LH-433]]): its project's admins own it through the project edge.
    owner = [] if isinstance(token, ServicePrincipal) else [fga_writes.ClientTuple(user=fga_ids.user_object(token.subject), relation="owner", object=obj)]
    await fga_writes.write_tuples(  # noqa: TID251
        client,
        [*owner, *cascade_tuples(settings, warehouse_id=warehouse_id, project=project)],
        actor=token.subject,
        origin="warehouse_create",
    )


def cascade_tuples(settings: CatalogAuthzSettings, *, warehouse_id: str, project: str) -> list[fga_writes.ClientTuple]:
    """The tuples a warehouse needs to be REACHABLE and MAINTAINED — its project edge, plus the sweep's grant.

    Shared by the create path and the backfill so the two cannot drift: a backfill that writes a different set
    from the one creates write leaves the estate with two populations of warehouse that differ in a way
    nothing reports.

    Granted at the WAREHOUSE, not per namespace: `namespace` and `table` both define `maintainer` as
    `... or maintainer from parent`, so one tuple at the container reaches every namespace and every table
    under it (`optimize-tuples.md`), and the sweep's reach stays enumerable.

    The creator's own grant is deliberately NOT here. It is the one tuple a backfill must never write
    — re-running the create path over an existing estate to repair it would make whoever ran the
    repair an owner of every tenant.
    """
    obj = f"warehouse:{warehouse_id}"
    return [
        fga_writes.ClientTuple(user=f"project:{project}", relation="project", object=obj),
        # `maintenance` ALONE: the platform's maintenance identity, a maintainer that may also be vended the
        # maintain tier. Maintenance rewrites how a dataset is stored and must not be able to drop it or change
        # what it says, which is exactly the separation `can_maintain` was minted to express.
        *maintainer_tuples(settings, obj),
    ]


def maintainer_tuples(settings: CatalogAuthzSettings, obj: str) -> list[fga_writes.ClientTuple]:
    """The maintenance rung of each of ``LANCE_FGA_MAINTAINERS`` on the warehouse ``obj``: one builder for every writer of
    it (a warehouse's create, the boot backfill, the declaration converge on the estate's own warehouse)."""
    return [fga_writes.ClientTuple(user=fga_ids.user_object(subject), relation="maintenance", object=obj) for subject in settings.fga_maintainers]


def own_users(settings: CatalogLineageSettings) -> set[str]:
    """The release's own accounts (``LANCE_OWN_ACCOUNTS``) as the users a tuple names."""
    return {fga_ids.user_object(account) for account in settings.own_accounts}


async def converge_warehouse_grants(
    client: OpenFgaClient | None,
    settings: Settings,
    *,
    warehouse_id: str,
    project: str,
    actor: str,
) -> int:
    """Converge one EXISTING warehouse's grants: assert :func:`cascade_tuples`, and take back the maintenance rung from
    each of the release's own accounts it no longer declares. Returns the number of tuples written plus those revoked.

    Why this is needed at all, given creates already seed: the grants are written ONCE, at create, from
    a config value that CHANGES (``LANCE_FGA_MAINTAINERS``), so every warehouse that predates a change is
    missing the new subject, and keeps one the change dropped ([[XC-164]]).

    Idempotent by the same mechanism the create path relies on: ``write_tuples`` retries one-by-one
    when OpenFGA rejects a batch for containing a duplicate, so a re-run lands only what is missing.
    It inherits fail-closed too — an unreachable OpenFGA raises rather than reporting success, because
    a backfill that silently wrote nothing is indistinguishable from one that had nothing to do.
    """
    if (client := wired(settings, client)) is None:
        return 0
    tuples = cascade_tuples(settings, warehouse_id=warehouse_id, project=project)
    await fga_writes.write_tuples(client, tuples, actor=actor, origin="warehouse_grant_backfill")  # noqa: TID251
    own = own_users(settings)
    revoked = await fga_writes.revoke_undeclared(  # noqa: TID251 — the converge's revoking site
        client, tuples, obj=f"warehouse:{warehouse_id}", relation="maintenance", own=own, actor=actor, origin="warehouse_grant_backfill"
    )
    return len(tuples) + len(revoked)
