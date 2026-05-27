package main

import (
	"dagger/rask/internal/dagger"
)

// Postgres credentials mirror `make pg-up` so local and CI commands behave the
// same. Ephemeral container — never use these values in real environments.
const (
	PgImage = "postgres:16"
	PgUser  = "rask"
	PgPass  = "rask"
	PgDB    = "rask"
	// PgDsn talks to the bound service via the "postgres" hostname; Dagger's
	// WithServiceBinding rewrites it to the tunnel address.
	PgDsn = "postgresql+asyncpg://rask:rask@postgres:5432/rask"
)

// Postgres returns an ephemeral postgres:16 service.
//
// Consumers attach it with `WithServiceBinding("postgres", m.Postgres())`;
// the binding waits for port 5432 to accept TCP before the dependent
// container's `WithExec` runs.
func (m *Rask) Postgres() *dagger.Service {
	return dag.Container().
		From(PgImage).
		WithEnvVariable("POSTGRES_USER", PgUser).
		WithEnvVariable("POSTGRES_PASSWORD", PgPass).
		WithEnvVariable("POSTGRES_DB", PgDB).
		WithExposedPort(5432).
		AsService()
}
