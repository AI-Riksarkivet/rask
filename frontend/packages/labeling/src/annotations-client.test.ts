/**
 * The insert wire carries the active-learning scores — and defaults them HONESTLY.
 *
 * `InsertRow` grew `confidence`/`uncertainty` when the assist path stopped dropping the scores the
 * service already sent (the review queue ranks by them; for a while every prediction landed
 * unrankable). The factory's defaults are the load-bearing half: a HUMAN row must carry null —
 * "no model score" — never an invented number that would shuffle it into the model-ranked half of
 * the queue, and a prediction's stated scores must pass through untouched.
 */

import { describe, expect, it } from 'vitest';
import { buildSavePayload, makeInsertRow } from './annotations-client.js';

describe('insert-row scores', () => {
	it('a human-drawn row defaults BOTH scores to null, not to a number', () => {
		const row = makeInsertRow({ shape_type: 'bbox' });

		expect(row.confidence).toBeNull();
		expect(row.uncertainty).toBeNull();
		expect(row.status).toBe('accepted');
		expect(row.source).toBe('human');
	});

	it('a prediction keeps the scores its producer stated', () => {
		const row = makeInsertRow({
			shape_type: 'polygon',
			status: 'prediction',
			source: 'model:sam-click',
			confidence: 0.85,
			uncertainty: 0.3,
		});

		expect(row.confidence).toBe(0.85);
		expect(row.uncertainty).toBe(0.3);
	});

	it('the save payload passes inserts through UNTOUCHED — scores included', () => {
		const insert = makeInsertRow({ shape_type: 'bbox', confidence: 0.7, uncertainty: 0.45 });
		const payload = buildSavePayload({
			overrides: new Map(),
			geoEdits: new Map(),
			temporalEdits: new Map(),
			inserts: [insert],
			deletes: [],
			version: 3,
			resolveId: () => null,
		});

		expect(payload.inserts).toEqual([insert]);
	});
});
