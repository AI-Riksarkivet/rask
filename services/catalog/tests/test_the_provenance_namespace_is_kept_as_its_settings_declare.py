"""The catalog keeps its provenance namespace as its settings declare it ([[LH-529]], ADR 0158, ADR 0162, ADR 0174).

Lineage's tables in the namespace gain a version and a fragment for each event lineage records, and Lance finds a
table's latest version by listing ``_versions/`` on every open, so their history must be bounded the way every table's
is. Two settings do it. ``LANCE_PROVENANCE_RETAIN_VERSIONS`` is the namespace's maintenance policy, the last versions
maintenance's expiry keeps; and each ``LANCE_PROVENANCE_MAINTAINERS`` principal, a bring-your-own compaction job, is
granted what a compaction needs, ``reader`` and ``maintainer``, and never ``writer``. Each boot holds the namespace to
what the settings say then: a principal or a policy they no longer name is taken back, and a grant or a hold made at a
door is not.

The boot step is ``ensure_provenance_namespace``, the function the catalog's lifespan runs, driven on a real ``dir``
namespace and a real OpenFGA store; the policy is read as maintenance reads it, off the control root.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from lance_namespace import DescribeNamespaceRequest, LanceNamespace, connect

from catalog.core.config import Settings, get_settings
from catalog.services.provenance_namespace import ensure_provenance_namespace
from service_kit.governed import fga
from service_kit.lakehouse import maintenance_policies


#: Lineage's service-account principal, the namespace's writer, as the chart names it.
_LINEAGE = "kubernetes~system:serviceaccount:rask:rask-sa-lineage"
#: A bring-your-own table-maintenance job's principal, in a release of its own.
_COMPACTOR = "kubernetes~system:serviceaccount:rask:table-maintenance"
#: A writer an earlier release named and this one does not.
_RETIRED = "kubernetes~system:serviceaccount:rask:rask-sa-retired"
#: A person granted reading the namespace through the access door.
_ANALYST = "dex~analyst"


@pytest.fixture(autouse=True)
def _settings_read_afresh() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **declared: str) -> Settings:
    """The settings one release declares: lineage the namespace's only writer, and no maintainer and no retention unless
    ``declared`` names them."""
    monkeypatch.delenv("LANCE_PROVENANCE_RETAIN_VERSIONS", raising=False)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path / "root"),
        "LANCE_CONTROL_ROOT": str(tmp_path / "control"),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_PROVENANCE_NAMESPACE": "provenance",
        "LANCE_PROVENANCE_WRITERS": f'["{_LINEAGE}"]',
        "LANCE_PROVENANCE_MAINTAINERS": "[]",
        **declared,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    # Authorization on after the settings are read, as the TestClient suites turn it on: the settings refuse FGA without
    # a verified subject, which the boot step never needs.
    governed = get_settings().model_copy(update={"fga_enabled": True})
    assert isinstance(governed, Settings)
    return governed


def _booted(settings: Settings, store: Any) -> tuple[LanceNamespace, str]:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """Run the boot step against ``store``; answer the namespace connection and the provenance namespace's uuid."""
    ns = connect("dir", {"root": settings.root})

    async def boot() -> None:
        async with fga.make_client(store.url, store.store_id, store.model_id, token_file=None) as client:
            await ensure_provenance_namespace(ns, settings, client)

    asyncio.run(boot())
    properties = ns.describe_namespace(DescribeNamespaceRequest(id=["provenance"])).properties or {}
    return ns, properties["rask.uuid"]


@pytest.mark.parametrize(
    ("declared_before", "declared_now", "holds"),
    [
        pytest.param(
            {},
            {"LANCE_PROVENANCE_MAINTAINERS": f'["{_COMPACTOR}"]'},
            {_LINEAGE: {"writer"}, _ANALYST: {"reader"}, _COMPACTOR: {"reader", "maintainer"}},
            id="a-declared-compaction-job-is-granted-its-rungs-and-never-writer",
        ),
        pytest.param(
            {"LANCE_PROVENANCE_WRITERS": f'["{_LINEAGE}", "{_RETIRED}"]', "LANCE_PROVENANCE_MAINTAINERS": f'["{_COMPACTOR}"]'},
            {},
            {_LINEAGE: {"writer"}, _ANALYST: {"reader"}},
            id="a-principal-no-longer-declared-loses-what-it-was-granted",
        ),
    ],
)
def test_the_boot_declares_the_retention_and_grants_exactly_the_principals_it_declares(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openfga_store: Any,  # noqa: ANN401 — the root conftest's OpenFgaStore
    declared_before: dict[str, str],
    declared_now: dict[str, str],
    holds: dict[str, set[str]],
) -> None:
    _ns, namespace_uuid = _booted(_settings(tmp_path, monkeypatch, **declared_before), openfga_store)
    namespace = fga.namespace_object(namespace_uuid)
    openfga_store.write((fga.user_object(_ANALYST), "reader", namespace))
    settings = _settings(tmp_path, monkeypatch, LANCE_PROVENANCE_RETAIN_VERSIONS="20", **declared_now)

    _booted(settings, openfga_store)

    policy = maintenance_policies.get_policy(settings.registry_root, settings.storage_options(), "namespace", namespace_uuid)
    assert policy is not None and (policy["retain_versions"], policy["retention_days"]) == (20, None), policy
    held: dict[str, set[str]] = {}
    for user, relation, obj in openfga_store.tuples():
        if obj == namespace and user.startswith("user:"):
            held.setdefault(fga.subject_of(user.removeprefix("user:")), set()).add(relation)
    assert held == holds


@pytest.mark.parametrize(
    ("changed_through_the_door", "stands"),
    [
        pytest.param({}, False, id="the-policy-it-wrote-is-removed"),
        pytest.param({"cleanup_enabled": False}, True, id="a-hold-set-through-the-policy-door-stands"),
    ],
)
def test_a_boot_that_declares_no_retention_removes_only_the_policy_it_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    openfga_store: Any,  # noqa: ANN401 — the root conftest's OpenFgaStore
    changed_through_the_door: dict[str, object],
    stands: bool,
) -> None:
    declared = _settings(tmp_path, monkeypatch, LANCE_PROVENANCE_RETAIN_VERSIONS="20")
    _ns, namespace_uuid = _booted(declared, openfga_store)
    root, options = declared.registry_root, declared.storage_options()
    # The policy door merges what its caller sets into the record it finds (`policies._record`), as this does.
    record = {**(maintenance_policies.get_policy(root, options, "namespace", namespace_uuid) or {}), **changed_through_the_door}
    maintenance_policies.put_policy(root, options, record)

    _booted(_settings(tmp_path, monkeypatch), openfga_store)

    assert maintenance_policies.get_policy(root, options, "namespace", namespace_uuid) == (record if stands else None)
