import * as v from 'valibot';

import { requestJSON } from '$lib/http';

// Wire contracts + client for the FGA workbench (`/admin/access`) — the catalog's estate-admin-gated
// /v1/access API, reached through this zone's narrow /capi pass-through (session bearer-forwarded,
// never a service token). The shapes are the frozen /v1/access contract, parsed (never cast) at the
// browser boundary per the @rask/api parse-don't-validate rule — like jetstream.ts, hand-written
// against the contract because the catalog OpenAPI dump has not yet caught up; swap to the generated
// types once `bun run gen:types:catalog` carries them.

/** One relationship tuple — the atom of the whole authorization model. */
export const TupleSchema = v.object({
	user: v.string(), // e.g. user:alice, team:eng#member, namespace:db1
	relation: v.string(), // e.g. owner, can_read_data, parent
	object: v.string(), // e.g. table:db1$t, warehouse:acme-wh
});
export type Tuple = v.InferOutput<typeof TupleSchema>;

export const TuplesPageSchema = v.object({
	tuples: v.array(TupleSchema),
	continuation: v.nullable(v.string()),
});

export const AccessModelSchema = v.object({
	dsl: v.string(), // the checked-in model.fga text
	authorization_model_id: v.string(),
});
export type AccessModel = v.InferOutput<typeof AccessModelSchema>;

/** The type names declared by the model DSL's `type <name>` lines. GET /v1/access/tuples rejects a
 * bare `user` filter by design (an OpenFGA Read needs an object type), so user-scoped readers fan
 * out one {user, objectType} request per model type — bounded by the model itself. */
export const parseModelTypes = (dsl: string): string[] => [
	...new Set(
		dsl
			.split('\n')
			.map((line) => /^\s*type\s+(\S+)\s*$/.exec(line)?.[1])
			.filter((t): t is string => t !== undefined),
	),
];

export const CheckVerdictSchema = v.object({
	allowed: v.boolean(),
	checked: TupleSchema, // the exact triple the catalog evaluated — echoed so the UI can't lie
});
export type CheckVerdict = v.InferOutput<typeof CheckVerdictSchema>;

export type TupleFilter = {
	objectType?: string;
	user?: string;
	object?: string;
	pageSize?: number;
	continuation?: string;
};

/** One filtered page of tuples (server-side pagination via the opaque continuation token). */
export const fetchTuples = (filter: TupleFilter = {}) => {
	const q = new URLSearchParams();
	if (filter.objectType) q.set('object_type', filter.objectType);
	if (filter.user) q.set('user', filter.user);
	if (filter.object) q.set('object', filter.object);
	if (filter.pageSize) q.set('page_size', String(filter.pageSize));
	if (filter.continuation) q.set('continuation', filter.continuation);
	const qs = q.toString();
	return requestJSON<unknown>('/capi', `v1/access/tuples${qs ? `?${qs}` : ''}`);
};

/** Grant: write one tuple. Session-only at the BFF; estate-admin gated by the catalog. */
export const writeTuple = (tuple: Tuple) =>
	requestJSON<unknown>('/capi', 'v1/access/tuples', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(tuple),
	});

/** Revoke: delete one tuple (same body as the write — the tuple IS the identity). */
export const deleteTuple = (tuple: Tuple) =>
	requestJSON<unknown>('/capi', 'v1/access/tuples', {
		method: 'DELETE',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(tuple),
	});

/** A live OpenFGA Check on any (user, relation, object) triple — the workbench's probe. */
export const checkAccess = (tuple: Tuple) =>
	requestJSON<unknown>('/capi', 'v1/access/check', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(tuple),
	});

/** The authorization model itself (read-only — model changes are code migrations). */
export const fetchAccessModel = () => requestJSON<unknown>('/capi', 'v1/access/model');

/** The catalog registry (`<ns>$<table>` ids) — one-click graph seeds. */
export const fetchTables = () => requestJSON<{ tables: string[] }>('/capi', 'v1/table');
