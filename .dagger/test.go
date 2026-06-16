package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// TestPg runs the viewer pytest suite with an ephemeral postgres attached.
//
// Workflow:
//  1. Start postgres:16 service.
//  2. Apply `alembic upgrade head` against it (proves migrations are
//     dialect-clean against postgres on every run).
//  3. Run `pytest components/services/core/tests/` with DATABASE_URL set.
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
		WithWorkdir("/src/components/services/core").
		WithExec([]string{"uv", "run", "--package", "core", "alembic", "upgrade", "head"}).
		// 2. pytest.
		WithWorkdir("/src").
		WithExec([]string{"uv", "run", "pytest", "components/services/core/tests/", "-q", "--no-cov"}).
		Stdout(ctx)
}
