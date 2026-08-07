import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { catalogJSON } from '$lib/server/doors';

/**
 * This zone's user-state plane — the two documents its DOCK owns: the arrangement and the named
 * views. JSON values ride remote functions (the estate's transport ruling), and the catalog is
 * OIDC-only, so the session bearer is attached by the door (`$lib/server/doors` over
 * `@rask/api/upstream`, #93) rather than by a proxy route.
 */
const UserStateArg = v.object({
	document: v.picklist(['dock-layout', 'dock-layout-library']),
});

/** The catalog's envelope: `{exists, value}` — `exists: false` is a genuine "never saved". */
export interface UserStateEnvelope {
	exists?: boolean;
	value?: unknown;
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
