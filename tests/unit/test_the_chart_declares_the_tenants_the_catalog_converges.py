"""What the chart renders of `catalog.declared` is a declaration the catalog reads ([[XC-213]], ADR 0206).

The chart and the catalog meet at one file: the ConfigMap `<release>-catalog-declaration`, mounted where
`LANCE_DECLARATION_FILE` names it. So the gate reads the render through the catalog's own model: a declaration the
model refuses would converge nothing on a deployed estate and report it only in the catalog's log.
"""

from __future__ import annotations

from typing import Any

from catalog.services.declaration import Declaration, DeclaredGrant, DeclaredProject, DeclaredWarehouse
from tests.unit.chart_render import DEFAULT_ARGS, render


PIPELINE = "kubernetes~system:serviceaccount:default:pipeline"


def _declaration(*extra: str) -> Declaration:
    """The declaration the catalog would read off a release rendered with ``extra``, found the way the pod finds it."""
    docs: tuple[dict[str, Any], ...] = render(*DEFAULT_ARGS, *extra)
    (catalog,) = [d for d in docs if d and d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-catalog")]
    spec = catalog["spec"]["template"]["spec"]
    (container,) = [c for c in spec["containers"] if c["name"] == "catalog"]
    path = next(e["value"] for e in container["env"] if e["name"] == "LANCE_DECLARATION_FILE")
    (mount,) = [m for m in container["volumeMounts"] if path.startswith(f"{m['mountPath']}/")]
    (volume,) = [v for v in spec["volumes"] if v["name"] == mount["name"]]
    (config,) = [d for d in docs if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == volume["configMap"]["name"]]
    return Declaration.model_validate_json(config["data"][path.removeprefix(f"{mount['mountPath']}/")])


def test_a_fresh_install_declares_the_default_project_administered_by_the_bootstrap_admin() -> None:
    declared = _declaration("--set-string", "auth.bootstrapAdmin=dex~alice")

    assert declared == Declaration(projects=[DeclaredProject(id="default", admins=["dex~alice"])])


def test_declared_projects_warehouses_and_grants_reach_the_catalog_as_declared() -> None:
    declared = _declaration(
        "--set-json",
        'catalog.declared.projects=[{"id":"acme","admins":["dex~bob"],"protected":true}]',
        "--set-json",
        'catalog.declared.warehouses=[{"id":"acme","project":"acme","bucket":"acme-bucket","delete_profile":{"mode":"soft","grace_days":7},"active":false}]',
        "--set-json",
        f'catalog.declared.grants=[{{"object":"warehouse:acme","relation":"writer","principal":"{PIPELINE}"}}]',
    )

    assert declared == Declaration(
        projects=[DeclaredProject(id="acme", admins=["dex~bob"], protected=True)],
        warehouses=[
            DeclaredWarehouse.model_validate(
                {"id": "acme", "project": "acme", "bucket": "acme-bucket", "delete_profile": {"mode": "soft", "grace_days": 7}, "active": False}
            )
        ],
        grants=[DeclaredGrant(object="warehouse:acme", relation="writer", principal=PIPELINE)],
    )
