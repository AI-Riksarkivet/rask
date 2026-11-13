// The Idempotency-Key a row write is sent with, held across its retries ([[LH-577]]).
//
// A write the catalog has not answered (the browser gave up on it, or the catalog failed it) may still have committed.
// Sent again under the same key, the catalog answers the first attempt's response and writes nothing a second time; sent
// under a new one, an append would land twice. So the key is kept while the same rows are retried, and a new one is
// minted once the write is settled (answered, or refused before it ran) or the rows change: the catalog answers a key it
// has seen with that key's first response, so an edited batch under the old key would never be written.

/** Whether a write's outcome leaves it unsettled: unanswered (0), a conflict a retry may clear (409), or a fault (5xx). */
function unsettled(status: number): boolean {
	return status === 0 || status === 409 || status >= 500;
}

export class WriteKey {
	#held = $state<{ key: string; rows: string } | undefined>(undefined);

	/** Whether a write of ``rows`` is waiting to be retried under its key. */
	pending(rows: string): boolean {
		return this.#held?.rows === rows;
	}

	/** The key to send ``rows`` with: the one an unsettled write of the same rows was sent with, else a new one. */
	take(rows: string): string {
		if (this.#held?.rows !== rows) this.#held = { key: crypto.randomUUID(), rows };
		return this.#held.key;
	}

	/** Keep the key for a retry while the write is unsettled; forget it once it is. */
	settle(outcome: { ok: true } | { ok: false; status: number }): void {
		if (outcome.ok || !unsettled(outcome.status)) this.#held = undefined;
	}
}
