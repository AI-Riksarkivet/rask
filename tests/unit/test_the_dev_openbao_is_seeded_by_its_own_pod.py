"""The dev OpenBao serves only a store its own pod seeded, and a signing key outlives the server holding it.

[[XC-076]], the clause every sidecar still loading lance-secrets. `server -dev` keeps its store in memory,
so every new OpenBao server starts empty. A seed that reaches the store through the Service writes to
whichever pod the Service routes to: during a rollout that is the outgoing pod, whose store is deleted with
it, and the replacement serves nothing while every sidecar restarting beside it fails on lance-secrets
(the outage XC-005 records, 13 pods for 19 h).

So the seed runs beside the server it seeds, writes only to 127.0.0.1 and writes the key the server's
readiness reads last.

[[LH-064]] puts an Ed25519 pair per signing identity through the same seed: `signing-public-<id>` (the public
list) and `signing-key-<id>` (the seed). A `mint` init container generates the candidates on a memory volume;
the seed chooses one pair per identity from the kept copy, else the store behind the Service (a key without
its list refuses the seed, a list without its key is a rotation that prepends and keeps one previous), else
the candidate; keeps it before the first write; and writes the public list first. A failed mint costs that
identity its key and never the readiness key. No seed reaches an argument list or an output stream.

The scripts run here against stand-ins for the bao and nk CLIs: the bao stand-in keeps two stores, the pod's
own server and the one the Service routes to, and the nk stand-in hands out generated pairs.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS, REPO
from tests.unit.openbao_standins import BAO, NK, install


_OWN = "http://127.0.0.1:8200"
_SENTINEL = "secret/openbao-seeded"

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


def _signing_identities(docs: tuple[dict, ...]) -> list[str]:
    """The identities the Deployments sign as, read from the env the chart renders on them."""
    found = {
        env["RASK_SIGNING_IDENTITY"] for _, _, container in chart_render.containers(docs) if "RASK_SIGNING_IDENTITY" in (env := chart_render.env_of(container))
    }
    return sorted(found)


def _arrange(case: str, ids: list[str], pool: list[Any], stores: Path, home: Path) -> tuple[dict[str, tuple[str, str] | None], dict[str, str]]:
    """Lay out one signing scenario: what the Service holds, what the kept copy holds, and which `nk` calls fail.

    Returns the pair the store must end up holding for each identity (None: nothing may be written for it) and the stand-in `nk`'s settings.
    `pool` is four generated pairs per identity: [0] the mint candidate, [1] another source's pair, [2] and [3] older public keys.
    """
    n = len(ids)

    def cand(i: int) -> Any:
        return pool[i]

    def other(i: int) -> Any:
        return pool[n + i]

    def older(i: int) -> Any:
        return pool[2 * n + i]

    def oldest(i: int) -> Any:
        return pool[3 * n + i]

    expected: dict[str, tuple[str, str] | None] = {identity: (cand(i).seed, cand(i).public) for i, identity in enumerate(ids)}
    nk: dict[str, str] = {}
    served = stores / "served" / "secret"

    def serve(identity: str, *, seed: str | None = None, keys: str | None = None) -> None:
        if seed is not None:
            (served / f"signing-key-{identity}").write_text(f"seed={seed}\n")
        if keys is not None:
            (served / f"signing-public-{identity}").write_text(f"keys={keys}\n")

    def keep(identity: str, half: str, content: str) -> None:
        """A kept copy as an earlier run of the seed leaves it: written under umask 077."""
        path = home / "seed" / f"{half}-{identity}"
        path.write_text(content)
        path.chmod(0o600)

    first, second = ids[0], ids[1]
    if case == "carried":
        serve(first, seed=other(0).seed, keys=other(0).public)
        serve(second, seed=other(1).seed, keys=f"{other(1).public},{older(1).public}")
        expected[first], expected[second] = (other(0).seed, other(0).public), (other(1).seed, f"{other(1).public},{older(1).public}")
    elif case == "rotated":
        serve(first, keys=older(0).public)
        serve(second, keys=f"{older(1).public},{oldest(1).public}")
        expected[first], expected[second] = (cand(0).seed, f"{cand(0).public},{older(0).public}"), (cand(1).seed, f"{cand(1).public},{older(1).public}")
    elif case == "kept":
        for i, identity in enumerate(ids):
            keep(identity, "signing-key", other(i).seed)
            keep(identity, "signing-public", other(i).public)
            expected[identity] = (other(i).seed, other(i).public)
    elif case == "kept-half":
        keep(first, "signing-key", other(0).seed)
    elif case == "mint-failed":
        nk, expected[second] = {"NK_FAIL": "2"}, None
    elif case == "mint-garbled":
        nk, expected[second] = {"NK_GARBAGE": "2"}, None
    elif case == "key-without-list":
        serve(first, seed=other(0).seed)
        expected = dict.fromkeys(ids)
    elif case == "malformed-pair":
        serve(first, seed=other(0).seed[:-1], keys=other(0).public)
        expected = dict.fromkeys(ids)
    elif case == "malformed-list":
        serve(first, keys="not-a-public-key")
        expected = dict.fromkeys(ids)
    return expected, nk


@pytest.mark.parametrize(
    ("overlay", "served", "local", "succeeds", "signing"),
    [
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "minted", id="minted-into-a-served-absence"),
        pytest.param(("--set", "image.localImages=true", "-f", str(REPO / "chart/values-local.yaml")), "lacks", "", True, "minted", id="minted-with-eso"),
        pytest.param(DEFAULT_ARGS, "refused", "", True, "minted", id="minted-when-nothing-serves"),
        pytest.param(DEFAULT_ARGS, "forbidden", "", False, "minted", id="a-served-store-it-cannot-read-stops-the-seed"),
        pytest.param(DEFAULT_ARGS, "lacks", "readonly", False, "minted", id="a-failed-write-stops-the-seed"),
        pytest.param(DEFAULT_ARGS, "lacks", "restart", True, "minted", id="a-server-restarted-in-place-gets-the-same-keys"),
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "carried", id="a-signing-pair-behind-the-service-is-carried"),
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "rotated", id="a-list-without-its-key-is-a-rotation-that-keeps-one-previous"),
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "kept", id="a-kept-pair-beats-a-new-mint-candidate"),
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "kept-half", id="half-a-kept-pair-is-not-kept"),
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "mint-failed", id="a-failed-mint-costs-that-identity-its-key-and-nothing-else"),
        pytest.param(DEFAULT_ARGS, "lacks", "", True, "mint-garbled", id="a-mint-that-prints-no-key-pair-leaves-nothing-behind"),
        pytest.param(DEFAULT_ARGS, "lacks", "", False, "key-without-list", id="a-key-without-its-list-stops-the-seed"),
        pytest.param(DEFAULT_ARGS, "lacks", "", False, "malformed-pair", id="a-malformed-pair-behind-the-service-is-refused"),
        pytest.param(DEFAULT_ARGS, "lacks", "", False, "malformed-list", id="a-malformed-list-is-not-rotated-onto"),
    ],
)
def test_the_dev_store_is_seeded_by_its_own_pod_and_keeps_its_signing_keys(  # noqa: PLR0913, PLR0915 — parametrized, one scenario per row
    tmp_path: Path,
    event_signer: Any,
    overlay: tuple[str, ...],
    served: str,
    local: str,
    succeeds: bool,  # noqa: FBT001
    signing: str,
) -> None:
    docs = chart_render.render(*overlay)
    workload, container = _seed(docs)
    env = chart_render.env_of(container)
    [mint] = [c for _, name, c in chart_render.containers(docs) if name == "mint"]
    ids = _signing_identities(docs)
    assert len(ids) >= 2, f"the signers the chart renders are {ids}: the scenarios need two"
    stores = tmp_path / "stores"
    (stores / "served" / "secret").mkdir(parents=True)
    if served in {"refused", "forbidden"}:
        (stores / f"served.{served}").touch()
    if local == "readonly":
        (stores / "local.readonly").touch()
    elif local == "restart":
        (stores / "restart").touch()
    install(tmp_path, bao=BAO, sleep=_SLEEP, nk=NK)
    (tmp_path / "home" / "seed").mkdir(parents=True)
    pool = [event_signer(f"pair-{i}") for i in range(4 * len(ids))]
    expected, nk = _arrange(signing, ids, pool, stores, tmp_path / "home")
    (tmp_path / "pairs").write_text("".join(f"{pair.seed}\n{pair.public}\n" for pair in pool[: len(ids)]))
    signing_dir = tmp_path / "signing"
    signing_dir.mkdir()
    [service] = [d for d in docs if d.get("kind") == "Service" and f"Deployment/{d['metadata']['name']}" == workload]
    service_addr = f"http://{service['metadata']['name']}:{service['spec']['ports'][0]['port']}"
    here = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STORES": str(stores),
        "SERVICE_ADDR": service_addr,
        "SIGNING_DIR": str(signing_dir),
        "NK_PAIRS": str(tmp_path / "pairs"),
        "NK_STATE": str(tmp_path / "nk-calls"),
        "NK_FAIL": "",
        "NK_GARBAGE": "",
        **nk,
    }

    def run(script_container: dict) -> subprocess.CompletedProcess[str]:
        argv = ["sh", "-c", script_container["command"][-1]]
        return subprocess.run(
            argv, env={**os.environ, **chart_render.env_of(script_container), **here}, capture_output=True, text=True, timeout=60, check=False
        )  # noqa: S603

    minted = run(mint)
    modes = {p.name: oct(stat.S_IMODE(p.stat().st_mode)) for p in signing_dir.iterdir() if stat.S_IMODE(p.stat().st_mode) != 0o400}
    ran = run(container)

    assert minted.returncode == 0, f"a failed mint must not stop the pod: exit {minted.returncode}: {minted.stderr[-1000:]}"
    assert not modes, f"mint candidates must be mode 0400 for their owner alone: {modes}"
    calls_text = (stores / "calls").read_text()
    calls = calls_text.splitlines()
    leaked = [pair.seed for pair in pool if pair.seed in (minted.stdout + minted.stderr + ran.stdout + ran.stderr + calls_text)]
    assert not leaked, "a private seed reached an output stream or a bao argument list"
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
        written = [
            identity
            for identity in ids
            if _store(stores, "local", f"secret/signing-key-{identity}") or _store(stores, "local", f"secret/signing-public-{identity}")
        ]
        assert not written, f"a pair was written for {written} after the seed refused it"
        assert not (tmp_path / "home" / "seed" / f"signing-key-{ids[0]}").exists(), (
            "the refused pair was kept, so every restart reads it back and refuses again"
        )
        return
    assert writes[-1].startswith(f"{_OWN} kv put {_SENTINEL} "), f"the readiness key is not the last write: {writes[-1]}"
    assert len(seeded) == (2 if local == "restart" else 1), f"seeds that completed: {len(seeded)}"

    for identity in ids:
        pair = expected[identity]
        stored = (
            _store(stores, "local", f"secret/signing-key-{identity}").get("seed"),
            _store(stores, "local", f"secret/signing-public-{identity}").get("keys"),
        )
        assert stored == (pair or (None, None)), f"{signing}: {identity} holds {stored}, not {pair}"
        public_put = next((i for i, call in enumerate(calls) if f" kv put secret/signing-public-{identity} " in call), None)
        key_put = next((i for i, call in enumerate(calls) if f" kv put secret/signing-key-{identity} " in call), None)
        if pair is None:
            assert public_put is None and key_put is None, f"{identity} had no pair to write and was written anyway"
            continue
        assert public_put is not None and key_put is not None and public_put < key_put, (
            f"{identity}: the public list must be written before the key it publishes"
        )
        for half, content in (("signing-key", pair[0]), ("signing-public", pair[1])):
            keep = tmp_path / "home" / "seed" / f"{half}-{identity}"
            assert keep.read_text() == content, f"{identity}: the {half} half was not kept, so a restart in place re-seeds another pair"
            assert stat.S_IMODE(keep.stat().st_mode) == 0o600, f"{keep.name} is mode {oct(stat.S_IMODE(keep.stat().st_mode))}"
    assert not list(signing_dir.iterdir()), f"mint candidates outlive the seed: {sorted(p.name for p in signing_dir.iterdir())}"

    [deployment] = [doc for doc in docs if f"{doc.get('kind')}/{doc['metadata']['name']}" == workload]
    pod = deployment["spec"]["template"]["spec"]
    rolling = deployment["spec"].get("strategy") or {}
    assert (rolling.get("type"), rolling.get("rollingUpdate", {}).get("maxUnavailable"), rolling.get("rollingUpdate", {}).get("maxSurge")) == (
        "RollingUpdate",
        0,
        1,
    ), f"the rollout is {rolling}: unless the outgoing pod serves until its replacement is Ready, there is nothing to carry the pairs from"
    [volume] = [v for v in pod["volumes"] if v["name"] == "signing"]
    assert volume["emptyDir"].get("medium") == "Memory" and volume["emptyDir"].get("sizeLimit"), (
        f"the candidate volume is {volume}: seeds would reach node disk"
    )
    mounted = {c["name"] for c in [*pod["initContainers"], *pod["containers"]] if any(m["name"] == "signing" for m in c.get("volumeMounts") or [])}
    assert mounted == {"mint", "seed"}, f"the candidate volume is mounted by {sorted(mounted)}, so a container that never needs a seed can read one"
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
    signers = {
        doc["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"]
        for doc in docs
        if doc.get("kind") == "Deployment" and any("RASK_SIGNING_IDENTITY" in chart_render.env_of(c) for c in doc["spec"]["template"]["spec"]["containers"])
    }
    assert signers and signers <= admitted, (
        f"the OpenBao lock does not admit the signers {sorted(signers - admitted)}: their sidecars cannot read their own keys"
    )
    [server] = [c for c in pod["containers"] if c["name"] == "openbao"]
    probe = server["readinessProbe"]["httpGet"]
    assert probe["path"] == f"/v1/{_SENTINEL.replace('/', '/data/', 1)}", f"the server is Ready on {probe['path']}, before the seed is complete"
    assert {"name": "X-Vault-Token", "value": env["BAO_TOKEN"]} in probe.get("httpHeaders", []), "the readiness read carries no token the server accepts"
