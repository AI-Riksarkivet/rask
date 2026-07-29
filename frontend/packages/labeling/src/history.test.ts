import { describe, expect, it } from 'vitest';
import { diffSignatures, rowSignature, type RowSig } from './history';

const sig = (r: Partial<RowSig>): string =>
	rowSignature({ label: '', status: '', shape: '', group: '', text: '', ...r });

describe('diffSignatures', () => {
	it('classifies added / removed / changed by id and signature', () => {
		const past = new Map([
			['a', sig({ label: 'line', status: 'accepted' })],
			['b', sig({ label: 'figure', status: 'accepted' })],
		]);
		const current = new Map([
			['a', sig({ label: 'line', status: 'rejected' })], // status changed
			['c', sig({ label: 'word', status: 'accepted' })], // new
		]);
		const d = diffSignatures(past, current);
		expect(d.added).toEqual(['c']);
		expect(d.removed).toEqual(['b']);
		expect(d.changed).toEqual(['a']);
	});

	it('reports no diff for identical signatures', () => {
		const a = new Map([['x', sig({ label: 'l', status: 'accepted' })]]);
		const b = new Map([['x', sig({ label: 'l', status: 'accepted' })]]);
		expect(diffSignatures(a, b)).toEqual({ added: [], removed: [], changed: [] });
	});

	it('rowSignature ignores fields outside the reviewable set (geometry-agnostic)', () => {
		// Same reviewable content ⇒ same signature; a geometry field is not part of RowSig.
		expect(sig({ label: 'l', status: 's' })).toBe(sig({ label: 'l', status: 's' }));
		expect(sig({ label: 'l' })).not.toBe(sig({ label: 'm' }));
	});

	it('rowSignature is injective across field boundaries (no separator collision)', () => {
		// ("ab","") must NOT equal ("a","b") — a shifted field split is a real change.
		expect(sig({ label: 'ab', status: '' })).not.toBe(sig({ label: 'a', status: 'b' }));
		// a value containing a quote is still safe (JSON-encoded)
		expect(sig({ label: 'a"b' })).not.toBe(sig({ label: 'ab' }));
		const past = new Map([['x', sig({ label: 'ab', status: '' })]]);
		const current = new Map([['x', sig({ label: 'a', status: 'b' })]]);
		expect(diffSignatures(past, current).changed).toEqual(['x']); // the edit is detected
	});
});
