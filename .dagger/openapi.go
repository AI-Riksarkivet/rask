package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// Openapi runs the CI `test` job's OpenAPI drift gate (`make openapi-check`): the committed specs
// (docs/*-openapi.json) are the canonical, reviewable contract of every endpoint we serve, and they are
// GENERATED from the live FastAPI apps — so a route added or changed without refreshing the spec must fail
// here, not surprise a client. base() excludes .git, so instead of `make openapi-check`'s `git diff` this
// snapshots the two committed specs, regenerates them from the apps, then diffs the regenerated files back
// against the snapshots — same fail-on-drift contract, no repo needed. A non-zero diff fails the function.
// OpenapiGenerate regenerates the specs from the live FastAPI apps and RETURNS `docs/`, so the refresh
// half of the contract is reachable without a local Python environment:
//
//	dagger call openapi-generate export --path=docs
//
// Openapi (below) only ever CHECKS. That asymmetry was invisible while every developer could run
// `make openapi`, and became a wall the moment one could not: uv cannot build this workspace on
// macOS/x86_64 at all, because lancedb ships no wheel for that platform (measured 2026-08-09 — the
// only macOS wheel is macosx_11_0_arm64). On such a machine an endpoint could be added but its spec
// could never be refreshed, so the drift gate would fail with no sanctioned way to satisfy it.
// Same container and same generator as the gate, so what this writes is exactly what that compares.
func (m *Rask) OpenapiGenerate(
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) *dagger.Directory {
	return m.base(src).
		WithExec([]string{"uv", "run", "--no-sync", "scripts/gen_openapi.py"}).
		Directory("docs")
}

func (m *Rask) Openapi(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.base(src).
		// Snapshot the committed specs before regenerating over them.
		WithExec([]string{"cp", "docs/catalog-openapi.json", "/tmp/catalog.orig.json"}).
		WithExec([]string{"cp", "docs/lineage-openapi.json", "/tmp/lineage.orig.json"}).
		// Regenerate from the live FastAPI apps (== `make openapi`); base() already synced, so --no-sync.
		WithExec([]string{"uv", "run", "--no-sync", "scripts/gen_openapi.py"}).
		// Fail on any drift between the committed spec and what the code now serves.
		WithExec([]string{"diff", "-u", "/tmp/catalog.orig.json", "docs/catalog-openapi.json"}).
		WithExec([]string{"diff", "-u", "/tmp/lineage.orig.json", "docs/lineage-openapi.json"}).
		Stdout(ctx)
}
