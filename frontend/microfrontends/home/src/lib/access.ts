import * as v from 'valibot';

// The FGA tuple wire CONTRACTS — the same shapes the lakehouse zone's `admin/access.ts` declares,
// because this zone writes through the SAME door (`POST /v1/access/tuples`) the FGA workbench does.
// Kept here rather than in `remote/access.remote.ts` because a `.remote.ts` may export only remote
// functions.

export const TupleConditionSchema = v.object({
	name: v.string(),
	context: v.record(v.string(), v.unknown()),
});

export const TupleSchema = v.object({
	user: v.string(), // e.g. user:alice, team:eng#member, namespace:db1
	relation: v.string(), // e.g. owner, can_read_data, parent
	object: v.string(), // e.g. project:acme, warehouse:acme-wh
	/** Present when the grant is TIME-BOXED. `null` is permanent — and the distinction has to survive
	 *  the read path, or an expiring grant renders as forever. */
	condition: v.optional(v.nullable(TupleConditionSchema), null),
});
export type Tuple = v.InferOutput<typeof TupleSchema>;
