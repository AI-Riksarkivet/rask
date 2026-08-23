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

// mcImage is the S3 client used to create the catalog's bucket before the server starts.
const mcImage = "minio/mc:latest"

// RustfsLifecycle runs the SAME catalog lifecycle e2e the MinIO stack runs, with the bytes on RustFS —
// proving the catalog is genuinely S3-agnostic rather than MinIO-shaped.
//
// It replaces `scripts/rustfs_e2e.sh`, which drove `docker compose up -d --build` over
// `docker-compose.yml` + `docker-compose.rustfs.yml`. That script was invisible to the estate's own
// docker gate for most of a day: the gate required `up` on the same LINE as `compose`, and the script
// wraps it (`compose() { docker compose -f "$BASE" -f "$RUSTFS" "$@"; }`).
//
// ORDERING IS THE INTERESTING PART, and it needs BOTH halves below. Compose expressed it
// declaratively — `depends_on: {createbuckets-rustfs: {condition: service_completed_successfully}}`
// — and Dagger has no such condition on a service binding, so `Sync` supplies the sequencing: the
// bootstrap is forced to finish before the catalog service is constructed.
//
// That alone is NOT enough, and the failure is worth knowing about because it looks like a catalog
// bug. Dagger services are reference-counted: `Sync` completing drops rustfs to zero references, so
// it stops and takes the bucket with it, and the catalog then starts a fresh empty one. The first
// run of this function died on `NoSuchBucket: Volume not found` from inside pylance. The cache volume
// on the service is what makes the bootstrap durable across that restart — the same job compose's
// named `rustfs-data` volume was doing.
func (m *Rask) RustfsLifecycle(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	rustfs := dag.Container().
		From(rustfsImage).
		WithEnvVariable("RUSTFS_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_SECRET_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_ADDRESS", ":9000").
		// A VOLUME, because Dagger services are REFERENCE-COUNTED and this one is used twice. The
		// bootstrap below creates the bucket, `Sync` forces it to finish — and finishing drops rustfs
		// to zero references, so it STOPS. The catalog then starts a fresh one, and the first thing
		// the lifecycle test saw was `NoSuchBucket: Volume not found`.
		//
		// The compose overlay this replaces had the same need and met it the same way
		// (`volumes: [rustfs-data:/data]`), so the durability is not a Dagger workaround — it is the
		// property the stack always relied on, made explicit.
		//
		// PER-RUN rather than a fixed name, unlike compose's `rustfs-data`. A shared volume leaks the
		// estate forward, and the governance chain proved that is not theoretical: a namespace left by
		// an earlier run made `create` answer 409 where the test expected 200, and the obvious repair —
		// dropping the bucket in the bootstrap — did nothing, because Dagger caches that exec by its
		// inputs and the command never changes. A fresh volume gives durability inside the run and
		// isolation between runs, which is what an e2e actually wants from both.
		WithMountedCache("/data", dag.CacheVolume("rask-rustfs-e2e-"+runToken())).
		WithExposedPort(9000).
		// NO `/data` argument, despite the compose overlay carrying `command: ["/data"]`. The image
		// has no ENTRYPOINT to prepend it to, so passing it makes Dagger try to EXECUTE `/data`
		// (`exec /data: permission denied`). Compose needed it because its `command:` replaces the
		// image CMD wholesale; here the image's own default is already right — the same shape
		// `SmokeRustfs` above uses and proves.
		AsService()

	// The bucket, before anything reads it. `Sync` is what orders this against the catalog below.
	if _, err := dag.Container().
		From(mcImage).
		WithServiceBinding("rustfs", rustfs).
		WithExec([]string{"sh", "-c", `until mc alias set rfs http://rustfs:9000 rustfsadmin rustfsadmin >/dev/null 2>&1; do sleep 2; done && mc mb --ignore-existing rfs/lance-catalog`}).
		Sync(ctx); err != nil {
		return "", err
	}

	catalog := m.Image(src, "rest-catalog", "", "", "", nil).
		WithServiceBinding("rustfs", rustfs).
		WithEnvVariable("LANCE_REST_IMPL", "dir").
		WithEnvVariable("LANCE_REST_ROOT", "s3://lance-catalog").
		WithEnvVariable("LANCE_NS_DELIMITER", "$").
		WithEnvVariable("LANCE_S3_ENDPOINT", "http://rustfs:9000").
		WithEnvVariable("LANCE_S3_ACCESS_KEY_ID", "rustfsadmin").
		WithEnvVariable("LANCE_S3_SECRET_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("LANCE_S3_REGION", "us-east-1").
		WithEnvVariable("LANCE_S3_ALLOW_HTTP", "true").
		WithEnvVariable("LANCE_S3_VIRTUAL_HOSTED", "false").
		WithExposedPort(2333).
		AsService()

	return m.base(src).
		WithServiceBinding("catalog", catalog).
		WithEnvVariable("LANCE_REST_E2E_URL", "http://catalog:2333").
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "tests/e2e-py/test_e2e.py", "-v"}).
		Stdout(ctx)
}
