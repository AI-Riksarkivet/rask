import * as v from 'valibot';

import { requestJSON } from '$lib/http';

// Wire contracts + client for the FGA workbench (`/admin/access`) — the catalog's estate-admin-gated
// /v1/access API, reached through this zone's narrow /capi pass-through (session bearer-forwarded,
// never a service token). The shapes are the frozen /v1/access contract, parsed (never cast) at the
// browser boundary per the @rask/api parse-don't-validate rule — like jetstream.ts, hand-written
// against the contract because the catalog OpenAPI dump has not yet caught up; swap to the generated
// types once `bun run gen:types:catalog` carries them.

/** A CEL condition on a grant — the parameters that ride the TUPLE.
 *
 *  For `non_expired_grant` that is `grant_time` + `grant_duration`: the window. `current_time` is NOT
 *  here — the clock belongs to the caller and is supplied per check, so a grant cannot pin "now" at
 *  the instant it was written. */
export const TupleConditionSchema = v.object({
	name: v.string(),
	context: v.record(v.string(), v.unknown()),
});
export type TupleCondition = v.InferOutput<typeof TupleConditionSchema>;

export const TupleSchema = v.object({
	user: v.string(), // e.g. user:alice, team:eng#member, namespace:db1
	relation: v.string(), // e.g. owner, can_read_data, parent
	object: v.string(), // e.g. table:db1$t, warehouse:acme-wh
	/** Present when the grant is TIME-BOXED. `null` is permanent — and the distinction has to survive
	 *  the read path, or an expiring grant renders as forever. */
	condition: v.optional(v.nullable(TupleConditionSchema), null),
});
export type Tuple = v.InferOutput<typeof TupleSchema>;

/** What a CALLER supplies to write/delete/check.
 *
 *  `condition` is optional here but required on `Tuple`, and that asymmetry is deliberate: a tuple READ
 *  back from the store always states whether it is conditional (`null` is a real answer — "permanent"),
 *  while a caller writing a permanent grant should not have to say `condition: null` to mean "no". */
export type TupleInput = Omit<Tuple, 'condition'> & { condition?: TupleCondition | null };

export const TuplesPageSchema = v.object({
	tuples: v.array(TupleSchema),
	continuation: v.nullable(v.string()),
});

export const AccessModelSchema = v.object({
	dsl: v.string(), // the checked-in model.fga text
	authorization_model_id: v.string(),
	/** `{condition: {parameter: type}}` from the compiled model, so the Grant dialog can render TYPED
	 *  inputs instead of a free-text box. Served rather than hand-copied: a hand-kept list drifts the
	 *  moment a parameter changes, and a missing CEL operand is a 503, not a soft failure. */
	conditions: v.optional(v.record(v.string(), v.record(v.string(), v.string())), {}),
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
export const writeTuple = (tuple: TupleInput) =>
	requestJSON<unknown>('/capi', 'v1/access/tuples', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(tuple),
	});

/** Revoke: delete one tuple (same body as the write — the tuple IS the identity). */
export const deleteTuple = (tuple: TupleInput) =>
	requestJSON<unknown>('/capi', 'v1/access/tuples', {
		method: 'DELETE',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(tuple),
	});

/** A live OpenFGA Check on any (user, relation, object) triple — the workbench's probe. */
export const checkAccess = (tuple: TupleInput, context: Record<string, unknown> = {}) =>
	requestJSON<unknown>('/capi', 'v1/access/check', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		// `context` supplies the runtime values a CONDITION evaluates against (`current_time`). Sent
		// even when empty is wrong — OpenFGA reads an empty context as "no operands", which ERRORS any
		// CEL expression on the path — so it is omitted entirely unless there is something to say.
		body: JSON.stringify(Object.keys(context).length ? { ...tuple, context } : tuple),
	});

/** The authorization model itself (read-only — model changes are code migrations). */
export const fetchAccessModel = () => requestJSON<unknown>('/capi', 'v1/access/model');

/** The catalog registry (`<ns>$<table>` ids) — one-click graph seeds. */
export const fetchTables = () => requestJSON<{ tables: string[] }>('/capi', 'v1/table');

// ── the derivation surfaces (POST /v1/access/{list-objects,list-users,expand}) ───────────────────────
//
// The three questions a tuple table structurally cannot answer. Read needs an object type per call and
// returns only STORED tuples, so "what can alice reach" used to mean one audited read per model type —
// and still missed every derived grant, which in a concentric model is most of them.

export const ListObjectsResultSchema = v.object({
	objects: v.array(v.string()),
	user: v.string(), // the RESOLVED subject the store saw — never the local form state
	relation: v.string(),
	type: v.string(),
});
export type ListObjectsResult = v.InferOutput<typeof ListObjectsResultSchema>;

export const ListUsersResultSchema = v.object({
	users: v.array(v.string()), // fully qualified: user:alice, role:validators#assignee, user:*
	object: v.string(),
	relation: v.string(),
	user_type: v.string(),
	user_relation: v.nullable(v.string()),
	/** The server truncated at its listUsersMaxResults ceiling — an under-reported review must never
	 *  render as a complete one, so this drives a visible warning rather than a silent short list. */
	truncated: v.boolean(),
});
export type ListUsersResult = v.InferOutput<typeof ListUsersResultSchema>;

/** One node of the OpenFGA userset tree. Recursive, and deliberately NOT flattened into a path: the
 *  operator (`union` / `intersection` / `difference`) is frequently the whole explanation for a
 *  surprising grant, and a flattened chain has already thrown it away. */
export type ExpandNode = {
	name: string | null;
	leaf: ExpandLeaf | null;
	union: ExpandNode[] | null;
	intersection: ExpandNode[] | null;
	difference: { base: ExpandNode | null; subtract: ExpandNode | null } | null;
	truncated: boolean | null;
	cycle: boolean | null;
};

export type ExpandLeaf = {
	users: string[] | null;
	computed: string | null;
	tuple_to_userset: { tupleset: string | null; computed: string[] } | null;
	expanded: ExpandNode[] | null;
	/** Goes further, but the depth budget stopped here — the affordance for "expand further". */
	continues: boolean | null;
};

// Input is `unknown` and output is the typed node — not the same type, which is the whole point of
// parsing at the boundary. Defaulting input to the output type (a bare `GenericSchema<ExpandNode>`)
// would claim the wire already matches, and these fields legitimately arrive absent or null.
const nullish = <T>(schema: v.GenericSchema<unknown, T>) => v.optional(v.nullable(schema), null);

export const ExpandNodeSchema: v.GenericSchema<unknown, ExpandNode> = v.lazy(() =>
	v.object({
		name: nullish(v.string()),
		leaf: nullish(ExpandLeafSchema),
		union: nullish(v.array(ExpandNodeSchema)),
		intersection: nullish(v.array(ExpandNodeSchema)),
		difference: nullish(
			v.object({ base: nullish(ExpandNodeSchema), subtract: nullish(ExpandNodeSchema) }),
		),
		truncated: nullish(v.boolean()),
		cycle: nullish(v.boolean()),
	}),
);

export const ExpandLeafSchema: v.GenericSchema<unknown, ExpandLeaf> = v.lazy(() =>
	v.object({
		users: nullish(v.array(v.string())),
		computed: nullish(v.string()),
		tuple_to_userset: nullish(
			v.object({ tupleset: nullish(v.string()), computed: v.array(v.string()) }),
		),
		expanded: nullish(v.array(ExpandNodeSchema)),
		continues: nullish(v.boolean()),
	}),
);

export const ExpandResultSchema = v.object({
	/** `null` means the relation resolves to nothing here — a real answer. An outage is a 503, and the
	 *  two must never render as each other. */
	tree: v.nullable(ExpandNodeSchema),
	object: v.string(),
	relation: v.string(),
	depth: v.number(),
});
export type ExpandResult = v.InferOutput<typeof ExpandResultSchema>;

const postJSON = (path: string, body: unknown) =>
	requestJSON<unknown>('/capi', path, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body),
	});

/** "What can this subject reach?" — every `type` object the subject holds `relation` on, model
 *  expansion included. `user` may be a userset (`team:eng#member`, `role:validators#assignee`). */
export const listObjects = (query: { user: string; relation: string; type: string }) =>
	postJSON('v1/access/list-objects', query);

/** "Who can do this?" — the effective subject set on one object. Also the blast radius a revoke needs
 *  BEFORE it writes, since a tuple high in the hierarchy cuts access for many principals at once. */
export const listUsers = (query: {
	object: string;
	relation: string;
	userType?: string;
	userRelation?: string | null;
}) =>
	postJSON('v1/access/list-users', {
		object: query.object,
		relation: query.relation,
		user_type: query.userType ?? 'user',
		user_relation: query.userRelation ?? null,
	});

export const SimulateResultSchema = v.object({
	/** With the hypothesis applied. */
	allowed: v.boolean(),
	/** WITHOUT it — the delta is the answer. A grant that changes nothing and a grant that unlocks
	 *  access both set `allowed`; only `baseline` tells them apart, and "this is a no-op" is the more
	 *  common finding. */
	baseline: v.boolean(),
	checked: TupleSchema,
	hypothetical: v.array(TupleSchema),
});
export type SimulateResult = v.InferOutput<typeof SimulateResultSchema>;

/** Assert before you grant: does the proposed tuple actually change the answer? Writes nothing. */
export const simulate = (query: {
	user: string;
	relation: string;
	object: string;
	// Proposals, not stored facts — a caller asking "what if I granted this" has no condition to
	// state unless it is proposing a time-boxed grant, so the input form applies here too.
	hypothetical: readonly TupleInput[];
}) =>
	postJSON('v1/access/simulate', {
		user: query.user,
		relation: query.relation,
		object: query.object,
		hypothetical: query.hypothetical,
	});

/** "Why does this resolve?" — the derivation tree. `depth` follows the cascade server-side (one gate,
 *  one audit row, one response) rather than costing a round trip per hop from the browser. */
export const expand = (query: { object: string; relation: string; depth?: number }) =>
	postJSON('v1/access/expand', {
		object: query.object,
		relation: query.relation,
		depth: query.depth ?? 1,
	});
