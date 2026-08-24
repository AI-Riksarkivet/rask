/**
 * Quality-gate contracts — the wire shapes of the catalog's gate door.
 *
 * The gate decides whether a stage's output may publish. Until 38758a3f every one of these values
 * lived only as env on a mover Deployment, so moving a threshold meant a values-file edit and a
 * `helm upgrade` — an operation nobody could enumerate, review, or be gated on.
 *
 * NULL IS A STATE, NOT A MISSING VALUE. `describe` answers `null` when nothing is declared, and the
 * page must render that as "the chart's settings govern" rather than as zeros. Collapsing the two
 * would show a band of 0.00 for an estate that never opted in — a threshold that holds every
 * promotion, displayed as if someone had chosen it.
 */

import * as v from 'valibot';

/** One project's declared gate, as the catalog returns it. */
export const GateSpecSchema = v.object({
	project: v.string(),
	key_column: v.string(),
	required_columns: v.optional(v.array(v.string()), []),
	review_band: v.number(),
	review_enabled: v.boolean(),
});
export type GateSpec = v.InferOutput<typeof GateSpecSchema>;

/** `describe` — the record, or `null` when the chart still governs. */
export const GateDescribeSchema = v.nullable(GateSpecSchema);
export type GateDescribe = v.InferOutput<typeof GateDescribeSchema>;

/** The body of `gate/set`. `project` is deliberately absent: it comes from the gated PATH, so an
 *  admin of one tenant cannot rewrite another's gate while passing the check on their own. */
export const GateDraftSchema = v.object({
	key_column: v.pipe(v.string(), v.trim(), v.minLength(1, 'A gate needs a key column.')),
	required_columns: v.optional(v.array(v.string()), []),
	review_band: v.pipe(
		v.number('The band must be a number.'),
		v.minValue(0, 'A band is a magnitude — it cannot be negative.'),
	),
	review_enabled: v.optional(v.boolean(), false),
});
export type GateDraft = v.InferOutput<typeof GateDraftSchema>;

/** `{removed}` from `gate/delete`. */
export const GateRemovedSchema = v.object({ removed: v.boolean() });

/** Parse the comma-or-newline separated column list a form collects.
 *
 * Accepts either separator because a person pasting a column list has one of the two, and refusing
 * the other is a papercut with no upside. Blanks are dropped rather than kept as empty names. */
export function parseColumns(text: string): string[] {
	return text
		.split(/[\n,]/)
		.map((part) => part.trim())
		.filter((part) => part !== '');
}
