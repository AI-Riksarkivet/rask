import type { Me } from '#lib/api/index.ts';
import type { ApiResult } from '#lib/api/client.ts';
import { isEstateAdmin } from '#lib/estate-admin.ts';
import type { VisibleProject } from '#lib/remote/warehouses.api.ts';

// The estate's PROJECT GALLERY, mounted by `/projects` (the navbar-addressable surface). A project is
// the TOP of the hierarchy (project › warehouse › namespace › table), so the estate's list of projects
// belongs to the main menu, never inside one project-scoped page's catalog.

/** One gallery card: the project, the caller's role in it (null = visible via estate-admin only),
 *  and the warehouse count when the estate listing supplied it. */
export type GalleryProject = {
	project: string;
	role: 'admin' | 'member' | null;
	warehouses: number | null;
};

/** What `ProjectGallery.svelte` renders. */
export type GalleryData = {
	signedIn: boolean;
	/** A session exists but the catalog could not confirm WHO — not the same as signed out. */
	identityUnavailable: boolean;
	estateAdmin: boolean;
	/** The signed-in caller's principal key (`<idp-id>~<user id>`), prefilled into the initial-admin
	 *  field of the create dialog. Empty when there is no resolvable identity. */
	meSubject: string;
	projects: GalleryProject[];
};

/**
 * The caller's project gallery. Memberships come straight from `me` (the frozen /management/v1/me
 * contract, resolved in the layout); an estate admin also gets the estate listing (`listing`, the
 * answer of `fetchVisibleProjects`, the read the project filter makes), annotated with their own role where they hold one. Degrade, never
 * fail: a listing that failed falls back to the membership list.
 *
 * MERGED, never replaced. The listing answering `200 []` is not the same claim as "you belong to
 * nothing": the catalog derives its project registry from registered warehouses, so a project that
 * exists in authz but has no warehouse yet is absent from it. You are never shown FEWER projects than
 * you belong to: the listing adds the rest of the estate (and the warehouse counts only it knows), and
 * memberships fill anything it has not caught up with.
 */
export function galleryOf(
	me: Me | null,
	hasSession: boolean,
	listing: ApiResult<VisibleProject[]> | undefined,
): GalleryData {
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
	const base = {
		signedIn: true,
		identityUnavailable: false,
		estateAdmin: isEstateAdmin(me),
		meSubject: me.sub,
	};
	if (!isEstateAdmin(me) || !listing?.ok) return { ...base, projects: memberships };
	const roleOf = new Map(me.projects.map((p) => [p.project, p.role]));
	const listed = listing.data.map((p): GalleryProject => ({
		project: p.project,
		role: roleOf.get(p.project) ?? null,
		warehouses: p.warehouses.length,
	}));
	const seen = new Set(listed.map((p) => p.project));
	return { ...base, projects: [...listed, ...memberships.filter((m) => !seen.has(m.project))] };
}
