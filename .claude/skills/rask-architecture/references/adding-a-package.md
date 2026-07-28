# Adding (or moving) a workspace member without breaking resolution

Membership itself is now **globbed**, so the old "you forgot to register it"
failure is mostly gone — but it is replaced by a sharper one: put the package in
the **wrong-language plane** and uv errors hard while bun says nothing at all.
Everything else (sources, first-party naming, testpaths, dockerfile) is still a
manual edit. Do every step that applies.

## 1. Pick the plane (don't blur them)

The tree is language-first:

- **Reusable Python library, no entrypoint** → `packages/<name>`. If it grows an
  `app` or a CLI, it's in the wrong layer.
- **Runnable Python** (an HTTP service, the runner CLI, the `core` domain
  package) → `services/<name>`.
- **A SvelteKit zone** → `frontend/microfrontends/<zone>`.
- **A reusable TS/Svelte library** → `frontend/packages/<name>`.
- **A one-shot setup/debug script**, shell or python → `scripts/<name>` (flat;
  **not** a workspace member — run it with `uv run python scripts/<name>.py`).

There is **no `projects/` layer** (removed 2026-07). A deployable is an ordinary
workspace member plus a `.docker/<name>.dockerfile` — no per-deployable
pyproject, no per-deployable lock.

## 2. Membership is a glob — the plane IS the registration

Neither root manifest enumerates members any more:

- `pyproject.toml` → `[tool.uv.workspace] members = ["packages/*", "services/*"]`
- `frontend/package.json` → `workspaces = ["microfrontends/*", "packages/*"]`
  (paths are relative to `frontend/`)

Create the directory in the right plane, give it the manifest its toolchain
expects, and it is a member. Nothing to append.

> **The globs are safe ONLY because each globbed directory is single-language.**
> The two toolchains fail asymmetrically the moment that stops being true:
>
> ```
> uv  lock, members=["packages/*"], one TS dir inside   → error: Workspace member `…/packages/tspkg`
>                                                          is missing a `pyproject.toml`
> bun install, workspaces=["packages/*"], one Py dir    → Done! Checked 2 packages  (SILENTLY skipped)
> ```
>
> uv shouts (and the only "fix" is an `exclude` list — enumeration renamed);
> bun says nothing and the package is simply never installed, built, linted or
> tested. So: **never put a JS package under root `packages/`/`services/`, and
> never put a Python package under `frontend/`.** The root `pyproject.toml` also
> notes `runners/*` is deliberately matched by *no* glob — sealed model envs
> whose heavy pins must never enter the fleet's resolution.

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

A JS member's `package.json` is the equivalent: `workspace:*` for first-party
deps, plus its own `lint` / `fmt` / `fmt:check` / `check` / `build` scripts —
those are **per-package turbo tasks**, never centralized in the root.

## 4. Register the import name for tooling

Add the import name to `pyproject.toml`
`[tool.ruff.lint.isort] known-first-party` so import sorting treats it as
first-party (current list: `storage, gateway, service_kit, compute, ray_kit,
tracker, validate`).

## 5. If it has tests

Add the test path to `[tool.pytest.ini_options] testpaths` — tests are **not**
auto-discovered (explicit `testpaths`, `--import-mode=importlib`). A test dir
not listed there simply never runs. Current entries look like
`services/core/tests`, `packages/storage/tests`. (The sealed `runners/htr` has its OWN
testpaths in its own pyproject — the root pytest cannot see it, so `make test` runs it
separately.)

JS tests are a per-package `test` script picked up by `turbo run test` from
`frontend/` — no root list to edit.

## 6. If it's deployable

Add `.docker/<name>.dockerfile` (see the existing service dockerfiles — the
pattern is `uv sync --frozen --no-install-workspace --package <name>
--no-editable` from bind-mounted root `uv.lock`/`pyproject.toml`, then COPY
sources + `uv sync --locked --package <name> --no-editable`), and wire it into
the `Makefile` `k3s-build` fleet list + the chart if it deploys to k3s.
Everything resolves from the **root** lock — regenerate it (`uv lock`) after
editing dependencies. A new SvelteKit zone needs **no new dockerfile** — the one
parametrized `.docker/frontend.dockerfile` is built per zone with
`--build-arg APP=<zone>`.

## Checklist

- [ ] correct plane (`packages/` Python lib / `services/` runnable Python /
      `frontend/packages/` JS lib / `frontend/microfrontends/` zone / `scripts/`)
- [ ] the dir carries the manifest its glob expects (`pyproject.toml` under
      `packages/`+`services/`, `package.json` under `frontend/*`) — and **no**
      cross-language package was added to either plane
- [ ] member `pyproject.toml`: `[project] dependencies` + `[tool.uv.sources]` workspace pins + hatch wheel `packages` (JS: `workspace:*` deps + its own turbo scripts)
- [ ] `known-first-party` += import name
- [ ] `testpaths` += test dir (if any)
- [ ] `.docker/<name>.dockerfile` + `k3s-build` list (if a deployable; zones reuse the parametrized one)
- [ ] `uv sync` resolves clean; first-party import works — and for a JS member,
      `bun --cwd=frontend install` actually **lists** it (a silent skip means the
      glob didn't match)
