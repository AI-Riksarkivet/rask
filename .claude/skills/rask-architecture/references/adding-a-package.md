# Adding (or moving) a workspace member without breaking resolution

Membership is **globbed**, so a member needs no registration — but that has a
sharper failure: put the package in the **wrong-language plane** and uv errors
hard while bun says nothing at all.
Everything else (sources, first-party naming, dockerfile) is still a manual
edit; test enrolment is globbed too (step 5). Do every step that applies.

## 1. Pick the plane (language first, then layer)

The tree is language-first:

- **Reusable Python library, no entrypoint** → `packages/<name>`. If it grows an
  `app` or a CLI, it's in the wrong layer.
- **Runnable Python** (an HTTP service, or a worker that pulls from the bus) →
  `services/<name>`. A model runtime or a compute workload does **not** go here: the lakehouse runs no engine
  (ADR 0155), and a workload is a client in its own repository.
- **A reusable TS/Svelte component** → the design system, `@riksarkivet/ui`, in the `AI-Riksarkivet/ui` repository; it
  reaches the console when `frontend/console/package.json` pins the commit that carries it. TS/Svelte code with one
  consumer lives in the console (`frontend/console/src/lib`). A new JS workspace member is named in
  `frontend/package.json`'s `workspaces`.
- **A one-shot setup/debug script**, shell or python → `scripts/<name>` (flat;
  **not** a workspace member — run it with `uv run python scripts/<name>.py`).

There is **no `projects/` layer**. A deployable is an ordinary
workspace member plus a `.docker/<name>.dockerfile` — no per-deployable
pyproject, no per-deployable lock.

## 2. Python membership is a glob — the plane IS the registration

The uv manifest enumerates no members; the bun root names its one:

- `pyproject.toml` → `[tool.uv.workspace] members = ["packages/*", "services/*"]`
- `frontend/package.json` → `workspaces = ["console"]`, its one member named
  (paths are relative to `frontend/`)

Create a Python directory in the right plane, give it a `pyproject.toml`, and it
is a member. Nothing to append. A JS member is added to `workspaces` by name.

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
> never put a Python package under `frontend/`.**

## 3. Wire the member's own `pyproject.toml`

First-party deps are workspace sources — list them under `[tool.uv.sources]`
with `{ workspace = true }`, and the package name under `[project] dependencies`:

```toml
[project]
dependencies = ["lakehouse", "uvicorn>=0.30"]

[tool.uv.sources]
lakehouse = { workspace = true }
```

`[tool.hatch.build.targets.wheel] packages = ["src/<import_name>"]` so the
import name resolves.

A service also declares its layers there, as `lineage` and `maintenance` do (ADR 0224): one `layers` contract under
`[tool.importlinter]` with `root_package` the service, `containers = ["<import_name>"]` and `exhaustive = true`, its
order the one its import graph already has. `make lint-imports` runs `.importlinter`, then every member pyproject that
declares `[tool.importlinter]`, found by grep; the service's import name is also a root package in `.importlinter`.

A JS member's `package.json` is the equivalent: `workspace:*` for first-party
deps, plus its own `lint` / `fmt` / `fmt:check` (and `check` / `test` / `build` where it has them) — the root
scripts run `bun run --filter '*' <task>`, so a package that does not declare a script is simply not run for it.

## 4. Register the import name for tooling

Add the import name to `pyproject.toml`
`[tool.ruff.lint.isort] known-first-party` so import sorting treats it as
first-party. The list is COMPLETE — every first-party import name on disk, and
nothing else — and nothing checks it: a member added without its name sorts into
the third-party block, and a deleted member's name lingers, with every lint still
green. Add or remove the name in the same change as the member.

## 5. If it has tests

Nothing to add: `[tool.pytest.ini_options] testpaths` globs `packages/*/tests`
and `services/*/tests`, so the member's `tests/` is collected by the root suite
(`--import-mode=importlib`) as soon as it exists, and coverage's
`source = ["services", "packages"]` already counts its `src`. Only a new TOP-LEVEL
`tests/<x>/` needs adding to `testpaths` by hand.

JS tests are a per-package `test` script, run by `bun --cwd=frontend run test` (`bun run --filter '*' test`) — no
root list to edit.

## 6. If it's deployable

Add `.docker/<name>.dockerfile` (see the existing service dockerfiles — the
pattern is `uv sync --frozen --no-install-workspace --package <name>
--no-editable` from bind-mounted root `uv.lock`/`pyproject.toml`, then COPY
sources + `uv sync --locked --package <name> --no-editable`), and wire it into
the `Makefile`: one line in `k3s-build` plus an entry in `K3S_IMAGES`, as `web-console` (stem `frontend`) and
`lance-rest-catalog` (stem `rest-catalog`) are. `k3s-import`
side-loads every `K3S_IMAGES` tarball from `build/k3s-images`. The kind lane needs no line: `make e2e-ci`
(`scripts/e2e_stack.sh`) builds every image its render side-loads (`scripts/side_loaded_images.py`) as a tarball and loads
it with `kind load image-archive`. Then a chart if it deploys to k3s: the product chart for a lakehouse service, the client's own release for a client. Everything resolves from the
**root** lock — regenerate it (`uv lock`) after editing dependencies. The console's image is
`.docker/frontend.dockerfile` (`dagger call image --name=frontend`, tagged `web-console`).

## Checklist

- [ ] correct plane (`packages/` Python lib / `services/` runnable Python /
      `frontend/console` / `scripts/`; a shared component goes to `AI-Riksarkivet/ui`)
- [ ] the dir carries the manifest its glob expects (`pyproject.toml` under
      `packages/`+`services/`, `package.json` in a named `frontend/` member) — and **no**
      cross-language package was added to either plane
- [ ] member `pyproject.toml`: `[project] dependencies` + `[tool.uv.sources]` workspace pins + hatch wheel `packages` (JS: `workspace:*` deps + its own scripts)
- [ ] `known-first-party` += import name
- [ ] a service: a `layers` contract in its `pyproject.toml`, and its name in `.importlinter`'s `root_packages`
- [ ] tests live in `<member>/tests` (the `testpaths` glob collects them; nothing to list)
- [ ] `.docker/<name>.dockerfile` and `K3S_IMAGES` (if a deployable)
- [ ] `uv sync` resolves clean; first-party import works — and for a JS member,
      `bun --cwd=frontend install` actually **lists** it (a silent skip means it
      carries no `package.json`)
