import * as v from 'valibot';
import { parse, type Me } from '@rask/api';

// The estate's PROJECT GALLERY, as ONE server load — mounted by both `/` (the landing) and
// `/projects` (the navbar-addressable surface). Two routes, one implementation: a second copy of the
// list is exactly the duplication the 2026-08-03 ruling deletes (a project is the TOP of the
// hierarchy — project › warehouse › namespace › table — so the estate's list of projects belongs to
// the main menu, never inside one project-scoped zone's catalog).

// The /v1/projects rows the estate-admin gallery reads (unknown keys stripped — only what the
// cards render is parsed).
const EstateProjectsSchema = v.array(
	v.object({
		project: v.string(),
		warehouses: v.array(v.object({ id: v.string() })),
	}),
);

/** One gallery card: the project, the caller's role in it (null = visible via estate-admin only),
 *  and the warehouse count when the estate listing supplied it. */
export type GalleryProject = {
	project: string;
	role: 'admin' | 'member' | null;
	warehouses: number | null;
};

/** What both routes hand `ProjectGallery.svelte`. */
export type GalleryData = {
	signedIn: boolean;
	/** A session exists but the catalog could not confirm WHO — not the same as signed out. */
	identityUnavailable: boolean;
	estateAdmin: boolean;
	/** The signed-in caller in FGA subject form (`user:<sub>`), prefilled into the initial-admin
	 *  field of the create dialog. Empty when there is no resolvable identity. */
	meSubject: string;
	projects: GalleryProject[];
};

/** The caller's identity in FGA subject form; an already-typed sub (`user:alice`) passes through. */
const subjectOf = (me: Me | null): string =>
	me === null ? '' : me.sub.includes(':') ? me.sub : `user:${me.sub}`;

/** The signed-in landing = the caller's project gallery. Memberships come straight from `me`
 *  (the frozen /v1/me contract, resolved in the layout); an estate admin instead sees ALL tenants
 *  via the /capi/v1/projects pass-through (estate-observer gated upstream), annotated with their
 *  own role where they hold one. Degrade, never 500: a failed estate listing falls back to the
 *  membership list. */
export async function loadGallery(event: {
	parent: () => Promise<{ me: Me | null; hasSession: boolean }>;
	fetch: typeof fetch;
}): Promise<GalleryData> {
	const { me, hasSession } = await event.parent();
	if (!me) {
		// A session WITHOUT a resolvable identity is not "signed out". Telling that user to sign in
		// sends them round a loop that cannot help — they already did, and the catalog is what failed.
		return {
			signedIn: false,
			identityUnavailable: hasSession,
			estateAdmin: false,
			meSubject: '',
			projects: [],
		};
	}

	const memberships: GalleryProject[] = me.projects.map((p) => ({
		project: p.project,
		role: p.role,
		warehouses: null,
	}));

	if (me.estate_admin) {
		try {
			const res = await event.fetch('/capi/v1/projects');
			if (res.ok) {
				const roleOf = new Map(me.projects.map((p) => [p.project, p.role]));
				const projects = parse(EstateProjectsSchema, await res.json()).map(
					(p): GalleryProject => ({
						project: p.project,
						role: roleOf.get(p.project) ?? null,
						warehouses: p.warehouses.length,
					}),
				);
				return {
					signedIn: true,
					identityUnavailable: false,
					estateAdmin: true,
					meSubject: subjectOf(me),
					projects,
				};
			}
		} catch {
			// fall through to the membership-derived gallery
		}
	}
	return {
		signedIn: true,
		identityUnavailable: false,
		estateAdmin: me.estate_admin,
		meSubject: subjectOf(me),
		projects: memberships,
	};
}
