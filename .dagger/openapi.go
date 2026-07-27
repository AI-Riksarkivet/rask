package main

import (
	"context"

	"dagger/lance-ns/internal/dagger"
)

// Openapi runs the CI `test` job's OpenAPI drift gate (`make openapi-check`): the committed specs
// (docs/*-openapi.json) are the canonical, reviewable contract of every endpoint we serve, and they are
// GENERATED from the live FastAPI apps — so a route added or changed without refreshing the spec must fail
// here, not surprise a client. base() excludes .git, so instead of `make openapi-check`'s `git diff` this
// snapshots the two committed specs, regenerates them from the apps, then diffs the regenerated files back
// against the snapshots — same fail-on-drift contract, no repo needed. A non-zero diff fails the function.
func (m *LanceNs) Openapi(
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
