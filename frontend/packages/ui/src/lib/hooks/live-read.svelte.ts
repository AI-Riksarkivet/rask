import { onMount, untrack } from 'svelte';

/**
 * A cursor a surface re-reads on: a monotonically advancing number, plus whether its stream is up.
 *
 * Deliberately structural, not a `query.live` type. That is what keeps this hook zone-agnostic —
 * the caller decides WHICH stream (lineage, control, or something that does not exist yet) and this
 * file never imports one. A cursor is anything with these two getters.
 */
export interface LiveCursor {
	readonly current: number | undefined;
	readonly connected: boolean;
}

/**
 * Read once, then re-read on every advance of a live cursor — the estate's replacement for
 * `$effect(() => { load(); const t = setInterval(load, MS); return () => clearInterval(t) })`.
 * Thirteen files in the lakehouse carried that shape before this hook replaced them.
 *
 * WHY IT LIVES IN `@rask/ui` AND NOT IN EACH ZONE. It was written in the lakehouse and then copied
 * into a second zone, which is the moment a helper stops being local and starts being duplication.
 * The mechanism has no zone in it: it takes an `open()` callback and never names a feed. What IS
 * per-zone is the BINDING — `query.live` must be declared inside an app to get its own endpoint, and
 * `getRequestEvent` only exists there — so each zone keeps its own `lineageTick()`/`controlTick()`
 * wrapper and shares this. Same split the estate applies everywhere else: extract the mechanism,
 * keep the domain (`@rask/dockview` holds the dock; the panels stay in their zone).
 *
 * FOUR RULES, and the last two are each here because getting them wrong broke a test:
 *
 *  - The first read is UNCONDITIONAL, so a surface renders immediately rather than waiting for a
 *    stream to connect — and a surface whose cursor never connects at all still shows its data and
 *    its own honest offline state.
 *  - `key` — a route parameter or a selection — stays TRACKED, so a navigation re-reads at once
 *    instead of waiting for the estate to change. Its value is passed to `read`, so a loader can
 *    drop a response that a later navigation superseded.
 *  - The cursor's ARRIVAL is not a change. `.current` is undefined until the stream delivers its
 *    first value, and treating `undefined -> 137` as an advance makes every page load read twice —
 *    once eagerly and once when the stream lands, for no new information. Only a cursor that moves
 *    AFTER that means the estate did something. (Caught by `e2e/lineage/live-refresh.spec.ts`, which
 *    counts reads: it expected 1 and got 2.)
 *  - The stream is OPENED ON MOUNT, never at component init, so it does not exist during SSR. A live
 *    query touched while rendering makes the server hold the render until the generator's first
 *    value — which for these cursors is a round trip to the lineage plane on the critical path of
 *    every page. A page whose HTML waits on a *liveness probe* is strictly worse than the timers
 *    this replaced, and it is measurable: opening at init took the lakehouse's e2e suite from 2
 *    failures to 9-18, nearly all of them shell elements simply absent.
 *
 * The read is `untrack`ed: a loader assigns the `$state` it also reads, so tracking it would re-enter.
 *
 * Nothing here is a timer. An idle surface issues exactly one request and then stays quiet.
 */
export function liveRead<K = void>(open: () => LiveCursor, read: (key: K) => unknown, key?: () => K): void {
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
