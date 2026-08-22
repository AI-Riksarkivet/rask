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

// goImage is the toolchain for the Go plane's own gates, pinned to the `go` directive in
// .dagger/go.mod so the formatter that judges this module is the one the module is written for.
const goImage = "golang:1.26.1-trixie"

// GoFmt is the Go plane's formatting gate, and it exists because there wasn't one.
//
// `.dagger/` implements every CI gate in this estate, and until 2026-08-22 nothing checked its own
// formatting — `gofmt -l .` reported two tracked, hand-written files unformatted (`images.go`,
// `main.go`). The Python plane has had `ruff format --check` on the merge path the whole time, so this
// was not a policy decision about Go, it was a plane nobody pointed a gate at.
//
// The narrow scope is deliberate. The broader claim this finding was filed under — "1,047 lines
// implementing every CI gate, tested by nothing" — is false and worth not re-litigating: `.dagger/go.mod`
// IS enumerated by osv-scanner (`scan.go`), and every `dagger call` compiles and type-checks the whole
// module, so a type error cannot reach main. Formatting was the one real hole.
//
// It runs in a container rather than assuming a local toolchain, which is not merely convenient: there
// is no Go on the developer PATH here, and the repository rule is absolute — every container goes
// through Dagger. A gate that needed a hand-installed toolchain would be a gate most people never run.
func (m *Rask) GoFmt(
	ctx context.Context,
	// +defaultPath="/.dagger"
	// +optional
	src *dagger.Directory,
) (string, error) {
	return dag.Container().
		From(goImage).
		WithDirectory("/src", src).
		WithWorkdir("/src").
		// `gofmt -l` prints the offenders and exits 0 either way, so the exit code alone proves nothing —
		// the same shape as a piped command masking its status. Turn a non-empty list into a failure.
		WithExec([]string{"sh", "-c", `out=$(gofmt -l .); if [ -n "$out" ]; then echo "gofmt: these files are not formatted:"; echo "$out"; exit 1; fi; echo "gofmt: clean"`}).
		Stdout(ctx)
}

// GoFmtFixed returns the `.dagger` tree with gofmt applied — the write half of GoFmt.
//
// The estate's Python plane has had both halves forever (`make fmt` writes, `ruff format --check`
// gates), and a gate with no paired fixer is a gate people work around. There is no Go toolchain on
// the developer PATH here, so without this the only way to satisfy GoFmt would be to install one —
// which the repository's Dagger rule exists to make unnecessary.
//
// Usage: `dagger call go-fmt-fixed export --path=.dagger`
func (m *Rask) GoFmtFixed(
	// +defaultPath="/.dagger"
	// +optional
	src *dagger.Directory,
) *dagger.Directory {
	return dag.Container().
		From(goImage).
		WithDirectory("/src", src).
		WithWorkdir("/src").
		WithExec([]string{"gofmt", "-w", "."}).
		Directory("/src")
}
