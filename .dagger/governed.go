package main

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"dagger/rask/internal/dagger"
)

const (
	openfgaImage  = "openfga/openfga:v1.18.3"
	dexImage      = "ghcr.io/dexidp/dex:v2.42.0"
	fgaPostgresIm = "postgres:16-alpine"
)

// The identity the lanes' lineage service presents to the catalog, and the namespace the catalog keeps for
// provenance (ADR 0158). In the chart lineage presents its projected service-account token; these lanes run no
// Kubernetes, so the lane's own Dex mints lineage a token for a user that exists for that purpose alone, and the
// catalog lists that user's subject in `LANCE_PROVENANCE_WRITERS` exactly as the chart lists the account's.
const (
	lineageIdentity     = "lineage@rask.internal"
	provenanceNamespace = "provenance"
	lineageTokenPath    = "/var/run/rask/catalog-token"
	// laneConnectors is the catalog's `RASK_OIDC_CONNECTORS` in these lanes ([[LH-063]]): the lane Dex's one connector,
	// its password database `local`, is the idp id `laneIDP`, as the chart maps the development Dex's.
	laneConnectors = `{"local":"dex"}`
	laneIDP        = "dex"
)

// lineageDexUser is the static Dex password entry for `lineageIdentity`, appended to `.docker/dex.config.yaml`'s
// `staticPasswords` (the file's last key), with the hash alice and bob share ("password").
const lineageDexUser = `  - email: lineage@rask.internal
    hash: "$2b$10$BP/xHXQM78bAKb09HQLIjuryXCFo72R5DQJITLd5Nql0KCU2zib/."
    username: lineage
    userID: lineage
`

// governedStack is the shared scaffolding behind the auth and governance e2es: the pieces both lanes
// need, built here once rather than as a near-identical function per lane — an object store with the
// catalog's bucket, OpenFGA on its own Postgres, and Dex.
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
// is re-running the OpenFGA migration each time, which is the correct cost for an e2e — `make dagger-gc`
// reclaims the volumes.
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
		WithMountedCache("/data", dag.CacheVolume("rask-governed-store-"+run), dagger.ContainerWithMountedCacheOpts{Owner: rustfsUser}).
		WithExposedPort(9000).
		AsService()

	// THE ESTATE'S OWN S3 SEAM, not a third-party CLI image: a registry can close one to anonymous pulls
	// ([[XC-075]]: Docker Hub hands out a token with `access: []`, quay.io one with `actions: []` and a 401 on
	// the manifest), and the lane then fails on the image RESOLVE rather than on anything it tests. `m.base`
	// carries uv, python and this repo; `packages/storage` is the same client the fleet uses, in an image no
	// registry can withdraw. The script retries: a service binding resolves before the store is listening.
	if _, err := m.base(src).
		WithServiceBinding("store", store).
		WithEnvVariable("RASK_S3_ENDPOINT_URL", "http://store:9000").
		WithEnvVariable("AWS_ACCESS_KEY_ID", "rustfsadmin").
		WithEnvVariable("AWS_SECRET_ACCESS_KEY", "rustfsadmin").
		WithExec([]string{"uv", "run", "--no-sync", "python", "scripts/ensure_bucket.py", "lance-catalog"}).
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

	dexConfig, err := src.File(".docker/dex.config.yaml").Contents(ctx)
	if err != nil {
		return nil, err
	}
	if !strings.HasSuffix(dexConfig, "\n") {
		dexConfig += "\n"
	}
	dex := dag.Container().
		From(dexImage).
		WithNewFile("/etc/dex/config.yaml", dexConfig+lineageDexUser).
		WithExposedPort(5556).
		WithDefaultArgs([]string{"dex", "serve", "/etc/dex/config.yaml"}).
		AsService()

	return &governedStack{Store: store, OpenFGA: openfga, Dex: dex}, nil
}

// provenanceWiring is what a catalog needs to announce its writes to lineage and keep lineage's tables: the
// hostname lineage answers on, and the subjects that may write the provenance namespace.
type provenanceWiring struct {
	LineageHost string
	Writers     []string
}

// catalogService builds the REST catalog from its own dockerfile and points it at a governed stack.
//
// `auth` decides whether OIDC + OpenFGA are enforced. Both e2es want the same image and differ only
// in that flag, which is exactly why this is one function rather than two near-copies.
func (m *Rask) catalogService(src *dagger.Directory, stack *governedStack, auth bool, prov *provenanceWiring) (*dagger.Service, error) {
	c := m.Image(src, "rest-catalog", "", "", "", nil).
		WithServiceBinding("store", stack.Store).
		WithEnvVariable("LANCE_REST_IMPL", "dir").
		WithEnvVariable("LANCE_REST_ROOT", "s3://lance-catalog").
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
			WithEnvVariable("RASK_OIDC_ENABLED", "true").
			WithEnvVariable("RASK_OIDC_ISSUER", "http://dex:5556/dex").
			WithEnvVariable("RASK_OIDC_AUDIENCE", "rask-lakehouse").
			WithEnvVariable("RASK_OIDC_CONNECTORS", laneConnectors).
			// Dex serves discovery over http here. Dev only — the chart never sets this.
			WithEnvVariable("RASK_OIDC_ALLOW_INSECURE", "true").
			WithEnvVariable("RASK_FGA_ENABLED", "true").
			WithEnvVariable("RASK_FGA_API_URL", "http://openfga:8080")
	} else {
		// THE OTHER BRANCH NEEDS A POSTURE TOO, and having none made this parameter unusable:
		// `assert_authentication_configured` refuses to boot on the ambiguity between "open because I
		// meant it" and "open because nothing set it", so `auth == false` produced a catalog that
		// exited 3 before serving anything. An unauthenticated lane must SAY that is what it meant.
		c = c.WithEnvVariable("RASK_INSECURE_ALLOW_UNAUTHENTICATED", "true")
	}
	if prov != nil {
		// The governance stack turns catalog->lineage emission ON. Without this the catalog runs
		// perfectly and simply records nothing, so the provenance assertions fail against a healthy
		// service — the emit is the thing under test, not a side effect of it.
		//
		// LINEAGE IS REACHED BY ITS SESSION HOSTNAME, not a binding: lineage keeps its tables in this
		// catalog and binds it, and a binding cannot run both ways. The hostname must resolve when the
		// first write emits, and the emit is BEST-EFFORT, so a target that does not resolve is
		// swallowed exactly as designed: the only symptom is a provenance assertion finding no author.
		writers, err := json.Marshal(prov.Writers)
		if err != nil {
			return nil, err
		}
		c = c.
			WithEnvVariable("LANCE_LINEAGE_EMIT_ENABLED", "true").
			WithEnvVariable("LANCE_LINEAGE_URL", "http://"+prov.LineageHost+":8000/api/v1/lineage").
			WithEnvVariable("LANCE_PROVENANCE_NAMESPACE", provenanceNamespace).
			WithEnvVariable("LANCE_PROVENANCE_WRITERS", string(writers))
	}
	return c.WithExposedPort(2333).AsService(), nil
}

// lineageToken mints the token lineage presents to the catalog: a password grant for `lineageIdentity` against the
// lane's Dex. Answers the token file and the principal it proves, which the catalog lists as a provenance writer: the
// connector's idp id (`laneConnectors`) and the user id Dex reports in `federated_claims`, read from the token because
// the catalog that would answer `/management/v1/me` boots with this very list.
//
// DEX MUST STAY UP FROM THE MINT TO THE LAST CHECK. Its signing keys live in memory, so a Dex that stops when
// this step drops its last reference comes back with new keys, and the catalog then refuses the minted token
// as signed by no key it can fetch. The caller starts Dex explicitly before minting.
func (m *Rask) lineageToken(ctx context.Context, src *dagger.Directory, dex *dagger.Service) (*dagger.File, string, error) {
	minted := m.base(src).
		WithServiceBinding("dex", dex).
		WithExec([]string{"uv", "run", "--no-sync", "python", "-c", `
import base64, json, pathlib, time, urllib.parse, urllib.request
form = urllib.parse.urlencode({"grant_type": "password", "client_id": "rask-web", "username": "` + lineageIdentity + `", "password": "password", "scope": "openid email federated:id audience:server:client_id:rask-lakehouse"}).encode()
for _ in range(60):
    try:
        body = json.load(urllib.request.urlopen("http://dex:5556/dex/token", form, timeout=5))
        break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit("dex never minted lineage's token")
token = body["id_token"]
claims = token.split(".")[1]
claims += "=" * (-len(claims) % 4)
out = pathlib.Path("/out")
out.mkdir(exist_ok=True)
(out / "token").write_text(token)
(out / "sub").write_text("` + laneIDP + `~" + json.loads(base64.urlsafe_b64decode(claims))["federated_claims"]["user_id"])
`})
	sub, err := minted.File("/out/sub").Contents(ctx)
	if err != nil {
		return nil, "", err
	}
	return minted.File("/out/token"), sub, nil
}

// lineageService is the lineage service, keeping its feed and edge list as tables of `catalog`'s provenance
// namespace (ADR 0158) and presenting `token` to it. It answers on the session hostname `host`, so the catalog
// it binds can reach it back without a binding cycle; the caller starts it.
func (m *Rask) lineageService(src *dagger.Directory, catalog *dagger.Service, token *dagger.File, host string) *dagger.Service {
	return m.Image(src, "rest-catalog", "", "", "", nil).
		WithServiceBinding("catalog", catalog).
		// Lineage calls `assert_authentication_configured` exactly as the catalog does, and refuses to
		// boot unless a posture is named. The authorization under test in these lanes is the CATALOG's;
		// lineage's own doors are open here.
		WithEnvVariable("RASK_INSECURE_ALLOW_UNAUTHENTICATED", "true").
		WithEnvVariable("LINEAGE_CATALOG_URL", "http://catalog:2333").
		WithEnvVariable("LINEAGE_PROVENANCE_NAMESPACE", provenanceNamespace).
		WithFile(lineageTokenPath, token).
		WithEnvVariable("RASK_CATALOG_IDENTITY_TOKEN_FILE", lineageTokenPath).
		WithExposedPort(8000).
		// The SAME image as the catalog, run with a different command; building it twice would let the
		// two drift.
		//
		// SHARING THE IMAGE MEANS INHERITING ITS HEALTHCHECK, and `.docker/rest-catalog.dockerfile`
		// bakes one that connects to 127.0.0.1:2333 — the CATALOG's port. Nothing listens on 2333
		// here, so the probe fails, Dagger refuses to start the service, and the run dies with
		// `health check errored` plus a `ConnectionRefusedError` naming a port this service was never
		// supposed to serve. Overridden rather than dropped. The start period covers lineage's lifespan,
		// which retries creating its tables through the catalog for up to 120 s.
		WithDockerHealthcheck(
			[]string{"python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/livez').read()"},
			dagger.ContainerWithDockerHealthcheckOpts{Interval: "5s", Timeout: "3s", StartPeriod: "150s", Retries: 20},
		).
		WithDefaultArgs([]string{"uvicorn", "lineage.main:app", "--host", "0.0.0.0", "--port", "8000"}).
		AsService().
		WithHostname(host)
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
	catalog, err := m.catalogService(src, stack, true, nil)
	if err != nil {
		return "", err
	}

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
		// `rask-web` as `public: true` — a public client, which rejects a client_secret with
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
// governance) to assemble the same thing. Everything it needs beyond the auth stack is the lineage
// service, which keeps its tables in the catalog it is wired to, so it is the auth stack plus one service.
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
	if stack.Dex, err = stack.Dex.Start(ctx); err != nil {
		return "", err
	}
	token, lineageSub, err := m.lineageToken(ctx, src, stack.Dex)
	if err != nil {
		return "", err
	}
	catalog, err := m.catalogService(src, stack, true, &provenanceWiring{LineageHost: "lineage-api", Writers: []string{lineageSub}})
	if err != nil {
		return "", err
	}
	// Started rather than bound: the catalog reaches it by hostname, and the test below does too.
	if _, err := m.lineageService(src, catalog, token, "lineage-api").Start(ctx); err != nil {
		return "", err
	}

	return m.base(src).
		WithServiceBinding("catalog", catalog).
		WithServiceBinding("dex", stack.Dex).
		WithServiceBinding("openfga", stack.OpenFGA).
		WithEnvVariable("LANCE_E2E_AUTH_SERVER", "http://catalog:2333").
		WithEnvVariable("LANCE_E2E_LINEAGE_URL", "http://lineage-api:8000").
		WithEnvVariable("LANCE_E2E_DEX", "http://dex:5556/dex").
		// THE OUTSIDER IS THIS STACK'S, not the live estate's, and the default is not portable between
		// them. `topology.OUTSIDER` names `publisher@rask.internal` because on k3s `team:eng` is bound
		// to `project:acme`, which makes bob a project admin and therefore a dishonest 403 subject.
		// Neither binding exists here — this stack seeds no teams and no projects, and its Dex
		// (`.docker/dex.config.yaml`) knows exactly alice and bob. Measured 2026-09-24: the inherited
		// default failed both outsider legs at the TOKEN GRANT (`access_denied: Invalid username or
		// password`), so the lane reported an authorization result it had never reached.
		WithEnvVariable("LANCE_E2E_OUTSIDER", "bob@example.com").
		// The rename leg reads the store itself: a tuple left on a retired name is what the reconciler reports as a
		// ghost table, and no catalog door lists a store's raw tuples to a tenant.
		WithEnvVariable("LANCE_E2E_FGA", "http://openfga:8080").
		// Wait for BOTH, not just the ports: uvicorn accepts before either lifespan is built, and the
		// lineage service additionally has to create its tables through the catalog. A racing first
		// request reads as a wrong answer rather than as an unready service.
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
			// `--require-live`: a skip in a lane that stood its stack up is a test that ran nowhere, so it fails the lane.
			return c.WithExec([]string{"sh", "-c", "LANCE_E2E_DEX_SECRET= uv run --no-sync pytest tests/e2e-py/test_governance_e2e.py -v -rs --require-live"})
		}).
		Stdout(ctx)
}
