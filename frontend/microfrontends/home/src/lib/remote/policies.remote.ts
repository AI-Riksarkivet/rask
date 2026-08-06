import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	PolicyDeleteSchema,
	PolicyResponseSchema,
	ProjectPoliciesSchema,
	type ProjectPolicies,
	type ProjectPolicy,
	type ProjectPolicyDelete,
} from '$lib/policies';
import { catalogJSON, parsed } from '$lib/server/catalog-fetch';

// The project's MAINTENANCE plane (#65): the tenant-scoped view of every compaction/retention policy
// governing its data, plus the set/delete pair for the project's own record.
//
// All four rungs of the read are one call. `GET /v1/projects/{id}/policies` answers the project record
// AND every table/namespace record inside the tenant, so the page never needs a `policy/describe` per
// object — which it could not have issued anyway, since it would have to already know each id.
//
// Everything here is gated at the catalog on `project:<id>#can_administer` — the same bar as
// describe/set/delete. The remote function forwards the signed-in session's bearer and nothing else,
// so the gate is decided against a REAL user, and a 401/403 is a state the page renders rather than
// an affordance guessed at from an identity the page does not hold.

const enc = encodeURIComponent;

/** What a caller may set. Every bound is optional and an omitted knob means INHERIT — `put_policy`
 *  overwrites the whole record and the catalog only carries forward what the caller did not SET, so a
 *  `null` here is an explicit erase, never a no-op. The form omits, it does not null (`policyRequestFrom`). */
const PolicyDraftSchema = v.object({
	compact_enabled: v.optional(v.boolean(), true),
	cleanup_enabled: v.optional(v.boolean(), true),
	optimize_indices_enabled: v.optional(v.boolean(), true),
	compact_interval_hours: v.optional(v.number()),
	retain_versions: v.optional(v.number()),
	retention_days: v.optional(v.number()),
	target_rows_per_fragment: v.optional(v.number()),
});

/** Every maintenance policy governing data inside this project, with the scope it was derived from.
 *
 *  `incomplete`/`skipped_bindings` cross the boundary deliberately: a namespace→warehouse binding the
 *  catalog could not read is a namespace whose policies it could not see, and a list that quietly
 *  narrowed would read as checked-and-clean. The page renders that as a banner, never swallows it. */
export const fetchProjectPolicies = query(
	v.string(),
	async (project): Promise<ApiResult<ProjectPolicies>> =>
		parsed(await catalogJSON(`/v1/projects/${enc(project)}/policies`), ProjectPoliciesSchema),
);

/** Set (or replace) the PROJECT-level policy — the tenant-wide fallback the sweep uses when no table
 *  or namespace record matches. Admin-gated; the catalog resolves the covered buckets from the
 *  warehouse registry at set time and refuses a project with no active warehouse (400) or a bucket a
 *  rival tenant's warehouse also claims (409). Both are rendered from the response detail.
 *
 *  Single-flight: the listing this write changes is refreshed in the same flight. */
export const setProjectPolicy = command(
	v.object({ project: v.string(), policy: PolicyDraftSchema }),
	async ({ project, policy }): Promise<ApiResult<ProjectPolicy>> => {
		const result = parsed(
			await catalogJSON(`/v1/project/${enc(project)}/policy/set`, {
				method: 'POST',
				body: JSON.stringify(policy),
			}),
			PolicyResponseSchema,
		);
		if (result.ok) void fetchProjectPolicies(project).refresh();
		return result;
	},
);

/** Remove the project's own policy (idempotent) — the tenant falls back to the sweep's global
 *  defaults. Table and namespace records are untouched: they shadow this one, so removing it changes
 *  nothing for any dataset one of them already governs. Same single-flight refresh. */
export const deleteProjectPolicy = command(
	v.string(),
	async (project): Promise<ApiResult<ProjectPolicyDelete>> => {
		const result = parsed(
			await catalogJSON(`/v1/project/${enc(project)}/policy/delete`, { method: 'POST' }),
			PolicyDeleteSchema,
		);
		if (result.ok) void fetchProjectPolicies(project).refresh();
		return result;
	},
);
