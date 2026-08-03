/**
 * The lakehouse elements' poll scaffold — same contract as compute's ray-poll.svelte.ts and
 * DELIBERATELY duplicated rather than shared: an element bundle may not import from another zone
 * (the whole architecture), and hoisting 40 lines into a package would put element plumbing in the
 * library plane for two consumers. Timeout-armed reads (a dead upstream HANGS, observed), an error
 * surface, and the poll counter + mount stamp that witness no-remount.
 */
import { timeoutSignal } from '@rask/api/client';

export class ElementPoll<T> {
	data = $state<T | null>(null);
	error = $state<string | null>(null);
	polls = $state(0);
	readonly mountedAt = new Date().toLocaleTimeString();

	#read: (fetchFn: typeof fetch) => Promise<T>;

	constructor(read: (fetchFn: typeof fetch) => Promise<T>) {
		this.#read = read;
	}

	/** Start polling; returns the cleanup for the caller's `$effect(() => poll.start(ms))` — ms is
	 *  read inside the effect so a property update re-arms the interval. */
	start(ms = 30_000): () => void {
		let live = true;
		const tick = async () => {
			try {
				const next = await this.#read((url, init) =>
					fetch(url, { ...init, signal: timeoutSignal(6000) }),
				);
				if (!live) return;
				this.data = next;
				this.error = null;
			} catch (e) {
				if (live) this.error = e instanceof Error ? e.message : String(e);
			} finally {
				if (live) this.polls += 1;
			}
		};
		void tick();
		// POLL REASON: catalog/audit state moves while the panel is on screen, and an element has no
		// server push channel across the zone boundary.
		const timer = setInterval(tick, ms);
		return () => {
			live = false;
			clearInterval(timer);
		};
	}
}
