/**
 * Transform contracts — the wire shapes of the catalog's transform-declaration door.
 *
 * A TRANSFORM IS NOT A RAY JOB, and this module is where that distinction is kept honest. The record the
 * catalog stores is a `TransformSpec`: one governed medallion edge — read `from_id`, run
 * `entrypoint`, write `to_id`. Ray is one of two ways to execute it (`MEDALLION_RAY_ENABLED`
 * defaults to FALSE and the in-process path is the default), so naming any of this after Ray would
 * tie a platform record to one execution engine — the coupling the agnostic ruling forbids.
 *
 * Declared here rather than imported from the generated catalog client on the estate's usual
 * grounds: a hand-written valibot schema is the boundary CHECK, and a generated type is only a
 * claim about what the server said it would send.
 */

import * as v from 'valibot';

/** One declared transform, as the catalog returns it.
 *
 * `params` is OPAQUE to the platform by design — the medallion forwards each key into the job's
 * `runtime_env.env_vars` under a `RASK_PARAM_` prefix, and never reads a value. The prefix is what
 * keeps this a workload channel rather than a hole in the platform: a transform cannot reach `S3_SECRET`
 * or an `OTEL_*` key by choosing a colliding name, because every key it supplies is rewritten
 * before it is sent. NEVER A SECRET — these values live in a governed record, readable by anyone
 * who can read the transform; a workload needing a credential resolves it from the Dapr secret store.
 */
export const TransformSpecSchema = v.object({
	name: v.string(),
	project: v.string(),
	from_id: v.string(),
	to_id: v.string(),
	entrypoint: v.string(),
	params: v.optional(v.record(v.string(), v.string()), {}),
	code_version: v.optional(v.string(), ''),
});
export type TransformSpec = v.InferOutput<typeof TransformSpecSchema>;

/** `GET /v1/projects/{id}/transforms`. The key is absent, not `[]`, on an estate that has declared
 *  nothing — `response_model_exclude_none=True` — so the fallback is load-bearing. */
export const TransformListSchema = v.object({
	transforms: v.optional(v.array(TransformSpecSchema), []),
});
export type TransformList = v.InferOutput<typeof TransformListSchema>;

/** The body of `transform/set`. `project` is deliberately NOT here: it comes from the gated PATH.
 *  A body-supplied project would let an admin of one tenant pass the `can_administer` gate on their
 *  own project while writing a transform into somebody else's. */
export const TransformDraftSchema = v.object({
	name: v.pipe(v.string(), v.trim(), v.minLength(1, 'A transform needs a name.')),
	from_id: v.pipe(v.string(), v.trim(), v.minLength(1, 'A transform reads from a table.')),
	to_id: v.pipe(v.string(), v.trim(), v.minLength(1, 'A transform writes to a table.')),
	entrypoint: v.pipe(v.string(), v.trim(), v.minLength(1, 'A transform runs an entrypoint.')),
	params: v.optional(v.record(v.string(), v.string()), {}),
	code_version: v.optional(v.string(), ''),
});
export type TransformDraft = v.InferOutput<typeof TransformDraftSchema>;

/** Parse the `KEY=value` lines a form collects into the wire's `params` record.
 *
 * Split on the FIRST `=` only: a value may legitimately contain one (a URI, a query string), and
 * splitting on every occurrence silently truncated it. Blank lines and `#` comments are skipped so
 * a paste-in block stays editable.
 */
export function parseParams(text: string): { params: Record<string, string>; bad: string[] } {
	const params: Record<string, string> = {};
	const bad: string[] = [];
	for (const raw of text.split('\n')) {
		const line = raw.trim();
		if (line === '' || line.startsWith('#')) continue;
		const eq = line.indexOf('=');
		if (eq <= 0) {
			bad.push(line);
			continue;
		}
		params[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
	}
	return { params, bad };
}

/** The inverse, for rendering an existing transform back into an editable block. */
export function formatParams(params: Record<string, string>): string {
	return Object.entries(params)
		.map(([k, val]) => `${k}=${val}`)
		.join('\n');
}
