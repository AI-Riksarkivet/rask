"""Naming a scoped storage identity is enough to provision it, and its secret is one string everywhere.

Q17-17. The estate's scoped identities are hand patches: `helm get values` carries no `rustfs` section
at all, so `rask-maintenance` and `rask-medallion` live on the Deployments and nowhere in the release's
intent. They survive only because Helm patches fields that CHANGED between releases — one values edit
and they revert to the tenant ROOT with nothing going red.

WHY THEY WERE NEVER WRITTEN DOWN: doing so meant committing a secret. Both live scoped secrets are
hand-minted random values (measured 2026-09-07 — neither matches any derivation), and a values file is
a git file. So the control that most wants to be declared was the one that could not be.

DERIVE IT, on the estate's own precedent. `lance.dedicatedServiceToken` already answers exactly this
for the service bearer: `sha256sum` of the identity plus a secret the chart already governs, truncated.
The same shape here means a scoped identity is named by ONE value — its access key — and its secret is
computed. Nothing new enters git, the render is deterministic so a re-render is not a rotation, and on
a real deployment `minio.secretKey` must already be overridden (`prod-credentials.yaml` refuses the
dev value), so ONE override makes every derived secret real too.

THE PAIRING IS THE WHOLE TEST, and it is why this cannot be a grep. The secret is read at EIGHT sites
across five templates — both maintenance Deployments, both medallion ones, the OpenBao seed and the
`mc` provisioning Job. A derivation applied at seven of eight renders a scoped access key beside a
secret that does not match it, which is `SignatureDoesNotMatch` on every S3 call: a diff that looks
correct and takes the cascade down on contact. So this asserts the SAME STRING reaches all of them.

An explicitly-set secret still wins, because an operator who supplies one from a secret manager must
not have it silently replaced by a derived value.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _helm_template, _rendered_docs  # noqa: E402


#: `(access-key value, the env prefix its plane uses, the OpenBao/Dapr field name)`.
PLANES = {
    "medallion": ("minio.medallionAccessKey=rask-medallion", "MEDALLION", "medallion-s3-secret-key"),
    "maintenance": ("minio.maintenanceAccessKey=rask-maintenance", "MAINTENANCE", "maintenance-s3-secret-key"),
}


def _env_of(prefix: str, *set_values: str) -> dict[str, dict[str, str]]:
    """`{deployment: env}` for every Deployment carrying `<PREFIX>_S3_ACCESS_KEY_ID`."""
    found: dict[str, dict[str, str]] = {}
    for doc in _rendered_docs(*set_values):
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            env = {e["name"]: e.get("value", "") for e in (container.get("env") or [])}
            if f"{prefix}_S3_ACCESS_KEY_ID" in env:
                found[doc["metadata"]["name"]] = env
    return found


@pytest.mark.parametrize("plane", sorted(PLANES))
def test_naming_the_identity_is_enough_to_scope_the_plane(plane: str) -> None:
    """The defect: an access key alone leaves the plane on the ROOT credential, because every site
    gates on `and accessKey secretKey`. Writing the identity down then requires writing a secret down."""
    access, prefix, _ = PLANES[plane]
    envs = _env_of(prefix, access, "openbao.enabled=false")
    assert envs, f"no {plane} Deployment rendered"
    expected = access.split("=", 1)[1]
    for name, env in envs.items():
        assert env[f"{prefix}_S3_ACCESS_KEY_ID"] == expected, f"{name} still runs as {env[f'{prefix}_S3_ACCESS_KEY_ID']} — naming the identity did not scope it"
        assert env[f"{prefix}_S3_SECRET_ACCESS_KEY"] not in ("", "minioadmin"), (
            f"{name} pairs a scoped access key with the ROOT secret — every S3 call fails SignatureDoesNotMatch"
        )


@pytest.mark.parametrize("plane", sorted(PLANES))
def test_one_secret_string_reaches_every_site(plane: str) -> None:
    """THE FAILURE THIS EXISTS TO CATCH. Eight read sites across five templates; a derivation applied
    to seven of them is a mismatched pair, which looks correct in a diff and fails every call."""
    access, prefix, field = PLANES[plane]
    envs = _env_of(prefix, access, "openbao.enabled=false")
    secrets = {env[f"{prefix}_S3_SECRET_ACCESS_KEY"] for env in envs.values()}
    assert len(secrets) == 1, f"{plane} Deployments disagree on the secret: {secrets}"
    secret = secrets.pop()

    rendered = _helm_template(access)
    assert f"{field}={secret}" in rendered, (
        f"the OpenBao seed writes a different {field} than the Deployments read — the pod fetches a secret that does not match its access key"
    )
    # ...and the `mc` Job that CREATES the RustFS user must mint it with that same secret, or the
    # credential the pods present belongs to a user whose secret is something else.
    assert re.search(rf"value:\s*\"{re.escape(secret)}\"", rendered), f"the scoped-users provisioning Job does not carry the {plane} secret the pods present"


@pytest.mark.parametrize("plane", sorted(PLANES))
def test_a_derived_secret_is_not_the_roots(plane: str) -> None:
    """A derivation that collapses onto `minio.secretKey` would scope the NAME and nothing else."""
    access, prefix, _ = PLANES[plane]
    envs = _env_of(prefix, access, "openbao.enabled=false")
    for name, env in envs.items():
        assert env[f"{prefix}_S3_SECRET_ACCESS_KEY"] != "minioadmin", f"{name} derived the root's own secret"


@pytest.mark.parametrize("plane", sorted(PLANES))
def test_an_explicit_secret_still_wins(plane: str) -> None:
    """An operator supplying one from a secret manager must not have it silently replaced."""
    access, prefix, _ = PLANES[plane]
    explicit = access.split("=", 1)[0].replace("AccessKey", "SecretKey")
    envs = _env_of(prefix, access, f"{explicit}=an-explicit-operator-supplied-secret", "openbao.enabled=false")
    assert envs, f"no {plane} Deployment rendered"
    for name, env in envs.items():
        assert env[f"{prefix}_S3_SECRET_ACCESS_KEY"] == "an-explicit-operator-supplied-secret", f"{name} overrode the operator"


def test_naming_the_identity_EMPTY_is_the_escape_hatch_back_to_root() -> None:
    """The default is now the provisioned identity; returning to the tenant root stays possible and
    has to be DELIBERATE. What is gone is reaching root by never learning the key existed — which is
    how lineage sat on `minioadmin` while holding the tightest policy in the estate."""
    envs = _env_of("MEDALLION", "openbao.enabled=false", "minio.medallionAccessKey=")
    assert envs, "no medallion Deployment rendered"
    for name, env in envs.items():
        assert env["MEDALLION_S3_ACCESS_KEY_ID"] == "minioadmin", f"{name} ignored an explicitly emptied identity"
