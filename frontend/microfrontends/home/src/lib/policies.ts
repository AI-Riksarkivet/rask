// The maintenance-policy wire contracts + the form's draft→body builder (#65).
//
// A `.remote.ts` may export ONLY remote functions, so the schemas live here, beside `catalog.ts`.
// Types come from the generated OpenAPI client; the valibot schemas are the RUNTIME half — a wire
// shape that drifted from the contract must become an `{ok:false}` the page renders honestly, not a
// cast that renders a lie.
import * as v from 'valibot';
import type { components } from '@rask/api/generated/catalog';

/** `PolicyRequest` — the catalog's full set body, from the generated client. */
export type PolicyRequest = components['schemas']['PolicyRequest'];

/** The knobs THIS form owns. The per-tier tuning the catalog also accepts (`scan_batch_size`,
 *  `auto_cleanup_interval_commits`) is deliberately absent: both are sized against a specific tier's
 *  row shape — a bronze page-image row is ~1.8 MB and a feature row is not — so a tenant-wide value
 *  for either is a guess, and they belong on the table/namespace cards that know the data. Omitting
 *  them from the body is what leaves an existing value standing. */
type FormKnob =
	| 'compact_enabled'
	| 'cleanup_enabled'
	| 'optimize_indices_enabled'
	| 'compact_interval_hours'
	| 'retain_versions'
	| 'retention_days'
	| 'target_rows_per_fragment';

/** The wire body the form posts — keyed off the generated `PolicyRequest`, so a field the catalog
 *  renames is a type error here rather than a silently ignored knob, and with `null` EXCLUDED from
 *  every value type. That exclusion is the invariant, stated in the type: `put_policy` overwrites the
 *  whole record, so a null is an explicit erase and only an ABSENT key means inherit. */
export type PolicySetBody = { [K in FormKnob]?: Exclude<PolicyRequest[K], null> };

/** One stored policy record. The nullable fields are ABSENT rather than null on the wire (the routes
 *  serialize with `response_model_exclude_none`), so every one is `optional` AND `nullable`. */
export const PolicyResponseSchema = v.object({
	kind: v.string(),
	id: v.string(),
	path: v.optional(v.string(), ''),
	buckets: v.optional(v.nullable(v.array(v.string()))),
	retention_days: v.optional(v.nullable(v.number())),
	retain_versions: v.optional(v.nullable(v.number())),
	compact_enabled: v.optional(v.boolean(), true),
	compact_interval_hours: v.optional(v.nullable(v.number())),
	target_rows_per_fragment: v.optional(v.nullable(v.number())),
	cleanup_enabled: v.optional(v.boolean(), true),
	optimize_indices_enabled: v.optional(v.boolean(), true),
	scan_batch_size: v.optional(v.nullable(v.number())),
	auto_cleanup_interval_commits: v.optional(v.nullable(v.number())),
});

/** The project-scoped LIST. `incomplete`/`skipped_bindings` are not diagnostics: a namespace→warehouse
 *  binding the catalog could not read is a namespace whose policies it could not see, so a page that
 *  swallows them shows a short list as if it were the whole one. */
export const ProjectPoliciesSchema = v.object({
	project: v.string(),
	buckets: v.array(v.string()),
	namespaces: v.array(v.string()),
	policies: v.array(PolicyResponseSchema),
	incomplete: v.optional(v.boolean(), false),
	skipped_bindings: v.optional(v.array(v.string()), []),
});

export const PolicyDeleteSchema = v.object({
	status: v.string(),
	kind: v.string(),
	id: v.string(),
});

/** `PolicyResponse` as the page holds it — inferred from the parser, so the type is what actually
 *  survived validation rather than what the wire claimed (the lakehouse's policy cards do the same). */
export type ProjectPolicy = v.InferOutput<typeof PolicyResponseSchema>;
/** `ProjectPoliciesResponse` — every record governing data inside one project, plus the SCOPE it was
 *  computed from and whether that scope was complete. */
export type ProjectPolicies = v.InferOutput<typeof ProjectPoliciesSchema>;
export type ProjectPolicyDelete = v.InferOutput<typeof PolicyDeleteSchema>;

/** The maintenance toggles the project policy form edits, before they become a `PolicyRequest`. */
export interface PolicyDraft {
	retention_days: number | null;
	retain_versions: number | null;
	interval: number | null;
	target: number | null;
	enabled: boolean;
}

/**
 * One policy form's state → the request body.
 *
 * **An omitted knob means INHERIT, and that is enforced here rather than believed.** `put_policy`
 * overwrites the whole record, and the catalog's `_record` merge only keeps a field the caller did
 * not SET — so sending `null` for an untouched field is not a no-op, it writes an explicit nothing
 * over a value that was governing real data (`scan_batch_size` bounds compaction memory;
 * `auto_cleanup_interval_commits` owns version reclamation, and neither has a control on this form).
 * Absent, therefore, never null.
 *
 * The three step flags follow the form's single `enabled` toggle: one switch means "maintenance on
 * this project", not "compaction only". Hardcoding cleanup/optimize to `true` would satisfy the type
 * and be wrong in the dangerous direction — cleanup DELETES old versions, so it would run against a
 * tenant that had explicitly turned maintenance off.
 *
 * The lakehouse zone has the twin of this function for its table/namespace forms. It is duplicated
 * rather than shared because zones may not import each other and the only honest shared home would be
 * `@rask/api` — a move that has to change the lakehouse in the same commit, which this one does not.
 */
export function policyRequestFrom(draft: PolicyDraft): PolicySetBody {
	const body: PolicySetBody = {
		compact_enabled: draft.enabled,
		cleanup_enabled: draft.enabled,
		optimize_indices_enabled: draft.enabled,
	};
	if (draft.retention_days != null) body.retention_days = draft.retention_days;
	if (draft.retain_versions != null) body.retain_versions = draft.retain_versions;
	if (draft.interval != null) body.compact_interval_hours = draft.interval;
	if (draft.target != null) body.target_rows_per_fragment = draft.target;
	return body;
}
