package main

import (
	"context"

	"dagger/lance-ns/internal/dagger"
)

// bunImage is the frontend toolchain's base image — the exact digest-pinned tag from
// .docker/frontend.dockerfile (oven/bun:1.3.14-slim), so the Dagger run matches the shipped zone images
// rather than trusting the CI setup-bun action's unpinned 'latest'. Bun is the whole toolchain here: turbo,
// oxlint and rsvelte-fmt both arrive as frontend/ root devDependencies via
// `bun install --frozen-lockfile` — no globally installed tool, no second package manager.
const bunImage = "oven/bun:1.3.14-slim@sha256:d56a2534ffd262e92c12fd3249d3924d296d97086da773f821d7d0477435ea04"

// frontendBase returns the installed turborepo workspace container: the bun image + source +
// `bun install --frozen-lockfile` (the packages/* + microfrontends/* workspace from frontend/bun.lock).
// Unexported → a shared private helper, not a Dagger Function. The bun install cache mirrors the
// dockerfile's `--mount=type=cache,target=/root/.bun/install/cache`.
func (m *LanceNs) frontendBase(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(bunImage).
		WithMountedCache("/root/.bun/install/cache", dag.CacheVolume("lance-ns-bun")).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			// Exclude checked-in build/install artefacts so the container installs clean from the lockfile.
			// The zone build outputs (.svelte-kit/build) are stripped generically via the globs below.
			Exclude: []string{
				".git",
				"node_modules",
				"frontend/node_modules",
				"frontend/.turbo",
				"frontend/microfrontends/*/.svelte-kit",
				"frontend/microfrontends/*/build",
				"frontend/microfrontends/*/test-results",
			},
		}).
		WithWorkdir("/src/frontend").
		WithExec([]string{"bun", "install", "--frozen-lockfile"})
}

// Frontend runs the frontend job's bun gates as ONE turbo invocation fanned across the workspace graph:
// svelte-check (check), TypeScript 7 via tsgo (check:tsgo), the unit tests, and lint + fmt:check — which
// are package tasks now, so they parallelise and cache instead of running once repo-wide. Turbo fails on
// the first non-zero task, which Dagger surfaces as the container error. The Playwright e2e leg
// (`test:e2e`) is browser-bound and lives in its own function; it is not part of this hermetic subset.
func (m *LanceNs) Frontend(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.frontendBase(src).
		WithExec([]string{"bunx", "turbo", "run", "check", "check:tsgo", "test", "lint", "fmt:check"}).
		Stdout(ctx)
}
