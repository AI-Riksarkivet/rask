/**
 * The compute elements' shared poll scaffold: one timeout-armed read on an interval, with the
 * error/poll-counter state every element renders. Exists so the four elements cannot drift apart
 * on the lessons already paid for here: a dead upstream HANGS rather than erroring (observed on
 * /api/ray/* with no Ray cluster), so every fetch is armed with AbortSignal.timeout; and the poll
 * counter + mount stamp are the workbench's no-remount witness.
 */
import { timeoutSignal } from '@rask/api/client';

export class RayPoll<T> {
	data = $state<T | null>(null);
	error = $state<string | null>(null);
	polls = $state(0);
	readonly mountedAt = new Date().toLocaleTimeString();

	#read: (fetchFn: typeof fetch) => Promise<T>;

	constructor(read: (fetchFn: typeof fetch) => Promise<T>) {
		this.#read = read;
	}

	/** Start polling; returns the cleanup for the caller's $effect. `ms` is passed HERE (not the
	 *  constructor) so the element's `$effect(() => poll.start(pollms))` reads the prop inside the
	 *  effect — a pollms property update re-runs the effect and re-arms the interval, instead of
	 *  silently keeping the initial value (svelte's state_referenced_locally warning, confirmed by
	 *  the MCP autofixer). */
	start(ms = 5000): () => void {
		let live = true;
		const tick = async () => {
			try {
				const next = await this.#read((url, init) =>
					fetch(url, { ...init, signal: timeoutSignal(4000) }),
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
		// POLL REASON: Ray cluster state (jobs/nodes/actors/serve apps) moves while the panel is on
		// screen, and an element has no server push channel across the zone boundary.
		const timer = setInterval(tick, ms);
		return () => {
			live = false;
			clearInterval(timer);
		};
	}
}
