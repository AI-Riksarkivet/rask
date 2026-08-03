import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
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

const ANNOTATOR_API = env.ANNOTATOR_API ?? 'http://localhost:8103';

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

/** The deleted route's `requireSession: true`: an assist run writes predictions into a unit the
 *  caller is reviewing, so it must be attributable on an auth-enabled stack. */
function signedOut(): boolean {
	const { locals } = getRequestEvent();
	return locals.authEnabled && !locals.session;
}

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
	}),
	async ({ key, dataset, producer, prompt, region, taskId }): Promise<ApiResult<AssistResult>> => {
		if (signedOut()) return { ok: false, status: 401, detail: 'sign in required' };
		const { fetch } = getRequestEvent();
		const search = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
		let res: Response;
		try {
			res = await fetch(`${ANNOTATOR_API}/api/assist/${key}${search}`, {
				method: 'POST',
				headers: { ...bearerHeaders(), 'content-type': 'application/json' },
				body: JSON.stringify({ producer, prompt, region, task_id: taskId }),
			});
		} catch (err) {
			return { ok: false, status: 0, detail: String(err) };
		}
		const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
		if (!res.ok) {
			return {
				ok: false,
				status: res.status,
				detail:
					typeof body.detail === 'string' ? body.detail : `assist failed (HTTP ${res.status})`,
			};
		}
		return { ok: true, data: body as unknown as AssistResult };
	},
);

/** One row of the assist registry, as the SERVICE reports it. */
export interface ProducerInfo {
	name: string;
	configured: boolean;
	returns: string[];
	/** null = no claim (no task, an unenforced one, or nothing known about what it emits). */
	compatible: boolean | null;
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
		if (signedOut()) return { ok: false, status: 401, detail: 'sign in required' };
		const { fetch } = getRequestEvent();
		const search = taskId ? `?task_id=${encodeURIComponent(taskId)}` : '';
		try {
			const res = await fetch(`${ANNOTATOR_API}/api/assist/producers${search}`, {
				headers: bearerHeaders(),
			});
			const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
			if (!res.ok) {
				return {
					ok: false,
					status: res.status,
					detail:
						typeof body.detail === 'string'
							? body.detail
							: `could not read the assist registry (HTTP ${res.status})`,
				};
			}
			return { ok: true, data: body as unknown as ProducerListing };
		} catch (err) {
			return { ok: false, status: 0, detail: String(err) };
		}
	},
);
