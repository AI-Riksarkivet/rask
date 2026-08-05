import * as v from 'valibot';
import { command, query, getRequestEvent } from '$app/server';
import {
	getIngestRun,
	listIngestSources,
	startIngest as postIngest,
	type IngestAccepted,
	type IngestRun,
	type SourceDescriptor,
} from '@rask/api';

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

/**
 * The source kinds this deployment has registered, and the options each one takes.
 *
 * The ingest form is BUILT from this rather than restating it. It previously called
 * `ingestIIIFVolume()` with `kind: 'iiif'`, `project: 'default'` and `dataset: 'pages'` baked in —
 * beneath its own comment explaining that the door is source-agnostic. That is invariant I1's weld
 * re-formed one layer out, and it is why `S3PrefixSource` was reachable by curl but not by anyone
 * using the product.
 *
 * No-arg, so the cache key is the function identity: the registry is populated once at app start
 * and cannot change under a running pod.
 */
export const getIngestSources = query(async (): Promise<SourceDescriptor[]> => {
	return listIngestSources(getRequestEvent().fetch);
});

/** What the form sends. Parsed at the boundary, so a malformed request is refused here rather than
 *  becoming a 422 from FastAPI that the UI has to interpret. */
const IngestInput = v.object({
	kind: v.pipe(v.string(), v.trim(), v.minLength(1)),
	project: v.pipe(v.string(), v.trim(), v.minLength(1)),
	dataset: v.pipe(v.string(), v.trim(), v.minLength(1)),
	options: v.optional(v.record(v.string(), v.unknown()), {}),
	// Per-run write partitioning. Every field optional — unset means the deployment default. The
	// DOOR is what refuses an out-of-range value (a `fragment_rows` at or above the queue's
	// max_ack_pending would hang the drain), so this only checks shape, never policy: duplicating
	// the ceiling here would be a second copy of a rule that can drift.
	sizing: v.optional(v.record(v.string(), v.pipe(v.number(), v.integer(), v.minValue(1))), {}),
	idempotencyKey: v.optional(v.string()),
});

/**
 * Accept an ingest run — a `command()`, and it has to be one.
 *
 * This was called CLIENT-side, straight from the form, against the relative `/api/ingest/ingests`
 * URL. That request carries cookies but no `Authorization` header, so on an auth-enabled estate it
 * reached the door as the GATEWAY's own identity with no user behind it — and the door correctly
 * refused it:
 *
 *     'gateway' is a public front door: its Dapr app-token authenticates the proxy, not the caller
 *
 * Found by pressing the button in a browser; every unit test passed throughout, because none of them
 * has a session. The gateway's Dapr app-token proves the PROXY, never the human, which is the whole
 * point of the public-caller rule — so the fix is not to loosen the door but to call it from
 * somewhere that holds the user's bearer.
 *
 * A remote command runs on the ZONE SERVER, where `getRequestEvent().fetch` carries the session
 * established by the OIDC BFF. Same seam the read queries above already use, and the estate's stated
 * direction for every JSON value surface.
 */
export const startIngest = command(IngestInput, async (input): Promise<IngestAccepted> => {
	return postIngest(input, getRequestEvent().fetch);
});
