# your-repo

Polyglot monorepo: Rust + Python + TypeScript/Svelte. JS managed with Bun.

## Layout

- `compontens/apps/` — user-facing apps (`cli`, `webapp`)
- `compontens/services/` — backend services (`fastapi-thing`)
- `packages/` — reusable libraries (mixed languages, flat)
  - `lib1`, `lib2` — Python libraries
  - `shared` — Rust crate
  - `oxen_componets` — Svelte 5 component library with Storybook (Bits UI + Tailwind 4)
- `charts/prjects_charts/` — Helm charts
- `contributions/` — contribution guidelines
- `docs/` — Zensical documentation site
- `projects/` — reserved (placeholder)

## Quick start

```bash
bun install
uv sync
make build
```

## Component library workflow

```bash
bun run dev:ui      # rebuild lib on save (svelte-package -w)
bun run storybook   # browse / develop components in isolation
```
