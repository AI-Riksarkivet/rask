// The NAMESPACE-scoped catalog CONTRACTS (sweep group 3: the catalog's namespace_router has served
// access list/check/grant/revoke/graph + policy set/describe/delete since #50/#51/#72). The access
// contracts are kind-generalized ('table' | 'namespace') so one code path targets either object type
// — the table-scoped surfaces speak exactly the same wire shapes. Types come from the generated
// catalog spec (never hand-mirrored); every response is valibot-parsed at the wire boundary because
// the catalog serializes with response_model_exclude_none (absent-not-null fields), so a drift is
// surfaced instead of lying downstream.
//
// The TRANSPORT moved to remote functions (the transport ruling, area 1): the access surfaces live in
// `remote/access-objects.remote.ts`, the lifecycle + policy ones in `remote/namespace.remote.ts`.
// This module keeps the contracts they parse against — a `.remote.ts` may export only remote
// functions.
import * as v from 'valibot';
import type { components } from '@rask/api/generated/catalog';

export type AccessList = components['schemas']['AccessListResponse'];
export type AccessCheck = components['schemas']['AccessCheckResponse'];
export type AccessGrant = components['schemas']['AccessGrantResponse'];
export type AccessGraph = components['schemas']['AccessGraphResponse'];
export type MyPermissions = components['schemas']['MyPermissionsResponse'];
export type ManagedAccess = components['schemas']['ManagedAccessResponse'];
export type PolicyRequest = components['schemas']['PolicyRequest'];

/** The FGA object kinds the catalog's access surface is mounted on — the owner-tier gate is
 * `can_drop` for a table, `can_delete` for a namespace (same bar, per-type relation). */
export type AccessKind = 'table' | 'namespace';

// The access wire contracts (#51/#68/#72/#81). Mirrors of the generated response models, as schemas —
// the remote functions parse against these, so the browser can never render an unparsed shape.
/** #51 the review: every relation the model defines on the type, with who holds it. */
export const AccessListSchema = v.object({
	object: v.string(),
	grants: v.array(v.object({ relation: v.string(), users: v.array(v.string()) })),
});
/** #68 the check verdict — `user` echoes the RESOLVED subject (bare id → `user:<id>`). */
export const AccessCheckSchema = v.object({
	allowed: v.boolean(),
	object: v.string(),
	relation: v.string(),
	user: v.string(),
});
/** The SELF-view: every `can_*` the model defines on the object, answered for the CALLER alone.
 *  Distinct from the review above and deliberately so — that one enumerates principals and clears the
 *  owner bar, this one describes only the person asking and is gated at the reader tier, so the page
 *  can decide what to RENDER before the user clicks something they were never allowed to do.
 *  `permissions` is an open record because its keys are the model's `can_*` set: pinning them here
 *  would be a fourth copy of the model that drifts the day a relation is added. */
export const MyPermissionsSchema = v.object({
	object: v.string(),
	subject: v.string(),
	permissions: v.record(v.string(), v.boolean()),
});
/** Whether granting on this container is CENTRALIZED — read at the reader tier on purpose, because
 *  this flag is the reason a page's grant controls are absent and the person who needs that
 *  explanation is exactly the one who may not change it. */
export const ManagedAccessSchema = v.object({
	object: v.string(),
	managed_access: v.boolean(),
});
/** #72 the grant/revoke acknowledgement — `granted` states which way it went. */
export const AccessGrantSchema = v.object({
	granted: v.boolean(),
	object: v.string(),
	relation: v.string(),
	user: v.string(),
});
/** #81 one hop of the authorization graph: the focus object, its grantees, its container edge. */
export const AccessGraphSchema = v.object({
	object: v.string(),
	nodes: v.array(v.object({ id: v.string(), label: v.string(), type: v.string() })),
	edges: v.array(v.object({ relation: v.string(), source: v.string(), target: v.string() })),
});

// The PolicyResponse wire contract (#50) — serialized with response_model_exclude_none, so the
// nullable bounds arrive absent, not null. Parsed (not cast) at the boundary per the @rask/api
// parse-don't-validate rule: a schema drift is surfaced instead of lying downstream.
export const PolicyResponseSchema = v.object({
	kind: v.string(),
	id: v.string(),
	path: v.string(),
	compact_enabled: v.optional(v.boolean(), true),
	compact_interval_hours: v.optional(v.nullable(v.number())),
	retain_versions: v.optional(v.nullable(v.number())),
	retention_days: v.optional(v.nullable(v.number())),
	target_rows_per_fragment: v.optional(v.nullable(v.number())),
	// Mirrors PolicyResponse. They were absent here AND on the response model, so a form save wrote
	// the model's defaults over them; the catalog's set door now MERGES unset fields, and parsing
	// them means the UI can show what is actually in force rather than a shape it invented.
	cleanup_enabled: v.optional(v.boolean(), true),
	optimize_indices_enabled: v.optional(v.boolean(), true),
	scan_batch_size: v.optional(v.nullable(v.number())),
	auto_cleanup_interval_commits: v.optional(v.nullable(v.number())),
});
export type NamespacePolicy = v.InferOutput<typeof PolicyResponseSchema>;

export const PolicyDeleteResponseSchema = v.object({
	status: v.string(),
	kind: v.string(),
	id: v.string(),
});
export type NamespacePolicyDelete = v.InferOutput<typeof PolicyDeleteResponseSchema>;

/** The maintenance toggles a policy form edits, before it becomes a `PolicyRequest`. */
export interface PolicyDraft {
	retention_days: number | null;
	retain_versions: number | null;
	interval: number | null;
	target: number | null;
	enabled: boolean;
}

/**
 * One policy form's state → the request body, for BOTH the table and the namespace form.
 *
 * Shared rather than written twice. It was written twice, identically, and when the catalog widened
 * `PolicyRequest` with per-step flags (`cleanup_enabled`, `optimize_indices_enabled` — a policy can
 * now skip a STEP) both copies broke in exactly the same way. One builder means the next contract
 * change is one edit and one test.
 *
 * The three step flags all follow the form's single `enabled` toggle, which is what preserves the
 * behaviour the UI already promises: one switch means "maintenance on this object", not "compaction
 * only". Hardcoding the two new flags to `true` would have satisfied the type and been WRONG in the
 * dangerous direction — cleanup deletes old versions, so it would run on objects whose owner had
 * explicitly turned maintenance off.
 */
export function policyRequestFrom(draft: PolicyDraft): PolicyRequest {
	const body: PolicyRequest = {
		compact_enabled: draft.enabled,
		cleanup_enabled: draft.enabled,
		optimize_indices_enabled: draft.enabled,
	};
	// Absent, not null: an omitted knob means "inherit", and sending null would overwrite an
	// inherited value with an explicit nothing.
	if (draft.retention_days != null) body.retention_days = draft.retention_days;
	if (draft.retain_versions != null) body.retain_versions = draft.retain_versions;
	if (draft.interval != null) body.compact_interval_hours = draft.interval;
	if (draft.target != null) body.target_rows_per_fragment = draft.target;
	return body;
}
