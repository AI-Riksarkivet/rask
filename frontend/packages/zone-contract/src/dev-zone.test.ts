/**
 * The single-zone dev loop and the zone's e2e loop must point at the SAME fake upstreams.
 *
 * `scripts/dev-zone.ts` and each zone's `playwright.config.ts` both answer one question — "which env
 * names point this zone at its mocks" — and they answer it in two files. That is a drift pair, and the
 * drift is SILENT in the direction that matters: if a zone's e2e config mocks four upstreams and the
 * dev loop points at three, the developer's zone reads a live-looking surface that is actually a
 * connection refused on the fourth, while the suite stays green. Exactly the class that produced
 * `MEDALLION_API` (chart injected it, nothing read it) and `COMPUTE_SERVE_URL` (code read a name the
 * chart had stopped setting).
 *
 * So: every upstream env name a zone's `playwright.config.ts` sets on its dev server must also be set
 * by that zone's `dev-zone.ts` stack.
 *
 * NOT asserted in the other direction. `dev-zone.ts` legitimately sets names the e2e config does not:
 * `LANCE_BACKEND` / `VIEWER_BACKEND` are the CLIENT-side vite proxy targets, which only matter when a
 * human drives a browser — Playwright's specs mock browser-side reads with `page.route` instead.
 *
 * `OIDC_*` is excluded by design: the e2e configs set it to force the governed path ON, and the dev
 * loop deliberately leaves auth OFF (no Dex to run). `JETSTREAM_EXPECTED_CONSUMERS` is excluded too —
 * it is an expectation list, not an upstream address.
 */
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, zoneDirs } from './manifest';

/** Env names that are not upstream addresses, so the two loops need not agree on them. */
const NOT_AN_UPSTREAM = /^(OIDC_|SESSION_SECRET$|JETSTREAM_|CI$|NODE_ENV$|PORT$)/;

/** The uppercase env keys a zone's playwright.config.ts sets on a dev server.
 *
 *  A regex rather than an import: the config is a `defineConfig` call with computed values, and
 *  evaluating it would start web servers. Over-matching is the safe direction — a key that is not
 *  really env would only make this gate stricter, and the `NOT_AN_UPSTREAM` filter absorbs the ones
 *  that showed up in practice. */
function e2eUpstreamKeys(zone: string): string[] {
	const config = resolve(FRONTEND_ROOT, `microfrontends/${zone}/playwright.config.ts`);
	if (!existsSync(config)) return [];
	const keys = [...readFileSync(config, 'utf8').matchAll(/^\s+([A-Z][A-Z0-9_]*):/gm)].flatMap(
		(m) => (m[1] ? [m[1]] : []),
	);
	return [...new Set(keys)].filter((k) => !NOT_AN_UPSTREAM.test(k)).sort();
}

/** The env keys `dev-zone.ts` declares for a zone, read from its ZONE_STACKS entry.
 *
 *  Parsed rather than imported for one reason worth stating: importing the module executes its
 *  top-level `process.exit(await main())`. Keeping the gate static also means it cannot be fooled by
 *  a stack whose `env` closure only produces a key under some condition. */
function devZoneKeys(zone: string): string[] | null {
	const source = readFileSync(resolve(import.meta.dirname, 'dev-zone.ts'), 'utf8');
	// The zone's block: `\n\tzone: {` up to the next top-level `\n\t},`
	const block = new RegExp(`\\n\\t${zone}: \\{([\\s\\S]*?)\\n\\t\\},`).exec(source);
	if (!block?.[1]) return null;
	const keys = [...block[1].matchAll(/^\t{3}([A-Z][A-Z0-9_]*):/gm)].flatMap((m) =>
		m[1] ? [m[1]] : [],
	);
	return [...new Set(keys)].sort();
}

describe('the single-zone dev loop mocks everything the e2e loop mocks', () => {
	const zones = zoneDirs();

	it('finds zones to check (guards the scanner itself)', () => {
		expect(zones.length).toBeGreaterThan(5);
	});

	for (const zone of zones) {
		const expected = e2eUpstreamKeys(zone);
		// A zone with no playwright.config.ts has no e2e loop to agree with. `compute` and `studio` are
		// this case today, and it is the same gap that leaves them outside every local gate.
		if (expected.length === 0) continue;

		it(`${zone}: dev-zone.ts points at every upstream its e2e config mocks`, () => {
			const declared = devZoneKeys(zone);
			expect(
				declared,
				`${zone} has a playwright.config.ts that mocks [${expected.join(', ')}], but no ZONE_STACKS ` +
					`entry in packages/zone-contract/src/dev-zone.ts — so \`make dev-zone ZONE=${zone}\` would run the ` +
					`zone against unmocked upstreams while the suite stays green.`,
			).not.toBeNull();

			const missing = expected.filter((k) => !(declared ?? []).includes(k));
			expect(
				missing,
				`${zone}'s e2e config mocks [${missing.join(', ')}] but dev-zone.ts does not point the zone ` +
					`at them. The dev loop would read a connection refused where the suite reads a mock.`,
			).toEqual([]);
		});
	}
});
