import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { savedViews } from '$lib/saved-views.svelte';

/**
 * The store is loaded from an `$effect`, and the svelte MCP autofixer flagged exactly that: a function
 * called inside an effect which assigns state the effect itself reads. The guard there (`!ready &&
 * unreadable === null`) cannot serialise anything, because it only closes one microtask AFTER the read
 * settles — so two components mounting the saved-views popover in the same tick would each open the guard
 * and issue a full GET of the user's document.
 *
 * This asserts the de-duplication that makes that structurally impossible, and that the second caller
 * still gets the data rather than an empty list.
 */
const store = new Map<string, string>();

beforeEach(() => {
	store.clear();
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
	let calls = 0;
	vi.stubGlobal('fetch', async () => {
		calls += 1;
		// Settle on a later microtask so both callers are genuinely in flight together.
		await Promise.resolve();
		return new Response(
			JSON.stringify({ exists: true, value: [{ name: 'a', dataset: 'd', spec: { query: 'x' } }] }),
			{ status: 200, headers: { 'content-type': 'application/json' } },
		);
	});

	await Promise.all([savedViews.load(), savedViews.load(), savedViews.load()]);

	expect(calls).toBe(1);
	expect(savedViews.ready).toBe(true);
	expect(savedViews.forDataset('d').map((v) => v.name)).toEqual(['a']);
});

it('a later load is a fresh read — de-duplication is per flight, not a cache', async () => {
	let calls = 0;
	vi.stubGlobal('fetch', async () => {
		calls += 1;
		return new Response(JSON.stringify({ exists: true, value: [] }), {
			status: 200,
			headers: { 'content-type': 'application/json' },
		});
	});

	await savedViews.load();
	await savedViews.load();

	expect(calls).toBe(2);
});
