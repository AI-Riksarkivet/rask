import { afterEach, beforeEach, expect, it, vi } from 'vitest';

/**
 * The store is loaded from an `$effect`, and the svelte MCP autofixer flagged exactly that: a function
 * called inside an effect which assigns state the effect itself reads. The guard there (`!ready &&
 * unreadable === null`) cannot serialise anything, because it only closes one microtask AFTER the read
 * settles — so two components mounting the saved-views popover in the same tick would each open the guard
 * and issue a full GET of the user's document.
 *
 * This asserts the de-duplication that makes that structurally impossible, and that the second caller
 * still gets the data rather than an empty list.
 *
 * The transport is a REMOTE FUNCTION now, so the stub is the remote module rather than a global `fetch`.
 * Mocking it also keeps `$app/server` out of this suite: vitest runs the plain `svelte` plugin, not
 * `sveltekit`, and the real module would fail to resolve it at import time.
 */
const transport = vi.hoisted(() => ({ reads: 0, value: [] as unknown }));

vi.mock('$lib/catalog/remote/catalog.remote', () => ({
	readUserStateDoc: async () => {
		transport.reads += 1;
		// Settle on a later microtask so concurrent callers are genuinely in flight together.
		await Promise.resolve();
		return { ok: true, data: { exists: true, value: transport.value } };
	},
	writeUserStateDoc: async () => ({ ok: true, data: {} }),
}));

const { savedViews } = await import('$lib/saved-views.svelte');

const store = new Map<string, string>();

beforeEach(() => {
	store.clear();
	transport.reads = 0;
	transport.value = [];
	savedViews.views = [];
	savedViews.ready = false;
	savedViews.unreadable = null;
	vi.stubGlobal('localStorage', {
		getItem: (k: string) => store.get(k) ?? null,
		setItem: (k: string, v: string) => void store.set(k, v),
		removeItem: (k: string) => void store.delete(k),
	});
});
afterEach(() => vi.unstubAllGlobals());

it('concurrent loads are one request, and both callers see the views', async () => {
	transport.value = [{ name: 'a', dataset: 'd', spec: { query: 'x' } }];

	await Promise.all([savedViews.load(), savedViews.load(), savedViews.load()]);

	expect(transport.reads).toBe(1);
	expect(savedViews.ready).toBe(true);
	expect(savedViews.forDataset('d').map((v) => v.name)).toEqual(['a']);
});

it('a later load is a fresh read — de-duplication is per flight, not a cache', async () => {
	await savedViews.load();
	await savedViews.load();

	expect(transport.reads).toBe(2);
});
