import { afterEach, describe, expect, it, vi } from 'vitest';

import { activity, MAX_AGE_MS, MAX_ENTRIES, prune, type ActivityEntry } from './activity.svelte';

const NOW = Date.parse('2026-11-07T12:00:00Z');

const entry = (index: number, at: number): ActivityEntry => ({
	id: `e${index}`,
	label: `Write ${index}`,
	at,
	ok: true,
});

describe('prune', () => {
	it('keeps the newest entries up to the cap', () => {
		const entries = Array.from({ length: MAX_ENTRIES + 5 }, (_, index) =>
			entry(index, NOW - index),
		);

		const kept = prune(entries, NOW);

		expect(kept.map((e) => e.id)).toEqual(entries.slice(0, MAX_ENTRIES).map((e) => e.id));
	});

	it('drops an entry once it is a day old', () => {
		const entries = [entry(0, NOW - MAX_AGE_MS + 1), entry(1, NOW - MAX_AGE_MS)];

		expect(prune(entries, NOW).map((e) => e.id)).toEqual(['e0']);
	});
});

describe('activity', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('keeps recording in memory once storage refuses a write', () => {
		// A tab whose storage takes the first write and then refuses (quota, blocked site data).
		const kept = new Map<string, string>();
		let writes = 0;
		vi.stubGlobal('window', {
			sessionStorage: {
				getItem: (key: string) => kept.get(key) ?? null,
				setItem: (key: string, value: string) => {
					writes += 1;
					if (writes > 1) throw new Error('QuotaExceededError');
					kept.set(key, value);
				},
				removeItem: (key: string) => kept.delete(key),
				key: (index: number) => [...kept.keys()][index] ?? null,
				get length() {
					return kept.size;
				},
			},
		});

		activity.record('Drop table bronze$events', { ok: true });
		activity.record('Undrop table bronze$events', { ok: false, status: 404, detail: 'purged' });

		expect(activity.entries.map(({ label, ok }) => ({ label, ok }))).toEqual([
			{ label: 'Undrop table bronze$events', ok: false },
			{ label: 'Drop table bronze$events', ok: true },
		]);
	});
});
