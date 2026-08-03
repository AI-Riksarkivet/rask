import { describe, expect, it } from 'vitest';
import * as v from 'valibot';
import { RASK_SELECT, SelectDetailSchema } from '@rask/dockview/contract';

/**
 * The CROSS-ZONE element contract, pinned. TypeScript cannot check across a script-tag boundary —
 * the compositor loads element bundles by URL — so the event name and detail shape are the whole
 * wire protocol between zones. A rename here is a breaking change for every element bundle already
 * deployed; this suite makes that a red gate instead of a runtime mystery (review finding: the
 * contract was pinned by no test).
 */
describe('the rask:select element contract', () => {
	it('the event name is frozen', () => {
		expect(RASK_SELECT).toBe('rask:select');
	});

	it('the detail shape is exactly {source, kind, id, label} — all strings, none optional', () => {
		expect(
			v.safeParse(SelectDetailSchema, {
				source: 'rask-compute-jobs',
				kind: 'ray-job',
				id: 'x',
				label: 'y',
			}).success,
		).toBe(true);
		for (const missing of ['source', 'kind', 'id', 'label']) {
			const detail: Record<string, string> = { source: 's', kind: 'k', id: 'i', label: 'l' };
			delete detail[missing];
			expect(v.safeParse(SelectDetailSchema, detail).success, `${missing} must be required`).toBe(
				false,
			);
		}
		// Unknown keys are tolerated (additive evolution), non-string values are not.
		expect(
			v.safeParse(SelectDetailSchema, { source: 1, kind: 'k', id: 'i', label: 'l' }).success,
		).toBe(false);
	});
});
