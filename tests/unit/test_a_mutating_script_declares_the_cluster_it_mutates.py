"""A script that mutates a cluster names which cluster, and the helm seam enforces it.

[[XC-057]]. `scripts/helm.sh` is the one seam every helm call goes through, and it defaults
`KUBECONFIG` to `/etc/rancher/k3s/k3s.yaml` — the live estate — for any caller that did not set one.
That default is right for `make k3s-up`, whose whole job is the live estate, and wrong for anything
that meant a different cluster.

MEASURED 2026-09-23, and it is not hypothetical: `scripts/e2e_stack.sh` creates a **kind** cluster and
then never sets `KUBECONFIG`, a context, or `--kube-context` — zero occurrences in the file — so its
`helm upgrade --install rask ./chart` lands on the live release instead. `scripts/ray_e2e_stack.sh`
has the same shape. Across every mutating script in `scripts/`, `--context` appears zero times; the
only file that uses it at all is `kind-browse.sh`, which is read-only.

THE GUARD IS A DECLARATION, NOT A PROMPT. A mutating script exports `RASK_EXPECT_CONTEXT`, and
`helm.sh` refuses when the cluster it is about to change is not that one. A prompt cannot run in CI
and a warning is read by nobody; a refusal is the only form that works in both places.

THE DEFAULT-ESTATE SCRIPTS DECLARE TOO, and that is the point rather than an exemption: `make k3s-up`
naming the k3s context is what makes "I meant the live estate" a statement in the file instead of an
absence that happens to be right.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

#: A helm subcommand that writes to a cluster. The read-only ones `helm.sh` execs past are excluded.
_MUTATING_HELM = re.compile(r"\bhelm(?:\.sh)?\s+(?:-\S+\s+)*(upgrade|install|uninstall|delete|rollback)\b")
#: A kubectl verb that writes.
_MUTATING_KUBECTL = re.compile(r"\bkubectl\s+(?:-\S+\s+)*(apply|delete|create|patch|replace|scale|set)\b")
#: How a script says which cluster it means — an ASSIGNMENT, never the bare name.
#:
#: The first version of this matched `RASK_EXPECT_CONTEXT` anywhere in the file, and a mutation that
#: replaced the export with `true` still passed: the comment ABOVE the export explains the variable, so
#: the name was still present and the gate read prose as code. Same shape as the "publishes nothing"
#: substring that once matched a docstring. The forms below all set something.
_DECLARES = re.compile(
    r"""(?mx)
      ^\s*export\s+RASK_EXPECT_CONTEXT=       # export RASK_EXPECT_CONTEXT=...
    | ^\s*RASK_EXPECT_CONTEXT=                 # RASK_EXPECT_CONTEXT=...
    | ^\s*:\s*"\$\{RASK_EXPECT_CONTEXT:=    # : "${RASK_EXPECT_CONTEXT:=default}"
    | --kube-context\s                         # passed per call
    | kubectl\s+config\s+use-context          # switched explicitly
    """
)


def _mutating_scripts() -> dict[str, str]:
    """Every script in `scripts/` that writes to a cluster, by name -> source."""
    found: dict[str, str] = {}
    for path in sorted(SCRIPTS.glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        if _MUTATING_HELM.search(text) or _MUTATING_KUBECTL.search(text):
            found[path.name] = text
    return found


def test_every_mutating_script_declares_its_cluster() -> None:
    """The invariant: a script that can change a cluster says which one."""
    scripts = _mutating_scripts()
    assert scripts, "no mutating script found — the walk proves nothing"
    silent = sorted(name for name, text in scripts.items() if not _DECLARES.search(text))
    assert silent == [], (
        f"{silent} mutate a cluster without naming it. `scripts/helm.sh` defaults KUBECONFIG to the "
        "LIVE k3s estate, so a script that means a different cluster silently changes that one — "
        "export RASK_EXPECT_CONTEXT with the context it intends."
    )


def test_the_helm_seam_refuses_a_cluster_it_was_not_promised() -> None:
    """A declaration nothing enforces is a comment. `helm.sh` must compare and refuse.

    Asserted on the seam's source rather than by running it: executing it needs a cluster, and what a
    commit can get wrong is whether the comparison is there at all.
    """
    seam = (SCRIPTS / "helm.sh").read_text(encoding="utf-8")
    assert "RASK_EXPECT_CONTEXT" in seam, "scripts/helm.sh does not read RASK_EXPECT_CONTEXT — the declaration is enforced by nothing"
    assert "current-context" in seam, "scripts/helm.sh never asks which cluster it is pointed at"
    # The refusal must be a hard exit, not a warning: CI reads exit codes, not prose.
    guard = seam[seam.index("RASK_EXPECT_CONTEXT") : seam.index("RASK_EXPECT_CONTEXT") + 1200]
    assert "exit 1" in guard, "the context mismatch must EXIT, not warn — a warning is read by nobody"
