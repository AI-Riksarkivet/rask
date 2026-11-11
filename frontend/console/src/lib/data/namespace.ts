// The NAMESPACE-scoped catalog CONTRACTS (sweep group 3: the catalog's namespace_router has served
// access list/check/grant/revoke/graph + policy set/describe/delete since #50/#51/#72). The access
// contracts are kind-generalized ('table' | 'namespace') so one code path targets either object type
// — the table-scoped surfaces speak exactly the same wire shapes. Types come from the generated
// catalog spec (never hand-mirrored); every response is valibot-parsed at the wire boundary because
// the catalog serializes with response_model_exclude_none (absent-not-null fields), so a drift is
// surfaced instead of lying downstream.
//
// The transport lives in `remote/access-objects.api.ts` (the access surfaces) and
// `remote/namespace.api.ts` (lifecycle + policy); this module keeps the contracts they parse against.
import * as v from 'valibot';
import type { components } from '#lib/api/generated/catalog.ts';

export type AccessList = components['schemas']['AccessListResponse'];
export type AccessCheck = components['schemas']['AccessCheckResponse'];
export type AccessGrant = components['schemas']['AccessGrantResponse'];
export type AccessGraph = components['schemas']['AccessGraphResponse'];
export type MyPermissions = components['schemas']['MyPermissionsResponse'];
export type ManagedAccess = components['schemas']['ManagedAccessResponse'];
export type PolicyRequest = components['schemas']['PolicyRequest'];

/** The FGA object kinds the catalog's FULL access surface is mounted on — the owner-tier gate is
 * `can_drop` for a table, `can_delete` for a namespace (same bar, per-type relation).
 *
 * Deliberately NOT widened to warehouse/project: `access/list`, `check`, `grant`, `revoke` and
 * `graph` exist only for these two, so a wider type here would advertise routes that 404. */
export type AccessKind = 'table' | 'namespace';

/** The kinds the PERMISSION SELF-VIEW (`access/my-permissions`) is mounted on — a superset, because
 * warehouse and project gained one so the UI could render a disabled action with its reason instead
 * of a button that 403s on click (ruling #143). */
export type PermissionKind = AccessKind | 'warehouse' | 'project';

/** kind -> URL segment. NOT the identity function, and that is the whole reason this exists: the
 * catalog mounts the warehouse access router at `/management/v1/warehouse` (SINGULAR) and the project one at
 * `/management/v1/projects` (PLURAL, matching the projects module's own prefix). Deriving the path from the
 * kind directly yields `/management/v1/project/...`, which 404s — a failure that would read as "you have no
 * permissions here" rather than as a wrong URL, i.e. it would render every action disabled. */
export const PERMISSION_SEGMENT: Record<PermissionKind, string> = {
	table: 'table',
	namespace: 'namespace',
	warehouse: 'warehouse',
	project: 'projects',
};

// The access wire contracts (#51/#68/#72/#81). Mirrors of the generated response models, as schemas —
// the data functions parse against these, so the browser can never render an unparsed shape.
/** #51 the review: every relation the model defines on the type, with who holds it. */
export const AccessListSchema = v.object({
	object: v.string(),
	// `truncated` per relation PLUS a roll-up (diff2 F10 item 8). The server gained both when the
	// silent `listUsersMaxResults` ceiling was closed; this schema did not, so the browser dropped the
	// flag and rendered a partial review as a complete one — which is the whole defect that finding is
	// about, surviving on the half nobody checked. Defaulted rather than required so a pre-fix catalog
	// still parses.
	truncated: v.optional(v.boolean(), false),
	grants: v.array(
		v.object({
			relation: v.string(),
			users: v.array(v.string()),
			truncated: v.optional(v.boolean(), false),
		}),
	),
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
