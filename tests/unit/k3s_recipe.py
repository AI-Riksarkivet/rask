"""The k3s deploy path as MAKE runs it, for gates that must not restate it.

Every helper here starts from `make -n`: the question these gates ask is what make hands to a shell,
and reading the Makefile text answers a different one — it cannot see how make splits a recipe into
shells, and it cannot expand `$(VARIABLES)`. Commands that would touch a cluster, a registry or the
container runtime are replaced by a shell function that prints its arguments, so a recipe is executed
for its ARGUMENTS without doing anything.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The prerequisites of `k3s-up`, marked up to date so `make -n` prints its own recipe alone.
K3S_UP_PREREQUISITES = ("k3s-deps", "k3s-crds", "k3s-stem-check")

#: A cluster identity no capture in the repo was taken from — what a fresh host looks like.
FRESH_CLUSTER_UID = "00000000-0000-4000-8000-000000000000"

#: A local tag nothing defaults to. Agreement gates run at THIS tag, not the Makefile's `dev`: the chart's
#: own `image.tag` default is also `dev`, so at `dev` a recipe that dropped the tag, or a literal `:dev`
#: put back in a target, would still agree and the gate could not see it.
PROBE_TAG = "rask-probe-tag"


def _make_env() -> dict[str, str]:
    """A clean environment for make. Every Makefile knob here is `?=`, so an exported HELM, ZONES,
    EXPLORER or K3S_TAG in a developer's shell would silently change the recipe under test."""
    return {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}}


def _overrides(variables: dict[str, str] | None) -> list[str]:
    return [f"{k}={v}" for k, v in (variables or {}).items()]


def make_value(expression: str, variables: dict[str, str] | None = None) -> str:
    """`$(expression)` as the Makefile evaluates it — `?=`, `$(foreach)` and all."""
    argv = ["make", "-s", "--no-print-directory", *_overrides(variables), "--eval", f"__rask_print: ; @echo {expression}", "__rask_print"]
    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=True, timeout=60, env=_make_env()).stdout.strip()  # noqa: S603


def logical_commands(target: str, *up_to_date: str, variables: dict[str, str] | None = None) -> list[str]:
    """Every command make would hand to a shell for `target`, backslash continuations joined."""
    argv = ["make", "-n", "--no-print-directory", *_overrides(variables), *(f"-o{t}" for t in up_to_date), target]
    printed = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=True, timeout=60, env=_make_env()).stdout  # noqa: S603
    commands: list[str] = []
    pending: list[str] = []
    for line in printed.splitlines():
        # The backslash stays: this text is handed to bash again, and without it every newline would
        # end the command where make meant it to continue.
        if line.endswith("\\"):
            pending.append(line)
            continue
        commands.append("\n".join([*pending, line]))
        pending = []
    return commands


def upgrade_shell(target: str, *up_to_date: str, variables: dict[str, str] | None = None) -> str:
    """The one shell `make <target>` runs `helm upgrade --install rask` in."""
    upgrades = [c for c in logical_commands(target, *up_to_date, variables=variables) if "upgrade --install rask" in c]
    assert len(upgrades) == 1, f"expected exactly one shell in `make {target}` running `helm upgrade --install rask`, found {len(upgrades)}"
    return upgrades[0]


def k3s_up_upgrade_shell(variables: dict[str, str] | None = None) -> str:
    """The one shell `make k3s-up` runs `helm upgrade --install rask` in."""
    return upgrade_shell("k3s-up", *K3S_UP_PREREQUISITES, variables=variables)


def _fake(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


#: Flags `helm upgrade` takes that `helm template` does not.
_UPGRADE_ONLY = {"--install", "--wait", "--wait-for-jobs", "--take-ownership"}
#: Flags followed by a separate value token (`--timeout` is upgrade-only and dropped with its value).
_TAKES_A_VALUE = {"-f", "--values", "--set", "--set-string", "--timeout"}


class UpgradeRefused(AssertionError):
    """The recipe exited non-zero before reaching `helm upgrade`; `.stderr` says why."""

    def __init__(self, stderr: str) -> None:
        super().__init__(stderr)
        self.stderr = stderr


def captured_upgrade(
    tmp: pathlib.Path,
    target: str,
    *,
    live: dict | None,
    cluster_uid: str = FRESH_CLUSTER_UID,
    up_to_date: tuple[str, ...] = (),
    variables: dict[str, str] | None = None,
) -> list[str]:
    """The flags `make <target>` passes to `helm upgrade`, from a run against a FAKE helm.

    The target's upgrade shell runs for real — the `.env` loader, `scripts/k3s-image-args.sh` and every
    other script it calls — in a scratch directory whose `scripts/` links every real script except
    `helm.sh`. That one is a stand-in: `get values` / `status` answer as the release `live` describes
    (`None` = no release), and `upgrade` records its argv instead of touching a cluster. kubectl is a
    stand-in reporting `cluster_uid`. No `.env` exists there, so a developer's own credentials cannot
    change the answer.

    Returns the flags in the form `chart_render.render()` accepts: everything after `<release> <chart>`,
    minus the upgrade-only flags, with `-f` paths made absolute. Raises `UpgradeRefused` when the
    recipe exits before the upgrade.
    """
    shell = upgrade_shell(target, *up_to_date, variables=variables)
    workdir = tmp / f"{target}-run"
    (workdir / "scripts").mkdir(parents=True)
    (workdir / "chart").symlink_to(REPO / "chart")
    for script in (REPO / "scripts").iterdir():
        if script.name != "helm.sh":
            (workdir / "scripts" / script.name).symlink_to(script)
    captured = tmp / f"{target}-helm-argv"
    # A recipe may delete its temp values file once helm returns, as real helm has already read it; the
    # stand-in snapshots every absolute `-f` file at call time so the render sees what helm saw.
    snapshots = tmp / f"{target}-values"
    snapshots.mkdir()
    live_json, live_yaml = tmp / f"{target}-live.json", tmp / f"{target}-live.yaml"
    if live is not None:
        live_json.write_text(json.dumps(live), encoding="utf-8")
        live_yaml.write_text(yaml.safe_dump(live), encoding="utf-8")
    # `helm get values -o json|yaml` — answered in the format asked for, as helm does.
    answer = (
        f"""case "$*" in *"-o json"*) cat '{live_json}' ;; *) cat '{live_yaml}' ;; esac"""
        if live is not None
        else 'echo "Error: release: not found" >&2; exit 1'
    )
    _fake(
        workdir / "scripts" / "helm.sh",
        f"""case "$1 $2" in
  "get values") {answer} ;;
  "status "*) {"exit 0" if live is not None else "exit 1"} ;;
  "upgrade "*) n=0; prev=""; for a in "$@"; do
      if [ "$prev" = "-f" ] && [ "${{a#/}}" != "$a" ]; then n=$((n+1)); cp "$a" '{snapshots}'/values-$n.yaml; a='{snapshots}'/values-$n.yaml; fi
      printf '%s\\n' "$a"; prev="$a"; done >'{captured}' ;;
  *) echo "fake helm.sh: unexpected $*" >&2; exit 2 ;;
esac
""",
    )
    kubectl = _fake(tmp / f"{target}-kubectl", f'case "$*" in *"namespace kube-system"*) printf %s {cluster_uid} ;; *) exit 1 ;; esac\n')
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp), "KUBECTL": str(kubectl)}
    done = subprocess.run(["bash", "-c", shell], cwd=workdir, capture_output=True, text=True, timeout=120, env=env)  # noqa: S603
    if done.returncode != 0 or not captured.exists():
        raise UpgradeRefused(done.stdout + done.stderr)

    argv = captured.read_text(encoding="utf-8").splitlines()
    assert argv[:1] == ["upgrade"], f"expected the captured call to start with `upgrade`: {argv[:6]}"
    flags: list[str] = []
    positional: list[str] = []
    tokens = argv[1:]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _TAKES_A_VALUE:
            value = tokens[i + 1]
            if token in {"-f", "--values"} and not pathlib.Path(value).is_absolute():
                value = str(REPO / value)
            if token != "--timeout":
                flags += [token, value]
            i += 2
        elif token.startswith("-"):
            if token not in _UPGRADE_ONLY:
                flags.append(token)
            i += 1
        else:
            positional.append(token)
            i += 1
    assert positional == ["rask", "./chart"], f"unexpected release/chart in the captured upgrade: {positional}"
    return flags


def fresh_host_template_args(tmp: pathlib.Path, variables: dict[str, str] | None = None) -> list[str]:
    """`make k3s-up`'s helm flags on a cluster with NO release and NO capture of its own."""
    return captured_upgrade(tmp, "k3s-up", live=None, up_to_date=K3S_UP_PREREQUISITES, variables=variables)


def rendered_images(docs: tuple[dict, ...]) -> list[tuple[str, str]]:
    """`(kind/name, image)` for every string-valued `image` key anywhere in the render.

    A recursive walk, not a list of workload shapes: KubeRay nests its pod templates under
    `headGroupSpec`/`workerGroupSpecs`, and a walk that knows only `spec.template` reports a RayCluster
    as having no containers at all.
    """
    found: list[tuple[str, str]] = []

    def walk(node: object, owner: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "image" and isinstance(value, str):
                    found.append((owner, value))
                else:
                    walk(value, owner)
        elif isinstance(node, list):
            for item in node:
                walk(item, owner)

    for doc in docs:
        walk(doc, f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name', '?')}")
    return found


def buildable_stems() -> set[str]:
    """Every image name this repository knows how to build — the independent side of the comparison.

    Read from the build definitions, NOT from `$(K3S_IMAGES)`, which is the list under test:
    `.docker/<stem>.dockerfile` builds `<stem>`, except the two parametrized dockerfiles —
    `frontend.dockerfile` builds `web-<zone>` per zone and `ray-runner.dockerfile` builds
    `ray-<runner>` per sealed runner — and `rest-catalog.dockerfile`, which is published as
    `lance-rest-catalog`.
    """
    stems = {p.name.removesuffix(".dockerfile") for p in (REPO / ".docker").glob("*.dockerfile")}
    stems -= {"frontend", "ray-runner", "rest-catalog"}
    stems.add("lance-rest-catalog")
    stems |= {f"web-{p.name}" for p in (REPO / "frontend" / "microfrontends").iterdir() if (p / "package.json").exists()}
    stems |= {f"ray-{p.name}" for p in (REPO / "runners").iterdir() if (p / "pyproject.toml").exists()}
    return stems


def split_reference(image: str) -> tuple[str, str, str]:
    """`(registry, stem, tag)` of an image reference. `registry` is '' for a bare (side-loaded or Docker
    Hub library) name; `stem` is the last path component when a registry is present, else the whole
    repository path, so `openfga/openfga` can never be mistaken for a first-party stem."""
    registry, rest = "", image
    head, slash, tail = image.partition("/")
    if slash and ("." in head or ":" in head or head == "localhost"):
        registry, rest = head, tail
    if "@" in rest:
        repository, _, digest = rest.partition("@")
        tag = "@" + digest
    else:
        repository, _, tag = rest.partition(":")
    return registry, repository.rsplit("/", 1)[-1] if registry else repository, tag


def dumps(pairs: list[tuple[str, str]]) -> str:
    return json.dumps(pairs, indent=1)
