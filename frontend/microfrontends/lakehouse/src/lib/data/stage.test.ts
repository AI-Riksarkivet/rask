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
 * The grammar itself now lives in `@rask/api/identifiers` and is covered by that package's tests; this
 * file keeps only the medallion-stage derivation built on top of it. The earlier note here claimed the
 * delimiter was an open gap awaiting a config endpoint — it is not. `chart/templates/services.yaml`
 * sets `LANCE_NS_DELIMITER` to a LITERAL `"$"` with no values.yaml knob, so changing it is already a
 * repo edit and a constant in the same repo ships in the same commit. That question is closed.
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

	it('a NESTED medallion zone resolves its stage from the LEAF segment', () => {
		// Was pinned as a known gap and ruled 2026-08-16. `STAGE_RE` is anchored with `$` outside its
		// character class, so a nested id matched nothing and the badge silently vanished — while the
		// same namespace one rung up showed `silver`. The stage belongs to the zone the data sits IN;
		// the ancestors above it are tenancy, not tier.
		expect(stageOf('acme-silver')).toEqual({ stage: 'silver', project: 'acme', media: false });
		expect(stageOf('acme$acme-silver')).toEqual({ stage: 'silver', project: 'acme', media: false });
		expect(stageOfTable('acme$acme-silver$features')).toEqual({
			stage: 'silver',
			project: 'acme',
			media: false,
		});
	});

	it('still returns null when the LEAF encodes no stage — the ancestors must not supply one', () => {
		// The other direction, and the reason this is leaf-only rather than "any segment": a table in
		// `acme-silver$scratch` is in `scratch`, which is not a medallion zone. Reading the stage off an
		// ancestor would badge it `silver` and claim a tier the data does not have.
		expect(stageOf('acme-silver$scratch')).toBeNull();
		expect(stageOfTable('acme$transcripts$rows')).toBeNull();
	});
});
