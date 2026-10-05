"""OpenFGA authenticates every caller by a projected ServiceAccount token, and every FGA client the chart renders presents one ([[XC-077]]).

Measured live on helm rev 283: the deployed OpenFGA ran with no `OPENFGA_AUTHN_*` at all, the playground on and
CORS `*`, so any pod, Ray workload code included, could write `estate#admin` for itself or replace the model.

The server half: OpenFGA runs OIDC with the audience the clients project and admits only their ServiceAccounts, fetches keys from the issuer mirror
the chart renders (the k3s issuer refuses an unauthenticated fetch), accepts the cluster issuer as the tokens'
`iss`, and serves no playground and no CORS origin. The client half: each container that names an OpenFGA
address reads `RASK_FGA_TOKEN_FILE` from a projected token volume for that audience, and its account, qualified by
the release namespace, is on OpenFGA's subject list, which names no other account. Whether the deployed server
then answers an uncredentialed call 401 is the live probe's (`scratch/probe_openfga_authn.py`).
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, render


#: The default render, and one with every optional FGA client on: the explorer trio and the stage runners.
_RENDERS = pytest.mark.parametrize(
    "overlay", [pytest.param((), id="default"), pytest.param(("--set", "explorer.enabled=true", "--set", "medallion.fgaEnabled=true"), id="every-client")]
)


def _openfga(docs: tuple[dict, ...]) -> dict[str, str]:
    return next(env_of(c) for workload, name, c in containers(docs) if workload == "Deployment/rask-openfga" and name == "openfga")


def test_openfga_runs_oidc_against_the_mirrored_cluster_issuer_with_no_playground() -> None:
    docs = render(*DEFAULT_ARGS)
    server = _openfga(docs)
    host, port = server.get("OPENFGA_AUTHN_OIDC_ISSUER", "").removeprefix("http://").partition(":")[::2]
    mirror_service = next((d for d in docs if d.get("kind") == "Service" and d["metadata"]["name"] == host), None)
    mirror = next((env_of(c) for workload, _, c in containers(docs) if workload == f"Deployment/{host}"), {})

    assert {k: server.get(k) for k in ("OPENFGA_AUTHN_METHOD", "OPENFGA_AUTHN_OIDC_AUDIENCE", "OPENFGA_PLAYGROUND_ENABLED")} == {
        "OPENFGA_AUTHN_METHOD": "oidc",
        "OPENFGA_AUTHN_OIDC_AUDIENCE": "rask-openfga",
        "OPENFGA_PLAYGROUND_ENABLED": "false",
    }
    assert mirror_service is not None and [p["port"] for p in mirror_service["spec"]["ports"]] == [int(port)], f"no Service {host}:{port} for OpenFGA's issuer"
    assert mirror.get("RASK_ISSUER_MIRROR_ISSUER") == server.get("OPENFGA_AUTHN_OIDC_ISSUER_ALIASES") == "https://kubernetes.default.svc.cluster.local"
    assert server.get("OPENFGA_HTTP_CORS_ALLOWED_ORIGINS", "*") != "*"


@_RENDERS
def test_every_fga_client_presents_a_projected_token_openfga_admits(overlay: tuple[str, ...]) -> None:
    docs = render(*DEFAULT_ARGS, *overlay)
    audience = _openfga(docs)["OPENFGA_AUTHN_OIDC_AUDIENCE"]
    pods = {f"{d['kind']}/{d['metadata']['name']}": d["spec"]["template"]["spec"] for d in docs if d.get("kind") in {"Deployment", "Job"}}
    subjects_ref = next(
        e["valueFrom"]["configMapKeyRef"]
        for workload, name, c in containers(docs)
        if workload == "Deployment/rask-openfga" and name == "openfga"
        for e in c.get("env") or []
        if e["name"] == "OPENFGA_AUTHN_OIDC_SUBJECTS"
    )
    config_map = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == subjects_ref["name"])
    admitted = set(config_map["data"][subjects_ref["key"]].split(","))

    clients, missing, accounts = [], [], set()
    for workload, name, container in containers(docs):
        env = env_of(container)
        if not ({"RASK_FGA_API_URL", "FGA_API_URL"} & env.keys()):
            continue
        clients.append(f"{workload}/{name}")
        accounts.add(f"system:serviceaccount:default:{pods[workload]['serviceAccountName']}")
        token = PurePosixPath(env.get("RASK_FGA_TOKEN_FILE", "/unset"))
        mount = next((m for m in container.get("volumeMounts") or [] if PurePosixPath(m["mountPath"]) == token.parent), None)
        volume = next((v for v in pods[workload].get("volumes") or [] if mount and v["name"] == mount["name"]), {})
        sources = [s["serviceAccountToken"] for s in volume.get("projected", {}).get("sources", []) if "serviceAccountToken" in s]
        if {(s.get("audience"), s["path"]) for s in sources} != {(audience, token.name)}:
            missing.append(f"{workload}/{name}")

    assert len(clients) >= 12, f"the render names fewer OpenFGA clients than the estate runs: {clients}"
    assert missing == [], f"these FGA clients present no projected {audience!r} token at RASK_FGA_TOKEN_FILE: {missing}"
    assert admitted == accounts, (
        f"OpenFGA's subject list differs from the FGA clients' accounts: refused {sorted(accounts - admitted)}, admitted non-clients {sorted(admitted - accounts)}"
    )
