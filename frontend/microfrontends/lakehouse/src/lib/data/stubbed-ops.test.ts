/**
 * NO ACTION IN THIS ZONE MAY BE WIRED TO A CATALOG OP THE DEPLOYED BACKEND STUBS.
 *
 * `docs/COVERAGE.md` records the live probe: the catalog wires 54/54 spec ops, but six of them are
 * genuine `NotImplementedError` stubs in the native Rust `DirectoryNamespace` the chart pins, so
 * they answer **501 for every input**. `alter_table_backfill_columns` is one of them.
 *
 * The lakehouse offered a per-column "backfill" button on the schema table that POSTed
 * `/v1/table/{id}/backfill_column`. It could not succeed — not for a wrong argument, not for a
 * missing grant, but for every caller on every table, forever. A control that can only error is the
 * one thing the estate's "show disabled, never hide" ruling does NOT permit: a denial has to be
 * stated up front, not discovered from a response.
 *
 * The gate is written against the WIRE PATH rather than a word, so the fix can (and does) keep
 * naming the op in prose — the reason a control is disabled is worth writing down, and writing it
 * down must not trip the gate that made it necessary.
 */

import { globSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const read = (p: string) => readFileSync(p, 'utf8');

/** Every zone source file — build output, dependencies and this gate's own text excluded (it
 *  spells the forbidden shape out, so it would otherwise report itself). */
const SELF = 'src/lib/data/stubbed-ops.test.ts';
function sources(): string[] {
	return globSync('src/**/*.{ts,svelte}').filter(
		(p) => !p.includes('node_modules') && !p.includes('/.svelte-kit/') && p !== SELF,
	);
}

/**
 * The six routes `docs/COVERAGE.md` classifies as native stubs (501 on every call).
 *
 * These are matched as PATH SHAPES, not as a word list under a shared prefix. The first version of
 * this gate anchored every op on `v1/table/`, which structurally could not match five of the six:
 * the materialized-view ops live under `/v1/materialized_view`, `alter_transaction` under
 * `/v1/transaction`, and the two batch routes spell their action with a HYPHEN (`batch-commit`,
 * `batch-create`) while the op names use underscores. It caught `backfill_column` and nothing else,
 * so it read as a six-op gate while guarding one — the shape of a test that cannot fail.
 */
const STUBBED_ROUTES = [
	/v1\/table\/[^`'"]*\/backfill_column/,
	/v1\/materialized_view\/[^`'"]*\/(create|refresh)/,
	/v1\/table\/batch-commit/,
	/v1\/table\/version\/batch-create/,
	/v1\/transaction\/[^`'"]*\/alter/,
];
const callsAStub = (src: string) => STUBBED_ROUTES.some((re) => re.test(src));

describe('the zone never calls a catalog op the pinned backend answers 501', () => {
	it('has a floor — a shrinking file set would make the gate vacuous', () => {
		expect(sources().length).toBeGreaterThan(50);
	});

	it('no source builds a request path for a stubbed op', () => {
		const offenders = sources().filter((p) => callsAStub(read(p)));

		expect(
			offenders,
			'this door answers 501 for every caller — state the unavailability instead of calling it',
		).toEqual([]);
	});

	it('the schema section states WHY backfill is unavailable rather than offering the stub', () => {
		// "Show disabled, never hide": the affordance stays on screen, carrying its reason.
		const src = read('src/lib/data/table-detail/SchemaSection.svelte');

		expect(src, 'the backfill affordance must not be deleted, only disabled').toContain(
			'aria-label="backfill',
		);
		expect(src, 'the reason belongs at the control, from the one place that words it').toContain(
			'BACKFILL_UNAVAILABLE',
		);
		expect(src, 'no caller of the 501 door may remain').not.toContain('backfillColumn');
	});
});
