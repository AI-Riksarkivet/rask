/**
 * The estate's ONE reading of a lineage run.
 *
 * `RunStatusLike` is the structural shape of the lineage service's `RunStatus`
 * (`services/lineage/schemas.py`, served by `GET /runs` and generated into
 * `@rask/api/lineage`). It is spelled out here rather than imported because `@rask/ui` must not
 * depend on the app API package — the shared library never owns a client, and a zone hands it
 * data. `tests/run-status.test.ts` pins every field name against the committed
 * `docs/lineage-openapi.json`, so a backend rename fails a test here instead of rendering blanks.
 *
 * The helpers are pure so the notification surface can be reasoned about (and tested) without a
 * DOM: which runs are unread, what a run's phase is, how far along it is.
 */

/** One run as `GET /runs` returns it. Every field but `run_id` is optional/nullable — the fold
 *  emits `null` for anything the run's events never carried. */
export type RunStatusLike = {
	run_id: string;
	/** `namespace/name`, e.g. `lance-medallion/ingest_events`. */
	job?: string | null;
	author?: string | null;
	/** The OpenLineage lifecycle state: START → RUNNING → COMPLETE / FAIL / ABORT. */
	state?: string | null;
	/** Datasets the run wrote. */
	outputs?: string[] | null;
	progress_done?: number | null;
	progress_total?: number | null;
	error_message?: string | null;
	started_at?: string | null;
	updated_at?: string | null;
	/** How many lifecycle events folded into this row. */
	events?: number;
	/** The catalog operation (`create_table`, `ingest_events`, …), when the run carried one. */
	operation?: string | null;
};

/** What a viewer needs to know at a glance. `unknown` keeps an unrecognised state honest — it is
 *  rendered verbatim rather than guessed into one of the others. */
export type RunPhase = 'started' | 'running' | 'complete' | 'failed' | 'unknown';

export function runPhase(run: RunStatusLike): RunPhase {
	const state = (run.state ?? '').toUpperCase();
	if (state === 'FAIL' || state === 'ABORT') return 'failed';
	if (state === 'COMPLETE') return 'complete';
	if (state === 'RUNNING') return 'running';
	if (state === 'START') return 'started';
	return 'unknown';
}

/** The words on the pill. `unknown` echoes whatever the backend said (or an em dash). */
export function runPhaseLabel(run: RunStatusLike): string {
	switch (runPhase(run)) {
		case 'failed':
			return (run.state ?? '').toUpperCase() === 'ABORT' ? 'Aborted' : 'Failed';
		case 'complete':
			return 'Completed';
		case 'running':
			return 'Running';
		case 'started':
			return 'Started';
		default:
			return run.state ?? '—';
	}
}

/**
 * The identity of ONE notification.
 *
 * A run is not a notification — a run *reaching a state* is. Keying on `(run_id, state)` is what
 * makes the surface a lifecycle feed: a run read at START becomes unread again when it COMPLETEs
 * or FAILs, while progress ticks inside RUNNING (which change `progress_done`, not `state`) do
 * NOT re-ring the bell.
 */
export function runNotificationId(run: RunStatusLike): string {
	return `${run.run_id}@${(run.state ?? 'UNKNOWN').toUpperCase()}`;
}

/** The job split for display: the trailing segment is the name, everything before it is the
 *  namespace the producer declared (`lance-medallion/`, `ray-jobs/`). A run with no job falls back
 *  to a short run id, because a nameless row is not a notification. */
export function runJobLabel(run: RunStatusLike): { name: string; namespace: string } {
	const job = (run.job ?? '').trim();
	if (!job) return { name: run.run_id.slice(0, 8), namespace: '' };
	const cut = job.lastIndexOf('/');
	if (cut < 0) return { name: job, namespace: '' };
	return { name: job.slice(cut + 1), namespace: job.slice(0, cut) };
}

/** Progress, or `null` when the run never carried a total — the producer only stamps the progress
 *  facet on RUNNING events, so most runs have none and must NOT be drawn as a 0% bar. */
export function runProgress(
	run: RunStatusLike,
): { done: number; total: number; percent: number } | null {
	const total = run.progress_total;
	if (typeof total !== 'number' || total <= 0) return null;
	const done = Math.max(
		0,
		Math.min(typeof run.progress_done === 'number' ? run.progress_done : 0, total),
	);
	return { done, total, percent: Math.round((done / total) * 100) };
}

/**
 * What the panel shows: undismissed runs, FAILURES FIRST, then newest.
 *
 * Recency alone was wrong, and the live estate proved it: against 891 real runs the first `FAIL` sat at
 * **position 445**, so a panel showing the newest 8 never displayed a single failure. That directly
 * defeats this surface's stated purpose — nobody should have to hunt for a failed run — and no test
 * caught it because every fixture had a handful of runs with the failure near the top.
 *
 * A dismissed failure is still gone: the point is that a failure the user has NOT dealt with cannot be
 * pushed off the end by a hundred successes. Within each group the order is newest-first as before, so a
 * quiet estate looks exactly as it did.
 */
export function visibleRuns(
	runs: RunStatusLike[],
	dismissed: Iterable<string> = [],
): RunStatusLike[] {
	const gone = new Set(dismissed);
	const failed = (run: RunStatusLike): boolean => runPhase(run) === 'failed';
	// `.sort` on the filtered COPY, not `.toSorted` — the package's tsconfig targets ES2022, where
	// `toSorted` is not in lib (svelte-check: "Property 'toSorted' does not exist"). The copy is what
	// keeps the caller's array unmutated.
	return runs
		.filter((run) => !gone.has(runNotificationId(run)))
		.sort((a, b) => {
			if (failed(a) !== failed(b)) return failed(a) ? -1 : 1;
			return (b.updated_at ?? '').localeCompare(a.updated_at ?? '');
		});
}

/** The runs whose CURRENT state the viewer has not seen — what the count on the bell means. */
export function unreadRuns(
	runs: RunStatusLike[],
	seen: Iterable<string> = [],
	dismissed: Iterable<string> = [],
): RunStatusLike[] {
	const read = new Set(seen);
	return visibleRuns(runs, dismissed).filter((run) => !read.has(runNotificationId(run)));
}

/**
 * The notification ids a close may mark as read: only the rows the panel actually RENDERED.
 *
 * The list renders `slice(0, limit)` and tells the reader "N older runs not shown". Marking the whole
 * ordered set instead meant that opening and closing the bell marked every run read — proven in a browser
 * with 13 runs where a FAILED one had been pushed past the cap: the badge cleared, the harness reported
 * `seen: 13`, and the failure the user never saw was gone for good, because `seen` is what a zone persists
 * per subject. That is the precise outcome the surface exists to prevent, so the cap belongs here, beside
 * the ordering, rather than inline in a component where no test can reach it.
 */
export function seenOnClose(runs: RunStatusLike[], limit: number): string[] {
	return runs.slice(0, Math.max(0, limit)).map(runNotificationId);
}
