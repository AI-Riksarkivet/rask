"""`scripts/helm.sh` must not impose the live estate's kubeconfig on a caller that named another cluster.

The seam defaults `KUBECONFIG` to `~/.kube/k3s.yaml` (the copy `make k3s-install` writes), which is right for
`make k3s-up` whose job IS the live estate, and wrong for `scripts/e2e_stack.sh`, a script that creates a KIND cluster
and declares `RASK_EXPECT_CONTEXT=kind-rask`. On a GitHub runner that path does not exist at all, so imposing it
resolves to NO context, and the guard then refuses with `context '<none>'`: a message that reads as "wrong cluster
selected" when the truth is "the kubeconfig you were handed is not there" (measured: the `e2e-stack` lane died on it
the first time it got far enough to deploy).

TWO CONDITIONS, because each one alone still leaves a way to be wrong. A declared caller has already
pointed its own kubeconfig (kind writes `~/.kube/config`), so the default must not touch it. And an
undeclared caller on a host with no k3s must not be handed a path that cannot be read, because an
unreadable kubeconfig and a wrong context are indistinguishable in the output.

The script runs for real with `kubectl` stubbed onto the front of the real PATH — a scrubbed PATH is
its own source of wrong answers — so what is under test is the seam rather than a copy of its logic. HOME is the
test's own directory, so the k3s kubeconfig exists exactly when a test writes it.
"""

import os
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_HELM = _ROOT / "scripts" / "helm.sh"

#: Answers per KUBECONFIG, which is the whole point: the seam PROBES the k3s kubeconfig to decide
#: whether its default can serve the declared context, so a stub that answered the same context for
#: every file would make that probe always agree and the discriminator untestable. The k3s path
#: answers `default` (what the real one carries on this estate); anything else answers the test's own.
_KUBECTL_STUB = """#!/usr/bin/env bash
if [ "$1 $2" = "config current-context" ]; then
  case "${KUBECONFIG:-}" in
    "$HOME/.kube/k3s.yaml") echo "${STUB_K3S_CONTEXT:-default}"; exit 0 ;;
  esac
  [ -n "${STUB_CONTEXT:-}" ] || exit 1
  echo "$STUB_CONTEXT"
  exit 0
fi
exit 0
"""
#: Records the KUBECONFIG it was invoked with, then succeeds — this is what the seam `exec`s into.
_HELM_STUB = """#!/usr/bin/env bash
printf '%s\\n' "${KUBECONFIG:-<unset>}" > "$STUB_KUBECONFIG_SEEN"
exit 0
"""


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _k3s_kubeconfig(tmp_path: Path) -> Path:
    """The k3s kubeconfig under the test's HOME, written."""
    kube = _home(tmp_path) / ".kube" / "k3s.yaml"
    kube.parent.mkdir(parents=True)
    kube.write_text("apiVersion: v1\nkind: Config\n")
    return kube


def _run(tmp_path: Path, *args: str, env_extra: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("kubectl", _KUBECTL_STUB), ("helm", _HELM_STUB)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    seen = tmp_path / "kubeconfig-seen"
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("KUBECONFIG", "RASK_EXPECT_CONTEXT")},
        "HOME": str(_home(tmp_path)),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_KUBECONFIG_SEEN": str(seen),
        **env_extra,
    }
    done = subprocess.run(["bash", str(_HELM), *args], cwd=_ROOT, env=env, capture_output=True, text=True, check=False)
    return done, seen.read_text().strip() if seen.exists() else ""


def test_a_declared_caller_keeps_its_own_kubeconfig(tmp_path: Path) -> None:
    """The CI shape: kind's kubeconfig, a declared context, and no k3s anywhere."""
    kube = tmp_path / "kind.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    done, seen = _run(
        tmp_path,
        "upgrade",
        "--install",
        "rask",
        "./chart",
        env_extra={"RASK_EXPECT_CONTEXT": "kind-rask", "STUB_CONTEXT": "kind-rask", "KUBECONFIG": str(kube)},
    )

    assert done.returncode == 0, f"a correctly declared caller was refused:\n{done.stderr}"
    assert seen == str(kube), f"the seam repointed a declared caller at {seen!r} instead of its own kubeconfig"


def test_a_declared_caller_with_no_kubeconfig_is_not_handed_the_live_estates(tmp_path: Path) -> None:
    """A declared caller that set nothing must fall through to kubectl's own default, never to the
    live estate — that path is how a kind lane ends up aimed at production."""
    _k3s_kubeconfig(tmp_path)
    done, seen = _run(tmp_path, "upgrade", "rask", "./chart", env_extra={"RASK_EXPECT_CONTEXT": "kind-rask", "STUB_CONTEXT": "kind-rask"})

    assert (done.returncode, seen) == (0, "<unset>"), f"a declared caller was handed the live estate's kubeconfig: {seen!r}\n{done.stderr}"


def test_a_genuine_context_mismatch_is_still_refused(tmp_path: Path) -> None:
    """The guard this seam exists for must survive the change: declaring one cluster and pointing at
    another still exits non-zero."""
    kube = tmp_path / "other.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    done, _ = _run(
        tmp_path,
        "upgrade",
        "rask",
        "./chart",
        env_extra={"RASK_EXPECT_CONTEXT": "kind-rask", "STUB_CONTEXT": "k3s-live", "KUBECONFIG": str(kube)},
    )

    assert done.returncode != 0, "a declared caller was allowed to mutate the wrong cluster"
    assert "kind-rask" in done.stderr and "k3s-live" in done.stderr, done.stderr


def test_an_unreadable_kubeconfig_says_so_instead_of_blaming_the_context(tmp_path: Path) -> None:
    """`<none>` has two causes needing opposite answers. The message must separate them, or a reader
    goes looking for contexts in a file that is not there."""
    done, _ = _run(
        tmp_path,
        "upgrade",
        "rask",
        "./chart",
        env_extra={"RASK_EXPECT_CONTEXT": "kind-rask", "KUBECONFIG": str(tmp_path / "does-not-exist")},
    )

    assert done.returncode != 0
    assert "not readable" in done.stderr, f"the refusal blames the context for a missing file:\n{done.stderr}"


@pytest.mark.parametrize("subcommand", ["template"])
def test_a_read_only_subcommand_still_bypasses_everything(tmp_path: Path, subcommand: str) -> None:
    """`make bootstrap` runs these on a host with no cluster at all; the seam must not start needing
    one."""
    done, _ = _run(tmp_path, subcommand, env_extra={})

    assert done.returncode == 0, done.stderr


def test_a_caller_declaring_the_context_the_default_serves_still_gets_it(tmp_path: Path) -> None:
    """The regression the narrow fix nearly shipped.

    Seven scripts declare `RASK_EXPECT_CONTEXT=default`, MEAN the live estate and set no KUBECONFIG of
    their own. Dropping the default for every declared caller would have left all seven resolving
    whatever `~/.kube/config` happens to hold — which on this host is a stale kind cluster that still
    answers. The discriminator is whether the default SERVES the declared context, not whether a
    declaration exists.
    """
    k3s = _k3s_kubeconfig(tmp_path)

    done, seen = _run(
        tmp_path, "upgrade", "rask", "./chart", env_extra={"RASK_EXPECT_CONTEXT": "default", "STUB_CONTEXT": "default", "STUB_K3S_CONTEXT": "default"}
    )

    assert (done.returncode, seen) == (0, str(k3s)), f"a caller declaring the context the default serves was not given it: {seen!r}\n{done.stderr}"


def test_a_caller_that_set_a_kubeconfig_keeps_it_even_when_the_default_would_serve(tmp_path: Path) -> None:
    """An explicit KUBECONFIG is an instruction, not a hint.

    Without this leg the guard's first condition is untestable: every other case has the inner probe
    disagreeing, so removing the `-z "${KUBECONFIG:-}"` check changes nothing they observe. Here the
    declared context is exactly what the k3s default carries, so only that check stands between the
    caller's own file and being silently repointed at the live estate.
    """
    _k3s_kubeconfig(tmp_path)
    kube = tmp_path / "mine.kubeconfig"
    kube.write_text("apiVersion: v1\nkind: Config\n")
    done, seen = _run(
        tmp_path,
        "upgrade",
        "rask",
        "./chart",
        env_extra={"RASK_EXPECT_CONTEXT": "default", "STUB_CONTEXT": "default", "STUB_K3S_CONTEXT": "default", "KUBECONFIG": str(kube)},
    )

    assert done.returncode == 0, done.stderr
    assert seen == str(kube), f"an explicitly set kubeconfig was replaced by the default: {seen!r}"
