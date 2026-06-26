# Adding (or moving) a brick without breaking resolution

The failure mode is **silent**: uv/Bun just can't find the new member, and a
first-party import fails at runtime or test-collection time with
`ModuleNotFoundError`, not a clear "you forgot to register a workspace member".
Do every step that applies.

## 1. Pick the layer (don't blur them)

- **Reusable library, no entrypoint** → `packages/<name>`. If it grows an `app`
  or a CLI, it's in the wrong layer.
- **Runnable** → a SvelteKit frontend → `components/frontends/<name>`; a CLI →
  `components/cli/<name>`; an HTTP service → `components/services/<name>`; a
  one-shot setup/debug script → `components/scripts/<name>`.
- **Deployable composition, no code** → `projects/<name>` (pins a member set
  only; it never contains importable modules).

## 2. The two-place edit (always)

Every Python brick must be registered in **both** root files:

- `pyproject.toml` → `[tool.uv.workspace] members` — append the path
  (e.g. `"components/services/foo"`). uv resolves first-party deps from here.
- root `package.json` → `workspaces` — **only if it carries JS/TS** (the JS
  members are the 7 frontend apps under `components/frontends/*` plus `packages/api`
  = @rask/api and `packages/ui` = @rask/ui). A pure-Python brick is **not**
  added here; a Svelte/TS brick is.

> The brief says "two-place edit (members AND workspaces)". In practice
> `package.json workspaces` only lists JS-bearing bricks. A Python-only brick
> touches just `pyproject.toml members`. Add to `package.json` **iff** the brick
> ships TS/Svelte. Forgetting the place that applies breaks resolution silently.

## 3. Wire the brick's own `pyproject.toml`

First-party deps are workspace sources — list them under `[tool.uv.sources]`
with `{ workspace = true }`, and the package name under `[project] dependencies`:

```toml
[project]
dependencies = ["service-kit", "storage", "uvicorn>=0.30"]

[tool.uv.sources]
service-kit = { workspace = true }
storage = { workspace = true }
```

`[tool.hatch.build.targets.wheel] packages = ["src/<import_name>"]` so the
import name resolves.

## 4. Register the import name for tooling

Add the import name to `pyproject.toml`
`[tool.ruff.lint.isort] known-first-party` so import sorting treats it as
first-party (current list: `htr, storage, runner, core, gateway, service_kit,
core_api, search_api, volumes_api, ray_api, orchestrator, ray_kit`).

## 5. If it has tests

Add the test path to `[tool.pytest.ini_options] testpaths` — tests are **not
auto-discovered** (explicit `testpaths`, `--import-mode=importlib`). A test dir
not listed there simply never runs.

## 6. If it's deployable

Create `projects/<name>/pyproject.toml`: `dependencies = ["<component>"]`, a
`[tool.uv.sources]` block marking every transitive first-party member
`{ workspace = true }`, and a `[tool.uv.workspace] members` list of the
`../../` relative paths it composes. Example — `projects/core-api/pyproject.toml`
pins `core_api` + `core` + `storage` + `service-kit` + `ray-kit`. The project
contains **no code**.

## Checklist

- [ ] correct layer (`packages` lib / `components` runnable / `projects` deploy)
- [ ] `pyproject.toml` `[tool.uv.workspace] members` += path
- [ ] root `package.json` `workspaces` += path **iff** JS/TS-bearing
- [ ] brick `pyproject.toml`: `[project] dependencies` + `[tool.uv.sources]` workspace pins + hatch wheel `packages`
- [ ] `known-first-party` += import name
- [ ] `testpaths` += test dir (if any)
- [ ] `projects/<name>/pyproject.toml` (if deployable)
- [ ] `uv sync` resolves clean; first-party import works
