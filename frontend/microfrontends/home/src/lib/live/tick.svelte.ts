import { onMount, untrack } from 'svelte';
import { controlCursor } from './feeds.remote';

/** The shape of a `query.live` instance this zone consumes as a cursor — see `feeds.remote.ts`. */
export interface LiveCursor {
	readonly current: number | undefined;
	readonly connected: boolean;
}

/**
 * Read once, then re-read on every advance of a live cursor. This is the replacement for the
 * `$effect(() => { load(); const t = setInterval(load, POLL_MS); return () => clearInterval(t) })`
 * that the estate carried before the live cursors landed.
 *
 * Four rules, and the last two are each here because getting them wrong broke a test:
 *
 *  - The first read is UNCONDITIONAL, so a surface renders immediately rather than waiting for a stream
 *    to connect (and a surface whose cursor never connects at all still shows its data and its own
 *    honest offline state).
 *  - `key` — a route parameter or a selection — stays TRACKED, so a navigation re-reads at once instead
 *    of waiting for the estate to change. Its value is passed to `read` so a loader can drop a response
 *    that a later navigation superseded.
 *  - The cursor's ARRIVAL is not a change. `.current` is undefined until the stream delivers its first
 *    value, and treating `undefined → 137` as an advance makes every page load read twice — once eagerly
 *    and once when the stream lands, for no new information. Only a cursor that moves after that means
 *    the estate did something.
 *  - The stream is OPENED ON MOUNT, never at component init, so it does not exist during SSR. A live
 *    query touched while rendering makes the server hold the render until the generator's first value —
 *    which for these cursors is a round trip to the catalog on the critical path of every page. A page
 *    whose HTML waits on a *liveness probe* is strictly worse than the timers this replaced. A live feed
 *    is a client concern; the server's job is to render the page.
 *
 * The read is `untrack`ed (the AuditViewer convention): a loader assigns the `$state` it also reads, so
 * tracking it would re-enter.
 *
 * Nothing here is a timer. An idle surface issues exactly one request and then stays quiet.
 */
export function liveRead<K = void>(
	open: () => LiveCursor,
	read: (key: K) => unknown,
	key?: () => K,
): void {
	let cursor = $state<LiveCursor | null>(null);
	onMount(() => {
		cursor = open();
	});

	let started = false;
	let lastCursor: number | undefined;
	let lastKey: K | undefined;

	$effect(() => {
		const seq = cursor?.current;
		const current = key?.() as K;
		const keyChanged = current !== lastKey;
		const cursorMoved = lastCursor !== undefined && seq !== lastCursor;
		lastKey = current;
		lastCursor = seq;

		if (started && !keyChanged && !cursorMoved) return;
		started = true;
		untrack(() => {
			void read(current);
		});
	});
}

/**
 * The CONTROL-plane cursor — for the surfaces whose changes are governance mutations (grants,
 * warehouses, tenants, promotions) rather than data movement. Those never touch a dataset, so they never
 * move the lineage cursor; `/settings/access` re-reads on this one instead. SvelteKit dedupes identical
 * live-query instances onto a single connection, so every control-backed surface on a page rides the
 * same stream and they cannot disagree about when the estate changed.
 *
 * Pass it (not a call of it) to `liveRead`, which opens it on mount:
 *
 *     liveRead(controlTick, () => load());
 */
export function controlTick(): LiveCursor {
	const feed = controlCursor();
	return {
		get current() {
			return feed.current;
		},
		get connected() {
			return feed.connected;
		},
	};
}
