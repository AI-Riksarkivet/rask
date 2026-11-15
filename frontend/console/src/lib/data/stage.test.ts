/**
 * A table's namespace, and the tier its namespaces declare.
 *
 * These exist because `namespaceOfTable` split on the FIRST `$` for as long as it had existed, which is
 * correct for exactly one shape — a flat `<ns>$<table>` — and wrong for every nested one. Namespaces
 * nest: `namespace#parent: [warehouse, namespace]` in the FGA model, and the catalog's create door takes
 * a nested id (bounded by `MAX_NAMESPACE_DEPTH`, so up to seven levels are legal). The live estate
 * happens to be flat today, which is why nothing noticed.
 *
 * The backend is the authority and says it in one line: `parent_namespace_id` is "all segments but the
 * last" (`packages/lakehouse/src/lakehouse/governed/fga.py:187-201`), and that id is what the grant
 * and check paths use. A frontend deriving a different one does not merely render oddly — it disagrees
 * with authorization about which object a table belongs to, and links the user to a namespace page for
 * an object that is not its parent.
 *
 * The grammar itself lives in `#lib/api/identifiers.ts` and is covered by its own tests; this
 * file covers the tier a namespace declares, read the way the catalog reads it.
 */

import { describe, expect, it } from 'vitest';

import { declaredTier, namespaceOfTable, tierCandidates } from './stage';

describe('namespaceOfTable', () => {
	it('takes every segment but the last, matching the backend', () => {
		expect(namespaceOfTable('bronze$pages')).toBe('bronze');
	});

	it('treats a bare name as its own root — the registry rule', () => {
		expect(namespaceOfTable('pages')).toBe('pages');
	});

	it('keeps the WHOLE nested namespace, not just its first segment', () => {
		// The regression. `indexOf` answered 'acme' here — a different object from 'acme$bronze', with
		// different grants and its own detail page.
		expect(namespaceOfTable('acme$bronze$pages')).toBe('acme$bronze');
	});

	it.each([
		['a$b$c$t', 'a$b$c'],
		['a$b$c$d$t', 'a$b$c$d'],
		['a$b$c$d$e$f$t', 'a$b$c$d$e$f'],
	])('holds at depth: %s -> %s', (table, expected) => {
		// Up to MAX_NAMESPACE_DEPTH levels are legal, so "one level of nesting" is not the ceiling and a
		// fix that only handled two segments would pass the case above and fail these.
		expect(namespaceOfTable(table)).toBe(expected);
	});
});

describe('declaredTier', () => {
	it('reads the tier a namespace declares, and only from its rask.tier property', () => {
		expect(declaredTier({ 'rask.tier': 'silver' })).toEqual({ stage: 'silver' });
		// A namespace NAMED like a tier declares nothing (ADR 0160): its name is the user's.
		expect(declaredTier({})).toBeNull();
		expect(declaredTier({ 'rask.tier': 'platinum' })).toBeNull();
	});
});

describe('tierCandidates', () => {
	it('names every namespace on the path, shallowest first, and never the warehouse', () => {
		expect(tierCandidates('acme$raw$audio')).toEqual(['acme$raw', 'acme$raw$audio']);
		expect(tierCandidates('acme')).toEqual([]);
	});
});
