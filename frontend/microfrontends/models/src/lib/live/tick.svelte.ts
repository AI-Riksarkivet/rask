import { onMount, untrack } from 'svelte';
import { lineageFeed } from './feeds.remote';

/** The shape of a `query.live` instance this zone consumes as a cursor — see `feeds.remote.ts`. */
export interface LiveCursor {
	readonly current: number | undefined;
	readonly connected: boolean;
}

/**
 * Read once, then re-read on every advance of a live cursor — the estate's replacement for
 * `$effect(() => { load(); const t = setInterval(load, POLL_MS); return () => clearInterval(t) })`.
 *
 * Ported from the lakehouse with the registry it serves. Deliberately WITHOUT that zone's
 * `controlTick`: the control cursor's consumers (the FGA workbench, the warehouse and tenant
 * consoles) all stayed behind, so carrying it here would ship a live stream nothing opens — and
 * `controlCursor` itself is not declared in this zone's `feeds.remote.ts`, because a `query.live`
 * only gets an endpoint in the app that declares it. A promotion is the one control-plane mutation
 * this zone performs, and `promoteModel` already single-flights its own re-read.
 *
 * Four rules, and the last two are each here because getting them wrong broke a test in the zone
 * this came from:
 *
 *  - The first read is UNCONDITIONAL, so a surface renders immediately rather than waiting for a
 *    stream to connect (and a surface whose cursor never connects still shows its data and its own
 *    honest offline state).
 *  - `key` — a route parameter or a selection — stays TRACKED, so a navigation re-reads at once
 *    instead of waiting for the estate to change.
 *  - The cursor's ARRIVAL is not a change. `.current` is undefined until the stream delivers its
 *    first value, and treating `undefined → 137` as an advance makes every page load read twice —
 *    once eagerly and once when the stream lands, for no new information.
 *  - The stream is OPENED ON MOUNT, never at component init, so it does not exist during SSR. A live
 *    query touched while rendering makes the server hold the render until the generator's first
 *    value — a round trip to the lineage plane on the critical path of every page.
 *
 * The read is `untrack`ed: a loader assigns the `$state` it also reads, so tracking it would re-enter.
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
 * The lineage cursor, taken from the ONE shared `lineageFeed` stream — the feed the shell's
 * notification bell also renders. SvelteKit dedupes identical live-query instances onto a single
 * connection, so every lineage-backed surface on a page and the bell above it ride the same stream,
 * advance on the same number, and cannot disagree about when the estate changed.
 *
 * Pass it (not a call of it) to `liveRead`, which opens it on mount:
 *
 *     liveRead(lineageTick, () => load());
 */
export function lineageTick(): LiveCursor {
	const feed = lineageFeed();
	return {
		get current() {
			return feed.current?.cursor;
		},
		get connected() {
			return feed.connected;
		},
	};
}
