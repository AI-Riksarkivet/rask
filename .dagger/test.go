package main

import (
	"context"
	"strings"

	"dagger/rask/internal/dagger"
)

// natsImage matches the version the chart deploys (chart/Chart.yaml pins the nats subchart at 2.14.2,
// whose appVersion is the same), so the broker CI tests against cannot drift from the broker the estate
// runs. The AGE binding in e2e.go pins its image for the same reason.
const natsImage = "nats:2.14.2-alpine"

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
			Exclude: []string{"**/.env", "**/.env.*", ".venv", ".git", "node_modules", ".dagger", "frontend/node_modules"},
		}).
		WithWorkdir("/src").
		WithExec(sync).
		// `path` is split on whitespace so several files can go in one call — pytest takes many
		// targets, and passing the whole string as ONE argument makes it a path that does not exist,
		// which pytest reports as "no tests ran" rather than as an error. That reads like a green run.
		// pytest-timeout is injected for the same reason pytest is: a `--package` sync omits the root
		// dev group. See Test below for why every lane in here carries a timeout.
		WithExec(append([]string{"uv", "run", "--no-sync", "--with", "pytest", "--with", "pytest-cov", "--with", "pytest-timeout", "pytest", "-q", "--no-cov", "--timeout=300", "--timeout-method=thread", "-m", "not e2e and not slow"}, strings.Fields(path)...)).
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
		// ── A BROKER, because seven ingest tests skip themselves without one ───────────────────────
		// services/ingest/tests/test_worker_queue.py carries a MODULE-level
		// `pytestmark = pytest.mark.skipif(not _reachable(), ...)` over a live TCP probe of
		// RASK_NATS_URL, and test_run_chain.py carries the same. No lane bound a broker, so the only
		// tests of "the stream IS the outstanding-work ledger", the exactly-once commit chain and the
		// DLQ poison-park vanished from every CI run while the job printed green — taking
		// ingest/worker.py from 82% to 35% coverage on a run that reported success.
		//
		// That DLQ test is the regression guard for a fix this repo already made: `park_poison` is
		// awaited BEFORE `msg.ack()` and unwrapped, so a missing DLQ stream means the unit is never
		// acked and the drain dies — the one mechanism meant to stop a poison unit stalling a run
		// becomes the stall. The fix is real in the source and was fiction in the gate.
		//
		// `-js` is not optional: the suite provisions and reads JetStream streams, and a core-only
		// server answers those calls with an error rather than a skip. Dagger, never docker — this is
		// exactly the "ephemeral broker for a test repro" case CLAUDE.md names.
		//
		// `WithDefaultArgs`, NEVER `WithExec`, and this cost two and a half hours to learn. A
		// `WithExec` is a BUILD STEP: Dagger runs it and waits for it to exit. `nats-server` is a
		// server, so it never exits — the whole pipeline sat at "1 steps running" with ZERO output for
		// 2h20m and had to be killed. `AsService()` runs the container's entrypoint plus its default
		// args, which is the form a long-running process needs.
		//
		// Measured three ways against services/ingest/tests/test_worker_queue.py:
		//
		//	no binding at all (the pre-fix state):  exit 0, 6 SKIPPED   <- the defect
		//	WithExec:                               exit 124, HUNG      <- the fix, broken
		//	WithDefaultArgs:                        exit 0, 6 PASSED    <- this
		//
		// The middle row is the worse failure of the three: a suite that skips still reports the rest
		// of the estate, while a hung lane reports nothing at all and looks like a slow machine.
		WithServiceBinding("nats", dag.Container().
			From(natsImage).
			WithExposedPort(4222).
			WithDefaultArgs([]string{"nats-server", "--jetstream", "--addr", "0.0.0.0", "--port", "4222"}).
			AsService()).
		WithEnvVariable("RASK_NATS_URL", "nats://nats:4222").
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "-q", "--timeout=300", "--timeout-method=thread", "-m", "not e2e and not slow"}).
		Stdout(ctx)
}
