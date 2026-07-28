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
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "-q", "-m", "not e2e and not slow"}).
		Stdout(ctx)
}
