import { readdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT } from './manifest';

// The cross-document view-transition at-rule stays OUT — and this is the gate that keeps it out.
//
// `tokens.css` has carried the reasoning at the top of the file since the estate was built, ending
// in the words "Don't re-add this". It was re-added anyway on 2026-08-06, by someone chasing the
// cross-zone flash without reading the file they were editing. What followed is exactly what that
// comment predicted, and then worse:
//
//   1. Opting in crossfades the WHOLE viewport on every cross-zone nav — including the sidebar and
//      navbar, which are identical in all seven zones. Nothing moves and every pixel animates, which
//      reads as a flash OF THE CHROME. (The original report said, verbatim, "everything flashes even
//      the sidebar and topnavbar".)
//   2. Naming the shell to hold it still made it WORSE: per spec a `view-transition-name` turns its
//      element into a STACKING CONTEXT and a containing block. Naming `header` and `main` re-ordered
//      the app's layering and dropped the top-navbar dropdown behind the page.
//
// The shipped behaviour is a paint-held document swap: the new document paints (dark, via app.html's
// no-flash boot script) before the old is removed, so the identical shell looks static. Soft in-app
// navs still animate through `onNavigate` -> `startViewTransition`, the SAME-document API, unrelated
// to this at-rule.
//
// A prose comment did not survive one contact with a determined editor. A test does.

/**
 * Every AUTHORED stylesheet in the estate, not just the shared one.
 *
 * This gate read exactly `packages/ui/src/lib/styles/tokens.css` — one file of eleven — while its own
 * docstring claimed to be "the gate that keeps it out". The at-rule is a DOCUMENT-level opt-in, so the
 * seven zone `app.css` files are precisely where a determined editor would put it back, and none of
 * them was read. A repo-wide grep finds no second gate anywhere.
 *
 * Build output is excluded rather than filtered afterwards: `storybook-static/` and `dist/` carry
 * COMPILED copies, so a gate that read them would report a violation that no source file contains, and
 * its answer would depend on whether someone had run a build.
 */
function authoredStylesheets(): string[] {
	const SKIP = new Set([
		'node_modules',
		'.svelte-kit',
		'dist',
		'build',
		'storybook-static',
		'.turbo',
	]);
	const out: string[] = [];
	const walk = (dir: string): void => {
		for (const e of readdirSync(dir, { withFileTypes: true })) {
			if (e.isDirectory()) {
				if (!SKIP.has(e.name)) walk(resolve(dir, e.name));
			} else if (e.name.endsWith('.css')) {
				out.push(resolve(dir, e.name));
			}
		}
	};
	for (const root of ['microfrontends', 'packages']) walk(resolve(FRONTEND_ROOT, root));
	return out.sort();
}

describe('cross-document view transitions stay OFF', () => {
	const sheets = authoredStylesheets();

	it('finds stylesheets to check', () => {
		// Without this the gate passes vacuously the day the walk root moves — the failure mode the
		// rest of this suite keeps finding in other scanners.
		expect(
			sheets.length,
			'no authored stylesheets found — this gate would pass vacuously',
		).toBeGreaterThan(5);
	});

	it.each(sheets.map((f) => [f.slice(FRONTEND_ROOT.length + 1), f] as const))(
		'%s declares no @view-transition at-rule',
		(_rel, file) => {
			const css = readFileSync(file, 'utf8');
			// The AT-RULE only. tokens.css's explanatory comment names it in prose, and that comment is
			// the thing worth keeping — a check that forbade the string would delete its own rationale.
			expect(css).not.toMatch(/^\s*@view-transition\b/m);
		},
	);
});

describe('the shared stylesheet, in detail', () => {
	const TOKENS = resolve(FRONTEND_ROOT, 'packages/ui/src/lib/styles/tokens.css');
	it('the shared stylesheet declares no @view-transition at-rule', () => {
		const css = readFileSync(TOKENS, 'utf8');
		// The AT-RULE only. The file's explanatory comment names it in prose, and that comment is the
		// thing worth keeping — a check that forbade the string would delete its own rationale.
		expect(css).not.toMatch(/^\s*@view-transition\b/m);
	});

	it('no element is given a view-transition-name', () => {
		// The stacking-context half. Inert today with the at-rule gone, but it is the exact edit that
		// broke the navbar dropdown, and it reads as harmless.
		const css = readFileSync(TOKENS, 'utf8');
		expect(css).not.toMatch(/view-transition-name\s*:/);
	});

	it('keeps the RULING itself, so the next reader learns why before trying', () => {
		// The gate is half the protection: a failing test with no rationale invites a workaround.
		const css = readFileSync(TOKENS, 'utf8');
		expect(css).toMatch(/Don't re-add this/);
	});
});
