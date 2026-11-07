# rask test fixtures

Each section names the live file to copy from and the non-obvious parts it relies on. Read the file
itself for the current code; this reference does not restate it. The generic mechanics behind each
(the lifespan, an app built at import, a live uvicorn on port 0) are in `fastapi` →
`references/testing.md`.

## Contents

- A service app, with its lifespan
- The catalog with authorization on
- Real pylance on `tmp_path`
- A moto server that pylance can reach
- A live uvicorn socket
- NATS, locally, through Dagger
- The chart

## A service app, with its lifespan

Every service builds `app` at module import. Pick the fixture by how settings reach the app.

**Settings through an `lru_cache`d getter**: `get_settings()` in catalog, lineage and maintenance. Copy
`tests/integration/conftest.py::real_ns_client`. The order is load-bearing:

1. `monkeypatch.setenv` every variable the service needs to build and boot. For the catalog that
   includes both S3 keys, even on the `dir` backend: with no projected token
   (`LANCE_S3_WEB_IDENTITY_TOKEN_FILE`), `Settings` requires `LANCE_S3_ACCESS_KEY_ID` at construction and
   the lifespan's `require_env_secret` refuses to boot with an empty `LANCE_S3_SECRET_ACCESS_KEY`. With
   `LANCE_SECRETS_FROM_DAPR` on, the store identity is that projected token exchanged at STS
   (`LANCE_S3_STS_ENDPOINT`), a key pair beside it is refused, and the sidecar's secret API serves only
   the signing keys: answer it with respx, and the STS exchange and the store with the moto server of
   `services/catalog/tests/conftest.py::two_stores`
   (`services/catalog/tests/test_the_catalog_signs_for_the_person_it_authenticated.py`, its `boot` fixture).
2. `.cache_clear()` on that getter, or the previous test's values survive.
3. Import the app **inside** the fixture.
4. Set `app.dependency_overrides[...]` on the registered callables (`real_ns_client` overrides
   `get_namespace` and `get_storage_options`).
5. `with TestClient(app) as client:` so that the lifespan runs.
6. Teardown: `app.dependency_overrides.clear()` and `.cache_clear()` again.

`cache_clear()` reaches only what is read per request. What the module reads at import stays as the
first importer left it: the module-level `_settings` in `services/catalog/src/catalog/main.py` fixes
the docs and audit switches, the control-relay binding, the body cap and the write-concurrency cap. A
test of one of those needs `importlib.reload`.

The sibling fixture `::client` has the same order over a `MagicMock(spec=LanceNamespace)`. It serves
only claims about routing, identifier parsing and error mapping.

**Settings read once at import** (a route mounted from the environment, a module-level `_settings`):

- Set the env a test needs through a function-scoped `monkeypatch`, as
  `services/maintenance/tests/conftest.py::catalog` does. Env left set leaks into every other testpath,
  because pytest collects all of them before it runs any test, and nothing checks for the leak: the
  suite stays green while a later module builds its app from the wrong values.
- A test that needs another value sets it with `monkeypatch.setenv`, clears the `lru_cache`d getter and calls
  `importlib.reload(<module>)`, clearing the getter again at teardown
  (`tests/integration/test_a_commit_expires_its_table_once_enough_versions_are_reclaimable.py::maintenance`).
- Code in `lakehouse` itself builds a bare `FastAPI()` carrying the piece under test
  (`packages/lakehouse/tests/test_shared_body_cap.py`).

**Authorization.** `lakehouse.governed.deps.make_auth_deps` builds each service's `current_subject`
and `get_checker`. Override those callables: `security._deps.current_subject` and
`security._deps.get_checker` on the service's security module
(`tests/unit/test_auth_deps_resolve.py` resolves them).

**Real OTLP exporters.** No suite builds one. A suite that does caps `OTEL_EXPORTER_OTLP_TIMEOUT` with a
function-scoped `monkeypatch` from the ambient value (so a developer's own setting still wins): an exporter aimed at an
endpoint nothing serves waits out the SDK's 10 s default at process exit (measured on the platform library's suite: 10.19 s,
and 3.08 s with a 1 s cap).

## The catalog with authorization on

Copy `services/catalog/tests/conftest.py::governed_catalog`. The app's own lifespan builds the deployed verifier
against the root conftest's `dex_issuer` and the deployed OpenFGA client against `openfga_store`
(`RASK_FGA_STORE_ID`/`RASK_FGA_MODEL_ID` pinned to it), so nothing is overridden. `bearer("alice")` signs alice in
through the `local` connector, mapped to idp `dex`, and `person("alice")` is the OpenFGA user her tuples name. Arrange
through the doors (a create seeds the creator's tuples, `/management/v1/<type>/{id}/access/grant` grants), and seed
only what no door writes with `store.write(...)`. Assert the outcome a caller sees, a status and the spec's `code`,
never which relation was asked. A model edit takes effect once `model.json` is regenerated
(`fga model transform`), since that is what `fga.provision` writes. `test_a_role_grant_stays_inside_its_tenant.py`
and `test_an_absent_object_is_not_found_rather_than_forbidden.py` are the examples.

## Real pylance on `tmp_path`

- **A catalog app over a real `dir` namespace**: `tests/integration/conftest.py::real_ns_client`
  (`LANCE_REST_IMPL=dir`, `LANCE_REST_ROOT=<tmp_path>`, and `get_namespace` overridden with
  `lance_namespace.connect("dir", {"root": ...})`). Every create goes through the real 2.2 +
  stable-row-ids write.
- **An unusual manifest, written by Lance**: `tests/unit/conftest.py::overlay_dataset`, a factory
  fixture that builds a dataset with data-overlay files (manifest feature flag 64) through
  `LanceFileWriter` and a `LanceOperation.DataOverlay` commit. Build other odd manifests the same way,
  through Lance's public API and not by hand, so the test agrees with what Lance writes.

## A moto server that pylance can reach

Copy `tests/integration/test_moto_s3.py::moto_endpoint`. `moto.server.ThreadedMotoServer(port=0)`
serves S3 over HTTP, and the catalog is pointed at it with `LANCE_REST_ROOT=s3://<bucket>` and
`LANCE_S3_ENDPOINT=<url>` (the module's `_client` helper also pins `RASK_OIDC_ENABLED=false` and
`RASK_FGA_ENABLED=false`).

- `mock_aws()` is not enough here. It patches botocore, and Lance's and Arrow's native S3 clients
  never go through botocore.
- The fixture is `scope="module"`: one server and one bucket per module. Each test isolates itself by
  namespace name, not by bucket.
- Plain `lakehouse.storage`/boto3 code does not need the server. `with mock_aws():` plus a boto3 client that
  seeds the bucket is enough (`packages/lakehouse/tests/test_storage.py`).
- To count what a door asks the store (how many listings of a table's `_versions/` an open costs), put
  `tests/integration/store_requests.counted(<moto url>)` between the catalog and the server and point
  `LANCE_S3_ENDPOINT` at its `url`: it forwards every request unchanged, chunked bodies included, and
  `taken()` answers what each asked. Lance's and pyarrow's native clients go through no Python hook, so a
  count is taken on the wire (`tests/integration/test_a_door_judges_whether_a_table_is_only_declared_by_its_storage.py`).
- To fail one read a door makes, the same proxy's `answering_unavailable(<predicate over StoreRequest>)` answers each
  matching request 503 `ServiceUnavailable` for the life of the block and forwards the rest: pylance then raises the
  object-store outage `lakehouse.outage` reads. A client that must keep reaching the store is pointed past the
  proxy with `LANCE_S3_VENDED_ENDPOINT`. The catalog suite imports the helper as `tests.integration.store_requests`, which
  its conftest makes resolvable by putting the repository root on `sys.path`
  (`services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py`, [[LH-545]]).

## A live uvicorn socket

Use one when the claim is about the wire. The rask example is
`tests/integration/test_a_catalog_reader_reads_a_whole_table_in_bounded_pages.py` (`uvicorn.Config(app, port=0, lifespan="on")` on a
thread, with a bounded wait for `server.started`). Why it matters here: a query-model binding can pass under `TestClient` while a live uvicorn answers 422 on every
call (measured on fastapi 0.140.13).

## NATS, locally, through Dagger

No offline suite needs a broker: the bus contract is proved by the `nats-auth` lane
(`tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py`), which brings its own
nats-server. For an ad-hoc broker, bring one up as a Dagger service, never a docker container.
`up` holds the foreground, so run it in the background or in a second shell:

```bash
IMG=$(grep -ohP 'natsImage = "\K[^"]+' .dagger/*.go)
dagger core container from --address="$IMG" with-exposed-port --port=4222 \
  with-default-args --args=nats-server,--jetstream as-service up --ports=14222:4222
```

## The chart

`from tests.unit.chart_render import DEFAULT_ARGS, render`. `render(*flags)` returns the parsed
manifests and is cached per verbatim flag tuple, once per xdist worker. `render_text(*flags)` returns
Helm's raw stdout; use it for size budgets, because parsing drops comments. The result is a tuple
because it is shared: never mutate it. Render through this module rather than a `subprocess` helm of
your own.

`render_text` calls `pytest.skip("helm not available")` when helm is absent: the missing-tool skip
SKILL.md says not to copy. It finds helm on `PATH` (`make k3s-install` installs it) or at
`.localbin/helm`; the CI lane installs its own.
