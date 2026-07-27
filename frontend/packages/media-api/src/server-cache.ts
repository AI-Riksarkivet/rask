/**
 * A **shared** payload cache for a zone's BFF: one stored copy serves every caller allowed to see it.
 *
 * The measurement it answers, taken 2026-07-26 through the ingress: `GET /media/api/atlas/points` is
 * **6,678,928 bytes**, and the browser-side {@link UrlMemo} only fixes the case where the same tab asks
 * twice. A refresh, a second tab and a second user each still paid it in full, and a burst of concurrent
 * multi-megabyte reads is what OOM-killed the viewer (`exit 137` on the 512Mi tier) and took the media
 * plane to 502. So the other half of the fix belongs in the zone server, where one fill can serve everyone.
 *
 * A shared cache is a disclosure surface in a way a tab-local memo is not, so the three properties that
 * make it safe are structural here rather than conventional:
 *
 * 1. **Authorization runs on every call, and is never cached.** {@link SharedPayloadCache.serve} awaits
 *    the caller's `authorize` before the entry map is so much as consulted — there is no ordering a call
 *    site can choose that reads bytes first. Only the payload is remembered; the decision never is. The
 *    naive shape of this feature (`if (cache.has(key)) return cache.get(key)` at the top of the handler)
 *    passes every happy-path test and hands a warm 6.6 MB projection to a caller who may not read it.
 * 2. **The key is the resource, never the subject.** Callers pass a key built from the dataset, the space
 *    and the payload's version token — never from a session, a bearer or a cookie. That is what lets one
 *    entry serve everyone, and it is why invalidation is free: a new build changes the token, so every old
 *    key becomes unreachable at once with no eviction message for anyone to maintain. The corollary is a
 *    hard precondition: point this only at a projection that is **dataset-wide**. If the upstream could
 *    vary its body by caller under a fixed URL, a resource key would be a cross-user leak — which is why
 *    a `Vary` the upstream cannot satisfy for a shared cache disables storage entirely (below).
 * 3. **Single-flight.** Ten cold hits trigger one upstream read, not ten. That is the property the viewer
 *    actually died for; it is not a hit-rate optimisation.
 *
 * Freshness is the upstream's to decide, not ours. The route states a ceiling it can defend (the resource
 * is versioned, so its bytes cannot change under a fixed key within that window) and the upstream's own
 * `Cache-Control` may only make it **stricter** — `no-store`, `no-cache`, `private` or a `Vary` beyond
 * what a shared cache can key on all mean "do not store this", and are obeyed. That keeps the estate's
 * one HTTP cache honest by the same rules any proxy between it and the browser would follow, instead of
 * inventing a TTL nobody can justify.
 *
 * Deliberately in-process, per zone replica. See `atlas-points.ts` in the media zone for what that costs
 * at `frontend.replicas: 2`, and why a shared store was not the answer here.
 */

/** Where a served response came from. Emitted as `x-cache` so a live drive can see it rather than infer it. */
export type CacheState =
	/** Filled from upstream by this request, and retained. */
	| 'miss'
	/** Served from a retained, unexpired entry. */
	| 'hit'
	/** Filled by a concurrent request this one waited on instead of starting its own. */
	| 'shared'
	/** Fetched upstream and NOT retained — the upstream forbade shared storage, or it did not fit. */
	| 'bypass';

/** A retained payload. `bytes` is never mutated after construction, so one buffer backs many responses.
 *  Pinned to `ArrayBuffer` rather than `ArrayBufferLike`: `BodyInit` excludes a SharedArrayBuffer-backed
 *  view, so the looser type would not be a legal Response body. */
interface Entry {
	readonly bytes: Uint8Array<ArrayBuffer>;
	/** The response headers that are part of the payload contract (content-type, cache-control). */
	readonly headers: Readonly<Record<string, string>>;
	/** Epoch ms at or after which this entry must not be served. */
	readonly expiresAt: number;
}

export interface ServeOptions {
	/**
	 * The authorization for THIS caller and THIS resource. Runs first on every call, before any cached
	 * byte is reachable. Return a `Response` to refuse (it is returned to the caller verbatim and nothing
	 * is served), or `null` to allow. Whatever it does — session check, FGA check, an upstream probe — its
	 * result is used once and thrown away; it is never associated with the entry.
	 */
	authorize: () => Promise<Response | null>;
	/** The upstream read for a cold key. Called at most once per key per in-flight window. */
	load: () => Promise<Response>;
	/**
	 * The route's freshness ceiling in ms — how long the route is willing to claim this resource cannot
	 * change under its key. The upstream's `Cache-Control` may lower it or veto storage outright; it can
	 * never raise it.
	 */
	maxAgeMs: number;
}

export interface CacheStats {
	entries: number;
	bytes: number;
	hits: number;
	misses: number;
	shared: number;
	bypasses: number;
	refusals: number;
	evictions: number;
	/** Upstream reads actually issued — the number ten concurrent cold hits must not multiply. */
	loads: number;
}

/** Response headers that travel with a cached payload. Everything else is the hop's business, not the
 *  payload's: hop-by-hop headers, CORS (same-origin here) and content-length (recomputed per response). */
const KEPT_HEADERS = ['content-type', 'cache-control'] as const;

/** `Vary` values a shared cache can honour without keying on the subject. Anything else — `authorization`,
 *  `cookie`, `*` — means the upstream body depends on WHO asked, and a resource-keyed shared entry would
 *  then be a cross-user disclosure. Those are never stored. */
const SHAREABLE_VARY = new Set(['accept-encoding', 'origin']);

/** Split a comma-separated header into lowercased, trimmed tokens. */
function tokens(value: string | null): string[] {
	return (value ?? '')
		.split(',')
		.map((t) => t.trim().toLowerCase())
		.filter(Boolean);
}

/**
 * How long a SHARED cache may retain this response, in ms, or `null` for "not at all".
 *
 * `ceilingMs` is the route's claim; the upstream can only reduce it. An absent `Cache-Control` leaves the
 * ceiling standing (the route, not the wire, is the authority on whether the resource is versioned), but
 * an explicit refusal is final.
 */
export function shareableTtlMs(res: Response, ceilingMs: number): number | null {
	const cc = tokens(res.headers.get('cache-control'));
	if (cc.includes('no-store') || cc.includes('no-cache') || cc.includes('private')) return null;

	const vary = tokens(res.headers.get('vary'));
	if (vary.some((v) => !SHAREABLE_VARY.has(v))) return null;

	// s-maxage is the shared-cache directive and outranks max-age when both are present (RFC 9111 §5.2.2).
	const directive =
		cc.find((t) => t.startsWith('s-maxage=')) ?? cc.find((t) => t.startsWith('max-age='));
	if (directive === undefined) return ceilingMs > 0 ? ceilingMs : null;

	const seconds = Number(directive.slice(directive.indexOf('=') + 1));
	if (!Number.isFinite(seconds) || seconds <= 0) return null;
	return Math.min(ceilingMs, seconds * 1000);
}

export class SharedPayloadCache {
	readonly #entries = new Map<string, Entry>();
	/** In-flight fills, so concurrent cold hits wait on one upstream read. Resolves `null` when the fill
	 *  produced nothing shareable — a refusal or an unshareable body, which each waiter must ask for with
	 *  its OWN credentials rather than inherit. */
	readonly #inflight = new Map<string, Promise<Entry | null>>();
	readonly #maxBytes: number;
	readonly #now: () => number;
	#bytes = 0;
	#stats = { hits: 0, misses: 0, shared: 0, bypasses: 0, refusals: 0, evictions: 0, loads: 0 };

	/**
	 * @param maxBytes retention budget across all entries. Each payload here is multi-MB and the zone pod
	 *   runs on the 512Mi default tier, so this is a memory budget, not a hit-rate dial.
	 * @param now injectable clock — expiry is a behaviour, so it has to be testable without sleeping.
	 */
	constructor(opts: { maxBytes: number; now?: () => number }) {
		if (opts.maxBytes < 1) throw new RangeError(`maxBytes must be positive, got ${opts.maxBytes}`);
		this.#maxBytes = opts.maxBytes;
		this.#now = opts.now ?? Date.now;
	}

	stats(): CacheStats {
		return { entries: this.#entries.size, bytes: this.#bytes, ...this.#stats };
	}

	/** Drop every entry. For tests, and for a future "the corpus was rebuilt" signal. */
	clear(): void {
		this.#entries.clear();
		this.#bytes = 0;
	}

	/**
	 * Authorize, then serve `key` — from a retained entry, from a concurrent fill, or from upstream.
	 *
	 * The order is the security property. `authorize` is awaited before the entry map is touched, so a
	 * refused caller cannot reach a warm payload no matter how hot the key is.
	 */
	async serve(key: string, opts: ServeOptions): Promise<Response> {
		const refusal = await opts.authorize();
		if (refusal !== null) {
			this.#stats.refusals += 1;
			return refusal;
		}

		const warm = this.#take(key);
		if (warm) {
			this.#stats.hits += 1;
			return this.#respond(warm, 'hit');
		}

		const inflight = this.#inflight.get(key);
		if (inflight) {
			const shared = await inflight;
			if (shared) {
				this.#stats.shared += 1;
				return this.#respond(shared, 'shared');
			}
			// The fill produced nothing a shared cache may hand on. Fall through and ask upstream with
			// this caller's own credentials — never inherit someone else's refusal or private body.
		}
		return this.#fill(key, opts);
	}

	/** A live, unexpired entry, promoted to most-recently-used. Expiry is enforced on read, so a stale
	 *  entry can never be served even if nothing has swept it. */
	#take(key: string): Entry | null {
		const entry = this.#entries.get(key);
		if (!entry) return null;
		if (entry.expiresAt <= this.#now()) {
			this.#drop(key);
			return null;
		}
		// Re-insert so eviction is least-recently-used: Map preserves insertion order, which is the whole
		// LRU implementation. It matters for the access pattern this exists for — toggling between the
		// three atlas spaces must never evict the one you are about to ask for again.
		this.#entries.delete(key);
		this.#entries.set(key, entry);
		return entry;
	}

	/**
	 * Start the one upstream read for `key` and publish it to concurrent waiters.
	 *
	 * Synchronous up to the point the in-flight promise is registered, which is what makes single-flight
	 * work: every waiter that resumes after this call finds the promise instead of starting a second
	 * multi-megabyte read.
	 */
	#fill(key: string, opts: ServeOptions): Promise<Response> {
		let publish!: (entry: Entry | null) => void;
		const promise = new Promise<Entry | null>((resolve) => {
			publish = resolve;
		});
		this.#inflight.set(key, promise);

		// The in-flight promise MUST settle on every path out of #run, or a waiter awaits it for ever —
		// and unlike a slow request, that hangs a connection with no error anywhere to explain it. So the
		// publish is guaranteed HERE rather than at each return inside #run: the second resolve of an
		// already-resolved promise is a no-op, which makes this a backstop and not a second policy.
		const done = this.#run(key, opts, publish);
		void done
			.catch(() => {})
			.finally(() => {
				publish(null);
				// Identity-checked: a later fill may already own the slot.
				if (this.#inflight.get(key) === promise) this.#inflight.delete(key);
			});
		return done;
	}

	async #run(
		key: string,
		opts: ServeOptions,
		publish: (entry: Entry | null) => void,
	): Promise<Response> {
		this.#stats.loads += 1;
		const upstream = await opts.load();
		if (!upstream.ok) {
			// Never stored and never shared: a non-2xx can be about the CALLER (a 403 on the winner's
			// bearer), and handing it to a waiter would refuse someone who was in fact authorized.
			publish(null);
			return upstream;
		}

		const ttl = shareableTtlMs(upstream, opts.maxAgeMs);
		const bytes = new Uint8Array(await upstream.arrayBuffer());
		const headers: Record<string, string> = {};
		for (const name of KEPT_HEADERS) {
			const value = upstream.headers.get(name);
			if (value !== null) headers[name] = value;
		}
		// The upstream's `cache-control` is about OUR shared cache, not about the browser's or any proxy
		// between us and it. This route requires a session, so replaying `public, max-age=300` verbatim
		// invites a shared cache in front of the estate to store an authorised payload and serve it to the
		// next caller — including one whose request would have been refused. No leak today (the Ingress
		// runs no `proxy_cache`), and wrong the moment anyone adds one, which the scaling note in this
		// module's own header recommends. `private` still lets the requesting browser reuse it.
		if (headers['cache-control'] !== undefined) {
			headers['cache-control'] = headers['cache-control']
				.split(',')
				.map((token) => (token.trim().toLowerCase() === 'public' ? 'private' : token.trim()))
				.join(', ');
			if (!/\bprivate\b/.test(headers['cache-control'])) {
				headers['cache-control'] = `private, ${headers['cache-control']}`;
			}
		}
		const entry: Entry = { bytes, headers, expiresAt: this.#now() + (ttl ?? 0) };

		if (ttl === null) {
			// The upstream forbade shared storage. Serve this caller and tell the waiters to ask again
			// themselves — sharing it would be exactly the disclosure `private`/`Vary` is warning about.
			publish(null);
			this.#stats.bypasses += 1;
			return this.#respond(entry, 'bypass');
		}

		// Retaining is a budget question; SHARING within the in-flight window is a safety question, and
		// this payload passed the safety one. So an oversized body still collapses its concurrent herd —
		// which is the case that actually killed the viewer — even though nothing is kept afterwards.
		const retained = this.#store(key, entry);
		publish(entry);
		if (retained) {
			this.#stats.misses += 1;
			return this.#respond(entry, 'miss');
		}
		this.#stats.bypasses += 1;
		return this.#respond(entry, 'bypass');
	}

	/** Retain `entry`, evicting least-recently-used entries to stay inside the budget. Returns false when
	 *  the payload alone exceeds the whole budget — storing it would evict everything including itself. */
	#store(key: string, entry: Entry): boolean {
		if (entry.bytes.byteLength > this.#maxBytes) return false;
		this.#drop(key);
		this.#entries.set(key, entry);
		this.#bytes += entry.bytes.byteLength;
		while (this.#bytes > this.#maxBytes) {
			const oldest = this.#entries.keys().next();
			if (oldest.done || oldest.value === key) break;
			this.#drop(oldest.value);
			this.#stats.evictions += 1;
		}
		return true;
	}

	#drop(key: string): void {
		const entry = this.#entries.get(key);
		if (!entry) return;
		this.#entries.delete(key);
		this.#bytes -= entry.bytes.byteLength;
	}

	/** A fresh Response over the retained bytes. The buffer is shared, never transferred or mutated. */
	#respond(entry: Entry, state: CacheState): Response {
		return new Response(entry.bytes, {
			status: 200,
			headers: { ...entry.headers, 'x-cache': state },
		});
	}
}
