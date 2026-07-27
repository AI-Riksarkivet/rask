package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// Lint runs ruff check + format --check over services + tests (the CI lint gate).
func (m *Rask) Lint(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return m.base(src).
		WithExec([]string{"uvx", "ruff", "check", "services", "tests"}).
		WithExec([]string{"uvx", "ruff", "format", "--check", "services", "tests"}).
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
