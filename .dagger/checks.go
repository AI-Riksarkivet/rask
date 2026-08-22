package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// Lint runs ruff check + format --check over the WHOLE tree — the same estate `make lint` walks.
//
// It named `services tests` until 2026-08-22, which left 39,244 tracked lines outside the merge-path
// gate: packages/ (24,580 — all seven first-party libraries, including the authorization kernel),
// scripts/ (5,667 — among them the ray_*_job.py entrypoints the cluster image BAKES, so production
// code) and runners/ (8,997). `make lint` has always run `ruff check .`, so the local gate and the CI
// gate measured different estates and nothing asserted they agree — which is how a lint-clean local
// run and a lint-clean CI run could both be true of different code.
//
// `uv run --no-sync`, not `uvx`: `uvx ruff` resolves the LATEST ruff while the Makefile runs the LOCKED
// one from the root dev group. Widening the scope while keeping two ruff versions would trade one
// divergence for another, and this estate has already been bitten by exactly that — an unpinned
// `uvx ty` drifting until its pre-commit hook blocked every commit. base() already runs
// `uv sync --all-packages`, so `--no-sync` is correct and free.
func (m *Rask) Lint(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.base(src).
		WithExec([]string{"uv", "run", "--no-sync", "ruff", "check", "."}).
		WithExec([]string{"uv", "run", "--no-sync", "ruff", "format", "--check", "."}).
		Stdout(ctx)
}

// Typecheck runs the ty type-checker (the CI type gate).
func (m *Rask) Typecheck(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.base(src).WithExec([]string{"uvx", "ty", "check"}).Stdout(ctx)
}
