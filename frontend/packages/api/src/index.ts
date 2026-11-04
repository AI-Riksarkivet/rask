// @rask/api — the console's data layer, split by domain. JIT TS: the app imports the source directly
// (Vite/svelte-check transpile it), no build step.
//   • parse  — the valibot parse-don't-validate boundary for typed client responses.
//   • me     — the frozen `GET /management/v1/me` identity contract (schema/types) and its fetch.
//   • client — the browser transport (`@rask/api/client`): same-origin `/api/*` with the person's bearer.
export * from './parse';
export * from './me';
