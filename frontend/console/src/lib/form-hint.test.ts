import { describe, expect, it } from 'vitest';
import { missingFieldsHint } from './form-hint.ts';

const hint = (name: string, id: string, bucket: string) =>
	missingFieldsHint('Creating', [
		['a name', name],
		['an id', id],
		['a bucket', bucket],
	]);

describe('missingFieldsHint', () => {
	it('names a single empty field alone', () => {
		expect(hint('n', ' ', 'b')).toBe('Creating needs an id.');
	});

	it('names several empty fields as a list', () => {
		expect(hint('', '', '')).toBe('Creating needs a name, an id and a bucket.');
	});

	it('is null once every field is filled', () => {
		expect(hint('n', 'i', 'b')).toBeNull();
	});
});
