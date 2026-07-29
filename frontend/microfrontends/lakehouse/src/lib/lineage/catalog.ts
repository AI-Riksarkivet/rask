// Typed client for the CATALOG service via the /capi BFF proxy (the /api proxy covers lineage).
// Types are generated from docs/catalog-openapi.json (`bun run gen:types:catalog`) — never hand-mirrored.
import type { components } from '@rask/api/generated/catalog';
import { requestJSON as request } from '$lib/http';

export type AccessList = components['schemas']['AccessListResponse'];
export type AccessCheck = components['schemas']['AccessCheckResponse'];

const requestJSON = <T>(path: string, init?: RequestInit) => request<T>('/capi', path, init);

const enc = encodeURIComponent;

/** The FGA object kinds the catalog's access surface is mounted on — the owner-tier gate is
 * `can_drop` for a table, `can_delete` for a namespace (same bar, per-type relation). */
export type AccessKind = 'table' | 'namespace';

/** Access review (#51, kind-generalized like the data zone's namespace.ts): who holds which can_*
 * action on the object. Owner-gated by the catalog (403 for non-owners); the BFF forwards only the
 * signed-in user's session. */
export const fetchAccess = (kind: AccessKind, id: string) =>
	requestJSON<AccessList>(`v1/${kind}/${enc(id)}/access/list`, { method: 'POST' });

/** #68 "who can do what" simulator — a live OpenFGA Check: does `user` hold `relation` on this
 * object? Owner-gated by the catalog, the same bar as the review (probing the graph == disclosing it). */
export const checkAccess = (kind: AccessKind, id: string, user: string, relation: string) =>
	requestJSON<AccessCheck>(`v1/${kind}/${enc(id)}/access/check`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ user, relation }),
	});

export type AccessGrant = components['schemas']['AccessGrantResponse'];

/** #72 grant a base rung (owner/writer/reader/validator) to a subject on the object. `user` may be a
 * bare id (`alice`) or a userset (`role:…#assignee` / `team:…#member`). Owner-gated by the catalog;
 * the BFF forwards only the signed-in user's session. */
export const grantAccess = (kind: AccessKind, id: string, user: string, relation: string) =>
	requestJSON<AccessGrant>(`v1/${kind}/${enc(id)}/access/grant`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ user, relation }),
	});
/** #72 revoke a base rung from a subject on the object — the write counterpart of the grant, same gate. */
export const revokeAccess = (kind: AccessKind, id: string, user: string, relation: string) =>
	requestJSON<AccessGrant>(`v1/${kind}/${enc(id)}/access/revoke`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ user, relation }),
	});
