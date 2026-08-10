package main

import (
	"context"
	"strings"

	"dagger/rask/internal/dagger"
)

// TestPackage runs ONE workspace member's suite against a SCOPED sync — the fast lane.
//
//	dagger call test-package --pkg=service-kit --extra=governed
//
// Test (below) is the whole-estate gate and pays for it: `uv sync --all-packages` (200 packages,
// including pylance/lancedb), an apt layer, a helm download and `make k3s-deps`'s ten subchart
// fetches — measured at ~54 minutes cold. That is the right price for the full suite and the wrong
// one for a change to a single package.
//
// The authz plane is the case that forced this. `service_kit.governed.fga` needs only the `governed`
// extra (openfga-sdk, pyjwt, aiohttp, tenacity, httpx, lance-namespace); it does not need lancedb,
// pyarrow, helm or the chart. Locally that distinction is not a nicety — lancedb ships no
// macOS/x86_64 wheel, so on an Intel Mac the full sync cannot complete at all and the scoped one
// finishes in seconds.
//
// pytest is injected with `--with` rather than declared: it belongs to the ROOT dev group, which a
// `--package` sync deliberately does not install. `--no-cov` because the root addopts turn coverage
// on for the whole workspace, which is meaningless for one member's paths.
//
// THE BOUNDARY, measured rather than guessed: a single file or one package's `tests/` dir runs
// cleanly (verified across four suites). A whole cross-service directory does not — `tests/unit`
// collected 96 ModuleNotFoundErrors on `--pkg=catalog`, and naming all seven services it spans takes
// that to 3, the last being root DEV-GROUP deps (`respx`, `lance_ray`) that a `--package` sync omits
// by design. Injecting those here would be re-implementing the dev group one `--with` at a time, and
// the thing it would reimplement already exists: `dagger call test`. This is the inner loop; that is
// the gate. Reach for it when the answer must cover everything.
func (m *Rask) TestPackage(
	ctx context.Context,
	// The workspace member, e.g. "service-kit". Comma-separate several when the path spans them:
	// `tests/unit` is a CROSS-SERVICE directory, so `--pkg=catalog` alone collects it with 96
	// ModuleNotFoundErrors for the siblings it also imports (annotator, medallion, lineage, …).
	// One package is the common case and stays the fast one; the list is there so a shared path is
	// runnable at all rather than silently under-collected.
	pkg string,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// An optional extra to install with it, e.g. "governed".
	// +optional
	extra string,
	// Test path to run; defaults to `packages/<pkg>/tests`.
	// +optional
	path string,
) (string, error) {
	pkgs := strings.Split(pkg, ",")
	sync := []string{"uv", "sync"}
	for _, p := range pkgs {
		if p = strings.TrimSpace(p); p != "" {
			sync = append(sync, "--package", p)
		}
	}
	if extra != "" {
		sync = append(sync, "--extra", extra)
	}
	if path == "" {
		// Default to the FIRST package's own suite — the single-package case, where the default is
		// unambiguous. A multi-package call is by definition about a shared path, so it names one.
		path = "packages/" + strings.TrimSpace(pkgs[0]) + "/tests"
	}
	return dag.Container().
		From(UvPythonImage).
		WithMountedCache("/root/.cache/uv", dag.CacheVolume("rask-uv-cache")).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			Exclude: []string{".venv", ".git", "node_modules", ".dagger", "frontend/node_modules"},
		}).
		WithWorkdir("/src").
		WithExec(sync).
		// `path` is split on whitespace so several files can go in one call — pytest takes many
		// targets, and passing the whole string as ONE argument makes it a path that does not exist,
		// which pytest reports as "no tests ran" rather than as an error. That reads like a green run.
		WithExec(append([]string{"uv", "run", "--no-sync", "--with", "pytest", "--with", "pytest-cov", "pytest", "-q", "--no-cov", "-m", "not e2e and not slow"}, strings.Fields(path)...)).
		Stdout(ctx)
}

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
