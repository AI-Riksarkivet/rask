"""The chart wires event signing the same way in every deploy mode, enforces it on one switch, and names each Dapr Configuration by its spec.

[[LH-064]] (values.yaml `signing:`). Three things the chart decides, each of which fails silently if it is wrong:

- A store nothing seeds (`openbao.devMode=false`, or `openbao.externalAddr`) mints no key, so every signer would stay not Ready
  and lineage could verify nothing. The render refuses it, naming every secret and the script that creates them, until the
  operator attests with `signing.provisioned`.
- `signing.enforce` is the one switch, on by default. On, lineage carries the signer set and the delegator set the chart derives,
  and only when the store and auth are on; off, it carries neither. An enforced render with no signer at all is refused rather
  than rendered as a lineage that requires nothing.
- daprd loads its Configuration once, at boot (HotReload is off), and Helm applies a Deployment before the Configuration it names. A
  deny-list edit under an unchanged name is therefore loaded stale, for the life of the pod, by any pod that boots in between, with every
  probe green. A Configuration is named by the hash of its spec instead: a pod that boots before Helm applies the new object finds none
  and daprd exits (Dapr v1.18.1 pkg/runtime/config.go:252-254) until it exists, and an edit rolls exactly the pods whose list changed.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS


#: Real values for the credentials `prod-credentials.yaml` refuses to see published, so the refusal under test is this one.
_REAL = (
    "--set", "dapr.appToken=a-real-app-token",
    "--set", "age.password=a-real-age-password",
    "--set", "minio.secretKey=a-real-rustfs-secret",
)  # fmt: skip

_SEALED = ("--set", "openbao.devMode=false", *_REAL)
_EXTERNAL = ("--set", "openbao.enabled=false", "--set", "openbao.externalAddr=https://vault.example:8200")


def _signers(docs: tuple[dict, ...]) -> list[str]:
    """The identities the Deployments sign as, from the env the chart renders on them."""
    return sorted(
        {
            env["RASK_SIGNING_IDENTITY"]
            for _, _, container in chart_render.containers(docs)
            if "RASK_SIGNING_IDENTITY" in (env := chart_render.env_of(container))
        }
    )


def _lineage_env(docs: tuple[dict, ...]) -> dict[str, str]:
    [lineage] = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-lineage")]
    return chart_render.env_of(lineage["spec"]["template"]["spec"]["containers"][0])


@pytest.mark.parametrize("mode", [pytest.param(_SEALED, id="a-sealed-store"), pytest.param(_EXTERNAL, id="an-external-store")])
def test_a_store_nothing_seeds_is_refused_until_the_operator_attests_its_signing_keys(mode: tuple[str, ...]) -> None:
    identities = _signers(chart_render.render(*DEFAULT_ARGS))
    assert identities, "the default render has no signer, so the refusal below would name nothing"

    with pytest.raises(subprocess.CalledProcessError) as refused:
        chart_render.render(*DEFAULT_ARGS, *mode)
    message = refused.value.stderr
    missing = [name for identity in identities for name in (f"signing-public-{identity}", f"signing-key-{identity}") if name not in message]
    assert not missing, f"the refusal does not name {missing}, so the operator cannot tell which secrets to create"

    docs = chart_render.render(*DEFAULT_ARGS, *mode, "--set", "signing.provisioned=true")
    assert _signers(docs) == identities, "an attested store must still render every signer's identity"
    assert not [name for _, name, _ in chart_render.containers(docs) if name == "mint"], "a store nothing seeds has no mint step to run"


@pytest.mark.parametrize(
    ("overlay", "enforced"),
    [
        pytest.param((), True, id="on-by-default-with-the-store-and-auth"),
        pytest.param(("--set", "signing.enforce=false"), False, id="off-when-asked"),
        pytest.param(("--set", "auth.enabled=false"), False, id="not-without-auth"),
        pytest.param(("--set", "openbao.enabled=false"), False, id="not-without-a-store"),
    ],
)
def test_lineage_requires_signatures_from_exactly_the_rendered_signers_wherever_the_store_and_auth_are_on(overlay: tuple[str, ...], enforced: bool) -> None:  # noqa: FBT001
    docs = chart_render.render(*DEFAULT_ARGS, *overlay)
    env = _lineage_env(docs)

    if not enforced:
        assert not {"LINEAGE_SIGNERS", "LINEAGE_DELEGATORS"} & env.keys(), (
            f"lineage carries a verifier set without enforcement: {sorted(env.keys() & {'LINEAGE_SIGNERS', 'LINEAGE_DELEGATORS'})}"
        )
        return
    assert {"LINEAGE_SIGNERS", "LINEAGE_DELEGATORS"} <= env.keys(), "lineage requires no signature: the chart gives it no signer set or delegator set"
    assert json.loads(env["LINEAGE_SIGNERS"]) == _signers(docs), "the signer set is not the identities the Deployments sign as"
    assert json.loads(env["LINEAGE_DELEGATORS"]) == ["service-catalog"], "only the catalog, which authenticated the person, may sign for one"


def test_an_enforced_render_with_no_signer_is_refused_rather_than_rendered_as_a_lineage_that_requires_nothing() -> None:
    nobody = (
        "--set",
        "catalog.serviceIdentity=",
        "--set",
        "maintenance.enabled=false",
        "--set",
        "medallion.enabled=false",
        "--set",
        "services.ingest.env.RASK_LINEAGE_SERVICE_IDENTITY=",
    )
    assert not _signers(chart_render.render(*DEFAULT_ARGS, *nobody, "--set", "signing.enforce=false")), (
        "the overlay still has a signer, so the refusal below is not what it names"
    )

    with pytest.raises(subprocess.CalledProcessError) as refused:
        chart_render.render(*DEFAULT_ARGS, *nobody)
    assert "no identity signs" in refused.value.stderr


def _loaded_configurations(docs: tuple[dict, ...]) -> dict[str, tuple[str, dict]]:
    """`Deployment/name` -> (the Configuration its sidecar is started with, that object's spec), for every pod that names one."""
    objects = {doc["metadata"]["name"]: doc["spec"] for doc in docs if doc.get("kind") == "Configuration"}
    loaded: dict[str, tuple[str, dict]] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        pod = doc["metadata"]["name"]
        name = ((doc["spec"]["template"].get("metadata") or {}).get("annotations") or {}).get("dapr.io/config")
        if name:
            assert name in objects, f"{pod} starts its sidecar with Configuration {name}, which the render does not contain, so its daprd never boots"
            loaded[pod] = (name, objects[name])
    return loaded


def test_a_configuration_is_renamed_when_and_only_when_its_spec_changes_so_a_pod_cannot_load_a_stale_one() -> None:
    before = _loaded_configurations(chart_render.render(*DEFAULT_ARGS))
    after = _loaded_configurations(chart_render.render(*DEFAULT_ARGS, "--set", "maintenance.catalogServiceIdentity=service-maintenance-renamed"))

    assert before.keys() == after.keys() and len(before) > 5, f"the pods that name a Configuration are {sorted(before)}"
    edited = {pod for pod in before if before[pod][1] != after[pod][1]}
    renamed = {pod for pod in before if before[pod][0] != after[pod][0]}
    # Maintenance denies every signing key but its own whichever name that is, so its Configuration is the one that does not change.
    assert edited and edited != before.keys(), (
        f"the overlay must edit some Configurations and leave others, or one direction below measures nothing: {sorted(edited)}"
    )
    # Helm applies a Deployment before the Configuration it names and daprd reads that Configuration once, at boot: a pod that boots in
    # between loads whatever already sits under its name. Under a new name there is nothing to load, and daprd exits until Helm applies it.
    assert not edited - renamed, (
        f"these pods' Configuration was edited under the name the old one had, so a pod that boots first loads the stale spec: {sorted(edited - renamed)}"
    )
    assert not renamed - edited, f"these pods are rolled onto a Configuration whose spec did not change: {sorted(renamed - edited)}"
