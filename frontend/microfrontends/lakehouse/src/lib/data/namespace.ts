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
});
export type NamespacePolicy = v.InferOutput<typeof PolicyResponseSchema>;

export const PolicyDeleteResponseSchema = v.object({
	status: v.string(),
	kind: v.string(),
	id: v.string(),
});
export type NamespacePolicyDelete = v.InferOutput<typeof PolicyDeleteResponseSchema>;
