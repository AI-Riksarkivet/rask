package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// natsImage matches the server the NATS fixture deploys (deploy/fixtures/nats pins the nats chart at 2.15.2,
// whose appVersion is the same), so the broker CI tests against cannot drift from the broker the estate
// runs.
const natsImage = "nats:2.15.2-alpine"

// withRenderableChart gives a python-gate container what `tests/unit/chart_render.py` needs to render the
// chart: the pinned helm and the vendored subcharts. Shared by the pytest lane (Test) and the NATS auth lane
// (NatsAuth), so the two cannot render the same chart under different Helm minors or subchart sets.
//
// The subcharts are not optional: chart/charts/ is gitignored, so a fresh checkout declares ten dependencies
// and vendors none, and every render dies with "found in Chart.yaml, but missing in charts/ directory".
// Charts() documents that trap and solves it the same way — `make k3s-deps`, not a bare
// `helm dependency build`, so the repository list (K3S_DEP_REPOS) stays in ONE place.
func withRenderableChart(c *dagger.Container) *dagger.Container {
	return c.
		WithExec([]string{"apt-get", "update"}).
		WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "make", "ca-certificates"}).
		WithFile("/usr/local/bin/helm", helmBinary()).
		// Cache helm's repo index + vendored archives: the source mount changes on every edit, so
		// `make k3s-deps` re-runs constantly, and without these it re-fetches ten subcharts each time.
		WithMountedCache("/root/.cache/helm", dag.CacheVolume("rask-helm-cache")).
		WithMountedCache("/root/.local/share/helm", dag.CacheVolume("rask-helm-data")).
		WithExec([]string{"make", "k3s-deps"})
}

// Test runs the offline pytest suite: every root-workspace testpath (packages, services,
// tests/unit, tests/integration — and tests/e2e-py stays collectable so the collection
// gate can see it), excluding the live-stack `e2e` marker and the model-bound `slow`
// marker. This is the pytest leg of the old `dagger call ci` composition (lint →
// typecheck → openapi → test).
func (m *Rask) Test(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.base(src).
		// base() only syncs the root project; the suites IMPORT the workspace members
		// (the kits, the services, …), so install every member like `make install` does.
		WithExec([]string{"uv", "sync", "--all-packages"}).
		// ── helm, so the chart-render invariants actually RUN (audit m5, fixed 2026-08-03) ──────────
		// THIRTEEN tests in tests/unit/test_invariants.py render the chart through _helm_template()
		// (:242) or _helm_notes() (:678), and both open with `pytest.skip("helm not available")`.
		// base() is a uv image with no helm, so all thirteen skipped on every CI run — and a skipped
		// test reads as a green suite, which is why nothing ever reported it. They pass on a developer
		// box only because helm happens to be on PATH there.
		//
		// Deliberately HERE and not in base(): base() is shared by openapi.go, checks.go (lint + ty)
		// and e2e.go, none of which touch the chart — putting an apt layer and a helm download in
		// front of the lint gate buys nothing and slows four lanes to fix one.
		//
		// helm ALONE would turn thirteen skips into thirteen FAILURES: see withRenderableChart.
		With(withRenderableChart).
		// ── the OpenFGA server, so the authorization tests assert outcomes against the real evaluator ──
		// root `conftest.py::openfga` spawns it in memory per worker and fails without it ([[LH-235]]); it is
		// the static binary of the image the governed lanes run, so every lane evaluates the model with one
		// server. `make openfga` exports the same file to `.localbin` for a developer box.
		WithFile("/usr/local/bin/openfga", dag.Container().From(openfgaImage).File("/openfga")).
		// ── a per-test ceiling, because a hang in HERE is undiagnosable without one ─────────────────
		// This suite has hung in Dagger while passing on a developer box, and the container is what
		// makes it undebuggable: Dagger buffers a WithExec's stdout until the exec COMPLETES, so a run
		// that never finishes prints nothing at all — no progress line, no test name, and SIGINT adds
		// nothing. The whole-suite bisect that followed cost more than the bug.
		//
		// `--timeout-method=thread` (not `signal`) because the observed profile was threads parked in
		// futex_wait with the main thread in nanosleep: the thread method dumps EVERY thread's stack
		// and fails the run, so the next occurrence names itself. 300s is far above the suite's whole
		// measured runtime (~2 min on a developer box) — it is a hang detector, not a speed budget.
		//
		// Not in `addopts`: that would put the same ceiling on `make test-slow`, whose tests load real
		// models over the network and whose honest runtime is not established here.
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "-q", "--timeout=300", "--timeout-method=thread", "-m", "not e2e and not slow"}).
		Stdout(ctx)
}
