import * as v from 'valibot';

/**
 * Strict boundary parse for @rask/api responses (parse-don't-validate, §7 of the
 * frontend canon). Untrusted upstream JSON is parsed ONCE here into a typed, trusted
 * value; downstream code then trusts the type. Documented nullability quirks
 * (`error: null`, `metadata: null`, …) are encoded in the schemas themselves — every
 * such field is a `v.nullable(...)` — NOT smuggled past validation with an `as` cast.
 *
 * On a real schema/nullability drift this THROWS; the per-app `query()` body lets that
 * propagate to the route's `svelte:boundary` `failed` snippet (§1/§3), so the UI renders
 * a real error instead of being built from values that lie about their type. A lenient
 * pass-through would defeat `noUncheckedIndexedAccess` and every downstream guarantee.
 */
export function parse<S extends v.GenericSchema>(schema: S, data: unknown): v.InferOutput<S> {
	return v.parse(schema, data);
}
