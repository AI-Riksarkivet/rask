import { describe, expect, it } from 'vitest';
import type { SearchSpec } from '@rask/explorer-api';
import { removeView, stripEphemeral, upsertView, viewsForDataset } from '$lib/saved-views';

describe('stripEphemeral', () => {
	it('drops only the uploaded image File; keeps the reproducible query incl. qVec', () => {
		const spec = {
			q: 'report',
			mode: 'hybrid',
			filters: { speaker: 'A' },
			image: new File([], 'x.png'),
			qVec: 'quarterly earnings summary', // the hybrid vector-leg TEXT — reproducible
		} as unknown as SearchSpec;
		const clean = stripEphemeral(spec);
		expect(clean.image).toBeUndefined(); // a File can't serialize
		expect(clean.qVec).toBe('quarterly earnings summary'); // kept — else hybrid views break
		expect(clean.q).toBe('report');
		expect(clean.filters).toEqual({ speaker: 'A' });
	});
});

describe('upsert / remove / forDataset', () => {
	const view = (
		name: string,
		dataset: string,
	): { name: string; dataset: string; spec: SearchSpec } => ({
		name,
		dataset,
		spec: { q: name } as SearchSpec,
	});

	it('upsert overwrites by (name, dataset) and puts newest first', () => {
		let views = [view('a', 'd1')];
		views = upsertView(views, view('b', 'd1'));
		expect(views.map((v) => v.name)).toEqual(['b', 'a']);
		views = upsertView(views, { name: 'a', dataset: 'd1', spec: { q: 'updated' } as SearchSpec });
		expect(views).toHaveLength(2); // overwrote, not duplicated
		expect(views[0]?.spec.q).toBe('updated'); // and moved to front
	});

	it('a same-named view in a DIFFERENT dataset is independent', () => {
		let views = [view('a', 'd1')];
		views = upsertView(views, view('a', 'd2'));
		expect(views).toHaveLength(2);
		expect(viewsForDataset(views, 'd1').map((v) => v.name)).toEqual(['a']);
		expect(viewsForDataset(views, 'd2').map((v) => v.name)).toEqual(['a']);
	});

	it('remove targets only the (name, dataset) pair', () => {
		const views = removeView([view('a', 'd1'), view('a', 'd2')], 'a', 'd1');
		expect(views).toEqual([view('a', 'd2')]);
	});
});
