import { describe, expect, it, vi } from 'vitest';
import { UrlMemo } from './memo';

/**
 * The memo is what stops `/api/atlas/points` being re-downloaded on every space toggle, so each property it
 * relies on is asserted here rather than assumed: that a repeat ask makes no second call, that a concurrent
 * ask shares the first one, that a failure is not remembered, and that eviction cannot throw away the entry
 * a toggle is about to want back.
 */
const later = <T>(value: T, ms = 0): Promise<T> =>
	new Promise((resolve) => setTimeout(() => resolve(value), ms));

describe('UrlMemo', () => {
	it('a repeat ask does not call the loader again', async () => {
		const memo = new UrlMemo<string>(3);
		const load = vi.fn(async () => 'points');

		expect(await memo.run('/a', load)).toBe('points');
		expect(await memo.run('/a', load)).toBe('points');

		expect(load).toHaveBeenCalledTimes(1); // the whole point: one 6.6 MB download, not two
	});

	it('concurrent asks share ONE in-flight load', async () => {
		const memo = new UrlMemo<string>(3);
		const load = vi.fn(() => later('points', 5));

		// Two components mounting in the same tick. Memoising the value rather than the promise would
		// let both of these start their own request, which is the case that actually hurts.
		const [a, b] = await Promise.all([memo.run('/a', load), memo.run('/a', load)]);

		expect(load).toHaveBeenCalledTimes(1);
		expect(a).toBe(b);
	});

	it('distinct urls are distinct entries', async () => {
		const memo = new UrlMemo<string>(3);
		const load = vi.fn(async (): Promise<string> => 'x');

		await memo.run('/text', load);
		await memo.run('/visual', load);

		expect(load).toHaveBeenCalledTimes(2);
		expect(memo.size).toBe(2);
	});

	it('a rejection is NOT remembered — a failed load stays retryable', async () => {
		const memo = new UrlMemo<string>(3);
		let attempt = 0;
		const load = vi.fn(async () => {
			attempt += 1;
			if (attempt === 1) throw new Error('502 from the viewer');
			return 'recovered';
		});

		await expect(memo.run('/a', load)).rejects.toThrow('502 from the viewer');
		expect(memo.size).toBe(0); // evicted itself, rather than pinning the failure for the session
		expect(await memo.run('/a', load)).toBe('recovered');
		expect(load).toHaveBeenCalledTimes(2);
	});

	it('evicts beyond the bound, oldest first', async () => {
		const memo = new UrlMemo<string>(2);
		const load = async (): Promise<string> => 'x';

		await memo.run('/1', load);
		await memo.run('/2', load);
		await memo.run('/3', load);

		expect(memo.size).toBe(2);
		const reload = vi.fn(async (): Promise<string> => 'refetched');
		await memo.run('/1', reload); // '/1' was the oldest, so it is gone
		expect(reload).toHaveBeenCalledTimes(1);
	});

	it('eviction is least-recently-USED, so a toggle never evicts what it is about to want', async () => {
		const memo = new UrlMemo<string>(2);
		const load = async (): Promise<string> => 'x';

		await memo.run('/text', load);
		await memo.run('/visual', load);
		await memo.run('/text', load); // touch it — under plain FIFO this would still be the eviction victim
		await memo.run('/third', load); // pushes one out: '/visual', not '/text'

		const textAgain = vi.fn(async (): Promise<string> => 'refetched');
		await memo.run('/text', textAgain);
		expect(textAgain).not.toHaveBeenCalled(); // still cached — this is the toggle case

		const visualAgain = vi.fn(async (): Promise<string> => 'refetched');
		await memo.run('/visual', visualAgain);
		expect(visualAgain).toHaveBeenCalledTimes(1); // the genuinely least-recently-used one went
	});

	it('refuses a bound that cannot hold anything', () => {
		expect(() => new UrlMemo<string>(0)).toThrow(RangeError);
	});
});
