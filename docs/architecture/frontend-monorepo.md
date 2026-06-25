# Frontend monorepo: multi-app + shared lib

!!! warning "Superseded (June 2026) — see [Frontend Microfrontends](frontend-microfrontends.md)"

    This was an early design sketch written before the split was built. Its concrete
    details are now stale (it imagined `shell`/`pipeline-studio`/`admin` apps and
    source-mode `exports`; the real split is `frontend`/`storage`/`compute`
    on **svelte-adapter-bun SSR** consuming the **built** `@rask/ui` + `@rask/api`). The
    authoritative, current doc is **[frontend-microfrontends.md](frontend-microfrontends.md)**.

    The one durable idea worth keeping — the **lib-vs-app litmus** — is retained below.

## What lives where — the litmus test

> Does the thing know about a _domain type_ (a batch, a chunk, an OCR line, a Ray job)?
> → it goes in an **app**.
> Does it only know about props, slots, classes, ARIA, design tokens? → it goes in the
> shared library (`@rask/ui`).

|                                              | `@rask/ui` (lib) | app |
| -------------------------------------------- | :--------------: | :-: |
| `Button.svelte` (variants, sizes, `asChild`) |        ✅        |     |
| `ResizeHandle.svelte` (drag + size)          |        ✅        |     |
| `tokens.css` (CSS custom properties)         |        ✅        |     |
| `cn()` utility                               |        ✅        |     |
| `AppShell` / `AppSidebar` (chrome, no data)  |        ✅        |     |
| a route's data `load` / domain component     |                  | ✅  |
| `state.svelte.ts` (per-app store)            |                  | ✅  |

Each app is a leaf consumer; the lib has zero knowledge of any app. Design unity is a
**compile-time** guarantee (bun workspaces, built `dist/`) — there is no runtime
federation. The shared sidebar is the `@rask/ui/shell` **library** every app imports, not
a host "shell" app — see [frontend-microfrontends.md](frontend-microfrontends.md) for how
that and the per-app `kit.paths.base` composition actually work today.
