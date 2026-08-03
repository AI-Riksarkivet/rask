import { base } from '$app/paths';
import { setApiBase } from '@rask/explorer-api/base';

// Every media-plane fetch (and every media/thumbnail/frame URL) goes through THIS zone's
// same-origin BFF proxy routes at `${base}/api/*` — set the shared client's base once,
// module-init on server and browser alike, before any page code builds a URL.
setApiBase(base);

// SSR on (the estate zone contract): hooks.server.ts runs on every request (real login
// gate + session), the layout shell server-renders, and the BFF routes serve `/api/*`.
// Client-heavy leaf pages that genuinely cannot SSR opt out per-page (+page.ts).
export const ssr = true;
export const prerender = false;
