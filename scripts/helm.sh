#!/usr/bin/env bash
# The ONE seam every helm call goes through — the same role scripts/dagger-image.sh plays for builds.
#
# It names the cluster a call changes: it defaults KUBECONFIG to the k3s estate only where that default serves what
# the caller declared, and refuses a call whose declared context (RASK_EXPECT_CONTEXT) is not the one kubectl
# resolves. Every deploy target uses `upgrade --install`, so a call that reaches the wrong cluster does not fail: it
# installs there.
#
# Every release lives in Helm's default store, one Secret per revision, which Kubernetes caps at 1 MiB. What fills a
# revision is the manifest and the chart's own templates and files (rask-helm skill, section 1), and the release sits
# at about a third of the cap (docs/adr/0038-helm-release-storage-the-sql-driver-stands-the-chart-is-not.md has the
# chronology).
set -euo pipefail

# Read-only subcommands touch no cluster, and guarding them would break `make k3s-install` / `make bootstrap` on a
# host that has none yet.
case "${1:-}" in
  template|lint|repo|dependency|dep|show|version|env|create|package|search|pull|plugin|completion|help|"")
    exec helm "$@"
    ;;
esac

# THE DEFAULT APPLIES ONLY WHERE IT CAN BE RIGHT, which is narrower than "always" and wider than
# "only when nobody declared".
#
# A caller that DECLARES a cluster the default does not serve has already pointed its own kubeconfig
# there — kind writes `~/.kube/config` — so imposing the live estate's path on it replaces a correct
# invocation with a wrong one. And the path has to EXIST: a KUBECONFIG naming an absent file resolves to no
# context at all, which the guard below then reports as a context mismatch against `<none>` rather
# than as the missing file it is. Measured 2026-09-24 on the `e2e-stack` CI lane, where the runner has
# no k3s and the k3s kubeconfig is not there to read: the lane declared `kind-rask`, got the
# live estate's path imposed over kind's kubeconfig, and died on a mismatch naming a cluster nobody
# had asked for.
#
# THE DISCRIMINATOR IS WHETHER THE DEFAULT CAN SERVE WHAT THE CALLER ASKED FOR, not a list of script
# names. Seven scripts declare `RASK_EXPECT_CONTEXT=default` and MEAN this estate, and they set no
# KUBECONFIG of their own — dropping the default for every declared caller would break all of them.
# The kind lanes declare `kind-<cluster>` and rely on `kind export kubeconfig` having written the
# default location. Asking the k3s kubeconfig which context it carries separates the two exactly.
_k3s="$HOME/.kube/k3s.yaml"
if [[ -z "${KUBECONFIG:-}" && -r "$_k3s" ]]; then
  if [[ -z "${RASK_EXPECT_CONTEXT:-}" ]] \
     || [[ "$(KUBECONFIG="$_k3s" kubectl config current-context 2>/dev/null || true)" == "${RASK_EXPECT_CONTEXT}" ]]; then
    export KUBECONFIG="$_k3s"
  fi
fi

# WHICH CLUSTER IS ABOUT TO CHANGE ([[XC-057]]). The default above is right for `make k3s-up`, whose
# job IS the live estate, and wrong for every caller that meant another one — and the difference is
# invisible, because a script that sets no KUBECONFIG looks identical to one that meant this cluster.
#
# MEASURED 2026-09-23: `scripts/e2e_stack.sh` creates a KIND cluster and then never points anything at
# it — no KUBECONFIG, no context, no --kube-context anywhere in the file — so its
# `helm upgrade --install rask ./chart` landed on the live release while the kind cluster sat unused.
# Across scripts/, `--context` appeared zero times.
#
# SO A MUTATING CALLER DECLARES AND THIS REFUSES. `RASK_EXPECT_CONTEXT` is the caller's statement of
# which cluster it means; a mismatch exits non-zero rather than warning, because CI reads exit codes
# and a warning is read by nobody. Unset stays permitted: the guard's job is to catch a caller whose
# intent is KNOWN and contradicted, not to break every target that has always meant this estate.
# Declaring is each mutating caller's own job; nothing checks that a caller did.
if [[ -n "${RASK_EXPECT_CONTEXT:-}" ]]; then
  actual="$(kubectl config current-context 2>/dev/null || echo '<none>')"
  if [[ "$actual" != "$RASK_EXPECT_CONTEXT" ]]; then
    echo "!! refusing to '$1' against context '$actual' — this caller declared RASK_EXPECT_CONTEXT='$RASK_EXPECT_CONTEXT'" >&2
    # Name the kubeconfig AND whether it is readable. `<none>` has two causes that need opposite
    # answers — the wrong cluster is selected, or the file naming any cluster is not there — and a
    # message that cannot tell them apart sends a reader to check contexts that do not exist.
    echo "   KUBECONFIG=${KUBECONFIG:-<unset, kubectl default>}" >&2
    if [[ -n "${KUBECONFIG:-}" && ! -r "${KUBECONFIG}" ]]; then
      echo "   (that kubeconfig is not readable, so NO context can resolve — this is a missing file, not a wrong cluster)" >&2
    fi
    exit 1
  fi
fi

exec helm "$@"
