import * as v from 'valibot';
import { error } from '@sveltejs/kit';
import { command, query, getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import { lineageAuthHeaders } from '@rask/api/runs-feed';
import { isIngestJob } from './ingest-job';
import {
	getIngestRun,
	IngestRefusal,
	ingestLifecycle,
	listIngestSources,
	startIngest as postIngest,
	type IngestAccepted,
	type IngestLifecycle,
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

/** The signed-in caller's bearer, for the governed doors.
 *
 *  SvelteKit's request-scoped `fetch` forwards COOKIES but attaches no `Authorization` header — that
 *  is the BFF proxy's job on the client path, and there is no proxy in front of a remote function. So
 *  without this every call here arrives at the ingest door carrying only the gateway's own Dapr
 *  app-token, and the door correctly refuses it ("'gateway' is a public front door"). The estate's
 *  reference for this is `lakehouse/src/lib/admin/remote/access.remote.ts:47-50`. */
function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** A run id. Parsed at the boundary so a malformed id is refused before it reaches the gateway. */
const RunId = v.pipe(v.string(), v.trim(), v.minLength(1));

/** Same default as this zone's notification feed — one zone, one lineage endpoint. */
const LINEAGE_API = env.LINEAGE_API ?? 'http://localhost:8001';

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
	try {
		return await getIngestRun(runId, getRequestEvent().fetch, bearerHeaders());
	} catch (cause) {
		// RE-THROW AS AN HttpError, so the STATUS reaches the browser.
		//
		// A plain thrown Error is redacted to "Internal Error" on its way to the client — SvelteKit
		// only sends `error()` through intact. So without this the page received one opaque failure
		// for every cause and rendered its single message, which was "No such run. The ingest plane
		// has no record of <id>."
		//
		// That was measured wrong on the live estate 2026-08-26: a 403 (the signed-in user held no
		// `admin` on the project) was reported as a vanished run, while the record AND its workflow
		// both existed. Telling someone their run is gone when it is merely not theirs to see sends
		// them to the wrong place, confidently. The page now branches on this status.
		if (cause instanceof IngestRefusal) error(cause.status, cause.message);
		throw cause;
	}
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
	return listIngestSources(getRequestEvent().fetch, bearerHeaders());
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
 *
 * WHY `command` AND NOT `form`, since SvelteKit's docs prefer `form` for its graceful degradation:
 * this form's FIELDS ARE NOT STATIC. They are rendered from `GET /v1/ingests/sources` at runtime —
 * `local-dir` asks for a directory and a glob, `iiif` for a volume and a page cap, and a source kind
 * added tomorrow brings its own — which is invariant I1's whole point, and precisely what keeps
 * adding a source a backend-only diff. `form()` derives its fields from a schema declared at build
 * time, so expressing a registry-driven form through it would mean restating every adapter's options
 * in TypeScript: the exact weld this page was rewritten to remove. The no-JS fallback is not worth
 * re-welding the sources into the frontend to buy.
 *
 * No single-flight refresh is attached: the two queries beside this one are the source REGISTRY
 * (immutable for the life of the pod) and a per-RUN status keyed by an id that does not exist until
 * this call returns. There is nothing on screen that this mutation staleness-invalidates — and when
 * a runs LIST lands, that is the query this command should refresh.
 */
export const startIngest = command(IngestInput, async (input): Promise<IngestAccepted> => {
	return postIngest(input, getRequestEvent().fetch, bearerHeaders());
});

/** What a lifecycle button gets back. A RESULT UNION, never a throw.
 *
 *  The three refusals this door issues are all things an operator must be able to READ: 409 for a run
 *  that already finished ("nothing to terminate"), 409 for a resume on a run that was never paused,
 *  503 for an engine that did not answer. Thrown, they collapse into one red toast; returned, the
 *  page can say which. This is the `ApiResult` shape the dock-layout store established — status-driven
 *  UI states rather than exception flow. */
export type LifecycleResult =
	| { ok: true; state: string; detail: string }
	| { ok: false; status: number; detail: string };

const LifecycleInput = v.object({
	runId: RunId,
	action: v.picklist(['terminate', 'pause', 'resume']),
});

/**
 * Stop, hold, or release a live ingest run.
 *
 * A `command()` and not a `query()` because it MUTATES — and a single-flight refresh is attached for
 * exactly the reason `startIngest`'s comment says one was not: there IS something on screen this
 * staleness-invalidates now. The run's own status and the runs list both change the moment the door
 * accepts, and a remote `query()` re-CALLED returns its cached value — so without the explicit
 * `.refresh()` the button would work, the door would act, and the page would keep showing the old
 * state until a reload. That is the estate's most-repeated remote-function trap.
 *
 * `void` on the refreshes deliberately: a refresh that fails must not fail the mutation the user
 * already succeeded at. The catch keeps an unhandled rejection from evicting the query from cache,
 * which is what silently kills a poll loop.
 */
export const runLifecycle = command(
	LifecycleInput,
	async ({ runId, action }): Promise<LifecycleResult> => {
		const res: Awaited<ReturnType<typeof ingestLifecycle>> = await ingestLifecycle(
			runId,
			action,
			getRequestEvent().fetch,
			bearerHeaders(),
		);
		if (!res.ok) return { ok: false, status: res.status, detail: res.detail };

		void getIngestRunStatus(runId)
			.refresh()
			.catch(() => {});
		void listIngestRuns()
			.refresh()
			.catch(() => {});

		const value: IngestLifecycle = res.value;
		return {
			ok: true,
			state: value.state ?? '',
			// The door's OWN words. `terminate` says further scheduling stops while an in-flight activity
			// runs to completion; `pause` says the run still holds its queue and its consumer. Rewriting
			// either into something friendlier is how an operator comes to believe the work has stopped.
			detail: value.detail ?? '',
		};
	},
);

/** One ingest run as the LIST renders it — deliberately fewer fields than the detail page. */
export interface IngestRunRow {
	run_id: string;
	/** The INGEST run id — the id `/ingests/{id}` answers to — from the producer's lance facet.
	 *  `run_id` above is the graph's DERIVED id (a one-way UUID5): linking with it produced a board
	 *  where every row 404'd. Null for runs recorded before producers stated it; the page renders
	 *  those unlinked with the reason, never guesses. */
	source_run_id: string | null;
	/** What the run wrote (the terminal event's outputs) — the human handle for a row. */
	table: string | null;
	state: string | null;
	progress_done: number | null;
	progress_total: number | null;
	error_message: string | null;
	started_at: string | null;
	updated_at: string | null;
}

// The job matcher lives in a sibling module: it is namespace-qualified in the graph, it needed a test,
// and a `.remote.ts` may export only remote functions. See `ingest-job.ts` for why a bare-name
// comparison left this board permanently empty.

/** How many rows the list shows. Trimmed on the SERVER, and that is not a nicety: `/runs` measured
 *  330_103 bytes for 875 runs on the live estate (`@rask/api/runs-feed:156`). Shipping that to a
 *  browser to filter it there would send a third of a megabyte to render twenty rows. */
const WINDOW = 50;

/**
 * The ingest plane's RUN LIST.
 *
 * It reads LINEAGE, not the ingest service, and that is the whole reason this exists at all: the
 * ingest service cannot list its own runs. It has three routes (`/sources`, `POST /ingests`,
 * `GET /ingests/{run_id}`) and its `RunStore` has only `get`/`put` — and the production store is
 * `InMemoryRunStore`, a per-pod dict that is DELIBERATELY not durable ("run truth is the workflow's,
 * not this cache"). So there is no index to add a `list()` to; the durable record of which runs
 * exist is the lineage graph, which every ingest run writes to at START and again at COMPLETE/FAIL.
 *
 * The trade is worth naming: lineage rows are generic runs, so this list carries state and progress
 * but NOT the ingest-specific half (committed version, the §D2 publication fields, per-unit errors).
 * Those live on the detail page, which reads the ingest door directly. A list that lies about having
 * them would be worse than one that links to where they are.
 *
 * Auth prefers the USER's bearer and falls back to the zone's read-only service identity, exactly
 * like the notification feed — so a signed-in operator sees what they are entitled to and an
 * anonymous page load still renders the read-only board.
 */
export const listIngestRuns = query(async (): Promise<IngestRunRow[]> => {
	const { fetch, locals } = getRequestEvent();
	const res = await fetch(`${LINEAGE_API}/runs`, {
		headers: lineageAuthHeaders({
			accessToken: locals.session?.accessToken,
			serviceToken: env.LINEAGE_SERVICE_TOKEN,
			serviceId: env.LINEAGE_SERVICE_ID,
		}),
	});
	// A governed refusal and an outage are both "no board" to this page, and neither is worth
	// throwing across the remote boundary — the caller renders an empty list with its own honest
	// message rather than a boundary error over a feed that is merely unavailable.
	if (!res.ok) return [];

	// Field-typed picks, not casts (#94): `(r.state as string) ?? null` would hand a NUMBER through
	// typed as string — a lie the board renders. A wrong-typed field degrades to null instead.
	const str = (u: unknown): string | null => (typeof u === 'string' ? u : null);
	const num = (u: unknown): number | null => (typeof u === 'number' ? u : null);
	const body: unknown = await res.json().catch(() => null);
	const rows =
		body !== null && typeof body === 'object' && 'runs' in body && Array.isArray(body.runs)
			? body.runs
			: [];
	return rows
		.filter((r): r is Record<string, unknown> => typeof r === 'object' && r !== null)
		.filter((r) => isIngestJob(r.job))
		.slice(0, WINDOW)
		.map((r) => ({
			run_id: String(r.run_id ?? ''),
			source_run_id: str(r.source_run_id),
			table: Array.isArray(r.outputs) && typeof r.outputs[0] === 'string' ? r.outputs[0] : null,
			state: str(r.state),
			progress_done: num(r.progress_done),
			progress_total: num(r.progress_total),
			error_message: str(r.error_message),
			started_at: str(r.started_at),
			updated_at: str(r.updated_at),
		}))
		.filter((r) => r.run_id);
});
