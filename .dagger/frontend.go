package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// bunImage is the frontend toolchain's base image, pinned by digest at the bun the workspace names
// (`packageManager` in frontend/package.json, which CI's setup-bun also reads, and the builder stage of
// .docker/frontend.dockerfile), so a bun bump moves all three together. Bun is the whole toolchain here: oxlint and
// rsvelte-fmt arrive as frontend/ root devDependencies via `bun install --frozen-lockfile` — no globally installed tool,
// no second package manager.
const bunImage = "oven/bun:1.4.4-slim@sha256:1846adf22496bd711036aae8be147e57bdebbaea2e5264e3e0b5af1db187d9a4"

// frontendBase returns the installed workspace container: the bun image + source + `bun install --frozen-lockfile`
// (the console and packages/* from frontend/bun.lock). Unexported → a shared private helper, not a Dagger Function.
// The bun install cache mirrors the dockerfile's `--mount=type=cache,target=/root/.bun/install/cache`.
func (m *Rask) frontendBase(src *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(bunImage).
		WithMountedCache("/root/.bun/install/cache", dag.CacheVolume("lance-ns-bun")).
		WithDirectory("/src", src, dagger.ContainerWithDirectoryOpts{
			// Exclude checked-in build/install artefacts so the container installs clean from the lockfile.
			Exclude: []string{
				// A developer's `.env` is NOT a build input. It is untracked, so CI checking out fresh
				// never has one while a LOCAL run shipped it into the container — the same command taking
				// a different, secret-bearing input depending on whose machine ran it.
				"**/.env",
				"**/.env.*",
				".git",
				"node_modules",
				"frontend/node_modules",
				"frontend/console/.svelte-kit",
				"frontend/console/build",
				"frontend/console/test-results",
			},
		}).
		WithWorkdir("/src/frontend").
		WithExec([]string{"bun", "install", "--frozen-lockfile"})
}

// Frontend runs the console's bun gates: svelte-check (check), the unit tests, and lint + fmt:check. The Playwright
// e2e leg (`test:e2e`) is browser-bound and runs in its own CI job; it is not part of this hermetic subset.
//
// EVERY GATE RUNS, and the exit is non-zero if any failed. A gate that stops at the first failure reports a sample of
// the estate, not its state: measured once on this workspace, a fail-fast run reported 1 failure where a run of every
// task reported 4, one of them a suite that had collected zero tests for weeks.
func (m *Rask) Frontend(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.frontendBase(src).
		WithExec([]string{"sh", "-c", `rc=0
for task in check test lint fmt:check; do
  echo "== bun run $task"
  bun run "$task" || { echo "!! $task failed"; rc=1; }
done
exit $rc`}).
		Stdout(ctx)
}
