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
		// (search_api, core, storage, …), so install every member like `make dev-micro` does.
		WithExec([]string{"uv", "sync", "--all-packages"}).
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "-q", "-m", "not e2e and not slow"}).
		Stdout(ctx)
}

// TestPg runs the viewer pytest suite with an ephemeral postgres attached.
//
// Workflow:
//  1. Start postgres:16 service.
//  2. Apply `alembic upgrade head` against it (proves migrations are
//     dialect-clean against postgres on every run).
//  3. Run `pytest services/core/tests/` with DATABASE_URL set.
//
// The current viewer test fixture builds its own ephemeral sqlite via
// SQLModel.metadata.create_all and ignores DATABASE_URL, so pytest itself
// still runs against sqlite for speed — the value of this Dagger function is
// the migration-against-pg signal that runs in the same CI step.
func (m *Rask) TestPg(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.pythonBase(src).
		WithServiceBinding("postgres", m.Postgres()).
		WithEnvVariable("DATABASE_URL", PgDsn).
		WithExec([]string{"uv", "sync", "--package", "core", "--extra", "postgres", "--extra", "migrations"}).
		// 1. Migrate.
		WithWorkdir("/src/services/core").
		WithExec([]string{"uv", "run", "--package", "core", "alembic", "upgrade", "head"}).
		// 2. pytest.
		WithWorkdir("/src").
		WithExec([]string{"uv", "run", "pytest", "services/core/tests/", "-q", "--no-cov"}).
		Stdout(ctx)
}
