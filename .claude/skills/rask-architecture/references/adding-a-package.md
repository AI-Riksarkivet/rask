# Adding (or moving) a workspace member without breaking resolution

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

There is **no `projects/` layer** (removed 2026-07). A deployable is an ordinary
workspace member plus a `.docker/<name>.dockerfile` — no per-deployable
pyproject, no per-deployable lock.

## 2. The two-place edit (always)

Every Python member must be registered in **both** root files:

- `pyproject.toml` → `[tool.uv.workspace] members` — append the path
  (e.g. `"components/services/foo"`). uv resolves first-party deps from here.
- root `package.json` → `workspaces` — **only if it carries JS/TS** (the JS
  members are the 7 frontend apps under `components/frontends/*` plus `packages/api`
  = @rask/api and `packages/ui` = @rask/ui). A pure-Python member is **not**
  added here; a Svelte/TS member is.

> The brief says "two-place edit (members AND workspaces)". In practice
> `package.json workspaces` only lists JS-bearing members. A Python-only member
> touches just `pyproject.toml members`. Add to `package.json` **iff** it
> ships TS/Svelte. Forgetting the place that applies breaks resolution silently.

## 3. Wire the member's own `pyproject.toml`

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

Add `.docker/<name>.dockerfile` (see the existing service dockerfiles — the
pattern is `uv sync --frozen --no-install-workspace --package <name>
--no-editable` from bind-mounted root `uv.lock`/`pyproject.toml`, then COPY
sources + `uv sync --locked --package <name> --no-editable`), and wire it into
the `Makefile` `k3s-build` fleet list + the chart if it deploys to k3s.
Everything resolves from the **root** lock — regenerate it (`uv lock`) after
editing dependencies.

## Checklist

- [ ] correct layer (`packages` lib / `components` runnable)
- [ ] `pyproject.toml` `[tool.uv.workspace] members` += path
- [ ] root `package.json` `workspaces` += path **iff** JS/TS-bearing
- [ ] member `pyproject.toml`: `[project] dependencies` + `[tool.uv.sources]` workspace pins + hatch wheel `packages`
- [ ] `known-first-party` += import name
- [ ] `testpaths` += test dir (if any)
- [ ] `.docker/<name>.dockerfile` + `k3s-build` list (if deployable)
- [ ] `uv sync` resolves clean; first-party import works
