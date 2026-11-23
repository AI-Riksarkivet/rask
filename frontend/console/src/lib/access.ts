import * as v from 'valibot';
import type { components } from '#lib/api/generated/catalog.ts';
import { mirrors } from '#lib/api/mirrors.ts';

type Wire = components['schemas'];

// Wire CONTRACTS for the FGA workbench (`/settings/access`) — the frozen shapes of the catalog's
// estate-admin-gated /management/v1/access API. The transport lives in `remote/access.api.ts` (the console's
// data-function dialect: the calls run in the browser with the session bearer and parse THESE
// schemas at that boundary). Each schema mirrors its generated model (`#lib/api/mirrors.ts`).
//
// `TupleConditionSchema`/`AccessTupleSchema` are also the project-creation flow's write contract — the
// initial-admin grant goes through the SAME door (`POST /management/v1/access/tuples`) the workbench writes with.
// One set of shapes for one door, which is why the workbench's contracts landed here rather than
// beside it: this file was already half of them.

/** A CEL condition on a grant — the parameters that ride the TUPLE.
 *
 *  For `non_expired_grant` that is `grant_time` + `grant_duration`: the window. `current_time` is NOT
 *  here — the clock belongs to the caller and is supplied per check, so a grant cannot pin "now" at
 *  the instant it was written. */
export const TupleConditionSchema = mirrors<Wire['TupleCondition']>()(
	v.object({
		name: v.string(),
		context: v.optional(v.record(v.string(), v.unknown()), {}),
	}),
);

/** One stored tuple, as a write or a revoke echoes it. */
export const AccessTupleSchema = mirrors<Wire['AccessTuple']>()(
	v.object({
		user: v.string(), // e.g. user:alice, role:<id>#assignee, namespace:db1
		relation: v.string(), // e.g. owner, can_read_data, parent
		object: v.string(), // e.g. table:<uuid>, warehouse:acme-wh, project:acme
		/** Present when the grant is TIME-BOXED. `null` is permanent — and the distinction has to survive
		 *  the read path, or an expiring grant renders as forever. */
		condition: v.optional(v.nullable(TupleConditionSchema), null),
	}),
);
export type AccessTuple = v.InferOutput<typeof AccessTupleSchema>;

/** A tuple as reads and verdicts answer it. */
export const TupleSchema = mirrors<Wire['NamedAccessTuple']>()(
	v.object({
		...AccessTupleSchema.entries,
		/** The name a table or namespace object holds now, beside `object`, which names its uuid (`table:<uuid>`).
		 *  `null` for any other object and for one no name holds. */
		name: v.optional(v.nullable(v.string()), null),
	}),
);
export type Tuple = v.InferOutput<typeof TupleSchema>;

/** An object as a person reads it: a table or namespace the catalog named as `<type>:<name>`, anything else as written.
 *  The uuid stays the object's id everywhere it is used. */
export const namedObject = (object: string, name: string | null | undefined): string =>
	name ? `${object.slice(0, object.indexOf(':'))}:${name}` : object;

export const TuplesPageSchema = mirrors<Wire['AccessTuplesPage']>()(
	v.object({
		tuples: v.array(TupleSchema),
		continuation: v.nullable(v.string()),
	}),
);

export const AccessModelSchema = mirrors<Wire['AccessModelResponse']>()(
	v.object({
		dsl: v.string(), // the checked-in model.fga text
		authorization_model_id: v.string(),
		/** `{condition: {parameter: type}}` from the compiled model, so the Grant dialog can render TYPED
		 *  inputs instead of a free-text box. Served rather than hand-copied: a hand-kept list drifts the
		 *  moment a parameter changes, and a missing CEL operand is a 503, not a soft failure. */
		conditions: v.optional(v.record(v.string(), v.record(v.string(), v.string())), {}),
	}),
);
export type AccessModel = v.InferOutput<typeof AccessModelSchema>;

/** The type names declared by the model DSL's `type <name>` lines. GET /management/v1/access/tuples rejects a
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

export const CheckVerdictSchema = mirrors<Wire['AccessCheckResult']>()(
	v.object({
		allowed: v.boolean(),
		checked: TupleSchema, // the exact triple the catalog evaluated — echoed so the UI can't lie
	}),
);
export type CheckVerdict = v.InferOutput<typeof CheckVerdictSchema>;

// ── the derivation surfaces (POST /management/v1/access/{list-objects,list-users,expand}) ───────────────────────
//
// The three questions a tuple table structurally cannot answer. Read needs an object type per call and
// returns only STORED tuples, so "what can alice reach" used to mean one audited read per model type —
// and still missed every derived grant, which in a concentric model is most of them.

export const ListObjectsResultSchema = mirrors<Wire['AccessListObjectsResponse']>()(
	v.object({
		objects: v.array(v.string()),
		user: v.string(), // the RESOLVED subject the store saw — never the local form state
		relation: v.string(),
		type: v.string(),
		/** The name each table or namespace object in `objects` holds now; an object no name holds is absent. */
		names: v.optional(v.record(v.string(), v.string()), {}),
	}),
);
export type ListObjectsResult = v.InferOutput<typeof ListObjectsResultSchema>;

export const ListUsersResultSchema = mirrors<Wire['AccessListUsersResponse']>()(
	v.object({
		users: v.array(v.string()), // fully qualified: user:alice, role:validators#assignee, user:*
		object: v.string(),
		/** The name `object` holds now, when it is a table or a namespace. */
		name: v.optional(v.nullable(v.string()), null),
		relation: v.string(),
		user_type: v.string(),
		user_relation: v.optional(v.nullable(v.string()), null),
		/** The server truncated at its listUsersMaxResults ceiling — an under-reported review must never
		 *  render as a complete one, so this drives a visible warning rather than a silent short list. */
		truncated: v.boolean(),
	}),
);
export type ListUsersResult = v.InferOutput<typeof ListUsersResultSchema>;

/** A generated model as the parse answers it: every field present, an absent one read as null. */
type Answered<T> = T extends readonly (infer U)[]
	? Answered<U>[]
	: T extends object
		? { [K in keyof T]-?: Answered<Exclude<T[K], undefined>> }
		: T;

/** One node of the OpenFGA userset tree. Recursive, and deliberately NOT flattened into a path: the
 *  operator (`union` / `intersection` / `difference`) is frequently the whole explanation for a
 *  surprising grant, and a flattened chain has already thrown it away. A leaf's `continues` says the
 *  depth budget stopped there: the affordance for "expand further". */
export type ExpandNode = Answered<Wire['AccessExpandNode']>;
type ExpandLeaf = Answered<Wire['AccessExpandLeaf']>;

// Input is `unknown` and output is the typed node — not the same type, which is the whole point of
// parsing at the boundary. Defaulting input to the output type (a bare `GenericSchema<ExpandNode>`)
// would claim the wire already matches, and these fields legitimately arrive absent or null.
const nullish = <T>(schema: v.GenericSchema<unknown, T>) => v.optional(v.nullable(schema), null);

const ExpandNodeSchema: v.GenericSchema<unknown, ExpandNode> = v.lazy(() =>
	mirrors<Wire['AccessExpandNode']>()(
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
	),
);

const ExpandLeafSchema: v.GenericSchema<unknown, ExpandLeaf> = v.lazy(() =>
	mirrors<Wire['AccessExpandLeaf']>()(
		v.object({
			users: nullish(v.array(v.string())),
			computed: nullish(v.string()),
			tuple_to_userset: nullish(
				v.object({ tupleset: nullish(v.string()), computed: v.optional(v.array(v.string()), []) }),
			),
			expanded: nullish(v.array(ExpandNodeSchema)),
			continues: nullish(v.boolean()),
		}),
	),
);

export const ExpandResultSchema = mirrors<Wire['AccessExpandResponse']>()(
	v.object({
		/** `null` means the relation resolves to nothing here — a real answer. An outage is a 503, and the
		 *  two must never render as each other. */
		tree: v.optional(v.nullable(ExpandNodeSchema), null),
		object: v.string(),
		/** The name `object` holds now, when it is a table or a namespace. */
		name: v.optional(v.nullable(v.string()), null),
		relation: v.string(),
		depth: v.number(),
	}),
);
export type ExpandResult = v.InferOutput<typeof ExpandResultSchema>;

export const SimulateResultSchema = mirrors<Wire['AccessSimulateResult']>()(
	v.object({
		/** With the hypothesis applied. */
		allowed: v.boolean(),
		/** WITHOUT it — the delta is the answer. A grant that changes nothing and a grant that unlocks
		 *  access both set `allowed`; only `baseline` tells them apart, and "this is a no-op" is the more
		 *  common finding. */
		baseline: v.boolean(),
		checked: TupleSchema,
		hypothetical: v.array(AccessTupleSchema),
	}),
);
export type SimulateResult = v.InferOutput<typeof SimulateResultSchema>;
