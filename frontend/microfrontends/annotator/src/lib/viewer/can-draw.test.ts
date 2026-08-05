/**
 * Editing requires an ATTACHED controller, not merely edit mode.
 *
 * Found by driving the canvas in a browser, not by any gate. `ImageViewer` loads the image, then
 * the annotations, and only THEN calls `attach()`. When the annotations read fails — a stale
 * dataset (#37), an FGA denial (#44), a viewer that is down (#64) — the catch returns and the
 * controller is never bound to the engine.
 *
 * Refusing to edit there is CORRECT: a save would overwrite annotations that were never read, and
 * `attach` also carries the `version` driving the OCC handshake. What was wrong is that the UI said
 * nothing. Measured before the fix, with annotations 404ing: every drawing tool enabled, the clicked
 * tool reporting `aria-pressed=true`, the mode button reading "Edit mode", the AI segment rendered,
 * and every drag silently discarded.
 *
 * These are the two flags that carry the fix. They are asserted here rather than only in the
 * browser because the browser drive needs a running dev server, two stubs and a real GPU context —
 * which is exactly the kind of test that stops being run.
 */

import { describe, expect, it, vi } from 'vitest';

// The controller reaches its upstreams through remote functions, which import `$app/server` — a
// SvelteKit virtual module with no meaning outside a running app. Stubbed exactly as
// `autosave.test.ts` does it, so both controller suites fail for the same reasons and neither
// pulls a server runtime into a unit test.
vi.mock('svelte-sonner', () => ({ toast: { error: () => {}, success: () => {} } }));
vi.mock('./remote/assist.remote', () => ({ requestAssist: async () => null }));
vi.mock('./remote/jobs.remote', () => ({ submitBatchJob: async () => null }));

import { AnnotatorController } from './annotator.svelte';

/** A controller as it exists BEFORE `attach()` — the state the failed-load path leaves behind. */
function detached(): AnnotatorController {
	return new AnnotatorController();
}

describe('canDraw', () => {
	it('is FALSE on a fresh controller, even though the mode defaults to edit', () => {
		// The whole defect in one assertion. `mode` starts as 'edit', so the old
		// `canDraw = mode === 'edit'` was true before anything had been attached — which is precisely
		// the window a failed annotations load leaves you sitting in.
		const c = detached();

		expect(c.mode).toBe('edit');
		expect(c.canDraw).toBe(false);
	});

	it('stays false in view mode', () => {
		const c = detached();
		c.mode = 'view';

		expect(c.canDraw).toBe(false);
	});
});

describe('attached — distinct from canDraw, and the distinction is the point', () => {
	it('is false before attach', () => {
		expect(detached().attached).toBe(false);
	});

	it('is false in VIEW mode too — but view mode is a CHOICE, not a failure', () => {
		// Why two flags rather than one. The toolbar's read-only warning gates on `attached`, so it
		// fires for a broken load and stays silent when someone simply toggles to read. Gating that
		// warning on `canDraw` would cry wolf on every deliberate switch to view mode — which is how
		// a warning stops being read.
		const c = detached();
		c.mode = 'view';

		expect(c.attached).toBe(false);
		expect(c.canDraw).toBe(false);

		c.mode = 'edit';
		// Still unattached: mode cannot conjure a binding.
		expect(c.attached).toBe(false);
	});

	it('tracks the ENGINE HANDLE, so it can never drift from the real binding', () => {
		// `ctx` is set only by `attach()` and cleared only by `detach()`. Deriving from it means there
		// is no second boolean anyone can forget to flip.
		const c = detached();
		expect(c.attached).toBe(c.ctx !== null);
	});
});
