// SSR is ON (the default) — the app is served by svelte-adapter-bun behind the
// gateway. Reads are server-only remote query()s in src/lib/remote/<domain>.remote.ts
// (getRequestEvent().fetch + @rask/api), SSR-inlined into the first frame — no onMount
// fetch waterfall (this catch-all host fetches nothing today; it's a pure shell).
// Prerendering stays off because routes are dynamic (viewer/[volume]/[page], etc.).
export const ssr = true;
export const prerender = false;
