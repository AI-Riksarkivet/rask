// Single place to import GSAP for the whole workspace (skill: gsap — "register once, on the client").
// Importing the module is SSR-safe; only *calling* gsap touches window/document, so the project
// defaults are set inside a browser guard. Components import { gsap } from './gsap'.
//
// The browser check is `typeof window` — NOT SvelteKit's `$app/environment` — because this lib must
// load outside a Kit app (the exports test forbids `$app`/`$lib`; `$app/environment` was the one
// SvelteKit tie the extraction from apps/web had to cut). Same SSR semantics: false on the server.
//
// GSAP + every plugin is free since Webflow acquired GreenSock — public `gsap` package, no token.
import { gsap } from 'gsap';

const browser = typeof window !== 'undefined';

if (browser) {
	// Project-wide tween defaults — every gsap.to()/from() inherits these.
	gsap.defaults({ ease: 'power3.out', duration: 0.5 });
}

/** True when the user asked for reduced motion. Client-only; false during SSR. */
export const reduced = (): boolean =>
	browser && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export { gsap };
