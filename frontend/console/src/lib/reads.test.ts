import { describe, expect, it } from 'vitest';

import { query } from './reads.svelte';

/** A read whose every start waits for the test to answer it, in whatever order the test chooses. */
function heldRead() {
	const answer: ((value: string) => void)[] = [];
	const read = query(() => new Promise<string>((resolve) => answer.push(resolve)));
	return { read, answer };
}

/** Let every pending promise callback run: a read starts after its input is validated. */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe('query', () => {
	it('keeps the newest read when an older one lands after it', async () => {
		const { read, answer } = heldRead();
		const held = read();
		void held.refresh();
		await settle();

		answer[1]!('newer');
		await settle();
		answer[0]!('older');
		await settle();

		expect(held.current).toBe('newer');
		expect(held.loading).toBe(false);
	});

	it('stays loading while the newest read is out, though an older one landed', async () => {
		const { read, answer } = heldRead();
		const held = read();
		void held.refresh();
		await settle();

		answer[0]!('older');
		await settle();

		expect(held.loading).toBe(true);
		expect(held.current).toBeUndefined();
	});
});

describe('visit', () => {
	it('shows no answer kept from before it, and the one read during it', async () => {
		const { read, answer } = heldRead();
		const held = read();
		await settle();
		answer[0]!('kept');
		await settle();
		void held.refresh();
		const visit = held.visit();
		await settle();

		expect(held.current).toBe('kept');
		expect(visit.settled).toBe(false);
		expect(visit.current).toBeUndefined();

		answer[1]!('read now');
		await settle();
		expect(visit.settled).toBe(true);
		expect(visit.current).toBe('read now');
	});

	it('settles on a failed read without taking the kept answer for its own', async () => {
		const answer: ((value: string) => void)[] = [];
		const fail: ((err: Error) => void)[] = [];
		const read = query(
			() =>
				new Promise<string>((resolve, reject) => {
					answer.push(resolve);
					fail.push(reject);
				}),
		);
		const held = read();
		await settle();
		answer[0]!('kept');
		await settle();
		void held.refresh().catch(() => {});
		const visit = held.visit();
		await settle();
		fail[1]!(new Error('down'));
		await settle();

		expect(visit.settled).toBe(true);
		expect(visit.current).toBeUndefined();
	});

	it("counts an answer that settled within the reuse window as this visit's", async () => {
		const { read, answer } = heldRead();
		read();
		await settle();
		answer[0]!('just read');
		await settle();
		const visit = read().visit();

		expect(visit.settled).toBe(true);
		expect(visit.current).toBe('just read');
	});
});
