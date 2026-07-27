package main

import (
	"context"

	"dagger/lance-ns/internal/dagger"
)

// chartsBaseImage is a small Debian userland matching the uv image's `trixie` release. The prod-render
// check (scripts/prod_render_check.sh) leans on GNU grep (`-A`, `-o`) + awk + bash `set -o pipefail`, so a
// busybox base would silently misbehave — Debian gives the same tools CI's ubuntu-latest runner has.
const chartsBaseImage = "debian:trixie-slim"

// helmVersion pins Helm deliberately. The CI `test` job uses azure/setup-helm@v4 with no version input
// (latest stable Helm 3, a moving target) — nothing in the repo pins one — so Dagger fixes it here for a
// reproducible render. The render invariants are object-count / string greps, insensitive to the Helm
// minor, and the chart deps are vendored (chart/charts/*.tgz) so template/lint run fully offline.
const helmVersion = "3.16.4"

// promVersion matches the CI job's promtool exactly (PV=2.55.1) — the version that authored rules_test.yml.
const promVersion = "2.55.1"

// chartsBase builds the tool container for the non-Python hermetic gates: a small Debian base with the
// pinned helm + promtool binaries curled from their release archives onto PATH, plus the repo source.
// `.localbin` is excluded (on top of base()'s excludes) so the Makefile's `export PATH := $(LOCALBIN):…`
// prepend can't shadow our pinned promtool with a host-downloaded one — the curled /usr/local/bin binaries
// are the sole source of truth. The gate make targets call bare `helm`/`promtool`/`bash`, so /usr/local/bin
// on PATH is all they need; no .localbin replication required.
func (m *LanceNs) chartsBase(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(chartsBaseImage).
		WithExec([]string{"apt-get", "update"}).
		// make (runs the gate targets) + curl/ca-certificates (fetch the tool archives). bash, tar, grep,
		// awk and coreutils are already in the Debian base.
		WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "make", "curl", "ca-certificates"}).
		// Pin helm — the official get.helm.sh archive extracts to linux-amd64/helm.
		WithExec([]string{"sh", "-c",
			"curl -fsSL https://get.helm.sh/helm-v" + helmVersion + "-linux-amd64.tar.gz" +
				" | tar xz -C /tmp && install -m0755 /tmp/linux-amd64/helm /usr/local/bin/helm"}).
		// Pin promtool — same URL + version the CI job uses; the archive nests promtool under its dir.
		WithExec([]string{"sh", "-c",
			"curl -fsSL https://github.com/prometheus/prometheus/releases/download/v" + promVersion +
				"/prometheus-" + promVersion + ".linux-amd64.tar.gz" +
				" | tar xz -C /tmp && install -m0755 /tmp/prometheus-" + promVersion +
				".linux-amd64/promtool /usr/local/bin/promtool"}).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			Exclude: []string{".venv", ".git", "node_modules", ".dagger", "frontend/node_modules", ".localbin"},
		}).
		WithWorkdir("/src")
}

// Charts runs the CI `test` job's non-Python hermetic gates that aren't daggerized elsewhere: helm lint,
// the chart render + `--set` invariant gates (networkPolicy / security service-accounts / dapr resiliency),
// the prod-overlay HA/security render check, and the promtool alert-rules validate-and-fire gate. Each
// WithExec fails the whole function on the first non-zero exit (Dagger surfaces the container error), so a
// green `dagger call charts` == that whole subset of CI green. Byte-for-byte equal to the ci.yml shell.
func (m *LanceNs) Charts(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.chartsBase(src).
		// 'Helm lint + render': lint the chart, then prove a default render succeeds.
		WithExec([]string{"helm", "lint", "chart"}).
		WithExec([]string{"sh", "-c", "helm template chart >/dev/null"}).
		// 'Helm render network-policy layer (flag on) + invariants'.
		WithExec([]string{"bash", "-c", networkPolicyGate}).
		// 'Helm render security hardening (flags on) + invariants'.
		WithExec([]string{"bash", "-c", securityGate}).
		// 'Helm render dapr resiliency + DLQ layer (default on) + invariants'.
		WithExec([]string{"bash", "-c", resiliencyGate}).
		// 'Helm render prod overlay + HA/security invariants' == make prod-render-check.
		WithExec([]string{"make", "prod-render-check"}).
		// 'Alert rules — validate + prove they fire (promtool)' == make alert-rules-check.
		WithExec([]string{"make", "alert-rules-check"}).
		Stdout(ctx)
}

// networkPolicyGate: the NetworkPolicy layer is off by default and, when flipped on, renders the full
// isolation set (default-deny, DNS allow, the exclusive openbao lock). Copied verbatim from ci.yml.
const networkPolicyGate = `set -euo pipefail
off=$(helm template chart --set networkPolicy.enabled=false | grep -c "kind: NetworkPolicy" || true); [ "$off" = "0" ] || exit 1
helm template chart --set networkPolicy.enabled=true > /tmp/np.yaml
count=$(grep -c "kind: NetworkPolicy" /tmp/np.yaml); [ "$count" -ge 9 ] || exit 1
grep -q "default-deny" /tmp/np.yaml
grep -q "allow-dns" /tmp/np.yaml
grep -q "k8s-app: kube-dns" /tmp/np.yaml
grep -q -- "-openbao" /tmp/np.yaml
grep -q "NotIn" /tmp/np.yaml`

// securityGate: service accounts + infra security contexts are off by default and, when flipped on, render
// the per-workload SAs (automount off, wired in) and the non-root runAsUser tiers. Verbatim from ci.yml.
const securityGate = `set -euo pipefail
off=$(helm template chart | grep -c -- "-sa-" || true); [ "$off" = "0" ] || exit 1
helm template chart --set security.serviceAccounts.enabled=true --set security.infraContexts.enabled=true > /tmp/sec.yaml
sa_objects=$(grep -c "kind: ServiceAccount" /tmp/sec.yaml || true); [ "$sa_objects" -ge 16 ] || exit 1
automount_off=$(grep -c "automountServiceAccountToken: false" /tmp/sec.yaml || true); [ "$automount_off" -ge 16 ] || exit 1
wired=$(grep -c "serviceAccountName: .*-sa-" /tmp/sec.yaml || true); [ "$wired" -ge 12 ] || exit 1
grep -q "runAsUser: 999" /tmp/sec.yaml
grep -q "runAsUser: 65532" /tmp/sec.yaml`

// resiliencyGate: the Dapr resiliency + DLQ layer is on by default (Resiliency CR, DLQ topics, the long
// 720s,720s retry) and, when flipped off, falls back to the legacy inline retry with no DLQ. Verbatim.
const resiliencyGate = `set -euo pipefail
helm template chart > /tmp/resil.yaml
grep -q "kind: Resiliency" /tmp/resil.yaml
grep -q "pubsubDeliveryRetry" /tmp/resil.yaml
dlq=$(grep -c "DLQ_TOPIC" /tmp/resil.yaml || true); [ "$dlq" -ge 3 ] || exit 1
grep -q '"dlq.>"' /tmp/resil.yaml
grep -q "720s,720s" /tmp/resil.yaml
! grep -q "30s,60s,120s,300s" /tmp/resil.yaml
helm template chart --set dapr.resiliency.enabled=false > /tmp/legacy.yaml
off=$(grep -c "kind: Resiliency" /tmp/legacy.yaml || true); [ "$off" = "0" ] || exit 1
offdlq=$(grep -c "DLQ_TOPIC" /tmp/legacy.yaml || true); [ "$offdlq" = "0" ] || exit 1
grep -q '30s,60s,120s,300s' /tmp/legacy.yaml`
