/**
 * "One store, one poll" — enforced, not just documented.
 *
 * `service-health.svelte.ts` opens with that invariant and gives the reason: two independent fetches of
 * `/api/health` would eventually show a green dot beside a disabled mode. The invariant was still violated
 * the day it was written — `descriptor-store.svelte.ts` called `getHealth()` directly to derive the default
 * dataset id, so an ordinary media page load made a second request for the same endpoint (measured through
 * the ingress, 2026-07-26). A docstring cannot fail a build, which is why this file exists.
 *
 * Two halves, because either alone is escapable:
 *   - the behavioural half proves the shared path actually deduplicates, including the concurrent case —
 *     without that, moving a caller onto the store changes which line fetches and nothing else;
 *   - the structural half proves no *new* caller can reintroduce a second fetcher. It is a grep over the
 *     zone's own source, in the spirit of the repo's other claim-lint invariants.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { serviceHealth } from '$lib/service-health.svelte';

const HEALTH = {
	db: { path: '/data/transcripts_v2.lance', tables: ['docs'], chunks: 12, documents: 3 },
	embed: { ok: false, url: 'http://127.0.0.1:8001', error: 'connection refused' },
	rerank: { ok: false, url: 'http://127.0.0.1:8002', error: 'connection refused' },
};

/** Counts only health requests, so an unrelated fetch cannot make a red test look green. */
function countingFetch(): { fetch: typeof fetch; calls: () => number } {
	let calls = 0;
	const impl = vi.fn(async (input: RequestInfo | URL) => {
		if (String(input instanceof Request ? input.url : input).includes('/api/health')) calls += 1;
		return new Response(JSON.stringify(HEALTH), {
			status: 200,
			headers: { 'content-type': 'application/json' },
		});
	});
	return { fetch: impl as unknown as typeof fetch, calls: () => calls };
}

describe('the zone fetches health once', () => {
	beforeEach(() => {
		// The store is a module singleton, so a reading from a previous test would mask a duplicate fetch
		// in this one — `ensure()` short-circuits on `current`.
		serviceHealth.current = null;
		serviceHealth.error = null;
	});

	it('concurrent callers share one request', async () => {
		const { fetch: f, calls } = countingFetch();
		vi.stubGlobal('fetch', f);

		// The real race: the descriptor store awaits a reading on mount while a component subscribes in an
		// effect during the same tick. Both need the payload; only one connection may open.
		const [a, b, c] = await Promise.all([
			serviceHealth.ensure(),
			serviceHealth.ensure(),
			serviceHealth.ensure(),
		]);

		expect(calls()).toBe(1);
		expect(a?.db.path).toBe('/data/transcripts_v2.lance');
		expect(b).toBe(a); // the same object, not three parses of three responses
		expect(c).toBe(a);
		vi.unstubAllGlobals();
	});

	it('a second caller after the reading has landed fetches nothing', async () => {
		const { fetch: f, calls } = countingFetch();
		vi.stubGlobal('fetch', f);

		await serviceHealth.ensure();
		expect(calls()).toBe(1);
		await serviceHealth.ensure();
		expect(calls()).toBe(1); // this is the assertion the old direct `getHealth()` call failed
		vi.unstubAllGlobals();
	});

	it('a failed fetch resolves null and says why, rather than throwing into a caller', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn(async () => new Response('upstream is down', { status: 502 })),
		);

		expect(await serviceHealth.ensure()).toBeNull();
		expect(serviceHealth.error).toBeTruthy();

		// And the failure must not be sticky: the next call retries rather than serving a cached null,
		// otherwise a page loaded during a restart stays permanently degraded.
		const { fetch: f, calls } = countingFetch();
		vi.stubGlobal('fetch', f);
		expect((await serviceHealth.ensure())?.db.chunks).toBe(12);
		expect(calls()).toBe(1);
		vi.unstubAllGlobals();
	});
});

describe('structural: exactly one module imports getHealth', () => {
	/** Every .ts/.svelte file under the zone's src, so a new caller anywhere is seen. */
	function sources(dir: string, acc: string[] = []): string[] {
		for (const entry of readdirSync(dir, { withFileTypes: true })) {
			const path = join(dir, entry.name);
			if (entry.isDirectory()) sources(path, acc);
			else if (/\.(ts|svelte)$/.test(entry.name)) acc.push(path);
		}
		return acc;
	}

	/** An IMPORT of the symbol, not a mention of it — prose about the invariant is not a violation of it.
	 *  (This distinction is not pedantic: the first run of this test failed on `descriptor-store.svelte.ts`
	 *  because the comment explaining why it no longer calls `getHealth` contains the word.) */
	const IMPORTS_IT = /import\s*(?:type\s*)?\{[^}]*\bgetHealth\b[^}]*\}\s*from/;

	it('only service-health.svelte.ts imports getHealth', () => {
		const root = new URL('.', import.meta.url).pathname; // src/lib
		const offenders = sources(join(root, '..'))
			.filter((p) => IMPORTS_IT.test(readFileSync(p, 'utf8')))
			.map((p) => p.slice(p.indexOf('/src/') + 1))
			.filter((p) => !p.endsWith('lib/service-health.svelte.ts'));

		expect(offenders).toEqual([]);
	});

	it('and no namespace import can smuggle it in', () => {
		// The import check above is only sound while nobody reaches the module as a namespace, because
		// `api.getHealth()` would carry no matching import specifier. Assert that door is shut too.
		const root = new URL('.', import.meta.url).pathname;
		const smugglers = sources(join(root, '..'))
			.filter((p) =>
				/import\s+\*\s+as\s+\w+\s+from\s+'@repo\/media-api/.test(readFileSync(p, 'utf8')),
			)
			.map((p) => p.slice(p.indexOf('/src/') + 1));

		expect(smugglers).toEqual([]);
	});
});
