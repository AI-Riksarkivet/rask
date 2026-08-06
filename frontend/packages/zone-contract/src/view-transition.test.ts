import { readFileSync } from 'node:fs';
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

const TOKENS = resolve(FRONTEND_ROOT, 'packages/ui/src/lib/styles/tokens.css');

describe('cross-document view transitions stay OFF', () => {
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
