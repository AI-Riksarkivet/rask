import { command, getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { AssistShape } from '@rask/labeling/annotations-client';

// Interactive AI-assist over ONE unit — GroundingDINO text / SAM region → predicted shapes
// (open_transport.md, area 4). The `/api/assist/[...path]` proxy is deleted; this command is the
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
}

export const requestAssist = command(
	v.object({
		/** The unit key-path (`doc/speech/chunk`), exactly as the annotations URL carries it. */
		key: v.string(),
		/** The dataset selector — null for the backend default (no `?dataset=`). */
		dataset: v.nullable(v.string()),
		producer: v.string(),
		prompt: v.string(),
		region: v.nullable(
			v.object({ x: v.number(), y: v.number(), width: v.number(), height: v.number() }),
		),
	}),
	async ({ key, dataset, producer, prompt, region }): Promise<ApiResult<AssistResult>> => {
		if (signedOut()) return { ok: false, status: 401, detail: 'sign in required' };
		const { fetch } = getRequestEvent();
		const search = dataset ? `?dataset=${encodeURIComponent(dataset)}` : '';
		let res: Response;
		try {
			res = await fetch(`${ANNOTATOR_API}/api/assist/${key}${search}`, {
				method: 'POST',
				headers: { ...bearerHeaders(), 'content-type': 'application/json' },
				body: JSON.stringify({ producer, prompt, region }),
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
