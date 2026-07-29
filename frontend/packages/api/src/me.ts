// @rask/api/me — the frozen `GET /v1/me` identity contract + the shared BFF-side fetch helper.
//
// The catalog serves `/v1/me` for any VERIFIED bearer: `{ sub, name, email, estate_admin, projects }`
// (estate_admin = can_observe_events on the FGA root object; projects via the #51 list wrappers, each
// relation degrading to [] on an FGA outage). Anon is a 401, and with FGA off the backend answers
// `estate_admin: true, projects: []` (dev parity). Both sides code against THIS shape — no drift.
//
// Client-safe on purpose (valibot + fetch only, no node:crypto / $env), so the `Me` type and schema can
// flow into universal load functions and components; the fetch itself belongs in a zone's server load,
// where the sealed session's access token lives.
import * as v from 'valibot';

/** One project membership row from `/v1/me` — the tenant and the caller's role in it. */
export const MeProjectSchema = v.object({
	project: v.string(),
	role: v.picklist(['admin', 'member']),
});

/** The frozen `/v1/me` response. Parsed ONCE at the fetch boundary (parse-don't-validate). */
export const MeSchema = v.object({
	sub: v.string(),
	name: v.nullable(v.string()),
	email: v.nullable(v.string()),
	estate_admin: v.boolean(),
	projects: v.array(MeProjectSchema),
});

export type MeProject = v.InferOutput<typeof MeProjectSchema>;
export type Me = v.InferOutput<typeof MeSchema>;

/** Default `fetchMe` budget — identity is chrome, not content; a slow catalog must not hold a page. */
const ME_TIMEOUT_MS = 5000;

/**
 * Fetch the caller's identity from `GET <catalogUrl>/v1/me`, forwarding the session bearer.
 *
 * BFF-side (call it from a zone's `+layout.server.ts` with `locals.session?.accessToken`): resolves to
 * the parsed `Me`, or `null` on ANY failure — no token, a 401/403, a network error, a timeout
 * (`timeoutMs`, default 5s), or a response that drifts from the frozen schema. Never throws: the navbar
 * renders signed-out chrome on `null` rather than 500ing the zone (same degrade posture as the backend's
 * own per-relation FGA fallbacks).
 */
export async function fetchMe(opts: {
	catalogUrl: string;
	accessToken?: string | null | undefined;
	fetchFn?: typeof fetch;
	timeoutMs?: number;
}): Promise<Me | null> {
	const { catalogUrl, accessToken, fetchFn = fetch, timeoutMs = ME_TIMEOUT_MS } = opts;
	if (!accessToken) return null;
	try {
		const res = await fetchFn(`${catalogUrl.replace(/\/$/, '')}/v1/me`, {
			headers: { authorization: `Bearer ${accessToken}` },
			signal: AbortSignal.timeout(timeoutMs),
		});
		if (!res.ok) return null;
		return v.parse(MeSchema, await res.json());
	} catch {
		return null;
	}
}
