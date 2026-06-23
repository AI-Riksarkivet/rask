import * as v from 'valibot';

/**
 * Best-effort boundary parse for @rask/api responses. The backend is rask's OWN
 * (trusted) services, but its JSON has nullability quirks the TS types don't capture
 * (e.g. `error: null`, `metadata: null`) — a strict `v.parse` THROWS and blanks the UI.
 * So: validate, and on a mismatch WARN (drift stays visible in dev) but pass the raw
 * data through rather than break. Happy-path responses are still fully validated.
 */
export function parse<S extends v.GenericSchema>(schema: S, data: unknown): v.InferOutput<S> {
	const result = v.safeParse(schema, data);
	if (result.success) return result.output;
	if (typeof console !== 'undefined') {
		console.warn('[@rask/api] response did not match its schema; passing through unvalidated', v.flatten(result.issues));
	}
	return data as v.InferOutput<S>;
}
