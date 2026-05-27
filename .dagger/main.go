// Dagger module for the rask monorepo.
//
// Functions live in sibling .go files by concern:
//   - postgres.go : ephemeral postgres:16 service (matches `make pg-up`)
//   - migrate.go  : `alembic upgrade head` against an ephemeral pg (CI proof)
//   - test.go     : viewer pytest with the migrated pg attached
//
// All callable via `dagger call <kebab-case-name>`.
package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

type Rask struct{}

// UvPythonImage carries Python 3.13 + uv preinstalled. Same Python version as
// the workspace `.python-version`; uv path is `/usr/local/bin/uv`.
const UvPythonImage = "ghcr.io/astral-sh/uv:python3.13-bookworm-slim"

// Echo returns whatever string you pass it (smoke-test for the module).
func (m *Rask) Echo(ctx context.Context, msg string) (string, error) {
	return dag.Container().
		From("alpine:latest").
		WithExec([]string{"echo", msg}).
		Stdout(ctx)
}

// pythonBase mounts the rask workspace at /src in a uv-equipped container and
// caches the uv resolver cache across runs. Unexported = shared helper, not a
// Dagger Function.
func (m *Rask) pythonBase(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(UvPythonImage).
		WithMountedCache("/root/.cache/uv", dag.CacheVolume("rask-uv-cache")).
		WithMountedDirectory("/src", src).
		WithWorkdir("/src")
}
