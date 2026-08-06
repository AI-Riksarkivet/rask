// The FGA access surface's e2e fixtures, shared by `access.spec.ts` and `../mock-catalog.ts`. They
// lived inline in the spec while the surface read through page.route-able /capi routes; the
// remote-function migration moved every read/write SERVER-side, so the fixtures feed the mock catalog
// (which the zone server's CATALOG_API points at) and the spec asserts through `/__mock/access`
// instead of intercepting the browser.
//
// They came from the lakehouse's `access-fixtures.ts` with the workbench itself (#105). The STORES
// registry that used to share that file did NOT come along — it belongs to the object browser, which
// stayed, so it is `lakehouse/e2e/admin/store-fixtures.ts` now.

export type FixtureTuple = {
	user: string;
	relation: string;
	object: string;
	condition?: { name: string; context: Record<string, unknown> };
};

export const TUPLES: FixtureTuple[] = [
	{ user: 'user:alice', relation: 'owner', object: 'table:db1$t' },
	{ user: 'user:bob', relation: 'reader', object: 'table:db1$t' },
	{ user: 'namespace:db1', relation: 'parent', object: 'table:db1$t' },
	// A TIME-BOXED grant. Present in the fixture so the canvas has a conditional edge to draw
	// differently — a permanent-only fixture would let the dashed-edge assertion pass vacuously.
	{
		user: 'user:contractor',
		relation: 'reader',
		object: 'table:db1$t',
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
export const EXPAND_TREE = node('table:db1$t#can_read_data', {
	union: [
		node('from-parent', {
			leaf: leaf({
				tuple_to_userset: { tupleset: 'table:db1$t#parent', computed: ['owner'] },
				expanded: [node('namespace:db1#owner', { leaf: leaf({ users: ['user:alice'] }) })],
			}),
		}),
	],
});
