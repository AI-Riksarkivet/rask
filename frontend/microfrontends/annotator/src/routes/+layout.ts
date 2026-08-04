import { base } from '$app/paths';
import { setApiBase } from '@rask/explorer-api/base';

// Every media-plane fetch this zone's labeling clients make goes through THIS zone's
// same-origin BFF proxy routes at `${base}/api/*` — set the shared client's base once,
// module-init on server and browser alike, before any page code builds a URL.
setApiBase(base);

// SSR on (the estate zone contract): hooks.server.ts runs on every request (real login
// gate + session), the layout shell server-renders, and the BFF routes serve `/api/*`.
// The canvas page itself opts out per-page (+page.ts) — Pixi cannot render server-side.
export const ssr = true;
export const prerender = false;
