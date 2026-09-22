package main

import (
	"context"

	"dagger/rask/internal/dagger"
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

// helmBinary is the pinned helm executable on its own, so every lane that needs helm gets the SAME one.
// Extracted 2026-08-03 when the pytest lane gained helm too (test.go): the thirteen chart-render
// invariants in tests/unit/test_invariants.py had been calling `pytest.skip("helm not available")` on
// every CI run because .dagger/base() is a uv image with no helm — a silent hole, since a skipped test
// reads as a passing suite. Two independently-curled helm installs would let the render gates and the
// pytest invariants disagree about the same chart under different Helm minors, which is exactly what
// helmVersion exists to prevent — so both take this file.
func helmBinary() *dagger.File {
	return dag.Container().
		From(chartsBaseImage).
		WithExec([]string{"apt-get", "update"}).
		WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "curl", "ca-certificates"}).
		// The official get.helm.sh archive extracts to linux-<arch>/helm, and the arch is READ off the
		// container rather than written here ([[XC-070]]). `dpkg --print-architecture` spells it exactly
		// as get.helm.sh does — `amd64`, `arm64` — so no translation table sits between them to drift.
		WithExec([]string{"sh", "-c",
			"set -eu; a=\"$(dpkg --print-architecture)\"; " +
				"curl -fsSL https://get.helm.sh/helm-v" + helmVersion + "-linux-${a}.tar.gz" +
				" | tar xz -C /tmp && install -m0755 /tmp/linux-${a}/helm /usr/local/bin/helm"}).
		File("/usr/local/bin/helm")
}

// chartsBase builds the tool container for the non-Python hermetic gates: a small Debian base with the
// pinned helm + promtool binaries curled from their release archives onto PATH, plus the repo source.
// `.localbin` is excluded (on top of base()'s excludes) so a host-downloaded binary cannot shadow the
// pinned one — the curled /usr/local/bin binaries are the sole source of truth in here.
//
// This comment used to justify that exclusion by "the Makefile's `export PATH := $(LOCALBIN):…` prepend".
// NO SUCH LINE HAS EVER EXISTED (`git log -S 'export PATH := $(LOCALBIN)' -- Makefile` returns nothing),
// and believing it hid a real defect: `make alert-rules-check` called a bare `promtool` that was on PATH
// nowhere, so it was dead on a developer box as well as in this container. The target now resolves
// promtool itself — PATH first, `$(LOCALBIN)` second — which is what makes the exclusion above load-
// bearing rather than decorative: PATH here holds the pinned binary, and .localbin is not mounted to
// compete with it.
func (m *Rask) chartsBase(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(chartsBaseImage).
		WithExec([]string{"apt-get", "update"}).
		// make (runs the gate targets) + curl/ca-certificates (fetch the tool archives). bash, tar, grep,
		// awk and coreutils are already in the Debian base.
		WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "make", "curl", "ca-certificates"}).
		// Pinned helm, shared with the pytest lane so the two cannot drift onto different Helm minors.
		WithFile("/usr/local/bin/helm", helmBinary()).
		// Pin promtool — same URL + version the CI job uses; the archive nests promtool under its dir.
		// Arch read off the container for the same reason helm's is: prometheus spells it `amd64`/`arm64`
		// too, so one `dpkg --print-architecture` serves both fetches.
		WithExec([]string{"sh", "-c",
			"set -eu; a=\"$(dpkg --print-architecture)\"; " +
				"curl -fsSL https://github.com/prometheus/prometheus/releases/download/v" + promVersion +
				"/prometheus-" + promVersion + ".linux-${a}.tar.gz" +
				" | tar xz -C /tmp && install -m0755 /tmp/prometheus-" + promVersion +
				".linux-${a}/promtool /usr/local/bin/promtool"}).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			Exclude: []string{"**/.env", "**/.env.*", ".venv", ".git", "node_modules", ".dagger", "frontend/node_modules", ".localbin"},
		}).
		WithWorkdir("/src")
}

// Charts runs the CI `test` job's non-Python hermetic gates that aren't daggerized elsewhere: helm lint,
// the chart render + `--set` invariant gates (networkPolicy / security service-accounts / dapr resiliency),
// the prod-overlay HA/security render check, and the promtool alert-rules validate-and-fire gate. Each
// WithExec fails the whole function on the first non-zero exit (Dagger surfaces the container error), so a
// green `dagger call charts` == that whole subset of CI green. Byte-for-byte equal to the ci.yml shell.
func (m *Rask) Charts(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.chartsBase(src).
		// Fetch the subcharts FIRST. `chart/charts/` is gitignored (.gitignore: "vendored helm subcharts
		// — rebuilt via `make k3s-deps`"), so a fresh checkout has Chart.yaml's ten dependencies declared
		// and none of them present. Every `helm template` below then dies with "found in Chart.yaml, but
		// missing in charts/ directory", which is what this gate did on every run — while passing on a
		// developer box, because there `make k3s-deps` had already populated the directory. `helm lint`
		// does NOT catch it: it exits 0 on the same tree ("1 chart(s) linted, 0 chart(s) failed") and the
		// render one line later is what fails, so the log opens with a green lint.
		//
		// Via `make k3s-deps`, not a bare `helm dependency build` here. The bare form fails with "no
		// repository definition for https://nvidia.github.io/k8s-device-plugin, …": `dependency build`
		// resolves Chart.lock against the LOCAL helm repo config, and a fresh container has none. The
		// Makefile target already carries that list (K3S_DEP_REPOS -> `helm repo add` -> `helm repo
		// update` -> `helm dependency build ./chart`), so calling it keeps ONE list of repositories
		// instead of a second copy here that drifts the first time a subchart moves — and holds the
		// same `dagger call charts` == `make charts` contract the gates below already do.
		//
		// The target uses `build`, not `update`: build installs the versions pinned in the COMMITTED
		// Chart.lock, while update re-resolves and rewrites it — which would let CI render a different
		// subchart version than a deploy does, and make this verdict depend on upstream release timing.
		WithExec([]string{"make", "k3s-deps"}).
		// 'Helm lint + render': lint the chart, then prove a render succeeds.
		//
		// ── THIS STEP WAS UNSATISFIABLE FOR 923 COMMITS, AND IT TOOK FIVE GATES DOWN WITH IT ─────────
		// A bare `helm template chart` has been REFUSED since 3c909e0a (2026-08-04, "registry required")
		// made `image.repository` a `required`, and again by b6accf32's `frontend.oidc.sessionSecret`
		// guard. So this line exited 1 on every run, and everything after it — the NetworkPolicy
		// isolation invariants, the service-account hardening invariants, the Dapr resiliency/DLQ
		// invariants, `make prod-render-check` and `make alert-rules-check` — never executed once.
		//
		// `helm lint chart` on the line ABOVE exits 0 on the same tree, emitting the guard text only as
		// `[INFO]`, which is why the job looked like it was failing on a render detail rather than on a
		// gate that could not start.
		//
		// The arguments are not a workaround: a `required` guard means the chart HAS no valid default
		// render, so a gate that renders must state a configuration. renderArgs is that statement, in
		// one place, used by every render below.
		WithExec([]string{"helm", "lint", "chart"}).
		WithExec([]string{"sh", "-c", "helm template chart " + renderArgs + " >/dev/null"}).
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

// renderArgs is the minimum configuration that makes the chart RENDERABLE — the values behind its two
// `required` guards. Every `helm template` in this file uses it, so there is one answer to "what does a
// gate render" instead of one per gate drifting apart.
//
// image.repository, NOT image.localImages. The alternative satisfies the same guard, and every one of
// the thirteen chart-render invariants in tests/unit/test_invariants.py takes it (`_helm_template()`
// appends `--set image.localImages=true` unconditionally, :401). That is the SIDE-LOAD path — what
// `make k3s-import` produces for a local kind/k3s node. It means the production path, where every image
// reference is a real registry address, is rendered by NO test in the estate. This gate is where that
// gets covered, so it takes the registry and the pytest suite keeps the side-load path. The value is a
// placeholder: the guard is about SHAPE (a registry-qualified name, not a bare one that would resolve
// to Docker Hub and ImagePullBackOff), not about which registry.
//
// THE CREDENTIALS BELOW ARE WHAT MAKE THE REGISTRY PATH RENDERABLE AT ALL ([[XC-070]]).
// `chart/templates/prod-credentials.yaml` refuses a real-registry render that still carries the
// well-known dev values — correctly, since "holding the repository is holding the credential" — and
// this gate renders exactly that shape on purpose. Without them `dagger call charts` fails on every
// invocation, which is not a gate: it cannot separate a good chart from a bad one, so its result
// stops being read and the production path goes back to uncovered.
//
// Supplying them is what a real deployment does, and it changes nothing the gate is testing: the
// guard is about the SHAPE of the reference, and these values are never resolved by a render.
// `tests/unit/test_the_production_render_gate_can_pass.py` reads the guard for every value it
// compares against a published literal and fails if this list stops covering one.
const renderArgs = "--set image.repository=ghcr.io/example/rask " +
	"--set-string frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum " +
	"--set-string frontend.oidc.publicIssuer=http://localhost:8080/dex " +
	"--set-string frontend.oidc.publicOrigin=http://localhost:8080 " +
	"--set openbao.devMode=false " +
	"--set-string dapr.appToken=render-gate-app-token-not-a-real-credential " +
	"--set-string age.password=render-gate-age-password-not-a-real-credential " +
	"--set-string minio.secretKey=render-gate-store-key-not-a-real-credential " +
	"--set-string dex.clientSecret=render-gate-client-secret-not-a-real-credential"

// networkPolicyGate: the NetworkPolicy layer is off by default and, when flipped on, renders the full
// isolation set (default-deny, DNS allow, the exclusive openbao lock). Copied verbatim from ci.yml.
const networkPolicyGate = `set -euo pipefail
RENDER_ARGS="` + renderArgs + `"
# Render CONFIGURATION, not prose. Every assertion in these gates is about what the chart produces for
# the API server, and a YAML comment is neither. Two assertions were failing on comments alone when this
# gate was first run after its 923-commit outage: '-sa-jobs' inside a note explaining why the sweep SA
# is exempt, and MEDALLION_DLQ_TOPIC inside a note about a historical subject name. Stripping comment
# lines at the source makes every check below mean what it says — and prevents the mirrored bug, where a
# "must be present" grep is satisfied by documentation rather than by a rendered field.
render() { helm template chart $RENDER_ARGS "$@" | grep -v '^[[:space:]]*#'; }
# THE ABSENCE ASSERTIONS RENDER TO A FILE FIRST, and that is the whole point of the extra line.
# Written as 'off=$(render ... | grep -c "kind: X" || true)', the '|| true' sits OUTSIDE the pipeline, so
# 'set -euo pipefail' cannot propagate a render failure into the substitution: a render that produces
# NOTHING makes grep -c print 0 and exit 1, || true swallows the exit, and [ "0" = "0" ] passes. The gate
# then reports "the toggle correctly rendered no such object" about a chart that rendered no objects at
# all. Redirecting to a file puts the render on its own command, where set -e sees its status, and the
# 'kind: Deployment' probe is the non-vacuity floor: it proves the render produced a real manifest before
# anything concludes something is absent from it.
render --set networkPolicy.enabled=false > /tmp/np-off.yaml
grep -q "kind: Deployment" /tmp/np-off.yaml
off=$(grep -c "kind: NetworkPolicy" /tmp/np-off.yaml || true); [ "$off" = "0" ] || exit 1
render --set networkPolicy.enabled=true > /tmp/np.yaml
count=$(grep -c "kind: NetworkPolicy" /tmp/np.yaml); [ "$count" -ge 9 ] || exit 1
grep -q "default-deny" /tmp/np.yaml
grep -q "allow-dns" /tmp/np.yaml
grep -q "k8s-app: kube-dns" /tmp/np.yaml
grep -q -- "-openbao" /tmp/np.yaml
grep -q "NotIn" /tmp/np.yaml`

// securityGate: service accounts + infra security contexts are off by default and, when flipped on, render
// the per-workload SAs (automount off, wired in) and the non-root runAsUser tiers. Verbatim from ci.yml.
const securityGate = `set -euo pipefail
RENDER_ARGS="` + renderArgs + `"
# Render CONFIGURATION, not prose. Every assertion in these gates is about what the chart produces for
# the API server, and a YAML comment is neither. Two assertions were failing on comments alone when this
# gate was first run after its 923-commit outage: '-sa-jobs' inside a note explaining why the sweep SA
# is exempt, and MEDALLION_DLQ_TOPIC inside a note about a historical subject name. Stripping comment
# lines at the source makes every check below mean what it says — and prevents the mirrored bug, where a
# "must be present" grep is satisfied by documentation rather than by a rendered field.
render() { helm template chart $RENDER_ARGS "$@" | grep -v '^[[:space:]]*#'; }
# OFF BY DEFAULT, with ONE documented exception. This asserted a flat zero until 2026-08-22, and it was
# right when written; the chart has since grown ` + "`-sa-dapr-sweep`" + `, which renders unconditionally and says
# why at its own definition ("This SA genuinely needs the k8s API, so it does NOT take the zero-grant
# -sa-jobs identity"). Nothing caught the divergence because this gate has not RUN since 2026-08-04 —
# see Charts() above. Excluding it by name rather than loosening to a count keeps the assertion exact:
# a SECOND unconditional service account still fails here, which is the property worth having.
# THE ABSENCE ASSERTIONS RENDER TO A FILE FIRST, and that is the whole point of the extra line.
# Written as 'off=$(render ... | grep -c "kind: X" || true)', the '|| true' sits OUTSIDE the pipeline, so
# 'set -euo pipefail' cannot propagate a render failure into the substitution: a render that produces
# NOTHING makes grep -c print 0 and exit 1, || true swallows the exit, and [ "0" = "0" ] passes. The gate
# then reports "the toggle correctly rendered no such object" about a chart that rendered no objects at
# all. Redirecting to a file puts the render on its own command, where set -e sees its status, and the
# 'kind: Deployment' probe is the non-vacuity floor: it proves the render produced a real manifest before
# anything concludes something is absent from it.
render > /tmp/sa-off.yaml
grep -q "kind: Deployment" /tmp/sa-off.yaml
off=$(grep -- "-sa-" /tmp/sa-off.yaml | grep -vc -- "-sa-dapr-sweep" || true); [ "$off" = "0" ] || exit 1
render --set security.serviceAccounts.enabled=true --set security.infraContexts.enabled=true > /tmp/sec.yaml
sa_objects=$(grep -c "kind: ServiceAccount" /tmp/sec.yaml || true); [ "$sa_objects" -ge 16 ] || exit 1
automount_off=$(grep -c "automountServiceAccountToken: false" /tmp/sec.yaml || true); [ "$automount_off" -ge 16 ] || exit 1
wired=$(grep -c "serviceAccountName: .*-sa-" /tmp/sec.yaml || true); [ "$wired" -ge 12 ] || exit 1
grep -q "runAsUser: 999" /tmp/sec.yaml
grep -q "runAsUser: 65532" /tmp/sec.yaml`

// resiliencyGate: the Dapr resiliency + DLQ layer is on by default (Resiliency CR, DLQ topics, the long
// 720s,720s retry) and, when flipped off, falls back to the legacy inline retry with no DLQ. Verbatim.
const resiliencyGate = `set -euo pipefail
RENDER_ARGS="` + renderArgs + `"
# Render CONFIGURATION, not prose. Every assertion in these gates is about what the chart produces for
# the API server, and a YAML comment is neither. Two assertions were failing on comments alone when this
# gate was first run after its 923-commit outage: '-sa-jobs' inside a note explaining why the sweep SA
# is exempt, and MEDALLION_DLQ_TOPIC inside a note about a historical subject name. Stripping comment
# lines at the source makes every check below mean what it says — and prevents the mirrored bug, where a
# "must be present" grep is satisfied by documentation rather than by a rendered field.
render() { helm template chart $RENDER_ARGS "$@" | grep -v '^[[:space:]]*#'; }
render > /tmp/resil.yaml
grep -q "kind: Resiliency" /tmp/resil.yaml
grep -q "pubsubDeliveryRetry" /tmp/resil.yaml
# The schedule must MATCH the policy it declares, the numbers must produce the window the comments
# claim, AND — new in the M4 correction (2026-08-03) — every field must be one the CRD ACCEPTS.
# Two prior shapes failed here, in opposite directions. Presence-only checking (the two greps above,
# the whole gate until 2026-07-28) passed "policy: exponential" with "duration: 30s", a
# PolicyConstant-ONLY field (dapr/kit retry.go): Dapr ignored the duration, initialInterval stayed at
# its 500ms default, and the real window was ~4s where both comments claimed minutes — resiliency ON
# was ~125x SHORTER than OFF. The 2026-07-28 fix then asserted initialInterval/multiplier/
# randomizationFactor — correct for dapr/kit, but those three fields DO NOT EXIST in
# resiliencies.dapr.io (absent from the vendored dapr-1.18.1 chart CRD, from the live cluster CRD,
# and from the Go type Retry), so the API server rejected the CR by strict decoding and it never
# applied ONCE while this gate stayed green. A render gate cannot see an apply failure — hence the
# explicit CRD-vocabulary assertion below, which is the part that would have caught it.
awk '/^ +pubsubDeliveryRetry:/{f=1;next} f&&/^ +[a-zA-Z]+: /{print} f&&/^ +[a-z]+:$/{exit}' /tmp/resil.yaml > /tmp/policy.txt
# THE WINDOW IS THE INVARIANT; THE POLICY SHAPE IS NOT. This block asserted
# "policy: exponential" / "duration: 30s" / a present "maxInterval", which the chart has not rendered
# since 2026-09-14: LH-106 moved it to constant/120s/4 because the exponential shape delivered 3.5
# seconds where both comments claimed 7.5 minutes, and the template states why maxInterval is now
# omitted — "it is read only under exponential, so carrying it here would be a value that looks like a
# bound and bounds nothing". The chart was right and this gate was stale, which it could not say
# because it has not RUN since 2026-08-04 (see Charts()). Asserting the WINDOW instead is what the
# comment above already promised, so the next equivalent re-shaping is measured rather than refused.
grep -qE "policy: (constant|exponential)" /tmp/policy.txt
grep -qE "duration: [0-9]+s" /tmp/policy.txt
grep -qE "maxRetries: [0-9]+" /tmp/policy.txt
# CRD vocabulary: a Resiliency retry accepts only policy/duration/maxInterval/maxRetries/matching and
# the status-code matchers. Anything else is dropped by strict decoding and the CR never lands.
! grep -qE "initialInterval:|multiplier:|randomizationFactor:" /tmp/policy.txt
# maxInterval is EXPONENTIAL-ONLY, so it is required exactly there and refused under constant, where it
# would read as a bound while bounding nothing. Conditional rather than unconditional in either
# direction, because both shapes are legitimate and only one of them uses the field.
if grep -q "policy: exponential" /tmp/policy.txt; then
  grep -q "maxInterval:" /tmp/policy.txt || { echo "FAIL: exponential retry with no maxInterval — a step can run away"; exit 1; }
else
  ! grep -q "maxInterval:" /tmp/policy.txt || { echo "FAIL: constant retry carries maxInterval, which Dapr reads only under exponential"; exit 1; }
fi
d=$(sed -n 's/.*duration: \([0-9]*\)s.*/\1/p' /tmp/policy.txt)
n=$(sed -n 's/.*maxRetries: \([0-9]*\).*/\1/p' /tmp/policy.txt)
cap=$(sed -n 's/.*maxInterval: \([0-9]*\)s.*/\1/p' /tmp/policy.txt)
# Sum the real backoff ladder: step doubles from ` + "`duration`" + `, capped at ` + "`maxInterval`" + `, ` + "`maxRetries`" + ` steps.
# The old ` + "`w=$((d*n))`" + ` was the CONSTANT-policy sum and understates exponential by 3.75x here (120s vs
# 450s), so leaving it in place would have failed the window check for a policy that in fact preserves
# the documented window exactly: 30 + 60 + 120 + 240 = 450s.
# Sum the ladder the RENDERED policy actually produces. A single exponential formula computed the wrong
# window for a constant policy — and with maxInterval legitimately absent, the step-vs-cap comparison
# compares against an empty string, so the arithmetic was not merely wrong but unevaluable.
if grep -q "policy: exponential" /tmp/policy.txt; then
  w=0; step=$d
  for _ in $(seq 1 "$n"); do
    w=$((w + step)); step=$((step * 2)); [ "$step" -gt "$cap" ] && step=$cap
  done
else
  w=$((d * n))
fi
[ "$w" -ge 400 ] && [ "$w" -le 500 ] || { echo "FAIL: rendered retry window ${w}s is not the ~450s (7.5 min) the comments claim"; exit 1; }
[ "$w" -lt 720 ] || { echo "FAIL: retry window ${w}s meets/exceeds the broker's 720s first backOff step — the broker would redeliver mid-retry"; exit 1; }
echo "resiliency window ${w}s (${d}s x${n}) — matches the documented 7.5 min, under the 720s broker step"
dlq=$(grep -c "DLQ_TOPIC" /tmp/resil.yaml || true); [ "$dlq" -ge 3 ] || exit 1
grep -q '"dlq.>"' /tmp/resil.yaml
grep -q "720s,720s" /tmp/resil.yaml
! grep -q "30s,60s,120s,300s" /tmp/resil.yaml
render --set dapr.resiliency.enabled=false > /tmp/legacy.yaml
off=$(grep -c "kind: Resiliency" /tmp/legacy.yaml || true); [ "$off" = "0" ] || exit 1
# THE TOGGLE OWNS THE POLICY LANES, NOT EVERY DLQ IN THE ESTATE. This asserted zero DLQ_TOPIC of any
# kind with resiliency off, which was true when the medallion lane was the only one that had a
# dead-letter topic. It is not true now: the maintenance WORK and INDEX lanes are BYO-worker lanes with
# their own Dapr components, their own ackWait and their own DLQ, none of which the medallion retry
# policy governs -- so they correctly survive the toggle. Measured 2026-09-22: ON renders LINEAGE(1),
# MEDALLION(4), NOTIFICATIONS(1), MAINTENANCE_WORK(2), MAINTENANCE_INDEX(2); OFF renders only the two
# maintenance pairs. Asserting the policy-governed three by NAME says what the toggle actually means,
# and a new lane that wires its own DLQ no longer reds a gate about somebody else's retry policy.
for governed in LINEAGE_DLQ_TOPIC MEDALLION_DLQ_TOPIC RASK_NOTIFICATIONS_DLQ_TOPIC; do
  ! grep -q "$governed" /tmp/legacy.yaml || { echo "FAIL: $governed survives dapr.resiliency.enabled=false, but the retry policy that gives it meaning does not"; exit 1; }
done
grep -q '30s,60s,120s,300s' /tmp/legacy.yaml`
