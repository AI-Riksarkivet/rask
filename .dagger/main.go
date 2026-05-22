// Dagger module for the rask monorepo.
//
// Bootstrap only — one placeholder function so the module has an exported entry.
// Run `dagger develop` to generate go.mod and internal/dagger bindings, then
// replace Echo with real build/test/publish functions for rask.
package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

type Rask struct{}

// Echo returns whatever string you pass it (placeholder).
func (m *Rask) Echo(ctx context.Context, msg string) (string, error) {
	return dag.Container().
		From("alpine:latest").
		WithExec([]string{"echo", msg}).
		Stdout(ctx)
}
