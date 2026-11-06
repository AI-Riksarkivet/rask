import { describe, expect, it } from 'vitest';
import { sameOriginPath } from './return-path.ts';

const ORIGIN = 'https://console.example';

describe('sameOriginPath', () => {
	it.each([
		['a backslash the parser reads as a second slash', '/\\evil.com'],
		['a tab the parser strips between two slashes', '/\t/evil.com'],
		['a protocol-relative path', '//evil.com'],
		['an absolute URL to another origin', 'https://evil.com'],
	])('refuses %s', (_case, candidate) => {
		expect(sameOriginPath(candidate, ORIGIN)).toBe('/');
	});

	it('keeps a same-origin path with its search and hash', () => {
		expect(sameOriginPath('/catalog/tables?x=1#y', ORIGIN)).toBe('/catalog/tables?x=1#y');
	});
});
