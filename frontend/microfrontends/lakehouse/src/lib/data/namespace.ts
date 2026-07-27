// Typed client for the NAMESPACE-scoped catalog surfaces via the /capi BFF (sweep group 3: the
// catalog's namespace_router has served access list/check/grant/revoke/graph + policy set/describe/
// delete since #50/#51/#72 — this module gives them a frontend). The access functions are kind-
// generalized ('table' | 'namespace') so GrantsPanel can target either object type with one code
// path; the table-only clients in catalog.ts stay untouched. Types come from the generated catalog
// spec (never hand-mirrored); the policy responses are additionally valibot-parsed at the boundary
// because the catalog serializes them with response_model_exclude_none (absent-not-null fields —
// same rationale as catalog.ts's DropNamespaceResponseSchema): a wire drift throws to the caller
// instead of lying downstream.
import { parse } from '@rask/api';
import * as v from 'valibot';
import type { components } from '@rask/api/generated/catalog';
import { type ApiResult, requestJSON as request } from '$lib/http';

export type AccessList = components['schemas']['AccessListResponse'];
export type AccessCheck = components['schemas']['AccessCheckResponse'];
export type AccessGrant = components['schemas']['AccessGrantResponse'];
export type AccessGraph = components['schemas']['AccessGraphResponse'];
export type PolicyRequest = components['schemas']['PolicyRequest'];

/** The FGA object kinds the catalog's access surface is mounted on — the owner-tier gate is
 * `can_drop` for a table, `can_delete` for a namespace (same bar, per-type relation). */
export type AccessKind = 'table' | 'namespace';

const requestJSON = <T>(path: string, init?: RequestInit) => request<T>('/capi', path, init);

const enc = encodeURIComponent;

/** Access review (#51, kind-generalized): who holds which can_* action on the object. Owner-gated
 * by the catalog (can_drop / can_delete); the BFF forwards only the signed-in user's session. */
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

/** #81 one hop of the authorization graph around the namespace — its direct grantees + the parent
 * edge. Owner-gated by the catalog (can_delete); session-only BFF. */
export const fetchNamespaceAccessGraph = (namespace: string) =>
	requestJSON<AccessGraph>(`v1/namespace/${enc(namespace)}/access/graph`, { method: 'POST' });

// The PolicyResponse wire contract (#50) — serialized with response_model_exclude_none, so the
// nullable bounds arrive absent, not null. Parsed (not cast) at the boundary per the @rask/api
// parse-don't-validate rule: a schema drift throws to the caller instead of lying downstream.
const PolicyResponseSchema = v.object({
	kind: v.string(),
	id: v.string(),
	path: v.string(),
	compact_enabled: v.optional(v.boolean(), true),
	compact_interval_hours: v.optional(v.nullable(v.number())),
	retain_versions: v.optional(v.nullable(v.number())),
	retention_days: v.optional(v.nullable(v.number())),
	target_rows_per_fragment: v.optional(v.nullable(v.number())),
});
export type NamespacePolicy = v.InferOutput<typeof PolicyResponseSchema>;

const PolicyDeleteResponseSchema = v.object({
	status: v.string(),
	kind: v.string(),
	id: v.string(),
});
export type NamespacePolicyDelete = v.InferOutput<typeof PolicyDeleteResponseSchema>;

/** The namespace's maintenance policy (reader-gated) — 404 when none is set, which the caller
 * renders as the honest "no policy, global defaults apply" state (never an error). */
export const fetchNamespacePolicy = async (
	namespace: string,
): Promise<ApiResult<NamespacePolicy>> => {
	const res = await requestJSON<unknown>(`v1/namespace/${enc(namespace)}/policy/describe`, {
		method: 'POST',
	});
	return res.ok ? { ok: true, data: parse(PolicyResponseSchema, res.data) } : res;
};

/** Set (or replace) the namespace-level maintenance policy — applies to every dataset under the
 * namespace's prefix unless a table policy overrides it. Owner-gated by the catalog (can_delete);
 * session-only BFF. */
export const setNamespacePolicy = async (
	namespace: string,
	policy: PolicyRequest,
): Promise<ApiResult<NamespacePolicy>> => {
	const res = await requestJSON<unknown>(`v1/namespace/${enc(namespace)}/policy`, {
		method: 'PUT',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(policy),
	});
	return res.ok ? { ok: true, data: parse(PolicyResponseSchema, res.data) } : res;
};

/** Remove the namespace's maintenance policy (idempotent) — owner-gated, session-only BFF. */
export const deleteNamespacePolicy = async (
	namespace: string,
): Promise<ApiResult<NamespacePolicyDelete>> => {
	const res = await requestJSON<unknown>(`v1/namespace/${enc(namespace)}/policy`, {
		method: 'DELETE',
	});
	return res.ok ? { ok: true, data: parse(PolicyDeleteResponseSchema, res.data) } : res;
};
