import { error } from '@sveltejs/kit';
import { getRequestEvent, query } from '#lib/reads.svelte.ts';
import { CATALOG_API } from '#lib/upstreams.ts';
import { parse } from '@rask/api';
import { EventsPageSchema, type ControlEvent } from '#lib/admin/control.ts';

const POLL_MS = 5000;
const WINDOW = 200; // the recent window the console renders (older events fall off)

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// The estate-wide control-plane change-event feed (GET /management/v1/events), as a live cursor. The generator runs
// in the browser (`query.live`, `#lib/reads.svelte.ts`): it holds the cursor, an accumulated recent window and
// event_id dedup, polls the catalog every POLL_MS with the signed-in admin's bearer, and yields the window whenever
// it changes. A non-ok answer other than 403 is polled again on the next tick; a 403 or a fetch that throws ends the
// feed, and the page renders that error. No argument: the catalog gates on the estate-admin relation
// `can_observe_events` over the fixed root object, so a non-estate-admin bearer gets a terminal 403 here.
// (This also cushions the per-replica-cursor boundary: dedup + reset make a multi-replica catalog degrade
// noisily, never wrongly — see docs/adr/0028-control-events-per-replica-cursor-boundary.md.)
export const controlEvents = query.live(async function* (): AsyncGenerator<ControlEvent[]> {
	const { locals, fetch } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	const headers: Record<string, string> = bearer ? { authorization: `Bearer ${bearer}` } : {};

	let cursor = 0;
	let first = true;
	const window: ControlEvent[] = [];
	const seen = new Set<string>();

	for (;;) {
		const res = await fetch(`${CATALOG_API}/management/v1/events?since=${cursor}`, { headers });
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
