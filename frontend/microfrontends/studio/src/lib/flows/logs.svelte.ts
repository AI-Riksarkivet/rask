/**
 * The run log — what actually happened, in order, so a failing flow can be debugged instead of
 * guessed at.
 *
 * The node cards carry a status dot and the last error, which answers "did this node fail" but not
 * "what did it send", "how long did the call take", or "which node blocked which". This is the
 * transcript: every node start/finish, every timing, every refusal, and the graph-level events
 * around them.
 *
 * A bounded ring buffer on purpose. A chatty flow run in a long-lived tab would otherwise grow
 * without limit, and the interesting end of a log is the RECENT end — so old entries fall off rather
 * than being paged.
 */

export type LogLevel = 'info' | 'warn' | 'error';

export interface LogEntry {
	/** Monotonic id — the key for `{#each}`, since two entries can share a millisecond. */
	seq: number;
	/** Wall-clock, for display only. */
	at: Date;
	level: LogLevel;
	/** The node this concerns, when it concerns one. */
	nodeId: string | null;
	message: string;
}

/** Entries kept. Chosen so a long session stays bounded while a whole run (a few dozen lines even
 *  on a wide graph) is comfortably inside the window. */
const LIMIT = 400;

class FlowLogs {
	/** `$state.raw` + whole-array replacement: entries are append-only records nothing mutates in
	 *  place, so per-entry reactivity would cost proxies for nothing. */
	entries = $state.raw<LogEntry[]>([]);
	/** Show only errors and warnings — the first thing you want when a run went wrong. */
	problemsOnly = $state(false);

	private seq = 0;

	get visible(): LogEntry[] {
		return this.problemsOnly ? this.entries.filter((e) => e.level !== 'info') : this.entries;
	}

	get problemCount(): number {
		return this.entries.filter((e) => e.level !== 'info').length;
	}

	/** Append one line. `new Date()` here rather than at render time so the log records WHEN the
	 *  event happened, not when it was last drawn. */
	push(level: LogLevel, message: string, nodeId: string | null = null): void {
		const entry: LogEntry = { seq: ++this.seq, at: new Date(), level, nodeId, message };
		this.entries = [...this.entries, entry].slice(-LIMIT);
	}

	clear(): void {
		this.entries = [];
	}

	/** The whole log as text, for the copy button — `hh:mm:ss LEVEL [node] message`. */
	asText(): string {
		return this.entries
			.map((e) => {
				const t = e.at.toTimeString().slice(0, 8);
				const node = e.nodeId ? ` [${e.nodeId}]` : '';
				return `${t} ${e.level.toUpperCase()}${node} ${e.message}`;
			})
			.join('\n');
	}
}

/** The singleton — the store writes to it, the bottom panel reads it. */
export const logs = new FlowLogs();
