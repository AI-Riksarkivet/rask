import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import {
	AccessCheckSchema,
	AccessGrantSchema,
	AccessGraphSchema,
	AccessListSchema,
	ManagedAccessSchema,
	MyPermissionsSchema,
	type AccessCheck,
	type AccessGrant,
	type AccessGraph,
	type AccessKind,
	type AccessList,
	type ManagedAccess,
	type MyPermissions,
} from '../namespace';

import { catalogJSON, parsed } from '$lib/server/doors';
// Transport: the zone's ONE catalog door (#93) — implementation in @rask/api/upstream.
// PER-OBJECT access (a table or a namespace), in the zone's remote-function dialect
// (the transport ruling, area 1) — same names, same ApiResult shapes as the /capi client this replaces,
// transport only. This is the OBJECT-scoped surface the catalog mounts under
// `/v1/{table|namespace}/{id}/access/*`; the estate-wide FGA workbench (`lib/admin/access*`) is a
// different surface with its own module.
//
// Kind-generalized exactly as the /capi client already was: one code path targets either FGA object
// type, and the owner-tier gate is the per-type relation (can_drop for a table, can_delete for a
// namespace), enforced by the CATALOG against the signed-in user's own bearer — the confused-deputy
// stance the deleted routes carried, kept intact (no service credential is ever forwarded).
// `fetchAccessGraph` absorbs the former table-only `fetchAccessGraph` and `fetchNamespaceAccessGraph`,
// which were the same request with a different path segment.
//
// A remote file may export only remote functions, so the wire contracts stay in `../namespace`.

const enc = encodeURIComponent;

/** The client module's `AccessKind` as a wire option list — `satisfies` keeps the two from drifting. */
const KINDS = ['table', 'namespace'] as const satisfies readonly AccessKind[];

/** The FGA object an access call targets — `kind` picks the catalog surface, never the gate. */
const TargetSchema = v.object({
	kind: v.picklist(KINDS),
	id: v.string(),
});

/** A target plus the (subject, rung) pair a check/grant/revoke carries in its body. */
const SubjectSchema = v.object({
	kind: v.picklist(KINDS),
	id: v.string(),
	user: v.string(),
	relation: v.string(),
});

/** Is granting on this container centralized? Reader-tier, so the page can EXPLAIN a missing control
 *  to the person it is missing for. Named `describe` to match the catalog's suffix-per-operation shape
 *  (`policy/describe` / `policy/set`) — the authz suffix map is keyed on the path alone, so a read and
 *  a write sharing one suffix could only ever carry one tier, and it would be the write's. */
export const fetchManagedAccess = query(
	TargetSchema,
	async ({ kind, id }): Promise<ApiResult<ManagedAccess>> =>
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/managed-access/describe`, { method: 'POST' }),
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
	TargetSchema,
	async ({ kind, id }): Promise<ApiResult<MyPermissions>> =>
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/my-permissions`, { method: 'POST' }),
			MyPermissionsSchema,
		),
);

/** #51 access review, kind-generalized: who holds which can_* action on the object. Owner-gated by
 *  the catalog (can_drop / can_delete). POST because the catalog's lance-namespace-style surface is
 *  POST-shaped; the operation itself is a read, so it is a query here. */
export const fetchAccess = query(
	TargetSchema,
	async ({ kind, id }): Promise<ApiResult<AccessList>> =>
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/list`, { method: 'POST' }),
			AccessListSchema,
		),
);

/** #68 "who can do what" simulator — a live OpenFGA Check: does `user` hold `relation` on this
 *  object? Owner-gated by the catalog, the same bar as the review (probing the graph == disclosing it). */
export const checkAccess = query(
	SubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessCheck>> =>
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/check`, {
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
		parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/graph`, { method: 'POST' }),
			AccessGraphSchema,
		),
);

/** #72 grant a base rung (owner/writer/reader/validator) to a subject on the object. `user` may be a
 *  bare id (`alice`) or a userset (`role:…#assignee` / `team:…#member`). On success the object's
 *  active review + graph refresh in the SAME flight, so a surface that issued a grant never renders
 *  an ACL it just changed. */
export const grantAccess = command(
	SubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessGrant>> => {
		const result = parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/grant`, {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessGrantSchema,
		);
		if (result.ok) {
			void fetchAccess({ kind, id }).refresh();
			void fetchAccessGraph({ kind, id }).refresh();
		}
		return result;
	},
);

/** #72 revoke a base rung from a subject on the object — the write counterpart of the grant, same
 *  gate, same single-flight refresh. */
export const revokeAccess = command(
	SubjectSchema,
	async ({ kind, id, user, relation }): Promise<ApiResult<AccessGrant>> => {
		const result = parsed(
			await catalogJSON(`/v1/${kind}/${enc(id)}/access/revoke`, {
				method: 'POST',
				body: JSON.stringify({ user, relation }),
			}),
			AccessGrantSchema,
		);
		if (result.ok) {
			void fetchAccess({ kind, id }).refresh();
			void fetchAccessGraph({ kind, id }).refresh();
		}
		return result;
	},
);
