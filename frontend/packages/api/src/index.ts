// @rask/api — shared frontend data layer, split by domain. JIT TS: apps import the source directly
// (Vite/svelte-check transpile it) — no build. The `.` entry is CLIENT-SAFE (no node:crypto / $env):
//   • gateway  — the single-sourced SSR `/api/*` → in-cluster gateway rewrite (handleFetch factory).
//   • parse    — the valibot parse-don't-validate boundary for typed client responses.
//   • me       — the frozen `GET /v1/me` identity contract (schema/types) + the BFF-side fetchMe helper.
// Server-only auth lives behind subpaths so it never reaches a client bundle:
//   • @rask/api/oidc — the OIDC crypto seam (PKCE, sealed AES-256-GCM session cookie).
//   • @rask/api/bff  — the SvelteKit BFF factories (makeOidcConfig / makeSessionHandle / makeBackendProxy).
export * from './gateway';
export * from './parse';
export * from './me';
// rask's own domain clients. ray backs the compute zone via the `ray` service (`/api/ray/*` +
// `/api/serve/*`). (batches.ts died at P7a with the batches table — ingestion is ingest.ts → the
// medallion producer; search.ts/volumes.ts died in the R6/R20 wave — lines FTS re-lands as a
// catalog-governed table behind /api/explorer/search, and the S3 object browser now rides the
// media-plane viewer at /api/explorer/object*.)
export * from './ray';
export * from './ingest';
export * from './projects';
