import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { SIGN_IN_REQUIRED, annotatorJSON, signedOut } from '$lib/server/doors';
import type { AssistShape } from '@rask/labeling/annotations-client';

// Interactive AI-assist over ONE unit — GroundingDINO text / SAM region → predicted shapes
// (the transport ruling, area 4). The `/api/assist/[...path]` proxy is deleted; this command is the
// whole surface it carried, with the same bearer-forwarding and the same fail-closed stance its
// `requireSession: true` gave it.
//
// The unit is addressed by its KEY (`doc/speech/chunk`) plus the dataset selector, not by a URL: the
// deleted route took whatever path the browser appended, and `assistUrlFor` built that path by
// string-replacing `/api/annotations/` → `/api/assist/` in the unit's annotations URL. The same
// surgery now yields ARGUMENTS instead of a URL, so the server builds the upstream path itself.
//
// Note what does NOT move: the annotations transport beside it stays a `+server.ts` route. It is
// Arrow IPC with the `X-Annotations-Version` header and a 409 save contract — bytes and HTTP
// semantics, which is exactly the half of the rule that keeps its own endpoint.

/** The producer's answer. Passed through with the type the client already read it as — the runner
 *  contract is the backend's, and hand-mirroring it in valibot would be a second source of truth. */
export interface AssistResult {
	shapes: AssistShape[];
	source: string;
	/** Predictions the TASK's contract refused, each naming the rule. Empty for an unconstrained
	 *  canvas. Surfaced rather than swallowed: a producer quietly returning work nobody sees is the
	 *  failure mode where a model looks configured and does nothing. */
	dropped?: string[];
}

export const requestAssist = command(
	v.object({
		/** The unit key-path (`doc/speech/chunk`), exactly as the annotations URL carries it. */
		key: v.string(),
		/** The dataset selector — null for the backend default (no `?dataset=`). */
		dataset: v.nullable(v.string()),
		producer: v.string(),
		prompt: v.string(),
		/** The labeling task, when the canvas was opened from one. The SERVER reads that task's
		 *  captured template — the client never sends the rules it is judged by. */
		taskId: v.nullable(v.string()),
		region: v.nullable(
			v.object({ x: v.number(), y: v.number(), width: v.number(), height: v.number() }),
		),
		/** The interactive point-prompt session (SAM click convention): every point clicked so
		 *  far, foreground/background signed. The request carries the FULL set each time — the
		 *  backend is stateless, so re-running with one more point REFINES the same object. */
		points: v.nullable(v.array(v.object({ x: v.number(), y: v.number(), positive: v.boolean() }))),
	}),
	async ({
		key,
		dataset,
		producer,
		prompt,
		region,
		taskId,
		points,
	}): Promise<ApiResult<AssistResult>> => {
		if (signedOut()) return SIGN_IN_REQUIRED;
		const search = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
		const result = await annotatorJSON(`/api/assist/${key}${search}`, {
			method: 'POST',
			body: JSON.stringify({ producer, prompt, region, task_id: taskId, points: points ?? [] }),
		});
		// The runner contract is the backend's — hand it on with the type the client already read
		// it as (the cast is #94's, not this transport's).
		return result.ok ? { ok: true, data: result.data as AssistResult } : result;
	},
);

/** One row of the assist registry, as the SERVICE reports it. */
export interface ProducerInfo {
	name: string;
	configured: boolean;
	returns: string[];
	/** null = no claim (no task, an unenforced one, or nothing known about what it emits). */
	compatible: boolean | null;
	/** False ⇒ jobs-seam only; the bar must not offer it as an interactive mode. Optional so an
	 *  older service (no flag) keeps every producer interactive, the previous behaviour. */
	interactive?: boolean;
}

export interface ProducerListing {
	producers: ProducerInfo[];
	default_configured: boolean;
}

/**
 * The settings surface: which producers exist, whether each is real or mocked, what it returns,
 * and — with a task — whether that output can satisfy it.
 *
 * Asked of the SERVICE, deliberately. This REPLACES the `zoneConfig` remote query, which answered
 * the same questions by re-parsing `MEDIA_ASSIST_BACKENDS` and `MEDIA_ASSIST_URL` out of THIS pod's
 * env — a second copy of the registry whose own comment conceded it could "drift from the service's".
 * A web pod configured differently from the service would then name producers that do not exist,
 * miss ones that do, and — worst — hide the honest-mock chip over shapes the service actually mocked.
 * The service is the process that resolves and calls a backend, so it is the only answer that cannot
 * be wrong.
 *
 * FAIL-HONEST: an unreachable service must leave the mock chip UP. Mock is the stack's default state,
 * so `ok: false` means the caller keeps its warning rather than clearing it on a failed read.
 *
 * Endpoints are never returned (the service redacts them): presence is the whole of what a surface
 * needs, and an internal model-server URL is not an annotator's to have.
 */
export const assistProducers = query(
	v.nullable(v.string()),
	async (taskId): Promise<ApiResult<ProducerListing>> => {
		if (signedOut()) return SIGN_IN_REQUIRED;
		const search = taskId ? `?task_id=${encodeURIComponent(taskId)}` : '';
		const result = await annotatorJSON(`/api/assist/producers${search}`);
		return result.ok ? { ok: true, data: result.data as ProducerListing } : result;
	},
);
