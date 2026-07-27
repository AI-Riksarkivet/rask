package main

import (
	"context"

	"dagger/lance-ns/internal/dagger"
)

// ageImage pins PostgreSQL 16 + Apache AGE — the 'latest' tag tracks PG18, on which AGE crashes the
// backend on create_graph (verified). Same pin as .docker/docker-compose.lineage.yml and the chart.
const ageImage = "apache/age:release_PG16_1.5.0"

// TestLineage runs the AGE-backed lineage e2e: the repository's REAL openCypher against a real Apache
// AGE Postgres service container — hermetic and identical locally and in CI (`dagger call test-lineage`
// == `make e2e-lineage` == the CI lineage-e2e job). The unit fakes can't validate AGE dialect behavior
// — the int-vs-string $ver at-version match, the RETURN-less DETACH DELETE prune, the functional-index
// DDL — so this gate runs them against the real engine. Port-readiness is safe: the postgres entrypoint's
// temporary initdb server listens on the unix socket only, so the first TCP accept IS the final server.
func (m *LanceNs) TestLineage(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	age := dag.Container().
		From(ageImage).
		WithEnvVariable("POSTGRES_USER", "lineage").
		WithEnvVariable("POSTGRES_PASSWORD", "lineage").
		WithEnvVariable("POSTGRES_DB", "lineage").
		// The same init script the compose overlay uses: LOAD age + create_graph('lineage').
		WithFile("/docker-entrypoint-initdb.d/10-age.sql", src.File(".docker/lineage-init.sql")).
		WithExposedPort(5432).
		AsService()

	return m.base(src).
		WithServiceBinding("age", age).
		WithEnvVariable("LINEAGE_DATABASE_URL", "postgresql://lineage:lineage@age:5432/lineage").
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "tests/e2e/test_lineage_e2e.py", "-q"}).
		Stdout(ctx)
}
