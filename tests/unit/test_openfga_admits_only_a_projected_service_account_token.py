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

import json
from pathlib import PurePosixPath

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, render


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


#: The accounts that write tuples ([[LH-442]]): the catalog and maintenance, plus the jobs that publish the model and seed the
#: estate's grants, whatever else the release runs. Every other governed service decides through the catalog.
_WRITERS = {"default": {"catalog", "maintenance", "jobs", "hooks"}, "every-client": {"catalog", "maintenance", "jobs", "hooks"}}


def _projected(pod: dict, container: dict, path: str) -> set[tuple[str | None, str]]:
    """The (audience, file) of the projected ServiceAccount token mounted where ``path`` lives."""
    token = PurePosixPath(path)
    mount = next((m for m in container.get("volumeMounts") or [] if PurePosixPath(m["mountPath"]) == token.parent), None)
    volume = next((v for v in pod.get("volumes") or [] if mount and v["name"] == mount["name"]), {})
    return {
        (s["serviceAccountToken"].get("audience"), s["serviceAccountToken"]["path"])
        for s in volume.get("projected", {}).get("sources", [])
        if "serviceAccountToken" in s
    }


@pytest.mark.parametrize("overlay", [pytest.param((), id="default"), pytest.param(("--set", "explorer.enabled=true"), id="every-client")])
def test_only_a_tuple_writer_holds_an_openfga_token_and_every_other_governed_service_decides_through_the_catalog(
    request: pytest.FixtureRequest, overlay: tuple[str, ...]
) -> None:
    docs = render(*DEFAULT_ARGS, "--set", "networkPolicy.enabled=true", *overlay)
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
    writers = {f"system:serviceaccount:default:rask-sa-{w}" for w in _WRITERS[request.node.callspec.id]}
    seeded = json.loads(next(env_of(c)["FGA_DECISION_ROLES"] for w, _n, c in containers(docs) if w == "Job/rask-bootstrap-admin"))
    reaching = next(d for d in docs if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "rask-openfga")["spec"]["ingress"][0]["from"]
    reachers = {v for peer in reaching for e in peer["podSelector"]["matchExpressions"] for v in e["values"]}

    openfga_clients, deciders, wrong = set(), {}, []
    for workload, name, container in containers(docs):
        env, pod = env_of(container), pods.get(workload, {})
        account = f"system:serviceaccount:default:{pod.get('serviceAccountName')}"
        if {"RASK_FGA_API_URL", "FGA_API_URL"} & env.keys():
            openfga_clients.add(account)
            if _projected(pod, container, env.get("RASK_FGA_TOKEN_FILE", "/unset")) != {(audience, "token")}:
                wrong.append(f"{workload}/{name}: no projected {audience!r} token at RASK_FGA_TOKEN_FILE")
        elif env.get("RASK_FGA_ENABLED") == "true":
            deciders[f"kubernetes~{account}"] = f"{workload}/{name}"
            if env.get("RASK_FGA_DECIDER_URL") != "http://rask-catalog:2333":
                wrong.append(f"{workload}/{name}: decides through {env.get('RASK_FGA_DECIDER_URL')!r}, not the catalog")
            if _projected(pod, container, env.get("RASK_FGA_DECIDER_TOKEN_FILE", "/unset")) != {("rask-catalog", "token")}:
                wrong.append(f"{workload}/{name}: no projected 'rask-catalog' token at RASK_FGA_DECIDER_TOKEN_FILE")
        if (
            any(
                s.get("audience") == audience
                for v in pod.get("volumes") or []
                for s in (v.get("projected") or {}).get("sources", [])
                for s in [s.get("serviceAccountToken") or {}]
            )
            and account not in writers
        ):
            wrong.append(f"{workload}/{name}: projects a {audience!r} token, and {account} writes no tuple")

    assert wrong == [], wrong
    assert admitted == openfga_clients == writers, (
        f"OpenFGA admits {sorted(admitted)}; its clients are {sorted(openfga_clients)}; the writers are {sorted(writers)}"
    )
    assert len(deciders) >= 2 and set(deciders) == set(seeded), f"deciders {sorted(deciders)} vs the roles bootstrap seeds {sorted(seeded)}"
    assert all("decider" in roles for roles in seeded.values()), seeded
    assert reachers == {"catalog", "maintenance", "bootstrap-admin", "openfga-model"}, f"OpenFGA's API is reachable from {sorted(reachers)}"
