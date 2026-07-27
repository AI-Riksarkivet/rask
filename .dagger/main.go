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
// the workspace `.python-version`; uv path is `/usr/local/bin/uv`. Trixie, NOT
// bookworm: `lance-graph` ships a single manylinux_2_39 wheel (glibc >= 2.39),
// so on bookworm (glibc 2.36) uv falls back to its sdist and dies in a
// Rust-less maturin build. Matches the trixie-pinned .docker/* images and the
// chartsBaseImage comment in charts.go.
const UvPythonImage = "ghcr.io/astral-sh/uv:python3.13-trixie-slim"

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

// base is the synced python-gate base the merged lance-ns functions (Lint,
// Typecheck, Openapi, TestLineage) build on: the uv container + the repo
// source with the artefact excludes (chartsBase extends this exclude set with
// `.localbin`) + a full `uv sync` — which is why those gates run
// `uv run --no-sync`. Unexported = shared helper, not a Dagger Function.
func (m *Rask) base(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(UvPythonImage).
		WithMountedCache("/root/.cache/uv", dag.CacheVolume("rask-uv-cache")).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			Exclude: []string{".venv", ".git", "node_modules", ".dagger", "frontend/node_modules"},
		}).
		WithWorkdir("/src").
		WithExec([]string{"uv", "sync"})
}
