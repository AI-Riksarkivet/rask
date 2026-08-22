// Dagger module for the rask monorepo.
//
// Functions live in sibling .go files by concern:
//   - test.go     : the offline pytest suite (the app-DB pg legs died at P7a —
//     the batches table and its Alembic lineage are gone)
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
// Rust-less maturin build. Matches the chartsBaseImage comment in charts.go.
//
// This used to also claim it "matches the trixie-pinned .docker/* images". It does not, and did not:
// of the 12 base-image references under .docker/, EIGHT are python:3.13-slim-bookworm and only four
// are trixie. `dagger call scan-image --name=gateway` is what caught it — the report is headed
// `/img.tar (debian 12.14)`, which is bookworm. The gate container being trixie is correct and
// load-bearing for the reason above; the deployables simply do not follow it.
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
// `.localbin`) + `uv sync --all-packages` — which is why those gates run
// `uv run --no-sync`. Unexported = shared helper, not a Dagger Function.
//
// --all-packages is LOAD-BEARING, not a flourish. A bare `uv sync` in a uv
// workspace installs the ROOT project only and silently omits every member, so
// `lineage_kit`, `service_kit` and the rest are absent from the environment ty
// then checks. This comment used to claim a bare sync was "a full uv sync"; it
// is not, and believing it kept `main` red from 2026-07-23 to 2026-07-29 with
// `error[unresolved-import]: Cannot resolve imported module 'lineage_kit'` on
// every run. `.dagger/test.go` always had the flag — only this base lacked it,
// which is why the pytest lane passed while the type gate could not.
func (m *Rask) base(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(UvPythonImage).
		WithMountedCache("/root/.cache/uv", dag.CacheVolume("rask-uv-cache")).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			Exclude: []string{".venv", ".git", "node_modules", ".dagger", "frontend/node_modules"},
		}).
		WithWorkdir("/src").
		WithExec([]string{"uv", "sync", "--all-packages"})
}
