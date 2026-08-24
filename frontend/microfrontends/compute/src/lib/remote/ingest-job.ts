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

/**
 * The lineage graph's id for the bronze dataset an ingest run wrote.
 *
 * SAME OFF-BY-A-QUALIFIER AS `isIngestJob` ABOVE, one field over, and it produced an emptier page
 * than an error would have. The ingest service records what a person typed — `project: 'acme'`,
 * `dataset: 'item3proof'` — while the lineage graph stores the catalog identifier the write actually
 * landed under, `acme-bronze$item3proof`. Linking with the bare name resolved to a real route that
 * rendered "No producing runs recorded for this dataset yet", so the run detail offered a Lineage
 * link whose destination said the run did not exist. Measured 2026-08-24: the bare name showed 0
 * runs, the qualified name showed 8.
 *
 * The `-bronze$` shape is the ingest plane's stated contract, not a guess — the ETL form tells the
 * user "Lands as `<project>-bronze$<table>`" before they submit, and ingest has no other landing
 * tier: raw is the external world and silver/gold are the cascade's to write.
 *
 * Returns null when either half is missing, because the caller must render NO link rather than a
 * guessed one — the same rule the run detail already applies to a run with no dataset.
 */
export function bronzeDatasetId(project: unknown, dataset: unknown): string | null {
	const p = typeof project === 'string' ? project.trim() : '';
	const d = typeof dataset === 'string' ? dataset.trim() : '';
	if (!p || !d) return null;
	return `${p}-bronze$${d}`;
}
