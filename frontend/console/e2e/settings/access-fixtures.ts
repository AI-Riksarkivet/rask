// The FGA access surface's e2e fixtures, shared by `access.spec.ts` and `../mock-catalog.ts`: they feed
// the mock catalog behind `/api/catalog`, and the spec asserts what the surface sent through
// `/__mock/access`.

export type FixtureTuple = {
	user: string;
	relation: string;
	object: string;
	condition?: { name: string; context: Record<string, unknown> };
	name?: string | null;
};

// OpenFGA holds a table and a namespace by the uuid the catalog minted (`table:<uuid>`), and the catalog's
// access doors resolve a name to it and answer each object's current name beside it, as the mock does here.
export const TABLE = 'table:6f0c2f3e-1b7a-4c41-9d3e-0a6f2b8c1d01';
export const OTHER_TABLE = 'table:6f0c2f3e-1b7a-4c41-9d3e-0a6f2b8c1d02';
const NAMESPACE = 'namespace:6f0c2f3e-1b7a-4c41-9d3e-0a6f2b8c1d03';
export const NAMES: Readonly<Record<string, string>> = {
	[TABLE]: 'db1$t',
	[OTHER_TABLE]: 'db1$u',
	[NAMESPACE]: 'db1',
};

/** The object a caller named, as the catalog holds it: `table:db1$t` is `TABLE`; anything else as written. */
export const held = (obj: string): string => {
	const type = obj.slice(0, obj.indexOf(':'));
	const id = obj.slice(obj.indexOf(':') + 1);
	return Object.keys(NAMES).find((o) => o.startsWith(`${type}:`) && NAMES[o] === id) ?? obj;
};

/** A tuple as the catalog answers it: its object held by uuid, with that object's name beside it. */
export const answered = (t: FixtureTuple): FixtureTuple => {
	const object = held(t.object);
	return { ...t, user: held(t.user), object, name: NAMES[object] ?? null };
};

export const TUPLES: FixtureTuple[] = [
	{ user: 'user:alice', relation: 'owner', object: TABLE },
	{ user: 'user:bob', relation: 'reader', object: TABLE },
	{ user: NAMESPACE, relation: 'parent', object: TABLE },
	// A TIME-BOXED grant. Present in the fixture so the canvas has a conditional edge to draw
	// differently — a permanent-only fixture would let the dashed-edge assertion pass vacuously.
	{
		user: 'user:contractor',
		relation: 'reader',
		object: TABLE,
		condition: {
			name: 'non_expired_grant',
			context: { grant_time: '2026-07-29T09:00:00Z', grant_duration: '4h' },
		},
	},
];

export const DSL = `model
  schema 1.1

type user

type namespace
  relations
    define parent: [warehouse, namespace]
    define owner: [user, role#assignee] or owner from parent
    define reader: [user, role#assignee] or owner

type table
  relations
    define parent: [namespace]
    define owner: [user, role#assignee] or owner from parent
    define reader: [user, role#assignee] or owner
    define can_read_data: reader
`;

export const MODEL_CONDITIONS = {
	non_expired_grant: {
		current_time: 'timestamp',
		grant_time: 'timestamp',
		grant_duration: 'duration',
	},
};

const leaf = (over: Record<string, unknown> = {}) => ({
	users: null,
	computed: null,
	tuple_to_userset: null,
	expanded: null,
	continues: null,
	...over,
});

const node = (name: string, over: Record<string, unknown> = {}) => ({
	name,
	leaf: null,
	union: null,
	intersection: null,
	difference: null,
	truncated: null,
	cycle: null,
	...over,
});

// The shape `fga.expand_tree` returns at depth>1: a `tuple_to_userset` leaf whose `expanded` child is
// the PARENT object's own tree. This is the thing a single Expand cannot give and a tuple table can
// never show — the spec exists to prove it reaches the screen.
export const EXPAND_TREE = node(`${TABLE}#can_read_data`, {
	union: [
		node('from-parent', {
			leaf: leaf({
				tuple_to_userset: { tupleset: `${TABLE}#parent`, computed: ['owner'] },
				expanded: [node(`${NAMESPACE}#owner`, { leaf: leaf({ users: ['user:alice'] }) })],
			}),
		}),
	],
});
