import { command, query } from '#lib/reads.svelte.ts';
import * as v from 'valibot';
import type { ApiResult } from '#lib/api/client.ts';
import {
	AccessCheckSchema,
	AccessGrantSchema,
	AccessGraphSchema,
	AccessListSchema,
	ManagedAccessSchema,
	MyPermissionsSchema,
	ACCESS_SEGMENT,
	hasAccessCheck,
	type AccessCheck,
	type AccessGrant,
	type AccessGraph,
	type AccessKind,
	type AccessList,
	type GrantKind,
	type ManagedAccess,
	type MyPermissions,
} from '../namespace';

import { catalogJSON, parsed } from '#lib/doors.ts';
// Transport: the console's catalog door, `#lib/doors`.
// PER-OBJECT access (a table, a namespace, a warehouse or a project), declared with `query`/`command`
// and answering the shared `ApiResult` union. This is the OBJECT-scoped surface the catalog mounts under
// `/management/v1/{table|namespace|warehouse|projects}/{id}/access/*`; the estate-wide FGA workbench
// (`lib/admin/access*`) is a different surface with its own module.
//
// Kind-generalized: list, grant, revoke and the self-view exist for all four kinds; check, graph and
// managed access only where `docs/catalog-openapi.json` mounts them. The gates are the CATALOG's,
// enforced against the signed-in user's own bearer (no service credential is ever sent).
//
// The wire contracts live in `../namespace`, beside the types the components import.

const enc = encodeURIComponent;

/** The kinds with `access/check` and `access/graph` — `satisfies` keeps the list and the type together. */
const KINDS = ['table', 'namespace'] as const satisfies readonly AccessKind[];

/** The kinds with `access/{list,grant,revoke,my-permissions}`. Its own picklist, so widening these
 *  cannot widen `check` or `graph` into routes that do not exist. */
const GRANT_KINDS = [
	'table',
	'namespace',
	'warehouse',
	'project',
] as const satisfies readonly GrantKind[];

/** The FGA object an access call targets — `kind` picks the catalog surface, never the gate. */
const TargetSchema = v.object({
	kind: v.picklist(KINDS),
	id: v.string(),
});

const GrantTargetSchema = v.object({
	kind: v.picklist(GRANT_KINDS),
	id: v.string(),
});

/** A target plus the (subject, rung) pair a check carries in its body. */
const SubjectSchema = v.object({
	kind: v.picklist(KINDS),
	id: v.string(),
	user: v.string(),
	relation: v.string(),
});

/** The (subject, rung) pair a grant or revoke carries, on any kind that has those doors. */
const GrantSubjectSchema = v.object({
	kind: v.picklist(GRANT_KINDS),
	id: v.string(),
	user: v.string(),
	relation: v.string(),
});

/** The door `/management/v1/<segment>/<id>/access/<door>` for `kind`. */
const accessPath = (kind: GrantKind, id: string, door: string): string =>
	`/management/v1/${ACCESS_SEGMENT[kind]}/${enc(id)}/access/${door}`;

/** Is granting on this container centralized? Reader-gated, so the page can EXPLAIN a missing control
 *  to the person it is missing for. Named `describe` to match the catalog's suffix-per-operation shape
 *  (`policy/describe` / `policy/set`) — the authz suffix map is keyed on the path alone, so a read and
 *  a write sharing one suffix could only ever carry one gate, and it would be the write's. */
export const fetchManagedAccess = query(
	TargetSchema,
	async ({ kind, id }): Promise<ApiResult<ManagedAccess>> =>
		parsed(
			await catalogJSON(`/management/v1/${kind}/${enc(id)}/managed-access/describe`, {
				method: 'POST',
			}),
			ManagedAccessSchema,
		),
);

/** The SELF-view: what the SIGNED-IN caller may do on this object. Reader-gated by the catalog
 *  (`can_get_metadata`), not owner-gated like `fetchAccess` below — describing the caller to
 *  themselves discloses no principals.
 *
 *  This is what a page renders FROM. Before it existed the only honest options were to show every
 *  action and let the click 403, or to hide actions behind a rung the page had to guess; the first is
 *  what the lakehouse did, and it only stayed invisible because a user without access never reached
 *  the page at all. Upward visibility changed that — a single-table grantee can now navigate to the
 *  namespace above their table — so the page has to be able to ask. */
export const fetchMyPermissions = query(
	GrantTargetSchema,
	async ({ kind, id }): Promise<ApiResult<MyPermissions>> =>
		parsed(
			await catalogJSON(accessPath(kind, id, 'my-permissions'), { method: 'POST' }),
			MyPermissionsSchema,
		),
);

/** #51 access review, kind-generalized: who holds which can_* action on the object. Gated by the
 *  catalog on the kind's review rung (`can_review_access` on a table or namespace, `can_read_assignments`
 *  on a warehouse or project). POST because the catalog's lance-namespace-style surface is POST-shaped;
 *  the operation itself is a read, so it is a query here. */
export const fetchAccess = query(
	GrantTargetSchema,
	async ({ kind, id }): Promise<ApiResult<AccessList>> =>
		parsed(await catalogJSON(accessPath(kind, id, 'list'), { method: 'POST' }), AccessListSchema),
);

/** #68 "who can do what" simulator — a live OpenFGA Check: does `user` hold `relation` on this
 *  object? Owner-gated by the catalog, the same bar as the review (probing the graph == disclosing it). */
export const checkAccess = query(
	SubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessCheck>> =>
		parsed(
			await catalogJSON(accessPath(kind, id, 'check'), {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessCheckSchema,
		),
);

/** #81 one hop of the authorization graph around the object — its direct grantees + the parent edge.
 *  Owner-gated by the catalog; the SvelteFlow explorer and the namespace page's list card read it. */
export const fetchAccessGraph = query(
	TargetSchema,
	async ({ kind, id }): Promise<ApiResult<AccessGraph>> =>
		parsed(await catalogJSON(accessPath(kind, id, 'graph'), { method: 'POST' }), AccessGraphSchema),
);

/** #72 grant a base rung (owner/writer/reader) to a subject on the object. `user` may be a
 *  bare id (`alice`) or a userset (`role:…#assignee`). On success the object's
 *  active review + graph refresh in the SAME flight, so a surface that issued a grant never renders
 *  an ACL it just changed. */
export const grantAccess = command(
	({ kind, id, user, relation }) => `Grant ${relation} on ${kind} ${id} to ${user}`,
	GrantSubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessGrant>> => {
		const result = parsed(
			await catalogJSON(accessPath(kind, id, 'grant'), {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessGrantSchema,
		);
		if (result.ok) {
			void fetchAccess({ kind, id }).refresh();
			if (hasAccessCheck(kind)) void fetchAccessGraph({ kind, id }).refresh();
		}
		return result;
	},
);

/** #72 revoke a base rung from a subject on the object — the write counterpart of the grant, same
 *  gate, same single-flight refresh. */
export const revokeAccess = command(
	({ kind, id, user, relation }) => `Revoke ${relation} on ${kind} ${id} from ${user}`,
	GrantSubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessGrant>> => {
		const result = parsed(
			await catalogJSON(accessPath(kind, id, 'revoke'), {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessGrantSchema,
		);
		if (result.ok) {
			void fetchAccess({ kind, id }).refresh();
			if (hasAccessCheck(kind)) void fetchAccessGraph({ kind, id }).refresh();
		}
		return result;
	},
);
