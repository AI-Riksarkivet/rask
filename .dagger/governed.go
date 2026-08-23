package main

import (
	"context"
	"strconv"
	"time"

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

// runToken makes every volume in a run unique to that run.
//
// The volumes are REQUIRED for durability inside a run (a bootstrap that finishes drops its service to
// zero references and stops it), and a NAMED volume shared across runs then leaks the estate: a
// namespace a previous run created is still there, so `create` answers 409 where the test expects 200.
// Worse, the obvious fix — dropping and recreating the bucket in the bootstrap — does nothing, because
// Dagger caches that exec by its inputs and the command string never changes. It ran once, months of
// runs ago in cache terms, and every run since reused the result.
//
// A per-run name gives both properties at once: durable within the run, fresh between runs. The cost
// is re-running the OpenFGA migration and the AGE init each time, which is the correct cost for an
// e2e — `make dagger-gc` reclaims the volumes.
func runToken() string {
	return strconv.FormatInt(time.Now().UnixNano(), 36)
}

func (m *Rask) governedStack(ctx context.Context, src *dagger.Directory) (*governedStack, error) {
	run := runToken()
	store := dag.Container().
		From(rustfsImage).
		WithEnvVariable("RUSTFS_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_SECRET_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_ADDRESS", ":9000").
		WithMountedCache("/data", dag.CacheVolume("rask-governed-store-"+run)).
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
		WithMountedCache("/var/lib/postgresql/data", dag.CacheVolume("rask-governed-fga-pg-"+run)).
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
func (m *Rask) catalogService(src *dagger.Directory, stack *governedStack, auth bool, lineage *dagger.Service) *dagger.Service {
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
	if lineage != nil {
		// The governance stack turns catalog->lineage emission ON. Without this the catalog runs
		// perfectly and simply records nothing, so the provenance assertions fail against a healthy
		// service — the emit is the thing under test, not a side effect of it.
		//
		// THE SERVICE BINDING IS AS LOAD-BEARING AS THE URL, and passing only the URL is how this
		// failed the first time. `LANCE_LINEAGE_URL` pointed at `http://lineage-api:8000`, a name the
		// catalog's container could not resolve because nothing bound it — and the emit is
		// BEST-EFFORT, so the failure was swallowed exactly as designed. The catalog ran perfectly,
		// the lineage service ran perfectly, and the only symptom was a governance assertion reading
		// `expected lineage creator=…, got None`. A misconfigured emit target and a missing author
		// are indistinguishable from the far end.
		c = c.
			WithServiceBinding("lineage-api", lineage).
			WithEnvVariable("LANCE_LINEAGE_EMIT_ENABLED", "true").
			WithEnvVariable("LANCE_LINEAGE_URL", "http://lineage-api:8000/api/v1/lineage")
	}
	return c.WithExposedPort(2333).AsService()
}

// lineageService is the AGE-backed provenance store plus the service that writes to it.
//
// The graph lives in Apache AGE (Postgres), initialised by `.docker/lineage-init.sql` — which
// `docker-entrypoint-initdb.d` runs ONCE, on first init. With the cache volume below that means once
// per volume rather than once per run, which is correct and is also why the volume is not optional:
// the same reference-counting that lost the catalog's bucket and the OpenFGA migration would drop
// this Postgres between the init and the first read, and the graph would simply not exist.
func (m *Rask) lineageService(src *dagger.Directory) (*dagger.Service, error) {
	pg := dag.Container().
		From(ageImage).
		WithEnvVariable("POSTGRES_USER", "lineage").
		WithEnvVariable("POSTGRES_PASSWORD", "lineage").
		WithEnvVariable("POSTGRES_DB", "lineage").
		WithEnvVariable("POSTGRES_HOST_AUTH_METHOD", "trust").
		WithFile("/docker-entrypoint-initdb.d/10-age.sql", src.File(".docker/lineage-init.sql")).
		WithMountedCache("/var/lib/postgresql/data", dag.CacheVolume("rask-governed-lineage-pg-"+runToken())).
		WithExposedPort(5432).
		WithDefaultArgs([]string{"docker-entrypoint.sh", "postgres"}).
		AsService()

	// WAIT FOR POSTGRES TO BE READY, not merely listening. `docker-entrypoint.sh` starts a TEMPORARY
	// server to run `/docker-entrypoint-initdb.d` and then restarts into the real one, so the port is
	// accepting during a window when the database is not usable — and Dagger's service binding waits
	// on the port. The lineage service then dies in its lifespan at `ensure_events_table()`, deep in
	// psycopg's pool, which reads as a lineage bug rather than a startup race.
	//
	// It is also why this failed INTERMITTENTLY before the wait existed: with the cache volume already
	// initialised the init scripts are skipped and the race usually loses, so the first symptom was a
	// gate that passed and then stopped passing for no visible reason.
	if _, err := dag.Container().
		From(ageImage).
		WithServiceBinding("lineage-postgres", pg).
		WithExec([]string{"sh", "-c", "until pg_isready -h lineage-postgres -U lineage -d lineage; do sleep 1; done"}).
		Sync(context.Background()); err != nil {
		return nil, err
	}

	api := m.Image(src, "rest-catalog", "", "", "", nil).
		WithServiceBinding("lineage-postgres", pg).
		WithEnvVariable("LINEAGE_DATABASE_URL", "postgresql://lineage:lineage@lineage-postgres:5432/lineage").
		WithEnvVariable("LINEAGE_GRAPH", "lineage").
		WithExposedPort(8000).
		// The SAME image as the catalog, run with a different command — the compose overlay says so
		// explicitly ("same image as the catalog (now ships the lineage service too)"), and building
		// it twice would let the two drift.
		//
		// SHARING THE IMAGE MEANS INHERITING ITS HEALTHCHECK, and `.docker/rest-catalog.dockerfile`
		// bakes one that connects to 127.0.0.1:2333 — the CATALOG's port. Nothing listens on 2333
		// here, so the probe fails, Dagger refuses to start the service, and the run dies with
		// `health check errored` plus a `ConnectionRefusedError` naming a port this service was never
		// supposed to serve. The compose overlay hit the same thing and solved it the same way, by
		// overriding the healthcheck rather than dropping it.
		WithDockerHealthcheck(
			[]string{"python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/livez').read()"},
			dagger.ContainerWithDockerHealthcheckOpts{Interval: "5s", Timeout: "3s", StartPeriod: "30s", Retries: 20},
		).
		WithDefaultArgs([]string{"uvicorn", "lineage.main:app", "--host", "0.0.0.0", "--port", "8000"}).
		AsService()

	return api, nil
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
	catalog := m.catalogService(src, stack, true, nil)

	return m.base(src).
		WithServiceBinding("catalog", catalog).
		WithServiceBinding("dex", stack.Dex).
		WithServiceBinding("openfga", stack.OpenFGA).
		WithEnvVariable("LANCE_E2E_AUTH_SERVER", "http://catalog:2333").
		WithEnvVariable("LANCE_E2E_DEX", "http://dex:5556/dex").
		// The narrated demo reads a different trio of names for the same three endpoints; the retired
		// script exported both sets side by side and so does this.
		WithEnvVariable("CATALOG_URL", "http://catalog:2333").
		WithEnvVariable("LINEAGE_URL", "http://lineage-api:8000").
		WithEnvVariable("DEX_URL", "http://dex:5556/dex").
		// EMPTY, and it must be set rather than left unset. `test_governance_e2e.py` DEFAULTS
		// `LANCE_E2E_DEX_SECRET` to "lance-catalog-secret", and both Dex configs in this repo declare
		// `lance-catalog` as `public: true` — a public client, which rejects a client_secret with
		// `401 invalid_client`. Its own comment names the fix (`LANCE_E2E_DEX_SECRET="" for a
		// public-client Dex`), and `scripts/governance_e2e.sh` never set it: it exported
		// LANCE_E2E_AUTH_SERVER, LANCE_E2E_LINEAGE_URL and LANCE_E2E_DEX and stopped. So that script
		// could not have passed as written against the stack it stood up — the same "exists but cannot
		// run" shape this estate's test audit kept finding.
		WithEnvVariable("LANCE_E2E_DEX_SECRET", "").
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

// GovernanceChain proves the dataops loop end to end: authorization, provenance AUTHORSHIP, and
// medallion lineage — with real Dex id_tokens and not one hand-written tuple.
//
// It replaces `scripts/governance_e2e.sh`, which layered FOUR compose files (base + auth + lineage +
// governance) to assemble the same thing. Everything it needs beyond the auth stack is the AGE graph
// and the lineage service, so it is the auth stack plus two bindings rather than a second stack.
//
// The catalog runs with emission ON. That is the distinction from `AuthChain`: authorization alone
// passes with emission off, so a run that forgot it would prove the authz half and silently skip the
// provenance half — which is the shape of every finding in this estate's test audit.
func (m *Rask) GovernanceChain(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// Run the narrated demo instead of the assertions (the retired script's DEMO=1).
	// +optional
	demo bool,
) (string, error) {
	stack, err := m.governedStack(ctx, src)
	if err != nil {
		return "", err
	}
	lineage, err := m.lineageService(src)
	if err != nil {
		return "", err
	}
	catalog := m.catalogService(src, stack, true, lineage)

	return m.base(src).
		WithServiceBinding("catalog", catalog).
		WithServiceBinding("lineage-api", lineage).
		WithServiceBinding("dex", stack.Dex).
		WithServiceBinding("openfga", stack.OpenFGA).
		WithEnvVariable("LANCE_E2E_AUTH_SERVER", "http://catalog:2333").
		WithEnvVariable("LANCE_E2E_LINEAGE_URL", "http://lineage-api:8000").
		WithEnvVariable("LANCE_E2E_DEX", "http://dex:5556/dex").
		// Wait for BOTH, not just the ports: uvicorn accepts before either lifespan is built, and the
		// lineage service additionally has to reach AGE. A racing first request reads as a wrong
		// answer rather than as an unready service.
		WithExec([]string{"sh", "-c", `
for i in $(seq 1 90); do
  python -c "import urllib.request,sys; urllib.request.urlopen('http://catalog:2333/livez',timeout=2)" 2>/dev/null && break
  sleep 2
done
for i in $(seq 1 90); do
  python -c "import urllib.request,sys; urllib.request.urlopen('http://lineage-api:8000/livez',timeout=2)" 2>/dev/null && exit 0
  sleep 2
done
echo "lineage-api never became ready"; exit 1`}).
		// `LANCE_E2E_DEX_SECRET=` IN THE SHELL, not via WithEnvVariable — and the difference is not
		// cosmetic. Dagger's `WithEnvVariable(name, "")` leaves the variable UNSET rather than setting
		// it to empty (measured: the container reported `'<UNSET>'`), and this test distinguishes the
		// two: it DEFAULTS `LANCE_E2E_DEX_SECRET` to "lance-catalog-secret" when unset, then sends
		// that secret to a client both Dex configs declare `public: true`, which answers
		// `401 invalid_client`. A secretless grant against the same Dex returns 200, verified directly.
		//
		// `scripts/governance_e2e.sh` never set this variable at all, so it could not have passed
		// against the stack it stood up — the "exists but cannot run" shape again.
		With(func(c *dagger.Container) *dagger.Container {
			// `DEMO=1` on the retired script ran the narrated walkthrough instead of the assertions
			// against the SAME stack. Keeping it as a flag preserves that without a second function —
			// and a demo that stands up its own stack is how a demo drifts from what the tests prove.
			if demo {
				return c.WithExec([]string{"sh", "-c", "LANCE_E2E_DEX_SECRET= uv run --no-sync python scripts/governance_demo.py"})
			}
			return c.WithExec([]string{"sh", "-c", "LANCE_E2E_DEX_SECRET= uv run --no-sync pytest tests/e2e-py/test_governance_e2e.py -v"})
		}).
		Stdout(ctx)
}

// MedallionDemo is the live medallion walkthrough: real Lance datasets on RustFS, a real OpenLineage
// event after each step, and the DAG building in front of you at http://localhost:8000/ui/.
//
//	make medallion-demo        # then open the UI and watch bronze -> silver -> gold appear
//
// It replaces `scripts/medallion_demo.sh`, which could not run: that script did
// `compose up -d --build rustfs-perms rustfs lineage-postgres lineage-api web`, and NONE of the five
// compose files it layers defines a `web` service — compose fails on an unknown service name. (Its
// other apparent problem, the missing `lakehouse` bucket, is not one: the driver's `ensure_bucket()`
// creates it.)
//
// SELF-DRIVING, and that is what preserves the demo. The point is watching the DAG build, not reading
// a finished one — but Dagger services are per-invocation, so a driver running in a separate call
// would address a different stack. Instead the service's own command starts uvicorn, waits for it,
// and then runs the driver in the same container, so `up` gives a live UI that fills in as it goes.
func (m *Rask) MedallionDemo(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// Seconds between steps — the walkthrough's pacing.
	// +optional
	// +default="2.5"
	stepDelay string,
) (*dagger.Service, error) {
	run := runToken()

	store := dag.Container().
		From(rustfsImage).
		WithEnvVariable("RUSTFS_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_SECRET_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_ADDRESS", ":9000").
		WithMountedCache("/data", dag.CacheVolume("rask-demo-store-"+run)).
		WithExposedPort(9000).
		AsService()

	pg := dag.Container().
		From(ageImage).
		WithEnvVariable("POSTGRES_USER", "lineage").
		WithEnvVariable("POSTGRES_PASSWORD", "lineage").
		WithEnvVariable("POSTGRES_DB", "lineage").
		WithEnvVariable("POSTGRES_HOST_AUTH_METHOD", "trust").
		WithFile("/docker-entrypoint-initdb.d/10-age.sql", src.File(".docker/lineage-init.sql")).
		WithMountedCache("/var/lib/postgresql/data", dag.CacheVolume("rask-demo-lineage-pg-"+run)).
		WithExposedPort(5432).
		WithDefaultArgs([]string{"docker-entrypoint.sh", "postgres"}).
		AsService()

	if _, err := dag.Container().
		From(ageImage).
		WithServiceBinding("lineage-postgres", pg).
		WithExec([]string{"sh", "-c", "until pg_isready -h lineage-postgres -U lineage -d lineage; do sleep 1; done"}).
		Sync(ctx); err != nil {
		return nil, err
	}

	return m.Image(src, "rest-catalog", "", "", "", nil).
		WithServiceBinding("lineage-postgres", pg).
		WithServiceBinding("rustfs", store).
		WithEnvVariable("LINEAGE_DATABASE_URL", "postgresql://lineage:lineage@lineage-postgres:5432/lineage").
		WithEnvVariable("LINEAGE_GRAPH", "lineage").
		// The demo overlay's own settings: the lineage service reads the same store the driver writes.
		WithEnvVariable("LINEAGE_DEMO_DATA_ENABLED", "true").
		WithEnvVariable("LINEAGE_S3_ENDPOINT", "http://rustfs:9000").
		WithEnvVariable("LINEAGE_S3_ACCESS_KEY_ID", "rustfsadmin").
		WithEnvVariable("LINEAGE_S3_SECRET_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("LINEAGE_S3_REGION", "us-east-1").
		WithEnvVariable("LINEAGE_S3_BUCKET", "lakehouse").
		// What the driver reads. The script exported the same set from the host.
		WithEnvVariable("S3_ENDPOINT", "http://rustfs:9000").
		WithEnvVariable("S3_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("S3_SECRET_KEY", "rustfsadmin").
		WithEnvVariable("S3_REGION", "us-east-1").
		WithEnvVariable("S3_BUCKET", "lakehouse").
		WithEnvVariable("LINEAGE_URL", "http://localhost:8000").
		WithEnvVariable("STEP_DELAY", stepDelay).
		WithDirectory("/srv/scripts", src.Directory("scripts")).
		WithExposedPort(8000).
		// The image's baked HEALTHCHECK probes the CATALOG's port 2333 — see lineageService above.
		WithDockerHealthcheck(
			[]string{"python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/livez').read()"},
			dagger.ContainerWithDockerHealthcheckOpts{Interval: "5s", Timeout: "3s", StartPeriod: "60s", Retries: 30},
		).
		WithDefaultArgs([]string{"sh", "-c", `
uvicorn lineage.main:app --host 0.0.0.0 --port 8000 &
until python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/livez')" 2>/dev/null; do sleep 1; done
echo "== UI is live at http://localhost:8000/ui/ — the DAG builds as the driver runs =="
python /srv/scripts/medallion_demo.py || echo "driver exited non-zero"
echo "== walkthrough complete — the stack stays up so you can explore it =="
wait`}).
		AsService(), nil
}
