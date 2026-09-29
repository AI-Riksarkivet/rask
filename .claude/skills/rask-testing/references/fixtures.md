# rask test fixtures

Each section names the live file to copy from and the non-obvious parts it relies on. Read the file
itself for the current code; this reference does not restate it. The generic mechanics behind each
(the lifespan, an app built at import, a live uvicorn on port 0) are in `fastapi` →
`references/testing.md`.

## Contents

- A service app, with its lifespan
- Real pylance on `tmp_path`
- A moto server that pylance can reach
- A live uvicorn socket
- NATS, locally, through Dagger
- The Ray job client
- The chart

## A service app, with its lifespan

Every service builds `app` at module import except ingest, whose `ingest.create_app()` is called per
test (`services/ingest/tests/test_ingest_api.py`). Pick the fixture by how settings reach the app.

**Settings through an `lru_cache`d getter**: `get_settings()` in catalog, lineage, maintenance and
medallion; `get_<service>_settings()` in annotator, search and viewer. Copy
`tests/integration/conftest.py::real_ns_client`. The order is load-bearing:

1. `monkeypatch.setenv` every variable the service needs to build and boot. For the catalog that
   includes both S3 keys, even on the `dir` backend: `Settings` requires `LANCE_S3_ACCESS_KEY_ID` at
   construction, and the lifespan's `consume_dapr_secrets` refuses to boot with an empty
   `LANCE_S3_SECRET_ACCESS_KEY` while `secrets_from_dapr` is off.
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

**Settings read once at import** (`make_service_app`'s `build_settings()` in compute, controlplane,
flows and notifications; `build_gateway_settings()` in the gateway):

- Set the env the whole directory needs around the first import, in its `conftest.py`, then hand it
  back. Copy `services/controlplane/tests/conftest.py`. Env left set leaks into every other testpath,
  because pytest collects all of them before it runs any test, and nothing checks for the leak: the
  suite stays green while a later module builds its app from the wrong values.
- A test that needs another value sets it with `monkeypatch.setenv` and calls
  `importlib.reload(<package>)` (`services/gateway/tests/test_notifications_proxy_shapes.py`).
- Code in `service_kit` itself builds a synthetic app with `make_service_app(..., settings=...)`
  (`packages/service-kit/tests/test_settings_are_injectable.py`).
- compute, controlplane and notifications also keep `lru_cache`d service getters
  (`get_compute_settings`, `get_controlplane_settings`, `get_notifications_settings`,
  `get_ingress_settings`). A test that changes one of their values clears that getter before and after,
  as `services/notifications/tests/conftest.py::_notifications_env` does.

**Authorization.** `service_kit.governed.deps.make_auth_deps` builds each service's `current_subject`
and `get_checker`. Override those callables: `security._deps.current_subject` and
`security._deps.get_checker` on the service's security module
(`services/controlplane/tests/test_projects_are_gated.py`; the flows, notifications and search suites
do the same), or the re-exported names where a service exports them (`annotator.api.security`).

**Real OTLP exporters.** `packages/service-kit/tests/conftest.py` pins required env from the ambient
value (so a developer's own setting still wins) and caps `OTEL_EXPORTER_OTLP_TIMEOUT` (measured on that
suite: 10.19 s, and 3.08 s with the cap). A new suite that builds real exporters needs the same cap.

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
- Plain `storage`/boto3 code does not need the server. `with mock_aws():` plus a boto3 client that
  seeds the bucket is enough (`packages/storage/tests/test_storage.py`).

## A live uvicorn socket

Use one when the claim is about the wire. The rask example is
`tests/unit/test_bronze_is_governed_end_to_end.py` (`uvicorn.Config(app, port=0, lifespan="on")` on a
thread, with a bounded wait for `server.started`). Why it matters here:
`tests/unit/test_search_spec_binding.py`'s docstring records a query-model binding that `TestClient`
accepted and a live uvicorn on fastapi 0.140.13 answered with 422 on every call.

## NATS, locally, through Dagger

CI binds a real NATS to the pytest container (`.dagger/test.go`). The ingest queue suites
(`services/ingest/tests/test_worker_queue.py`, `test_run_chain.py`) read `RASK_NATS_URL` and **skip**
when nothing answers there, so a local run with no broker reports skips, not failures. Bring one up as
a Dagger service at the image `.dagger/test.go` pins. `up` holds the foreground, so run it in the
background or in a second shell:

```bash
IMG=$(grep -oP 'natsImage = "\K[^"]+' .dagger/test.go)
dagger core container from --address="$IMG" with-exposed-port --port=4222 \
  with-default-args --args=nats-server,--jetstream as-service up --ports=14222:4222
```

```bash
RASK_NATS_URL=nats://127.0.0.1:14222 uv run pytest services/ingest/tests/test_worker_queue.py services/ingest/tests/test_run_chain.py -rs
```

Verified 2026-09-29 against `nats:2.14.2-alpine`: 10 passed, no skips (without the broker,
`test_worker_queue.py` alone reports 4 skipped).

## The Ray job client

Copy `packages/ray-kit/tests/test_dashboard_bounds.py`: a small class that implements only the methods
the code under test calls (there, `list_jobs`), returned through `cast(JobSubmissionClient, fake)`. No
Ray runtime starts, and the function under test keeps its real parameter type without a
`# type: ignore`. Nothing checks the cast against Ray (`writing-python` → `references/testing.md`
§ Doubles).

## The chart

`from tests.unit.chart_render import DEFAULT_ARGS, render`. `render(*flags)` returns the parsed
manifests and is cached per verbatim flag tuple, once per xdist worker. `render_text(*flags)` returns
Helm's raw stdout; use it for size budgets, because parsing drops comments. The result is a tuple
because it is shared: never mutate it. Render through this module rather than a `subprocess` helm of
your own.

`render_text` calls `pytest.skip("helm not available")` when helm is absent: the missing-tool skip
SKILL.md says not to copy. It finds helm on `PATH` (`make k3s-install` installs it) or at
`.localbin/helm`; the CI lane installs its own.
