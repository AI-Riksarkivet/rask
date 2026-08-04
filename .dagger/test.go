package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// Test runs the offline pytest suite: every root-workspace testpath (packages, services,
// tests/unit, tests/integration — and tests/e2e-py stays collectable so the collection
// gate can see it), excluding the live-stack `e2e` marker and the model-bound `slow`
// marker. This is the pytest leg of the old `dagger call ci` composition (lint →
// typecheck → openapi → test); the sealed runners/htr project runs its own suite outside
// the workspace (`make test`'s second leg) and is not covered here.
func (m *Rask) Test(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.base(src).
		// base() only syncs the root project; the suites IMPORT the workspace members
		// (compute, gateway, storage, …), so install every member like `make dev-micro` does.
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
		// helm ALONE would turn thirteen skips into thirteen FAILURES: chart/charts/ is gitignored, so
		// a fresh checkout declares ten dependencies and vendors none, and every render dies with
		// "found in Chart.yaml, but missing in charts/ directory". Charts() already documents that trap
		// and solves it the same way — `make k3s-deps`, not a bare `helm dependency build`, so the
		// repository list (K3S_DEP_REPOS) stays in ONE place instead of drifting in a second copy.
		WithExec([]string{"apt-get", "update"}).
		WithExec([]string{"apt-get", "install", "-y", "--no-install-recommends", "make", "ca-certificates"}).
		WithFile("/usr/local/bin/helm", helmBinary()).
		// Cache helm's repo index + vendored archives: the source mount above changes on every edit, so
		// `make k3s-deps` re-runs constantly, and without these it re-fetches ten subcharts each time.
		WithMountedCache("/root/.cache/helm", dag.CacheVolume("rask-helm-cache")).
		WithMountedCache("/root/.local/share/helm", dag.CacheVolume("rask-helm-data")).
		WithExec([]string{"make", "k3s-deps"}).
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "-q", "-m", "not e2e and not slow"}).
		Stdout(ctx)
}
