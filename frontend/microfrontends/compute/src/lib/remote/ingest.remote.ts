import * as v from 'valibot';
import { query, getRequestEvent } from '$app/server';
import { getIngestRun, type IngestRun } from '@rask/api';

// The ingest plane's READ surface for the compute zone (open_ingest.md A20).
//
// A remote `query()`, not a `+server.ts` route: the payload is a typed app VALUE — a run's
// status, its committed version, its errors — which is exactly the half of the transport rule that
// belongs on remote functions. Bytes and row batches go to `+server.ts`; a run record does not.
//
// It reuses `@rask/api`'s `getIngestRun` and its valibot schema rather than re-fetching and
// re-validating here, so the wire contract has ONE definition. `getRequestEvent().fetch` is
// SvelteKit's request-scoped fetch: it resolves the relative `/api/*` URL against the request origin
// during SSR (a bare global `fetch` has no origin on the server) and inlines the response into the
// SSR payload, so the first frame is rendered rather than fetched after mount.

/** A run id. Parsed at the boundary so a malformed id is refused before it reaches the gateway. */
const RunId = v.pipe(v.string(), v.trim(), v.minLength(1));

/**
 * One ingest run's live status.
 *
 * `defect` is the field worth having a UI for at all: A8 says *"a green sync with no lineage edge
 * is a bug the UI should surface, not report green"*. The server sets it only when a run reports
 * success AND the lineage graph, having been asked and answered, does not contain it — an
 * unreachable graph reports no defect, because absent and unknown are different claims.
 *
 * Takes the run id as a parameter, so navigating between runs re-keys the query instead of serving
 * the first run's cached answer for every id.
 */
export const getIngestRunStatus = query(RunId, async (runId): Promise<IngestRun> => {
	return getIngestRun(runId, getRequestEvent().fetch);
});
