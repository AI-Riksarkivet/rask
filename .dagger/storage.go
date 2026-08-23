package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// rustfsImage is the S3-compatible backend the storage smoke runs against. Pinned rather than
// `:latest`, which is what the retired `make rustfs-up` used: a smoke test whose backend version
// changes underneath it cannot tell a rask regression from an upstream one.
const rustfsImage = "rustfs/rustfs:1.0.0-alpha.60"

// SmokeRustfs proves `packages/storage` works against a REAL S3 backend rather than moto.
//
// It REPLACES three make targets — `rustfs-up`, `smoke-rustfs`, `rustfs-down` — and collapsing them
// is what makes the Dagger form strictly better rather than a trade. The old shape started a DETACHED
// container, ran a script against `localhost:9000`, and left the operator to remember the teardown;
// its `docker run` was one of the estate's last three, and the objection to converting it was that
// `as-service up` holds a terminal where `up -d` does not.
//
// That objection only applies to a two-step shape. As ONE function the service is bound for exactly
// the life of the exec: nothing is detached, nothing leaks if the smoke fails, no host port is taken
// (so it cannot collide with a `make dev-*` stack or a second developer), and there is no teardown to
// forget. `dagger call smoke-rustfs` is the whole thing.
//
// The credentials are env-driven on purpose — the estate's storage layer must swap
// rustfs/MinIO/AWS with environment variables alone, and hard-coding them here would quietly test a
// narrower contract than the one `packages/storage` promises.
func (m *Rask) SmokeRustfs(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	rustfs := dag.Container().
		From(rustfsImage).
		WithEnvVariable("RUSTFS_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_SECRET_KEY", "rustfsadmin").
		WithExposedPort(9000).
		AsService()

	return m.base(src).
		WithServiceBinding("rustfs", rustfs).
		WithEnvVariable("RASK_S3_ENDPOINT_URL", "http://rustfs:9000").
		WithEnvVariable("AWS_ACCESS_KEY_ID", "rustfsadmin").
		WithEnvVariable("AWS_SECRET_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("AWS_REGION", "us-east-1").
		WithEnvVariable("RASK_S3_INSECURE", "1").
		WithEnvVariable("RASK_SMOKE_BUCKET", "rask-rustfs-smoke").
		WithExec([]string{"uv", "run", "--no-sync", "python", "scripts/smoke_rustfs.py"}).
		Stdout(ctx)
}
