/**
 * A bounded, promise-keyed memo for GETs whose response cannot change without the URL changing.
 *
 * Why this exists, measured 2026-07-26 through the ingress: `/api/atlas/points` returned **6,679,228
 * bytes** and was re-fetched on every mount and every Text/Visual toggle — 25.5 MiB in about thirty seconds
 * of ordinary clicking, which OOM-killed the viewer mid-measurement (`exitCode 137`, limit 512Mi) and took
 * the media plane to 502. The projection for a given space and dataset cannot change without a redeploy, so
 * every request after the first was waste.
 *
 * It memoises the **promise**, not the value, which is what makes the concurrent case work: two components
 * mounting in the same tick share one request instead of racing two multi-megabyte downloads. A rejection
 * evicts its own entry, because a failed load must stay retryable — caching a failure would turn one bad
 * moment into a permanently broken surface.
 *
 * The URL is the whole key on purpose. Callers build URLs that already carry a version token or a content
 * hash (`&v=6`, `api/thumbnail/<hash>`), so **invalidation is free**: bump the token and every old key
 * becomes unreachable at once. There is no TTL to tune and no invalidation message for anyone to maintain.
 * That property is what makes this safe in the browser; do not point it at an endpoint whose body can change
 * under a fixed URL.
 *
 * This is the TAB's half of the fix, and it is only half. It cannot help a refresh, a second tab or a second
 * user, each of which still paid the full 6.6 MB — so the server half lives in
 * {@link ../server-cache.ts | SharedPayloadCache}, wired into the media zone's `/api/atlas/points` route.
 *
 * An earlier version of this note argued a server cache was the wrong place, on the grounds that a shared
 * cache of authorised results needs a key derived from the subject. That reasoning was wrong, and worth
 * recording as wrong: the right key is the RESOURCE, and the authorization is simply not cached at all — it
 * runs on every request, before any byte is reachable. A subject key would be the mistake, not the fix,
 * because it would defeat the entire point (one entry serving everyone allowed to see it) while adding a
 * disclosure surface for anyone who derived it slightly wrong. What IS true is that a tab-local memo needs
 * no coherence story across the estate's `frontend.replicas: 2`; the server half accepts one cold fill per
 * replica instead, which is argued out where it is implemented.
 */
export class UrlMemo<T> {
	readonly #entries = new Map<string, Promise<T>>();
	readonly #max: number;

	/** @param max entries to keep. Each can be megabytes, so this is a memory budget, not a hit-rate dial. */
	constructor(max: number) {
		if (max < 1) throw new RangeError(`UrlMemo needs room for at least one entry, got ${max}`);
		this.#max = max;
	}

	get size(): number {
		return this.#entries.size;
	}

	/** Drop everything. For tests, and for a future "the dataset changed under us" signal. */
	clear(): void {
		this.#entries.clear();
	}

	/** The memoised load for `url`, starting it if this is the first ask. */
	run(url: string, load: () => Promise<T>): Promise<T> {
		const hit = this.#entries.get(url);
		if (hit) {
			// Re-insert to make eviction least-recently-used rather than first-in-first-out. Map preserves
			// insertion order, so this is the whole LRU implementation. It matters for the exact access
			// pattern this was built for: toggling back and forth between two spaces must never evict the
			// one you are about to ask for again.
			this.#entries.delete(url);
			this.#entries.set(url, hit);
			return hit;
		}

		const pending = load().catch((e: unknown) => {
			this.#entries.delete(url);
			throw e;
		});
		this.#entries.set(url, pending);

		while (this.#entries.size > this.#max) {
			const oldest = this.#entries.keys().next();
			if (oldest.done) break;
			this.#entries.delete(oldest.value);
		}
		return pending;
	}
}
