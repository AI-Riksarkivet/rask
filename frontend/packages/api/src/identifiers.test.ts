/**
 * The catalog's identifier grammar.
 *
 * These live in the shared package rather than in a zone because the bug they pin was a CROSS-ZONE
 * one: the lakehouse fixed `parentNamespace` in its own `$lib`, and the `home` zone — which cannot
 * import another zone's `$lib` — kept a copy that split on the first delimiter. A test parked in the
 * lakehouse would have gone on passing while the home zone stayed wrong, which is exactly what
 * happened.
 */

import { describe, expect, it } from 'vitest';

import { DELIMITER, leafSegment, namespacePrefix, parentNamespace } from './identifiers';

describe('parentNamespace', () => {
	it('takes every segment but the last, matching the backend', () => {
		expect(parentNamespace('bronze$pages')).toBe('bronze');
	});

	it('treats a bare name as its own root — the registry rule', () => {
		expect(parentNamespace('pages')).toBe('pages');
	});

	it('keeps the WHOLE nested namespace, not just its first segment', () => {
		// THE regression, and the reason this module exists. `indexOf` answers 'acme' here — a different
		// FGA object from 'acme$bronze', with different grants and its own detail page. The home zone's
		// access explorer built its namespace picker this way, so for any nested table it offered an
		// object that was not the parent and omitted the one that was.
		expect(parentNamespace('acme$bronze$pages')).toBe('acme$bronze');
	});

	it.each([
		['a$b$c$t', 'a$b$c'],
		['a$b$c$d$t', 'a$b$c$d'],
		['a$b$c$d$e$f$t', 'a$b$c$d$e$f'],
	])('holds at depth: %s -> %s', (id, expected) => {
		// Up to MAX_NAMESPACE_DEPTH levels are legal, so a fix that only handled two segments would pass
		// the case above and fail these.
		expect(parentNamespace(id)).toBe(expected);
	});
});

describe('leafSegment', () => {
	it('is the object own name, without its ancestors', () => {
		expect(leafSegment('acme$bronze$pages')).toBe('pages');
		expect(leafSegment('pages')).toBe('pages');
	});
});

describe('namespacePrefix', () => {
	it('keeps the trailing delimiter, and is empty at the root', () => {
		expect(namespacePrefix('acme$bronze$pages')).toBe('acme$bronze$');
		expect(namespacePrefix('pages')).toBe('');
	});
});

describe('DELIMITER', () => {
	it('matches what the chart pins into the catalog', () => {
		// Not a tautology: it pins the frontend to `chart/templates/services.yaml`, which sets
		// LANCE_NS_DELIMITER to a LITERAL "$" with no values.yaml knob. If someone makes the chart
		// configurable, this constant is the thing that must change with it — and this line is where
		// they will be told so.
		expect(DELIMITER).toBe('$');
	});
});
