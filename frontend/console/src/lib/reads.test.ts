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
