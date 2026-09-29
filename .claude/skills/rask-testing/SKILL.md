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

A change works a row of the register, `open_backlog_left_new2.md`, and each row states what it
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
| The rendered chart                                | `tests/unit`, through `tests/unit/chart_render.py`                                              |
| A deployed stack                                  | `tests/e2e-py`, run by name from a lane: § Wiring                                               |
| A sealed runner's code                            | `runners/<name>/tests`, under that runner's own `pyproject.toml`, run by the Makefile's runner loop: § Wiring |

## Wiring

Read `[tool.pytest.ini_options]` in the root `pyproject.toml` for the current values. These are the
consequences it does not spell out.

- **`testpaths` is globbed.** `packages/*/tests` and `services/*/tests` enrol a new member's `tests/`
  the moment it exists; only a new top-level `tests/<x>/` needs adding by hand. Coverage's
  `source = ["services", "packages"]` with `[tool.coverage.report] include_namespace_packages` counts
  every member's `src` in the denominator with no edit.
- **A live suite runs only when something names it.** Every offline lane deselects `e2e`. A new
  `tests/e2e-py` file needs a per-suite marker registered in the root `pyproject.toml` with a
  `make e2e-<suite>` target (listed in `E2E_SUITES`), or its path in the Makefile, `ci.yml`, a
  `.dagger/*.go` lane or a `scripts/*.sh`; `tests/unit/test_e2e_collection_gate.py` fails a suite
  with neither. A `make e2e-<suite>` target alone is not CI: if `scripts/e2e_stack.sh`,
  `scripts/ray_e2e_stack.sh` and `ci.yml` never name that suite's files,
  `tests/unit/test_a_declared_e2e_suite_is_driven_by_something.py` fails until they do or the suite is
  added to that file's `UNDRIVEN` set.
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
  per-suite selector (`auth`, `media`, ...) rides on top of it. Neither `strict_markers` nor
  `--strict-markers` is configured, so pass `--strict-markers` when you add or use a marker (the whole
  suite, 12,225 tests, collects cleanly under it, measured 2026-09-28).
- **Async is strict.** No `asyncio_mode` is set, so pytest-asyncio runs in strict mode. The `anyio`
  plugin is loaded too; most async tests use `@pytest.mark.asyncio`, so write new ones that way.
- **Code-shape rules live in ruff and import-linter.** A banned name is a `TID251` entry in the root
  `[tool.ruff.lint.flake8-tidy-imports.banned-api]`, restated in `services/ingest` and
  `services/viewer`, whose nested maps replace the root one. A sanctioned use takes a line-level
  `# noqa: TID251`, and only a module that IS the seam takes a per-file ignore. A layer rule is a
  contract in `.importlinter`, run by `make lint-imports` (part of `make check`).

### Lanes

| Lane                                              | What it runs                                                                                                      |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `make test`                                       | `-m "not slow and not e2e"` at `-n 16` (`PYTEST_WORKERS=0` for one process), then every `runners/*/tests` suite with `-m "not slow"` |
| `make check-fast`                                 | `tests/unit tests/integration` at `-n 16`, with no marker filter. `--dist loadfile`, because three suites are parallel-unsafe at the file level (the `lance.audit` process-global logger, `configure_audit`'s level, the registry CAS markers) |
| `make test-slow`                                  | `-m "not e2e"`, then every runner suite unfiltered                                                                |
| `make coverage`                                   | `-m "not e2e and not slow" --cov`, on request only                                                                |
| CI: `dagger call test` (`.dagger/test.go`)        | the `make test` marker filter, serially, with `--timeout=300 --timeout-method=thread`, helm installed and a real NATS bound at `RASK_NATS_URL`. **No runner suite** |
| CI: `dagger call auth-chain`, `dagger call governance-chain` | a real Dex, OpenFGA and catalog as Dagger services (plus lineage for `governance-chain`), asserting through `scripts/auth_chain.sh` and `tests/e2e-py/test_governance_e2e.py` respectively |
| CI: `e2e-stack` (= `make e2e-ci`), `e2e-ray` (= `make e2e-ray-ci`), `dagger call test-lineage` | the `tests/e2e-py` files named in `scripts/e2e_stack.sh`, `scripts/ray_e2e_stack.sh` and `.dagger/e2e.go`, against a stack the lane brings up |
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
process-global actor-proxy factory, and closes medallion's pooled Ray client.

## What stands in for what

| Collaborator                  | In a test                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| A service app                 | the real app under `with TestClient(app)`, set up per the service's settings shape: `references/fixtures.md` § A service app |
| Lance / lance-namespace       | real pylance on `tmp_path`: `tests/integration/conftest.py::real_ns_client`, `tests/unit/conftest.py::overlay_dataset`. The `MagicMock(spec=LanceNamespace)` behind `::client` cannot see what Lance writes (a table landed at the wrong file version passes), so it serves only routing and error-mapping claims |
| S3 through boto3 / `storage`  | moto `mock_aws()`                                                                                           |
| S3 read by pylance or pyarrow | a moto server, `tests/integration/test_moto_s3.py::moto_endpoint`: `mock_aws()` patches botocore, which the native Lance and Arrow clients never go through |
| Outbound HTTPX                | `respx` (`services/flows/tests/test_routes.py`)                                                             |
| NATS JetStream                | a real broker: CI binds one; locally `references/fixtures.md` § NATS                                        |
| Ray job client                | a structural fake `cast` to `JobSubmissionClient`: `packages/ray-kit/tests/test_dashboard_bounds.py`        |
| OpenFGA, authz not the claim  | `RASK_FGA_ENABLED` / `RASK_OIDC_ENABLED` left at their default, off; with FGA off the checker allows everything. Where a route needs a named subject or a decision, override that service's `current_subject` / `get_checker` |
| OpenFGA, authz is the claim   | the model's `.fga.yaml` suite (`make fga-test`), or a real Dex and OpenFGA via `dagger call auth-chain` / `governance-chain`. The offline suite has no OpenFGA server |
| The chart                     | `tests/unit/chart_render.py`: `references/fixtures.md` § The chart                                          |

## Skips and prose

- **Skips not to copy**: `pytest.skip("helm not available")` in `chart_render.render_text` and every
  file `git grep -l 'helm not available'` lists, and the ingest suites' `skipif` when no NATS answers.
  CI provides both, so the skip only hides the test on a developer box.
- **Shapes not to copy**: many `tests/unit` files read the Makefile, `pyproject.toml`, source or prose
  instead of running code (51 of 507 parse an AST, measured 2026-09-28), including the wiring gates
  § Wiring names. They still fail as described there. Don't write a new test in their shape: a
  code-shape rule goes to ruff, and a behaviour claim drives the code.
- **Test docstrings and comments follow CLAUDE.md's comment rule**: rationale and provenance, never
  history. The `comment-history-gate` prek hook checks staged lines; `make comment-gate` checks the
  worktree.

## References

- `references/fixtures.md`: for each fixture (a service app, real pylance, a moto server for pylance, a
  live uvicorn, NATS through Dagger, the Ray fake, the chart), the file to copy from and the parts it
  relies on.
