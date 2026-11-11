import { onMount, untrack } from 'svelte';

/**
 * THE ESTATE'S LIVENESS READER — read once, then re-read on every advance of a live cursor.
 *
 * Use it instead of a polling effect,
 *
 *     $effect(() => { load(); const t = setInterval(load, POLL_MS); return () => clearInterval(t) })
 *
 * which re-reads on a timer whether or not anything changed, and misses a change until the next tick.
 *
 * WHY IT IS ONE FILE. Copies of it diverge: three measured copies (274 lines between them) had, and
 * only one carried the measurement behind rule four, so a page adopting another inherited the rule with
 * no evidence for it, which is exactly how a hard-won rule gets "simplified" back out.
 *
 * Nothing here touches any feed. It takes a cursor as an argument, so it is a MECHANISM — which is the
 * only thing a `frontend/packages/*` entry may be. The cursors themselves (`lineageTick`,
 * `controlCursor`, …) live in the console's own `feeds.api.ts`.
 *
 * Four rules, and the last two are each here because getting them wrong broke a test:
 *
 *  - The first read is UNCONDITIONAL, so a surface renders immediately rather than waiting for a
 *    stream to connect (and a surface whose cursor never connects at all still shows its data and its
 *    own honest offline state).
 *  - `key` — a route parameter or a selection — stays TRACKED, so a navigation re-reads at once
 *    instead of waiting for the estate to change. Its value is passed to `read` so a loader can drop a
 *    response that a later navigation superseded.
 *  - The cursor's ARRIVAL is not a change. `.current` is undefined until the stream delivers its first
 *    value, and treating `undefined → 137` as an advance makes every page load read twice — once
 *    eagerly and once when the stream lands, for no new information. Only a cursor that moves after
 *    that means the estate did something. (Caught by `lakehouse/e2e/lineage/live-refresh.spec.ts`,
 *    which counts reads: it expected 1 and got 2.)
 *  - The stream is OPENED ON MOUNT, never at component init, so it does not exist during SSR. A live
 *    query touched while rendering makes the server hold the render until the generator's first value
 *    — which for these cursors is a round trip to the lineage plane on the critical path of every
 *    page. A page whose HTML waits on a *liveness probe* is strictly worse than the timers this
 *    replaced, and it is measurable: opening at init took the lakehouse e2e suite from 2 failures to
 *    9–18, nearly all of them shell elements simply absent, and moving it here put the count back. A
 *    live feed is a client concern; the server's job is to render the page.
 *
 * The read is `untrack`ed: a loader assigns the `$state` it also reads,
 * so tracking it would re-enter.
 *
 * Nothing here is a timer. An idle surface issues exactly one request and then stays quiet.
 */

/** The shape of a live query consumed as a cursor — see the console's `feeds.api.ts`. */
export interface LiveCursor {
	readonly current: number | undefined;
	readonly connected: boolean;
}

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
 * The read-scheduling decision, factored out of `liveRead` so it can be tested without a component.
 *
 * `liveRead` itself needs a mount and an effect root, so its rules were only ever exercised
 * indirectly — by one page's e2e suite counting HTTP reads. That is a real test and a slow, distant
 * one: the estate learned rule three from an e2e failure, in a different page, on a different plane.
 * The rules are pure, so they can be pinned here directly, and a page adopting this gets them
 * verified rather than inherited.
 */
export function shouldRead(state: {
	started: boolean;
	keyChanged: boolean;
	cursorMoved: boolean;
}): boolean {
	return !state.started || state.keyChanged || state.cursorMoved;
}

/** Whether a cursor moving from `previous` to `next` is an ADVANCE, or merely its first arrival. */
export function cursorMoved(previous: number | undefined, next: number | undefined): boolean {
	return previous !== undefined && next !== previous;
}
