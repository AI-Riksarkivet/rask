import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, zoneDirs } from './manifest';

// #107 — the CROSS-DOCUMENT half of the navigation transition.
//
// A cross-zone nav tears down one SvelteKit app and boots the next, so the `onNavigate` +
// `startViewTransition` pattern three zone layouts run cannot reach it: that API animates SOFT,
// same-document navigations only. The browser's own cross-document transition is opt-in via the
// `@view-transition` at-rule, and the opt-in must be present in BOTH documents or it is skipped.
//
// This is a test rather than a lint rule for the same reason the cross-zone-reload gate is: the
// failure is INVISIBLE. Three zone layouts carried a comment saying "cross-document MFE navs are
// handled by the `@view-transition` rule" for as long as the estate has existed, and the rule was
// written nowhere — a described mechanism, never implemented, and the white flash between zones
// read as "MFEs are just like that" instead of as a missing three-line stylesheet.

const TOKENS = resolve(FRONTEND_ROOT, 'packages/ui/src/lib/styles/tokens.css');

describe('cross-document view transitions', () => {
	it('the shared stylesheet opts in', () => {
		const css = readFileSync(TOKENS, 'utf8');
		// Whitespace-tolerant: the formatter owns the layout, this owns the CONTRACT.
		expect(css).toMatch(/@view-transition\s*\{[^}]*navigation:\s*auto/);
	});

	it('EVERY zone imports the stylesheet carrying it', () => {
		// The opt-in only fires when both the outgoing AND incoming document declare it. A zone that
		// skipped the import would flash on exactly the pairs involving it — the hardest kind of bug
		// to attribute, because six of the seven zones would look fine.
		// zoneDirs() returns NAMES, not paths — resolve against the microfrontends root.
		const missing = zoneDirs().filter((zone) => {
			const appCss = readFileSync(
				resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src/app.css'),
				'utf8',
			);
			return !appCss.includes('@rask/ui/styles/tokens.css');
		});
		expect(missing).toEqual([]);
	});

	it('the SHELL CHROME is named, so it carries across the swap instead of cross-fading', () => {
		// The opt-in alone leaves the default WHOLE-document cross-fade: the navbar fades into an
		// identical navbar, the sidebar into an identical sidebar. Every pixel animates while nothing
		// moved, which is what "everything flashes, even the sidebar and topnavbar" describes. A
		// `view-transition-name` makes the browser treat them as ONE element across documents.
		const css = readFileSync(TOKENS, 'utf8');
		for (const name of ['rask-shell-header', 'rask-shell-sidebar', 'rask-shell-main']) {
			expect(css).toContain(`view-transition-name: ${name}`);
		}
		// …and naming alone is not enough: a named element still cross-fades unless told not to.
		const held = css.slice(css.indexOf('::view-transition-old(rask-shell-header)'));
		expect(held).toMatch(/animation:\s*none/);
	});

	it('honours prefers-reduced-motion', () => {
		// A full-page cross-fade is exactly the class of motion the setting exists to suppress, and a
		// browser-driven transition does not consult it on our behalf.
		const css = readFileSync(TOKENS, 'utf8');
		// The WILDCARD, not `(root)`: once the shell chrome carries its own names, a root-only rule
		// would leave every named element still animating for a user who asked for no motion.
		const reduced = css.slice(css.indexOf('prefers-reduced-motion'));
		expect(reduced).toMatch(/::view-transition-(group|old|new)\(\*\)/);
		expect(reduced).toMatch(/animation:\s*none/);
	});
});
