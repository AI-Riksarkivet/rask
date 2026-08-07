/**
 * Matching an ingest run in the estate-wide lineage feed.
 *
 * Lives beside `ingest.remote.ts` rather than inside it because a `.remote.ts` module may export only
 * remote functions — and this needs to be importable by a test.
 */

/** The lineage job name every ingest run shares (`ingest.lineage.JOB_NAME`). Correlation is by run id;
 *  the NAME is what groups the lane, and it is the only server-side handle for "runs of this plane" —
 *  the lineage board is estate-wide and carries catalog drops, movers and training runs. */
export const INGEST_JOB = 'ingest.run';

/**
 * Does this lineage row belong to the ingest plane?
 *
 * THE BOARD WAS EMPTY BY CONSTRUCTION. The filter compared the row's job to the bare `'ingest.run'`,
 * and the lineage service does not store it that way: `repository.py:477` composes
 * `f"{event.job.namespace}/{event.job.name}"`, so what is written — and what `MATCH (r:Run) RETURN
 * DISTINCT r.job` reads back — is **`rask/ingest.run`**. A bare-name comparison never matched a single
 * row, so `/compute/ingest` said "No ingest runs on the board" while the graph held START and COMPLETE
 * events for every run that had ever succeeded.
 *
 * It reads as an authorization or outage symptom, which is what the page's own fallback text offers
 * ("or the lineage board is not readable with this session") — and the same off-by-a-namespace was
 * made independently in a verification query the same day, reporting the graph as empty when it was
 * not. That is the tell: the qualified form is easy to forget precisely because the bare name is what
 * the producing service calls it.
 *
 * SUFFIX, not a hardcoded `rask/`. The namespace comes from `LineageSettings().namespace` and is
 * deployment config; pinning it here would move the same bug one level down and break the moment a
 * release renames it. The bare form is still accepted so a single-namespace or test emitter matches
 * too.
 *
 * The `/` is load-bearing in the suffix check — without it a job named `pre-ingest.run` would match.
 */
export function isIngestJob(job: unknown): boolean {
	const value = typeof job === 'string' ? job : '';
	return value === INGEST_JOB || value.endsWith(`/${INGEST_JOB}`);
}
