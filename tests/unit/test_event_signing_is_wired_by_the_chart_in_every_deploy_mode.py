"""The chart wires event signing the same way in every deploy mode, enforces it on one switch, and restarts the pods whose key access changed.

[[LH-064]] (values.yaml `signing:`). Three things the chart decides, each of which fails silently if it is wrong:

- A store nothing seeds (`openbao.devMode=false`, or `openbao.externalAddr`) mints no key, so every signer would stay not Ready
  and lineage could verify nothing. The render refuses it, naming every secret and the script that creates them, until the
  operator attests with `signing.provisioned`.
- `signing.enforce` is the one switch between the revisions that sign and the revision that requires a signature. Off, lineage
  carries neither set. On, it carries the signer set and the delegator set the chart derives, and only when the store and auth
  are on; an enforced render with no signer at all is refused rather than rendered as a lineage that requires nothing.
- daprd loads its Configuration at boot and HotReload is off, so a deny-list edit reaches a sidecar only when its pod restarts.
  Each pod carries the hash of its own app-id's Configuration: an edit rolls the pods it changed and no others.
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
    assert "scripts/provision_signing_keys.sh" in message
    missing = [name for identity in identities for name in (f"signing-public-{identity}", f"signing-key-{identity}") if name not in message]
    assert not missing, f"the refusal does not name {missing}, so the operator cannot tell which secrets to create"

    docs = chart_render.render(*DEFAULT_ARGS, *mode, "--set", "signing.provisioned=true")
    assert _signers(docs) == identities, "an attested store must still render every signer's identity"
    assert not [name for _, name, _ in chart_render.containers(docs) if name == "mint"], "a store nothing seeds has no mint step to run"


@pytest.mark.parametrize(
    ("overlay", "enforced"),
    [
        pytest.param((), False, id="off-by-default"),
        pytest.param(("--set", "signing.enforce=true"), True, id="on-with-the-store-and-auth"),
        pytest.param(("--set", "signing.enforce=true", "--set", "auth.enabled=false"), False, id="not-without-auth"),
        pytest.param(("--set", "signing.enforce=true", "--set", "openbao.enabled=false"), False, id="not-without-a-store"),
    ],
)
def test_lineage_requires_signatures_only_when_asked_and_from_exactly_the_signers_the_chart_renders(overlay: tuple[str, ...], enforced: bool) -> None:  # noqa: FBT001
    docs = chart_render.render(*DEFAULT_ARGS, *overlay)
    env = _lineage_env(docs)

    if not enforced:
        assert not {"LINEAGE_SIGNERS", "LINEAGE_DELEGATORS"} & env.keys(), (
            f"lineage carries a verifier set without enforcement: {sorted(env.keys() & {'LINEAGE_SIGNERS', 'LINEAGE_DELEGATORS'})}"
        )
        return
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
    assert not _signers(chart_render.render(*DEFAULT_ARGS, *nobody)), "the overlay still has a signer, so the refusal below is not what it names"

    with pytest.raises(subprocess.CalledProcessError) as refused:
        chart_render.render(*DEFAULT_ARGS, *nobody, "--set", "signing.enforce=true")
    assert "no identity signs" in refused.value.stderr


def _config_checksums(docs: tuple[dict, ...]) -> dict[str, str]:
    """`Deployment/name` -> the `checksum/dapr-config` of every pod whose sidecar loads a per-app Configuration."""
    found: dict[str, str] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        annotations = (doc["spec"]["template"].get("metadata") or {}).get("annotations") or {}
        if str(annotations.get("dapr.io/config", "")).startswith("lance-config-"):
            assert "checksum/dapr-config" in annotations, (
                f"{doc['metadata']['name']} loads {annotations['dapr.io/config']} and has no checksum of it, so an edit never restarts it"
            )
            found[doc["metadata"]["name"]] = annotations["checksum/dapr-config"]
    return found


def test_a_pod_restarts_when_its_own_dapr_configuration_changes_and_only_then() -> None:
    before = _config_checksums(chart_render.render(*DEFAULT_ARGS))
    after = _config_checksums(chart_render.render(*DEFAULT_ARGS, "--set", "maintenance.catalogServiceIdentity=service-maintenance-renamed"))

    assert before.keys() == after.keys() and len(before) > 5, f"the pods with their own Configuration are {sorted(before)}"
    # Maintenance denies every signing key but its own whichever name that is, so its Configuration is the one that did not change.
    unchanged = sorted(name for name in before if before[name] == after[name])
    assert unchanged == ["rask-maintenance", "rask-maintenance-worker"], f"pods whose Configuration did not change are {unchanged}"
