// SSR is ON (the default) — the app is served by svelte-adapter-bun behind the
// gateway. Data still loads client-side in onMount; the server renders the shell.
// Prerendering stays off because routes are dynamic (viewer/[volume]/[page], etc.).
export const ssr = true;
export const prerender = false;
