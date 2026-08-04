import { beforeEach, describe, expect, it } from 'vitest';
import { getHealth } from './api';
import { resetApiBase } from './base';

/**
 * `/api/health` answers 200 with `db: null` when the media service is up but no corpus dataset resolves,
 * and this client must ACCEPT that.
 *
 * Live-proof 2026-07-28, defect 4. The endpoint used to resolve the default dataset before answering, so
 * a deployment with an empty corpus volume — now the chart's default, because the old unconditional
 * hostPath wedged every fresh cluster in ContainerCreating — returned
 * `404 dataset 'transcripts_v2' not found under /media-corpus` to the ONE endpoint both the media and the
 * annotator zone poll on every page load. Measured through both zones' BFF proxies: a console 404 per
 * load, a permanently red status dot, and the zone reporting "backend unreachable" about a backend that
 * was Running and healthy with nothing loaded.
 *
 * The backend now degrades (db: null + db_error) — but a tolerant server and a strict client is still a
 * broken page: the valibot schema would have thrown on the missing `db`, turning a 200 into a client-side
 * parse error and changing nothing a user could see. Both halves are the fix, so both are pinned.
 */
beforeEach(() => resetApiBase());

/** A fetch that answers one JSON body with 200, the way the degraded probe really does. */
function respondWith(body: unknown): typeof fetch {
	return (() =>
		Promise.resolve(
			new Response(JSON.stringify(body), {
				status: 200,
				headers: { 'content-type': 'application/json' },
			}),
		)) as unknown as typeof fetch;
}

const DEAD_PING = { ok: false, url: 'http://127.0.0.1:8001', error: 'ConnectionError: refused' };

describe('health with no dataset loaded', () => {
	it('parses a 200 whose db is null', async () => {
		const health = await getHealth(
			respondWith({
				db: null,
				db_error: "NotFoundError: dataset 'transcripts_v2' not found under /media-corpus",
				embed: DEAD_PING,
				rerank: DEAD_PING,
			}),
		);
		expect(health.db).toBeNull();
		// The reason must survive parsing — it is what lets the UI say "no dataset" instead of guessing
		// "unreachable", which is the entire behavioural difference this fix buys.
		expect(health.db_error).toContain('transcripts_v2');
	});

	it('parses a 200 that omits db entirely', async () => {
		// `nullish`, not `nullable`: an older/leaner backend may omit the key rather than send null, and a
		// probe is the last place that should fail closed on a missing optional field.
		const health = await getHealth(respondWith({ embed: DEAD_PING, rerank: DEAD_PING }));
		expect(health.db ?? null).toBeNull();
	});

	it('still reports encoder capability when there is no dataset', async () => {
		// The zone gates its Meaning/Hybrid search modes on `embed.ok`. While health 404'd, the store's
		// reading stayed null and the modes fell back to "assume available" — offering a mode that 503s.
		const health = await getHealth(
			respondWith({ db: null, db_error: 'x', embed: DEAD_PING, rerank: DEAD_PING }),
		);
		expect(health.embed.ok).toBe(false);
		expect(health.rerank.ok).toBe(false);
	});

	it('still parses a fully healthy response', async () => {
		const health = await getHealth(
			respondWith({
				db: {
					path: '/media-corpus/transcripts_v2.lance',
					tables: ['chunks'],
					chunks: 3,
					documents: 1,
				},
				embed: { ok: true, url: 'http://e:8001' },
				rerank: { ok: true, url: 'http://r:8002' },
			}),
		);
		expect(health.db?.path).toBe('/media-corpus/transcripts_v2.lance');
		expect(health.db?.chunks).toBe(3);
	});
});
