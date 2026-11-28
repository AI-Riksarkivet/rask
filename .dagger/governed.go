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
	dexImage      = "ghcr.io/dexidp/dex:v2.46.1"
	fgaPostgresIm = "postgres:16-alpine"
)

// The identity the lanes' lineage service presents to the catalog, and the namespace the catalog keeps for
// provenance (ADR 0158), a namespace of the estate's own warehouse `lance_catalog`. In the chart lineage presents its
// projected service-account token; these lanes run no Kubernetes, so the lane's own Dex mints lineage a token for a user
// that exists for that purpose alone, and the catalog lists that user's subject in `LANCE_PROVENANCE_WRITERS` exactly as
// the chart lists the account's.
const (
	lineageIdentity     = "lineage@rask.internal"
	provenanceNamespace = "lance_catalog$provenance"
	lineageTokenPath    = "/var/run/rask/catalog-token"
	// laneConnectors is the catalog's `RASK_OIDC_CONNECTORS` in these lanes ([[LH-063]]): the lane Dex's one connector,
	// its password database `local`, is the idp id `laneIDP`, which also names the lane Dex's clients (`RASK_OIDC_IDP_ID`), as
	// the chart maps the development Dex's.
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

// governedStack is the platform the governance chain's catalog runs on: an object store with the catalog's
// bucket, the catalog's database, OpenFGA on its own Postgres, and Dex.
//
// ORDERING IS EXPLICIT AND EACH STEP EARNED ITS PLACE. Dagger has no "after this one-shot completed"
// condition on a binding, so a one-shot is sequenced by forcing it to evaluate. Two traps this already hit for real, both of
// which surface as somebody else's bug:
//
//   - Dagger services are REFERENCE-COUNTED, and this bit twice. A bootstrap that finishes drops its
//     dependency to zero references, which STOPS it. The bucket created in step one was gone by step
//     three and pylance reported `NoSuchBucket` as if the catalog were broken; then the OpenFGA
//     migration was rolled back the same way and `/healthz` answered 500 as if the server were.
//     Every backing service with state keeps a cache volume for that reason.
//     The tell is that the symptom always names the wrong component.
//   - OpenFGA needs `migrate` before `run`, and skipping it does not fail at startup: the server
//     comes up and the first authorization call fails against absent tables.
type governedStack struct {
	Store   *dagger.Service
	OpenFGA *dagger.Service
	Dex     *dagger.Service
	// Database is the catalog's own database, provisioned and migrated as a release does it.
	Database *catalogDatabase
	// StoreToken is the catalog's ServiceAccount token the store's STS exchanges (`laneIdentity`).
	StoreToken *dagger.File
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
	identity := m.laneIdentity(src)
	store := trustingLaneIssuer(dag.Container().
		From(rustfsImage).
		WithEnvVariable("RUSTFS_ACCESS_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_SECRET_KEY", "rustfsadmin").
		WithEnvVariable("RUSTFS_ADDRESS", ":9000").
		WithMountedCache("/data", dag.CacheVolume("rask-governed-store-"+run), dagger.ContainerWithMountedCacheOpts{Owner: rustfsUser}).
		WithExposedPort(9000), identity.Issuer).
		AsService()

	// The catalog's bucket and the catalog's canned policy, as the chart's provisioning hook makes them.
	if err := m.provisionLaneStore(ctx, src, store, "store"); err != nil {
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
	// file not found in $PATH`). The wait is its own step and `Sync` orders it.
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

	database, err := m.catalogDatabase(ctx, src)
	if err != nil {
		return nil, err
	}

	return &governedStack{Store: store, OpenFGA: openfga, Dex: dex, Database: database, StoreToken: identity.CatalogToken}, nil
}

// provenanceWiring is what a catalog needs to announce its writes to lineage and keep lineage's tables: the bus it
// publishes each announcement on, and the subjects that may write the provenance namespace.
type provenanceWiring struct {
	Bus     *dagger.Service
	Writers []string
}

// The lane's bus keeps lineage's events on the stream and hands them to the durable consumer the chart's stream Job
// declares on the estate (`chart/templates/nats-stream-job.yaml`, `lance.busConsumers`): lineage binds to its consumer
// and creates none, so the lane declares both before either service starts.
const (
	laneBusURL          = "nats://bus:4222"
	laneBusTokenPath    = "/var/run/rask/bus-token"
	laneLineageStream   = `{"name":"LINEAGE","subjects":["lineage.events.>"],"retention":"limits","max_consumers":-1,"max_msgs":-1,"max_bytes":-1,"max_msgs_per_subject":-1,"max_msg_size":-1,"max_age":86400000000000,"storage":"file","discard":"old","num_replicas":1,"duplicate_window":120000000000,"deny_delete":true,"deny_purge":true,"allow_rollup_hdrs":false,"allow_direct":true}`
	laneLineageConsumer = `{"durable_name":"lineage-ingest","ack_policy":"explicit","ack_wait":300000000000,"max_deliver":-1,"filter_subject":"lineage.events.v1","deliver_policy":"all","replay_policy":"instant","max_ack_pending":64,"max_waiting":512}`
)

// laneBus is a NATS JetStream server holding lineage's stream and its durable consumer, started.
//
// IT ADMITS EVERY CONNECTION. Which user may publish or pull what, the token a pod presents and the session the signer
// grants it are the nats-auth lane's claim, on the permission table the chart renders; this lane's claim is that an
// announcement the catalog publishes is what lineage records.
func (m *Rask) laneBus(ctx context.Context) (*dagger.Service, error) {
	bus, err := dag.Container().
		From(natsImage).
		WithExposedPort(4222).
		WithDefaultArgs([]string{"nats-server", "-js"}).
		AsService().
		Start(ctx)
	if err != nil {
		return nil, err
	}
	_, err = dag.Container().
		From(natsBoxImage).
		WithServiceBinding("bus", bus).
		WithNewFile("/tmp/stream.json", laneLineageStream).
		WithNewFile("/tmp/consumer.json", laneLineageConsumer).
		WithExec([]string{"nats", "--server", laneBusURL, "stream", "add", "--config", "/tmp/stream.json"}).
		WithExec([]string{"nats", "--server", laneBusURL, "consumer", "add", "LINEAGE", "lineage-ingest", "--config", "/tmp/consumer.json"}).
		Sync(ctx)
	if err != nil {
		return nil, err
	}
	return bus, nil
}

// onLaneBus names `ctr` the lane's bus as the chart names a pod its own: the address, the user it connects as, and a
// file holding the token it presents, which this bus does not read.
func onLaneBus(ctr *dagger.Container, bus *dagger.Service, user string) *dagger.Container {
	return ctr.
		WithServiceBinding("bus", bus).
		WithNewFile(laneBusTokenPath, "lane").
		WithEnvVariable("RASK_BUS_URL", laneBusURL).
		WithEnvVariable("RASK_BUS_USER", user).
		WithEnvVariable("RASK_BUS_TOKEN_FILE", laneBusTokenPath)
}

// catalogService builds the REST catalog from its own dockerfile and points it at a governed stack: its database, OIDC
// against the stack's Dex, OpenFGA enforced, and every committed write announced on the bus `prov` names.
//
// THE EMISSION IS ON, as the chart renders it (`LANCE_LINEAGE_EMIT_ENABLED`). Without it the catalog runs perfectly and
// simply records nothing, so the provenance assertions fail against a healthy service: the emit is the thing under
// test, not a side effect of it.
func (m *Rask) catalogService(src *dagger.Directory, stack *governedStack, prov provenanceWiring) (*dagger.Service, error) {
	writers, err := json.Marshal(prov.Writers)
	if err != nil {
		return nil, err
	}
	return withLaneStoreSession(stack.Database.attach(onLaneBus(m.Image(src, "rest-catalog", "", "", "", nil, ""), prov.Bus, "catalog")).
		WithServiceBinding("store", stack.Store), stack.StoreToken, "store").
		WithEnvVariable("LANCE_REST_IMPL", "dir").
		WithEnvVariable("LANCE_REST_ROOT", "s3://lance-catalog").
		WithEnvVariable("LANCE_S3_ENDPOINT", "http://store:9000").
		WithEnvVariable("LANCE_S3_REGION", "us-east-1").
		WithEnvVariable("LANCE_S3_ALLOW_HTTP", "true").
		WithEnvVariable("LANCE_S3_VIRTUAL_HOSTED", "false").
		WithServiceBinding("dex", stack.Dex).
		WithServiceBinding("openfga", stack.OpenFGA).
		WithEnvVariable("RASK_OIDC_ENABLED", "true").
		WithEnvVariable("RASK_OIDC_ISSUER", "http://dex:5556/dex").
		WithEnvVariable("RASK_OIDC_AUDIENCE", "rask-lakehouse").
		WithEnvVariable("RASK_OIDC_IDP_ID", laneIDP).
		WithEnvVariable("RASK_OIDC_CONNECTORS", laneConnectors).
		// Dex serves discovery over http here. Dev only — the chart never sets this.
		WithEnvVariable("RASK_OIDC_ALLOW_INSECURE", "true").
		WithEnvVariable("RASK_FGA_ENABLED", "true").
		WithEnvVariable("RASK_FGA_API_URL", "http://openfga:8080").
		WithEnvVariable("LANCE_LINEAGE_EMIT_ENABLED", "true").
		WithEnvVariable("LANCE_PROVENANCE_NAMESPACE", provenanceNamespace).
		WithEnvVariable("LANCE_PROVENANCE_WRITERS", string(writers)).
		WithExposedPort(2333).
		AsService(), nil
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
form = urllib.parse.urlencode({"grant_type": "password", "client_id": "rask-web", "client_secret": "lance-catalog-secret", "username": "` + lineageIdentity + `", "password": "password", "scope": "openid email federated:id audience:server:client_id:rask-lakehouse"}).encode()
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
// namespace (ADR 0158) and presenting `token` to it, and takes the catalog's announcements from its consumer on `bus`.
// It answers on the session hostname `host`, where the test reaches it; the caller starts it.
func (m *Rask) lineageService(src *dagger.Directory, catalog *dagger.Service, bus *dagger.Service, token *dagger.File, host string) *dagger.Service {
	return onLaneBus(m.Image(src, "rest-catalog", "", "", "", nil, ""), bus, "lineage").
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

// GovernanceChain proves the dataops loop end to end: authorization, provenance AUTHORSHIP, and
// derivation lineage — with real Dex id_tokens and not one hand-written tuple.
//
// Everything it needs beyond the governed stack is the lineage service, which keeps its tables in the
// catalog it is wired to, so it is the governed stack plus one service.
//
// The catalog runs with emission ON, as the chart renders it (`catalogService`): authorization alone passes with
// emission off, so a run without it would prove the authz half and skip the provenance half.
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
	bus, err := m.laneBus(ctx)
	if err != nil {
		return "", err
	}
	catalog, err := m.catalogService(src, stack, provenanceWiring{Bus: bus, Writers: []string{lineageSub}})
	if err != nil {
		return "", err
	}
	// Started rather than bound: the test below reaches it by hostname.
	if _, err := m.lineageService(src, catalog, bus, token, "lineage-api").Start(ctx); err != nil {
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
		// them. `topology.OUTSIDER` names `publisher@rask.internal` because on k3s the seed assigns bob
		// to acme's admins role, which makes him a project admin and therefore a dishonest 403 subject.
		// Neither exists here — this stack seeds no roles and no projects, and its Dex
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
		// The estate's first owner, which the chart's bootstrap hook seeds on a release and nothing seeds here: a
		// namespace is created by a caller holding can_create_namespace on its warehouse, the estate's own included.
		// After the wait above, since the store exists once the catalog has booted.
		WithExec([]string{"sh", "-c", "uv run --no-sync python scripts/lane_estate_owner.py " + laneIDP + "~alice"}).
		// The suite and the demo sign in as `rask-web`, a confidential client in `.docker/dex.config.yaml` as in the Dex
		// fixture, with its secret: the suite's `dex-secret` credential and the demo's `DEX_CLIENT_SECRET` both default to
		// the dev Dex's `lance-catalog-secret`, so neither is set here. Dagger's `WithEnvVariable(name, "")` leaves a
		// variable UNSET rather than empty (measured: the container reported `'<UNSET>'`), which is why no empty value is
		// passed that way either.
		With(func(c *dagger.Container) *dagger.Container {
			// `DEMO=1` on the retired script ran the narrated walkthrough instead of the assertions
			// against the SAME stack. Keeping it as a flag preserves that without a second function —
			// and a demo that stands up its own stack is how a demo drifts from what the tests prove.
			if demo {
				return c.WithExec([]string{"sh", "-c", "uv run --no-sync python scripts/governance_demo.py"})
			}
			// `--require-live`: a skip in a lane that stood its stack up is a test that ran nowhere, so it fails the lane.
			return c.WithExec([]string{"sh", "-c", "uv run --no-sync pytest tests/e2e-py/test_governance_e2e.py -v -rs --require-live"})
		}).
		Stdout(ctx)
}
