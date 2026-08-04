import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';

/**
 * This zone's user-state plane — the two documents its DOCK owns: the arrangement and the named
 * views. JSON values ride remote functions (the estate's transport ruling), and the catalog is
 * OIDC-only, so the session bearer is attached here rather than by a proxy route.
 */
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

const UserStateArg = v.object({
	document: v.picklist(['dock-layout', 'dock-layout-library']),
});

/** The catalog's envelope: `{exists, value}` — `exists: false` is a genuine "never saved". */
export interface UserStateEnvelope {
	exists?: boolean;
	value?: unknown;
}

/** One catalog call → `ApiResult`. The STATUS is load-bearing for the dock's three outcomes: 409 is
 *  "a document exists and cannot be read" (never overwrite), 401 is signed out, 0 is unreachable —
 *  none of which may be flattened into "empty", the flattening that destroys a user's workspace. */
async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	let res: Response;
	try {
		res = await fetch(`${CATALOG_API}${path}`, {
			...init,
			headers: {
				'content-type': 'application/json',
				...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
				...init?.headers,
			},
		});
	} catch (e) {
		return { ok: false, status: 0, detail: e instanceof Error ? e.message : 'catalog unreachable' };
	}
	if (!res.ok) return { ok: false, status: res.status, detail: await res.text().catch(() => '') };
	const body: unknown = await res.json().catch(() => null);
	return { ok: true, data: body };
}

export const readUserStateDoc = query(
	UserStateArg,
	async ({ document }): Promise<ApiResult<UserStateEnvelope>> => {
		const res = await catalogJSON(`/v1/user-state/${document}`);
		return res.ok ? { ok: true, data: res.data as UserStateEnvelope } : res;
	},
);

export const writeUserStateDoc = command(
	v.object({ ...UserStateArg.entries, value: v.unknown() }),
	async ({ document, value }): Promise<ApiResult<unknown>> =>
		catalogJSON(`/v1/user-state/${document}`, { method: 'PUT', body: JSON.stringify(value) }),
);
