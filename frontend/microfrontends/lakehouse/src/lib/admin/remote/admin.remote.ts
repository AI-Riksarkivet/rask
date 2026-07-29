import { error } from '@sveltejs/kit';
import { getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import { parse } from '@rask/api';
import { EventsPageSchema, type ControlEvent } from '$lib/admin/control';

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';
const POLL_MS = 5000;
const WINDOW = 200; // the recent window the console renders (older events fall off)

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// The estate-wide control-plane change-event feed (GET /v1/events), as a LIVE query. The generator runs on
// the zone (SvelteKit/Bun) server: it holds the cursor + an accumulated recent window + event_id dedup,
// polls the catalog with the signed-in admin's bearer, and yields the window WHENEVER it changes. SvelteKit
// streams each yield to the browser and owns reconnect (exponential backoff + on `navigator.onLine`) — no
// hand-rolled SSE, no client cursor bookkeeping. No argument: the catalog gates on the estate-admin relation
// `can_observe_events` over the fixed root object, so a non-estate-admin bearer gets a terminal 403 here.
// (This also cushions the per-replica-cursor boundary: dedup + reset make a multi-replica catalog degrade
// noisily, never wrongly — see docs/DECISIONS.md #control-events--per-replica-cursor-boundary.)
export const controlEvents = query.live(async function* (): AsyncGenerator<ControlEvent[]> {
	const { locals, fetch } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	const headers: Record<string, string> = bearer ? { authorization: `Bearer ${bearer}` } : {};

	let cursor = 0;
	let first = true;
	const window: ControlEvent[] = [];
	const seen = new Set<string>();

	for (;;) {
		const res = await fetch(`${CATALOG_API}/v1/events?since=${cursor}`, { headers });
		if (res.status === 403) error(403, 'not authorized to observe the control-plane event feed');
		if (res.ok) {
			const page = parse(EventsPageSchema, await res.json());
			cursor = page.cursor;
			let changed = page.reset;
			if (page.reset) {
				window.length = 0;
				seen.clear();
			}
			for (const event of page.events) {
				if (seen.has(event.event_id)) continue;
				seen.add(event.event_id);
				window.push(event);
				changed = true;
			}
			if (window.length > WINDOW) {
				for (const dropped of window.splice(0, window.length - WINDOW))
					seen.delete(dropped.event_id);
			}
			// Always yield the first successful poll (even empty) so the consumer resolves + renders the
			// empty state, rather than hanging in `pending`; after that, only yield on a real change.
			if (changed || first) {
				first = false;
				yield window.slice().reverse(); // newest first; a fresh copy each yield
			}
		}
		await sleep(POLL_MS);
	}
});
