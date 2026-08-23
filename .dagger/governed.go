package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

const (
	openfgaImage  = "openfga/openfga:latest"
	dexImage      = "dexidp/dex:latest"
	fgaPostgresIm = "postgres:16-alpine"
)

// governedStack is the shared scaffolding behind the auth and governance e2es.
//
// Three scripts drove overlapping `docker compose` stacks — `auth_e2e.sh`, `governance_e2e.sh` and
// `medallion_demo.sh` — and converting them one at a time would have produced three near-identical
// 150-line functions. The pieces they share are here once: an object store with the catalog's bucket,
// OpenFGA on its own Postgres, and Dex.
//
// ORDERING IS EXPLICIT AND EACH STEP EARNED ITS PLACE. Compose said
// `depends_on: {condition: service_completed_successfully}`; Dagger has no such thing on a binding,
// so a one-shot is sequenced by forcing it to evaluate. Two traps this already hit for real, both of
// which surface as somebody else's bug:
//
//   - Dagger services are REFERENCE-COUNTED, and this bit twice. A bootstrap that finishes drops its
//     dependency to zero references, which STOPS it. The bucket created in step one was gone by step
//     three and pylance reported `NoSuchBucket` as if the catalog were broken; then the OpenFGA
//     migration was rolled back the same way and `/healthz` answered 500 as if the server were.
//     BOTH backing services keep a cache volume, for exactly the reason compose kept named ones.
//     The tell is that the symptom always names the wrong component.
//   - OpenFGA needs `migrate` before `run`, and skipping it does not fail at startup: the server
//     comes up and the first authorization call fails against absent tables.
type governedStack struct {
	Store   *dagger.Service
	OpenFGA *dagger.Service
	Dex     *dagger.Service
}

func (m *Rask) governedStack(ctx context.Context, src *dagger.Directory) (*governedStack, error) {
	store := dag.Container().
		From(rustfsImage).
		WithEnvVariable("RUSTFS_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_SECRET_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_ADDRESS", ":9000").
		WithMountedCache("/data", dag.CacheVolume("rask-governed-store")).
		WithExposedPort(9000).
		AsService()

	if _, err := dag.Container().
		From(mcImage).
		WithServiceBinding("store", store).
		WithExec([]string{"sh", "-c", `until mc alias set s3 http://store:9000 rustfsadmin rustfsadmin >/dev/null 2>&1; do sleep 2; done && mc mb --ignore-existing s3/lance-catalog`}).
		Sync(ctx); err != nil {
		return nil, err
	}

	const fgaDSN = "postgres://openfga:openfga@fga-postgres:5432/openfga?sslmode=disable"
	fgaPostgres := dag.Container().
		From(fgaPostgresIm).
		WithEnvVariable("POSTGRES_USER", "openfga").
		WithEnvVariable("POSTGRES_PASSWORD", "openfga").
		WithEnvVariable("POSTGRES_DB", "openfga").
		WithEnvVariable("POSTGRES_HOST_AUTH_METHOD", "trust").
		// A VOLUME, for the SAME reason the store has one, and I hit this twice in one sitting after
		// writing the warning above. `migrate` runs, `Sync` forces it to finish — and finishing drops
		// fga-postgres to zero references, so it stops and the migration goes with it. OpenFGA then
		// starts against empty tables and answers `/healthz` with a 500, which reads as "OpenFGA is
		// broken" rather than "its database was rolled back underneath it".
		WithMountedCache("/var/lib/postgresql/data", dag.CacheVolume("rask-governed-fga-pg")).
		WithExposedPort(5432).
		WithDefaultArgs([]string{"docker-entrypoint.sh", "postgres"}).
		AsService()

	// Wait for Postgres in a container that HAS a shell. The OpenFGA image is distroless — no `sh`,
	// no `nc` — so the readiness loop cannot live in the migrate step itself (`exec: "sh": executable
	// file not found in $PATH`). Compose expressed this as `condition: service_healthy` on a
	// dependency; here the wait is its own step and `Sync` orders it.
	if _, err := dag.Container().
		From(fgaPostgresIm).
		WithServiceBinding("fga-postgres", fgaPostgres).
		WithExec([]string{"sh", "-c", "until pg_isready -h fga-postgres -U openfga; do sleep 1; done"}).
		Sync(ctx); err != nil {
		return nil, err
	}

	// `migrate` BEFORE `run`, and it must finish first — see the type's comment. No shell wrapper:
	// the image has none, so the binary is invoked directly.
	if _, err := dag.Container().
		From(openfgaImage).
		WithServiceBinding("fga-postgres", fgaPostgres).
		WithEnvVariable("OPENFGA_DATASTORE_ENGINE", "postgres").
		WithEnvVariable("OPENFGA_DATASTORE_URI", fgaDSN).
		WithExec([]string{"/openfga", "migrate"}).
		Sync(ctx); err != nil {
		return nil, err
	}

	openfga := dag.Container().
		From(openfgaImage).
		WithServiceBinding("fga-postgres", fgaPostgres).
		WithEnvVariable("OPENFGA_DATASTORE_ENGINE", "postgres").
		WithEnvVariable("OPENFGA_DATASTORE_URI", fgaDSN).
		WithEnvVariable("OPENFGA_LOG_FORMAT", "json").
		WithExposedPort(8080).
		WithDefaultArgs([]string{"/openfga", "run"}).
		AsService()

	dex := dag.Container().
		From(dexImage).
		WithFile("/etc/dex/config.yaml", src.File(".docker/dex.config.yaml")).
		WithExposedPort(5556).
		WithDefaultArgs([]string{"dex", "serve", "/etc/dex/config.yaml"}).
		AsService()

	return &governedStack{Store: store, OpenFGA: openfga, Dex: dex}, nil
}

// catalogService builds the REST catalog from its own dockerfile and points it at a governed stack.
//
// `auth` decides whether OIDC + OpenFGA are enforced. Both e2es want the same image and differ only
// in that flag, which is exactly why this is one function rather than two near-copies.
func (m *Rask) catalogService(src *dagger.Directory, stack *governedStack, auth bool) *dagger.Service {
	c := m.Image(src, "rest-catalog", "", "", "", nil).
		WithServiceBinding("store", stack.Store).
		WithEnvVariable("LANCE_REST_IMPL", "dir").
		WithEnvVariable("LANCE_REST_ROOT", "s3://lance-catalog").
		// A literal `$`. The compose files write `"$$"` because compose interpolates; nothing
		// interpolates here, so writing `$$` would make the delimiter two characters and every
		// `ns$table` identifier on the wire would stop resolving.
		WithEnvVariable("LANCE_NS_DELIMITER", "$").
		WithEnvVariable("LANCE_S3_ENDPOINT", "http://store:9000").
		WithEnvVariable("LANCE_S3_ACCESS_KEY_ID", "rustfsadmin").
		WithEnvVariable("LANCE_S3_SECRET_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("LANCE_S3_REGION", "us-east-1").
		WithEnvVariable("LANCE_S3_ALLOW_HTTP", "true").
		WithEnvVariable("LANCE_S3_VIRTUAL_HOSTED", "false")

	if auth {
		c = c.
			WithServiceBinding("dex", stack.Dex).
			WithServiceBinding("openfga", stack.OpenFGA).
			WithEnvVariable("LANCE_OIDC_ENABLED", "true").
			WithEnvVariable("LANCE_OIDC_ISSUER", "http://dex:5556/dex").
			WithEnvVariable("LANCE_OIDC_AUDIENCE", "lance-catalog").
			// Dex serves discovery over http here. Dev only — the chart never sets this.
			WithEnvVariable("LANCE_OIDC_ALLOW_INSECURE", "true").
			WithEnvVariable("LANCE_FGA_ENABLED", "true").
			WithEnvVariable("LANCE_FGA_API_URL", "http://openfga:8080")
	}
	return c.WithExposedPort(2333).AsService()
}

// AuthChain runs the app-side-seeding authorization chain against a real Dex and a real OpenFGA.
//
// It replaces `scripts/auth_e2e.sh`'s STACK half only. The assertions are untouched and now live in
// `scripts/auth_chain.sh`, ONE copy shared with that script — seven steps proving the app SEEDS
// ownership tuples on create (no tuple is ever written by hand), that owner cascades to reader and
// writer, and that a second identity holding no grant gets 403 on both read and write.
//
// The compose caller still exists because `.github/workflows/ci.yml` runs it and a concurrent session
// holds that file. That is a sequencing constraint, not a fallback: there is one copy of the
// assertions, and the compose half is deleted the moment the CI job can move here.
func (m *Rask) AuthChain(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	stack, err := m.governedStack(ctx, src)
	if err != nil {
		return "", err
	}
	catalog := m.catalogService(src, stack, true)

	return m.base(src).
		WithServiceBinding("catalog", catalog).
		WithServiceBinding("dex", stack.Dex).
		WithServiceBinding("openfga", stack.OpenFGA).
		WithEnvVariable("LANCE_E2E_AUTH_SERVER", "http://catalog:2333").
		WithEnvVariable("LANCE_E2E_DEX", "http://dex:5556/dex").
		WithEnvVariable("LANCE_E2E_FGA", "http://openfga:8080").
		WithExec([]string{"sh", "-c", "command -v curl >/dev/null || (apt-get update -qq && apt-get install -y -qq curl)"}).
		// WAIT FOR THE APP, not just the port. Dagger's service binding waits for the socket to
		// ACCEPT, and uvicorn accepts before the catalog's lifespan has built its namespace — so the
		// first request raced it and the script reported `got , want 401`, an empty curl reading as an
		// assertion failure rather than as "nothing was listening yet".
		//
		// The old script had this wait; it lived inside the compose block that was removed with the
		// orchestration, which is how a readiness check gets lost in a migration that looks purely
		// mechanical.
		WithExec([]string{"sh", "-c", `command -v curl || echo "NO CURL"; for i in $(seq 1 45); do echo "try $i: $(curl -s -o /dev/null -w '%{http_code}' http://catalog:2333/livez 2>&1)"; curl -fsS http://catalog:2333/livez >/dev/null 2>&1 && exit 0; sleep 2; done; echo "catalog never became ready"; exit 1`}).
		WithExec([]string{"bash", "scripts/auth_chain.sh"}).
		Stdout(ctx)
}
