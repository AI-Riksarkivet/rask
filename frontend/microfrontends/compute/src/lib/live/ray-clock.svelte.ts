import type { LiveCursor } from '@rask/api/live';

/**
 * THE ZONE'S ONE RAY CLOCK — a `LiveCursor` that advances on a timer instead of on an event.
 *
 * WHY A TIMER AT ALL, since every other feed in the estate moved onto a cursor. Because for the Ray
 * plane there is nothing to subscribe to. The chain terminates at the Ray dashboard REST API
 * (`ray_kit/dashboard.py` -> `/api/cluster_status`, `/nodes`, `/api/jobs/`, `/api/v0/actors`, the
 * Serve v2 `ServeInstanceDetails` proxy): snapshot introspection with no subscribe verb, and Ray
 * Event Export is deferred to NATS and not wired. `lineageFeed` is the wrong signal and dangerously
 * so — on an idle estate it never moves at all, so a cursor-driven board would render a dead node
 * alive and a crash-looping replica HEALTHY, under a pulsing "live" dot. A board that LIES is worse
 * than one that blanks. Re-audit this only when Ray Event Export actually lands on the bus.
 *
 * POLL REASON: the Ray dashboard REST API is snapshot-only introspection — Ray publishes no change
 * events a cursor could ride, and Event Export is deferred to NATS and unwired, so re-reading
 * cluster / node / job / actor / task / Serve state on a clock is the only transport available.
 *
 * WHY IT IS ONE CLOCK AND NOT EIGHT. Every board used to own a private `setInterval` over the zone's
 * SHARED no-arg cached queries, and `liveRead`'s own docstring already names the debt: "ten in
 * `compute` (a zone that already has a live cursor and uses it for none of them)". The cost was not
 * only the copies. `/compute/workbench` mounts Jobs, Cluster, Actors and Serve at once, and
 * `ActorsBoard` refreshed `getRayCluster()` as well as its own — so with both panels docked, one
 * query was refetched by two out-of-phase clocks, twice per interval, forever. One clock means one
 * phase: every Ray read in the zone lands together and the boards cannot disagree about when they
 * looked.
 *
 * REF-COUNTED, exactly like `explorer/src/lib/service-health.svelte.ts::subscribe()` — the first
 * subscriber starts the interval and the last one to leave clears it. That is what makes the bound
 * possible: a page with nothing to watch (a terminal job, a closed panel) subscribes to nothing, so
 * the clock does not tick and issues no requests at all. It is the difference between this and the
 * timers it replaces, one of which kept re-reading the full Ray job registry every 5 s for as long as
 * a tab sat open on a SUCCEEDED job.
 *
 * USE IT THROUGH `liveRead`, so the first read is unconditional and a keyed surface re-reads on
 * navigation rather than on the next tick:
 *
 *     $effect(() => rayClock.subscribe());
 *     liveRead(() => rayClock.cursor, () => {
 *         rayClock.refresh(jobsQuery);
 *     });
 *
 * Go through `rayClock.refresh(q)` rather than `q.refresh()`: it carries the mandatory
 * `.catch(() => {})` so a call site cannot forget it (one uncaught rejection evicts the query from
 * cache and silently kills its updates), and it coalesces two boards asking for the same shared query
 * in the same tick — see its own note.
 */
const POLL_MS = 5000;

class RayClock {
	/** Advances once per interval. Read it through `cursor`; it is public only because a Svelte class
	 *  field has to be to carry `$state`. */
	tick = $state(0);
	#refs = 0;
	#timer: ReturnType<typeof setInterval> | null = null;
	#refreshedAt = new WeakMap<object, number>();

	/** Start the clock while at least one surface wants it; stop when the last one goes away.
	 *
	 *  Call from an `$effect` and RETURN the result, so unmount tears the subscription down:
	 *  `$effect(() => rayClock.subscribe())`. Ref-counted because the workbench docks four panels that
	 *  each want it, and the first to unmount must not stop the clock the other three still need. */
	subscribe(): () => void {
		this.#refs += 1;
		this.#timer ??= setInterval(() => {
			this.tick += 1;
		}, POLL_MS);
		return () => {
			this.#refs -= 1;
			if (this.#refs <= 0 && this.#timer !== null) {
				clearInterval(this.#timer);
				this.#timer = null;
			}
		};
	}

	/** Refresh a SHARED query at most once per tick, whoever asks.
	 *
	 *  Sharing the clock puts every board in one phase; it does NOT stop two of them asking for the
	 *  same thing. `ActorsBoard` needs `getRayCluster()` (it resolves node names) and so does
	 *  `ClusterBoard`, and `/compute/workbench` docks both — so the same no-arg cached query was
	 *  refetched twice per tick. MEASURED against the deployed zone, 11 s on the workbench:
	 *  `getRayCluster` 8, every sibling 4. Putting them on one clock made the duplicate punctual
	 *  rather than absent, which is not the same fix.
	 *
	 *  De-duplicated on the QUERY OBJECT, not on a name: a no-arg remote `query()` is cached on the
	 *  function's identity, so both boards hold the very same instance and a `WeakMap` keyed on it
	 *  coalesces them without either board knowing the other exists. A keyed query (`getRayJobs(id)`)
	 *  is a different instance per key and is correctly NOT coalesced.
	 *
	 *  The loser of the race is not starved: it shares the winner's result, because it is the same
	 *  cached query. And `.catch(() => {})` lives HERE now, so it cannot be forgotten at a call site —
	 *  one uncaught rejection evicts the query from cache and silently kills its updates.
	 */
	refresh(query: { refresh(): Promise<unknown> }): void {
		if (this.#refreshedAt.get(query) === this.tick) return;
		this.#refreshedAt.set(query, this.tick);
		query.refresh().catch(() => {});
	}

	/** The clock IS the cursor — `LiveCursor` is two getters and this class has them.
	 *
	 *  Getters rather than a snapshot, the same shape as a zone's `lineageTick`: the value is read
	 *  inside the consumer's effect, which is what makes the dependency track. Returning `this`
	 *  instead of a wrapper object also avoids aliasing `this` into a closure, which oxlint refuses
	 *  (`typescript(no-this-alias)`) — and the wrapper bought nothing the prototype does not. */
	get current(): number {
		return this.tick;
	}

	/** Whether the clock is actually running, so a surface can render an honest "not watching" state
	 *  rather than a stale frame that looks live. */
	get connected(): boolean {
		return this.#timer !== null;
	}

	get cursor(): LiveCursor {
		return this;
	}
}

/** The zone's single Ray clock. Module-level, so every board shares one interval and one phase. */
export const rayClock = new RayClock();
