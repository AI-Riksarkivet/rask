---
name: rask-testing
description: "rask's pytest wiring, its real-collaborator fixtures, and the row rule for how many tests a change may add: the globbed testpaths, importlib mode, the e2e/slow markers, which lane runs what (make test, check-fast, dagger call test, the runner suites), what the root conftest already does, and what stands in for Lance, S3, NATS, Ray, OpenFGA and a service app. Use when writing, changing, deleting or auditing any test in rask, when a new tests/ directory or runner suite runs in no lane, or when a test needs a real collaborator."
---

# rask × pytest — wiring, fixtures, and the row rule

Load the generic skills first. This one repeats none of them:

- `writing-python` → `references/testing.md`: what a test is for, tiers (integration tests are the
  keepers), red, what is not a test, doubles, async, parametrize.
- `testing-python`: the suite as a whole, including the red-check recipe and pruning.
- `fastapi` → `references/testing.md`: the client that runs the lifespan, an app built at import,
  `dependency_overrides`.

What follows is only what is true of rask.

## The row rule

A change works a row — a GitHub issue on `AI-Riksarkivet/rask`, title `LH-243: …` (`docs/agents/issue-tracker.md`) — and each row states what it
*Closes when:*.

1. **Find the test that already drives the seam.** Test files are named per claim, not per module, so
   a file name will not lead you there. Search by symbol or route:
   `git grep -l '<symbol>' -- 'packages/*/tests/*' 'services/*/tests/*' 'runners/*/tests/*' tests`. A
   wildcard pathspec must match the whole path, so `'packages/*/tests'` matches nothing.
2. **At most one test per closes-when clause**, and it replaces the test of the same seam rather than
   sitting beside it.
3. **Red check** (`testing-python` § The red check), with `<src-root>` = `<member>/src` and the scope
   `<member>/tests tests/integration <the directories step 1 found> -m "not slow and not e2e" -n 16`.
4. **State the net test delta in the commit message**: `tests: +N −M`.

Done when: each closes-when clause names the one test that pins it, that test was red with the fix
reverted, and the commit message carries the delta.

## Where a test goes

| The claim needs                                   | Directory                                                                                       |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| One member's code, through its app or public API  | `packages/<m>/tests`, `services/<m>/tests`                                                      |
| Several members in one flow, or a fixture from `tests/integration/conftest.py` (`real_ns_client`) | `tests/integration`: a conftest's fixtures reach only the tests beneath it |
| The rendered chart, or a fixture's render         | `tests/unit`, through `tests/unit/chart_render.py` (`render_fixture` for `deploy/fixtures/<name>`) |
| A deployed stack                                  | `tests/e2e-py`, run by the e2e-stack lane unless it carries a marker the lane leaves out: § Wiring |
| A sealed runner's code                            | `runners/<name>/tests`, under that runner's own `pyproject.toml`, run by the Makefile's runner loop: § Wiring |

## Wiring

Read `[tool.pytest.ini_options]` in the root `pyproject.toml` for the current values. These are the
consequences it does not spell out.

- **`testpaths` is globbed.** `packages/*/tests` and `services/*/tests` enrol a new member's `tests/`
  the moment it exists; only a new top-level `tests/<x>/` needs adding by hand. Coverage's
  `source = ["services", "packages"]` with `[tool.coverage.report] include_namespace_packages` counts
  every member's `src` in the denominator with no edit.
- **The e2e-stack lane runs every `tests/e2e-py` module it can serve** (ADR 0167). Every offline lane
  deselects `e2e`; the lane's first session selects by the markers a module does NOT carry (`phase1`,
  `nats_auth`, `observability`, `edge`, `media_catalog`, each with its reason in
  `scripts/e2e_stack.sh`), under `--require-live`, so a new module runs on push the day it lands and one
  the lane cannot serve fails the lane. A module that needs what the lane does not stand up gets a
  per-suite marker registered in the root `pyproject.toml` with a `make e2e-<suite>` target (listed in
  `E2E_SUITES`), the lane's exclusion with its reason, and either the lane that runs it (`ci.yml`, a
  `.dagger/*.go` lane) or the reason it runs only against a deployed estate. A `make e2e-<suite>` target
  alone is not CI. `tests/unit/test_e2e_collection_gate.py` checks only that every
  `tests/e2e-py/test_*.py` collects.
  A change that deletes a suite deletes its marker, its target, its `E2E_SUITES` entry and every
  script line that names it: pytest exits 4 on a path that does not exist and 5 on a marker no test
  carries, and either kills the lane.
- **Phase 1's acceptance reads one drive.** A `phase1` module (one per criterion, XC-090) asserts on the
  bring-your-own scenario `tests/e2e-py/byo_scenario.py` drives once per session (`conftest.py`'s `byo_tenant`
  and `byo_run`), so a session running several modules reads one tenant and one run; add what a new criterion
  needs to `ScenarioRun` rather than driving again. A criterion whose claim is about the catalog's doors rather
  than the run (criterion 2's principal × door matrix, `test_the_catalog_is_correct.py`) makes its own objects in
  the shared tenant's warehouse, under names that do not carry the project id, so the other criteria read only what
  the scenario made. The bus is read through `tests/e2e-py/bus.py`, by sequence, with no consumer of its own.
  What every criterion reads to call the estate correct (each ref's history, the edge list, the feed's rows and the
  inboxes' about the drive, the DLQ and both outboxes) is `tests/e2e-py/correctness.py`, so a new criterion holds the
  estate to the same readings. The acceptance is the marker, not a file list: `make e2e-phase1` and the e2e-stack
  lane run `-m phase1`, so a module joins both by carrying it; the scenario's own module
  (`test_a_bring_your_own_pipeline_runs_end_to_end.py`) carries it beside `byo_pipeline`.
- **The soak reads a kept drive a day later.** A run passed `--keep-drive PATH` keeps the scenario's tenant when every
  test it ran passed, and records the drive at PATH (`tests/e2e-py/kept_drive.py`); a run passed `--soak PATH`
  resumes it, runs `test_the_estate_stays_correct_while_it_runs.py` (marker `soak`, deselected without `--soak`)
  once `LANCE_E2E_SOAK_SECONDS` (a day) have passed, and removes the tenant when it passes. The e2e-stack lane soaks
  at once (`LANCE_E2E_SOAK_SECONDS=0`) so keeping and resuming run on every push; the deployed estate soaks the day
  through `bash scripts/e2e_live.sh -m soak --require-live --soak PATH` or `make e2e-soak`.
- **A chaos drill runs only when the run asks for it.** A test marked `chaos` takes a dependency of the release
  down, so `tests/e2e-py/conftest.py` deselects it unless the run passes `--chaos`: a plain live drive, `-m phase1`
  included, runs none, and a run that asks for them runs them after every other test it selected
  (`tests/unit/test_e2e_collection_gate.py`). Criterion 5's drills (`test_the_estate_is_resilient.py`, XC-095)
  carry both `phase1` and `chaos`; the e2e-stack lane runs them with `--chaos`, and the deployed release only through
  an explicit `bash scripts/e2e_live.sh -m phase1 --require-live --chaos` or `make e2e-chaos`. A drill acts through
  `tests/e2e-py/chaos.py`, which takes each dependency down without destroying what it holds, and restores it in a
  `finally`.
- **A runner suite runs only from the Makefile.** No root testpath reaches `runners/`. `make test`
  and `make test-slow` loop over every `runners/*/tests`, running `uv run --frozen pytest` from inside
  that runner's directory, so a new `runners/<name>/tests` runs in both with no edit; `--frozen` needs
  the runner to commit its own `uv.lock`.
- **`--import-mode=importlib`.** The `from test_invariants import ...` lines in `tests/unit` resolve
  only because `tests/unit/conftest.py` inserts that directory into `sys.path`; don't copy them. A
  shared helper goes in a plain module, as `tests/unit/chart_render.py` does (`writing-python` →
  `references/testing.md` § Fixtures).
- **Markers.** `slow` means real models or a long runtime; `make test` and CI deselect it. `e2e`
  means a live stack: `tests/e2e-py/conftest.py` applies it to everything collected there, and each
  per-suite selector (`cas`, `nats_auth`, ...) rides on top of it. Neither `strict_markers` nor
  `--strict-markers` is configured, so pass `--strict-markers` when you add or use a marker (the whole
  suite, 6,040 tests, collects cleanly under it, measured 2026-09-29).
- **Async is strict.** No `asyncio_mode` is set, so pytest-asyncio runs in strict mode. The `anyio`
  plugin is loaded too; most async tests use `@pytest.mark.asyncio`, so write new ones that way.
- **Code-shape rules live in ruff and import-linter.** A banned name is a `TID251` entry in the root
  `[tool.ruff.lint.flake8-tidy-imports.banned-api]`, restated in `services/viewer`,
  whose nested map replaces the root one. A sanctioned use takes a line-level
  `# noqa: TID251`, and only a module that IS the seam takes a per-file ignore. A layer rule is a
  contract in `.importlinter`, run by `make lint-imports` (part of `make check`).

### Lanes

| Lane                                              | What it runs                                                                                                      |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `make test`                                       | (`make test`, `check-fast`, `test-slow` and `coverage` first export `.localbin/openfga` through Dagger, `make openfga`) `-m "not slow and not e2e"` at `-n 16` (`PYTEST_WORKERS=0` for one process), then every `runners/*/tests` suite with `-m "not slow"` |
| `make check-fast`                                 | `tests/unit tests/integration` at `-n 16`, with no marker filter. `--dist loadfile`, because three suites are parallel-unsafe at the file level (the `lance.audit` process-global logger, `configure_audit`'s level, the registry CAS markers) |
| `make test-slow`                                  | `-m "not e2e"`, then every runner suite unfiltered                                                                |
| `make coverage`                                   | `-m "not e2e and not slow" --cov`, on request only                                                                |
| CI: `dagger call test` (`.dagger/test.go`)        | the `make test` marker filter, serially, with `--timeout=300 --timeout-method=thread`, helm installed. **No runner suite** |
| CI: `dagger call auth-chain`, `dagger call governance-chain` | a real Dex, OpenFGA and catalog as Dagger services on RustFS (plus lineage for `governance-chain`), asserting through `scripts/auth_chain.sh` and `tests/e2e-py/test_governance_e2e.py` (under `--require-live`) respectively |
| CI: `dagger call rustfs-lifecycle`, `dagger call smoke-rustfs` | `tests/e2e-py/test_e2e.py` under `--require-live` against a catalog whose bytes are on RustFS, and `scripts/smoke_rustfs.py` through `packages/storage` |
| CI: `dagger call rustfs-provisioning` (= `make rustfs-provisioning`) | `tests/e2e-py/test_a_policy_the_store_refuses_fails_the_scoped_users_hook.py` (marker `rustfs_provisioning`) under `--require-live`: the scoped-users hook's `scripts/rustfs_admin.py` against a throwaway RustFS booted as the fixture boots it (root pair from files); the e2e-stack lane leaves the marker out, since it provisions IAM on the store it runs against |
| CI: `dagger call nats-auth` (= `make nats-auth`, ms-gates leg `nats-auth`) | `tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py` (marker `nats_auth`) under `--require-live`: the NATS fixture's nats-server in operator mode with test-only nsc keys, the permission table read off the render's dev seed, every user's call set, foreign and credential-less publishes refused, and one real daprd 1.18.1 per component shape, judged by the broker's log ([[XC-078]]) |
| CI: `e2e-stack` (= `make e2e-ci`) | against a stack the lane brings up, three sessions under `--require-live`: every `tests/e2e-py` module carrying none of the markers the lane leaves out, then Phase 1's acceptance (`-m phase1 --chaos --keep-drive`), then its soak at once (`-m soak`) |
| `make fga-test` (part of `make check`)            | the OpenFGA model's own `.fga.yaml` suite                                                                         |
| `make e2e-<suite>`, `make e2e-live`               | `tests/e2e-py` against a stack already deployed: one marker at the addresses you pass, or `-m e2e` against the k3s release |

A failure that appears under `make test` and not under `check-fast` points at a test that depends on
sharing a worker with the rest of its file.

### What the root `conftest.py` already does

Don't redo any of it in a suite. At import, before collection builds any app, it:

- strips the OTLP endpoint, header and protocol variables a harness injects (Dagger sets
  `OTEL_EXPORTER_OTLP_ENDPOINT` for its own telemetry), and strips them again once per session;
- sets `RASK_INSECURE_ALLOW_UNAUTHENTICATED=true` and `RASK_ALLOW_UNAUTHENTICATED_DAPR=true` with
  `setdefault`, so an app builds with auth off unless a test turns it on. A test of those guards
  removes the variable with `monkeypatch.delenv` or passes the value to the guard directly;
- makes bare `@respx.mock` strict (`assert_all_called`). A test whose unused routes are the point takes
  the `respx_allows_unused_routes` fixture.

Around every test, as autouse fixtures, it bounds the Dapr sidecar handshake to 1 s, clears Dapr's
process-global actor-proxy factory, gives the test a fresh cache of secret-store reads
(`service_kit.governed.secret_cache.STORE_READS`; a test that drives the TTL replaces it with one on its
own clock), and writes the projected service-account token files the catalog's and lineage's doors read.

On request it serves the identity and authorization plane for real: `sa_issuer` and `dex_issuer` (loopback issuers
that mint tokens), `openfga` (the pinned OpenFGA server in memory, one per xdist worker, failing the test when the
binary is missing) and `openfga_store` (a fresh store on it holding the bundled model, with `write()` for seeding
tuples as the chart's bootstrap hook does).

## What stands in for what

| Collaborator                  | In a test                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| A service app                 | the real app under `with TestClient(app)`, set up per the service's settings shape: `references/fixtures.md` § A service app |
| Lance / lance-namespace       | real pylance on `tmp_path`: `tests/integration/conftest.py::real_ns_client`, `tests/unit/conftest.py::overlay_dataset`. The `MagicMock(spec=LanceNamespace)` behind `::client` cannot see what Lance writes (a table landed at the wrong file version passes), so it serves only routing and error-mapping claims |
| S3 through boto3 / `storage`  | moto `mock_aws()`                                                                                           |
| S3 read by pylance or pyarrow | a moto server, `tests/integration/test_moto_s3.py::moto_endpoint`: `mock_aws()` patches botocore, which the native Lance and Arrow clients never go through |
| Outbound HTTPX                | `respx` (`services/flows/tests/test_routes.py`)                                                             |
| NATS JetStream                | a real broker through Dagger (`references/fixtures.md` § NATS); the `nats-auth` lane brings its own         |
| Ray job client                | a structural fake `cast` to `JobSubmissionClient`: `packages/ray-kit/tests/test_dashboard_bounds.py`        |
| OpenFGA, authz not the claim  | `RASK_FGA_ENABLED` / `RASK_OIDC_ENABLED` left at their default, off; with FGA off the checker allows everything. Where a route needs a named subject or a decision, override that service's `current_subject` / `get_checker` |
| OpenFGA, authz is the claim   | a real OpenFGA in the offline suite: root `conftest.py::openfga_store`, a store of the test's own on the pinned server, and for the catalog `services/catalog/tests/conftest.py::governed_catalog` (`references/fixtures.md` § The catalog with authorization on). Beyond one service: the model's `.fga.yaml` suite (`make fga-test`), a real Dex and OpenFGA via `dagger call auth-chain` / `governance-chain`, or on a deployed estate criterion 2's principal × door matrix (`tests/e2e-py/test_the_catalog_is_correct.py`). Fake the client only for a transport or fail-closed leg |
| The chart                     | `tests/unit/chart_render.py`: `references/fixtures.md` § The chart                                          |

## Skips and prose

- **Skips not to copy**: `pytest.skip("helm not available")` in `chart_render.render_text` and every
  file `git grep -l 'helm not available'` lists. CI provides helm,
  so the skip only hides the test on a developer box.
- **Shapes not to copy**: some `tests/unit` files still read the Makefile, `pyproject.toml`, source or
  prose instead of running code (4 of 371 parse an AST, measured 2026-09-29). Don't write a new test in
  their shape: a code-shape rule goes to ruff or an import-linter contract, and a behaviour claim
  drives the code.
- **Test docstrings and comments follow CLAUDE.md's comment rule**: rationale and provenance, never
  history. The `comment-history-gate` prek hook checks staged lines; `make comment-gate` checks the
  worktree.

## References

- `references/fixtures.md`: for each fixture (a service app, real pylance, a moto server for pylance, a
  live uvicorn, NATS through Dagger, the Ray fake, the chart), the file to copy from and the parts it
  relies on.
