"""The dev OpenBao serves only a store its own pod seeded, and a minted token outlives the server holding it.

[[XC-076]], the clause every sidecar still loading lance-secrets. `server -dev` keeps its store in memory,
so every new OpenBao server starts empty. A seed that reaches the store through the Service writes to
whichever pod the Service routes to: during a rollout that is the outgoing pod, whose store is deleted with
it, and the replacement serves nothing while every sidecar restarting beside it fails on lance-secrets
(the outage XC-005 records, 13 pods for 19 h). And a replacement that mints its tokens afresh rotates them
under every caller and verifier that cached the old ones, a 401 with every pod Ready ([[LH-304]]).

So the seed runs beside the server it seeds, writes only to 127.0.0.1, writes the key the server's
readiness reads last, and resolves a minted token from its own earlier write, then from the store the
Service routes to, and mints only where neither holds one. The script runs here against a stand-in for
the bao CLI that keeps two stores, the pod's own server and the one the Service routes to.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS, REPO


_OWN = "http://127.0.0.1:8200"
_SENTINEL = "secret/openbao-seeded"
_MINTED = "service-token-service-catalog"
_SERVED_TOKEN = "Kx7Q2mZ9pLr4Vn8Bt1Wc6Hy3Ja5Ds0Fg2Ek9Uo4Z"

#: Answers by BAO_ADDR from two directories of `key=value` files, the second only at the rendered Service's address. A store holding `<name>.refused` refuses
#: the connection, `<name>.forbidden` answers 403, and `local.readonly` fails every write; the messages are
#: OpenBao 2.2.0's, measured on the estate 2026-10-02.
_BAO = """\
#!/bin/sh
case "$BAO_ADDR" in
  http://127.0.0.1:8200) name=local ;;
  "$SERVICE_ADDR") name=served ;;
  *) echo "Get \\"$BAO_ADDR/v1/sys/internal/ui/mounts/secret\\": dial tcp: lookup: no such host" >&2; exit 2 ;;
esac
store="$STORES/$name"
echo "$BAO_ADDR $*" >> "$STORES/calls"
[ "$1" = status ] && exit 0
if [ -e "$store.refused" ]; then
  echo "Get \\"$BAO_ADDR/v1/sys/internal/ui/mounts/secret\\": dial tcp 10.43.63.224:8200: connect: connection refused" >&2; exit 2
fi
if [ -e "$store.forbidden" ]; then printf 'Error making API request.\\n\\nCode: 403. Errors:\\n\\n* permission denied\\n' >&2; exit 2; fi
if [ "$1 $2" = "kv put" ]; then
  [ -e "$store.readonly" ] && { echo "Error writing data to $3" >&2; exit 2; }
  key=$3; shift 3
  mkdir -p "$(dirname "$store/$key")"
  printf '%s\\n' "$@" > "$store/$key"
  exit 0
fi
if [ "$1 $2" = "kv get" ]; then
  field=${3#-field=}
  if [ -f "$store/$4" ] && line=$(grep "^$field=" "$store/$4"); then printf '%s\\n' "${line#*=}"; exit 0; fi
  echo "No value found at ${4%%/*}/data/${4#*/}" >&2; exit 2
fi
exit 0
"""

#: The first sleep ends the run, unless the scenario restarts the server in place: then the first sleep
#: empties the pod's own store and cuts the Service off, and the second ends the run.
_SLEEP = """\
#!/bin/sh
n=$(cat "$STORES/sleeps" 2>/dev/null || echo 0)
echo $((n + 1)) > "$STORES/sleeps"
if [ -e "$STORES/restart" ] && [ "$n" -eq 0 ]; then rm -rf "$STORES/local"; : > "$STORES/served.refused"; exit 0; fi
exit 1
"""

_WRITES = ("kv put ", "auth ", "write ", "policy ")


def _seed(docs: tuple[dict, ...]) -> tuple[str, dict]:
    """The workload and container that write `secret/lance`, wherever the chart puts them."""
    [found] = [(workload, c) for workload, _, c in chart_render.containers(docs) if "bao kv put secret/lance" in " ".join(c.get("command") or [])]
    return found


def _store(stores: Path, name: str, key: str) -> dict[str, str]:
    path = stores / name / key
    return dict(line.split("=", 1) for line in path.read_text().splitlines()) if path.exists() else {}


@pytest.mark.parametrize(
    ("overlay", "served", "local", "succeeds", "token"),
    [
        pytest.param(DEFAULT_ARGS, "holds", "", True, "carried", id="carried-from-the-pod-the-service-routes-to"),
        pytest.param(("--set", "image.localImages=true", "-f", str(REPO / "chart/values-local.yaml")), "holds", "", True, "carried", id="carried-with-eso"),
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "minted", id="minted-into-a-served-absence"),
        pytest.param(DEFAULT_ARGS, "refused", "", True, "minted", id="minted-when-nothing-serves"),
        pytest.param(DEFAULT_ARGS, "forbidden", "", False, None, id="a-served-store-it-cannot-read-stops-the-seed"),
        pytest.param(DEFAULT_ARGS, "holds", "readonly", False, None, id="a-failed-write-stops-the-seed"),
        pytest.param(DEFAULT_ARGS, "holds", "restart", True, "carried", id="a-server-restarted-in-place-gets-the-same-token"),
        pytest.param(DEFAULT_ARGS, "malformed", "", False, None, id="a-malformed-token-behind-the-service-is-refused-and-not-kept"),
        pytest.param(DEFAULT_ARGS, "holds", "kept-malformed", True, "carried", id="a-malformed-kept-copy-is-read-again"),
    ],
)
def test_the_dev_store_is_seeded_by_its_own_pod_and_keeps_its_minted_tokens(  # noqa: PLR0913 — parametrized
    tmp_path: Path,
    overlay: tuple[str, ...],
    served: str,
    local: str,
    succeeds: bool,
    token: str | None,  # noqa: FBT001
) -> None:
    docs = chart_render.render(*overlay)
    workload, container = _seed(docs)
    env = chart_render.env_of(container)
    stores = tmp_path / "stores"
    (stores / "served" / "secret").mkdir(parents=True)
    if served == "holds":
        (stores / "served" / "secret" / _MINTED).write_text(f"token={_SERVED_TOKEN}\n")
    elif served == "malformed":
        (stores / "served" / "secret" / _MINTED).write_text(f"token={_SERVED_TOKEN[:39]}\n")
    elif served in {"refused", "forbidden"}:
        (stores / f"served.{served}").touch()
    if local == "readonly":
        (stores / "local.readonly").touch()
    elif local == "restart":
        (stores / "restart").touch()
    for name, body in (("bao", _BAO), ("sleep", _SLEEP)):
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755)
    (tmp_path / "home" / "seed").mkdir(parents=True)
    kept = tmp_path / "home" / "seed" / _MINTED
    if local == "kept-malformed":
        kept.write_text(_SERVED_TOKEN[:12])
    [service] = [d for d in docs if d.get("kind") == "Service" and f"Deployment/{d['metadata']['name']}" == workload]
    service_addr = f"http://{service['metadata']['name']}:{service['spec']['ports'][0]['port']}"
    run_env = {
        **os.environ,
        **env,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STORES": str(stores),
        "SERVICE_ADDR": service_addr,
    }

    ran = subprocess.run(["sh", "-c", container["command"][-1]], env=run_env, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603, S607

    calls = (stores / "calls").read_text().splitlines()
    writes = [call for call in calls if call.split(" ", 1)[1].startswith(_WRITES)]
    assert writes, "the seed wrote nothing"
    assert all(call.startswith(f"{_OWN} ") for call in writes), (
        f"writes reached {sorted({call.split(' ', 1)[0] for call in writes} - {_OWN})}, the Service's pick of OpenBao pod, not the server beside the seed"
    )
    assert workload == "Deployment/rask-openbao", f"the seed runs in {workload}, so 127.0.0.1 is not the server it seeds"
    assert (ran.returncode == 0) is succeeds, f"exit {ran.returncode}: {ran.stderr[-2000:]}"
    seeded = [call for call in writes if call.startswith(f"{_OWN} kv put {_SENTINEL} ")]
    if not succeeds:
        assert not seeded, f"the seed stopped but still marked the store ready: {seeded}"
        assert not _store(stores, "local", f"secret/{_MINTED}"), "a token was written after the seed refused"
        if served == "malformed":
            assert not kept.exists(), "the refused token was kept, so every restart reads it back and refuses again"
        return
    assert writes[-1].startswith(f"{_OWN} kv put {_SENTINEL} "), f"the readiness key is not the last write: {writes[-1]}"
    assert len(seeded) == (2 if local == "restart" else 1), f"seeds that completed: {len(seeded)}"

    written = _store(stores, "local", f"secret/{_MINTED}").get("token", "")
    if token == "carried":
        assert written == _SERVED_TOKEN, "the minted token was replaced, so every caller that cached it now disagrees with its verifier"
    else:
        assert re.fullmatch(r"[A-Za-z0-9]{40}", written), f"not a 40-character token: {written!r}"

    [deployment] = [doc for doc in docs if f"{doc.get('kind')}/{doc['metadata']['name']}" == workload]
    rolling = deployment["spec"].get("strategy") or {}
    assert (rolling.get("type"), rolling.get("rollingUpdate", {}).get("maxUnavailable"), rolling.get("rollingUpdate", {}).get("maxSurge")) == (
        "RollingUpdate",
        0,
        1,
    ), f"the rollout is {rolling}: unless the outgoing pod serves until its replacement is Ready, there is nothing to carry the tokens from"
    policies = [
        d
        for d in chart_render.render(*overlay, "--set", "networkPolicy.enabled=true")
        if d.get("kind") == "NetworkPolicy" and d["spec"]["podSelector"].get("matchLabels") == {"app.kubernetes.io/component": "openbao"}
    ]
    admitted = {
        value
        for policy in policies
        for rule in policy["spec"]["ingress"]
        for source in rule["from"]
        for expr in source.get("podSelector", {}).get("matchExpressions", [])
        for value in expr["values"]
    }
    assert policies and "openbao" in admitted, f"the OpenBao lock admits {sorted(admitted)}: a seed cannot read the outgoing pod through it"
    [server] = [c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "openbao"]
    probe = server["readinessProbe"]["httpGet"]
    assert probe["path"] == f"/v1/{_SENTINEL.replace('/', '/data/', 1)}", f"the server is Ready on {probe['path']}, before the seed is complete"
    assert {"name": "X-Vault-Token", "value": env["BAO_TOKEN"]} in probe.get("httpHeaders", []), "the readiness read carries no token the server accepts"
