/**
 * THE QUEUE'S ONE WALL CLOCK — a shared 1 s tick for lease countdowns.
 *
 * POLL REASON: a lease deadline is a wall-clock fact with no event. The countdown, and the
 * held -> expired flip, can only be observed by SAMPLING TIME: nothing publishes "one more second
 * passed", and no cursor could, because the value changes when the clock advances rather than when
 * the estate does. This is the one class of poll a live query can never replace. 1 s matches the
 * chip's own display resolution, and the SERVER remains the authority throughout — its reminder
 * fires `lease_expired` whether or not any browser is watching. The chip exists so a lease that
 * lapsed between server ticks reads EXPIRED rather than "held", because showing a corpse lease as
 * held is how an annotator loses work to a reclaim they were never warned about.
 *
 * WHY IT IS SHARED. `LeaseChip` used to own this interval itself, which meant ONE TIMER PER RENDERED
 * ROW — a queue of forty tasks ran forty 1 s intervals, each waking the scheduler independently, to
 * compute the same `new Date()`. Worse, it ran for rows that never read it: the `pinned` and
 * `unheld` branches of the chip's markup contain no countdown at all.
 *
 * REF-COUNTED, the same shape as `explorer/src/lib/service-health.svelte.ts::subscribe()` and
 * `compute/src/lib/live/ray-clock.svelte.ts`: the first subscriber starts the interval, the last to
 * leave clears it. That is what lets a chip subscribe ONLY when a countdown is actually possible, so
 * a queue of pinned tasks runs no timer whatsoever.
 *
 *     $effect(() => {
 *         if (task.state !== 'claimed' || !task.lease_expires_at) return;
 *         return wallClock.subscribe();
 *     });
 *     const view = $derived(leaseView(task, me, wallClock.now));
 *
 * SUBSCRIBE OFF THE PROPS, never off the derived view. `view` is computed FROM `now`, so guarding on
 * it would make the effect re-run every tick and tear down and re-create the subscription each
 * second — the exact churn this file removes, reintroduced one layer up.
 */
const TICK_MS = 1000;

class WallClock {
	/** The current instant, advanced once a second while anyone is subscribed. Public only because a
	 *  Svelte class field must be to carry `$state`. */
	now = $state(new Date());
	#refs = 0;
	#timer: ReturnType<typeof setInterval> | null = null;

	/** Tick while at least one chip needs a countdown; stop when the last one goes away.
	 *
	 *  Call from an `$effect` and RETURN the result so unmount tears it down. The first subscriber
	 *  also gets a fresh reading immediately: the shared instant may be up to a second stale when a
	 *  row mounts, and a chip that renders one second behind on its first frame is the thing this
	 *  component exists to prevent. */
	subscribe(): () => void {
		this.#refs += 1;
		if (this.#timer === null) {
			this.now = new Date();
			this.#timer = setInterval(() => {
				this.now = new Date();
			}, TICK_MS);
		}
		return () => {
			this.#refs -= 1;
			if (this.#refs <= 0 && this.#timer !== null) {
				clearInterval(this.#timer);
				this.#timer = null;
			}
		};
	}
}

/** The zone's single wall clock. Module-level, so a queue of N rows runs one interval, not N. */
export const wallClock = new WallClock();
