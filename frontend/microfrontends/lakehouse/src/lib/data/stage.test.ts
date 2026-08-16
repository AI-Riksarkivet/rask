/**
 * A table's namespace, and the medallion stage derived from it.
 *
 * These exist because `namespaceOfTable` split on the FIRST `$` for as long as it had existed, which is
 * correct for exactly one shape — a flat `<ns>$<table>` — and wrong for every nested one. Namespaces
 * nest: `namespace#parent: [warehouse, namespace]` in the FGA model, and the catalog's create door takes
 * a nested id (bounded by `MAX_NAMESPACE_DEPTH`, so up to seven levels are legal). The live estate
 * happens to be flat today, which is why nothing noticed.
 *
 * The backend is the authority and says it in one line: `parent_namespace_id` is "all segments but the
 * last" (`packages/service-kit/src/service_kit/governed/fga.py:187-201`), and that id is what the grant
 * and check paths use. A frontend deriving a different one does not merely render oddly — it disagrees
 * with authorization about which object a table belongs to, and links the user to a namespace page for
 * an object that is not its parent.
 *
 * NOT COVERED, deliberately: the delimiter. `LANCE_NS_DELIMITER` is operator-settable server-side and
 * this module hardcodes `$`, as does every other frontend module that splits an id. That is a real
 * pre-existing gap and a separate change — the zone has no route to the catalog's configured delimiter
 * today, so a test here could only assert the hardcoding it would be documenting.
 */

import { describe, expect, it } from 'vitest';

import { namespaceOfTable, stageOf, stageOfTable } from './stage';

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

describe('stageOfTable', () => {
	it('still resolves the flat case it always handled', () => {
		expect(stageOfTable('bronze$pages')).toEqual({ stage: 'bronze', project: null, media: false });
	});

	it('is null when the namespace encodes no stage — a derived hint, never a catalog fact', () => {
		expect(stageOfTable('media$documents')).toBeNull();
	});

	it('a NESTED medallion zone gets no stage — a known gap, pinned here rather than fixed silently', () => {
		// `stageOf` takes a namespace NAME; `STAGE_RE` is anchored and `$` is outside its character
		// class, so a nested namespace ID never matches however it is spelled. The leaf segment does:
		expect(stageOf('acme-silver')).toEqual({ stage: 'silver', project: 'acme', media: false });
		expect(stageOf('acme$acme-silver')).toBeNull();
		expect(stageOfTable('acme$acme-silver$features')).toBeNull();

		// This is NOT a regression from the namespaceOfTable fix — the old first-delimiter split fed
		// `stageOf('acme')`, which was equally null. It is the same root cause (nesting was never
		// considered in this module) surfacing one function over, and fixing it means deciding whether a
		// nested namespace's stage comes from its LEAF segment. That is a visible behaviour change —
		// stage badges would appear where today there are none — so it is a separate call, not a rider
		// on a correctness fix. Pinned so the answer is deliberate whenever it comes.
		expect(stageOfTable('acme$transcripts$rows')).toBeNull();
	});
});
