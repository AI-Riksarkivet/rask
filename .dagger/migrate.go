package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// MigrateUp applies all alembic migrations against an ephemeral postgres:16.
//
// CI signal that the migration set is dialect-clean and applies from zero
// against postgres. Equivalent local workflow:
//
//	make pg-up && make pg-migrate
//
// Returns the alembic upgrade head stdout.
func (m *Rask) MigrateUp(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.pythonBase(src).
		WithServiceBinding("postgres", m.Postgres()).
		WithEnvVariable("DATABASE_URL", PgDsn).
		WithExec([]string{"uv", "sync", "--package", "core", "--extra", "postgres", "--extra", "migrations"}).
		WithWorkdir("/src/components/services/core").
		WithExec([]string{"uv", "run", "--package", "core", "alembic", "upgrade", "head"}).
		Stdout(ctx)
}
