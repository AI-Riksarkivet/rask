import { describe, expect, it } from 'vitest';
import { INGEST_JOB, isIngestJob } from './ingest-job';

/**
 * The board said "No ingest runs" while the graph held every run that had ever succeeded.
 *
 * The filter compared a row's job to the bare `'ingest.run'`. The lineage service writes
 * `f"{namespace}/{name}"` (`repository.py:477`), so the stored value is `rask/ingest.run` — read back
 * verbatim from AGE with `MATCH (r:Run) RETURN DISTINCT r.job`. The comparison could not match a row,
 * ever, and the failure presents as an auth or outage symptom because that is what the page's own
 * fallback text suggests.
 */
describe('isIngestJob', () => {
	it('matches the QUALIFIED job the lineage graph actually stores', () => {
		// The exact value read from the deployed graph. This single assertion is the regression.
		expect(isIngestJob('rask/ingest.run')).toBe(true);
	});

	it('matches under ANY namespace, because the namespace is deployment config', () => {
		// `LineageSettings().namespace` is configurable; hardcoding `rask/` would move the same bug one
		// level down and break on the release that renames it.
		expect(isIngestJob('anything/ingest.run')).toBe(true);
	});

	it('still matches the bare name, for a single-namespace or test emitter', () => {
		expect(isIngestJob(INGEST_JOB)).toBe(true);
	});

	it('does NOT match a different lane that merely ends in similar text', () => {
		// The `/` in the suffix check is what makes this false. Without it the board would quietly
		// adopt another plane's runs — the mirrored failure, and a harder one to notice than an empty
		// board because it looks like data.
		expect(isIngestJob('rask/pre-ingest.run')).toBe(false);
	});

	it('does NOT match the other jobs that share this estate-wide feed', () => {
		// Real values from the same graph: the board is estate-wide and carries catalog operations,
		// movers and training runs, so the filter is what makes it an INGEST board.
		expect(isIngestJob('lance-catalog/create_table.bind86-bronze$pages-e2e')).toBe(false);
		expect(isIngestJob('lance-catalog/drop_table.undropns$demo2')).toBe(false);
	});

	it('survives a row with no job at all', () => {
		// `/runs` rows are parsed as loose records; a missing or non-string job must be a non-match
		// rather than a throw across the remote boundary.
		expect(isIngestJob(undefined)).toBe(false);
		expect(isIngestJob(null)).toBe(false);
		expect(isIngestJob(42)).toBe(false);
	});
});
