import * as v from 'valibot';
import { parse } from '@rask/api';
import type { PageServerLoad } from './$types';

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

// The signed-in landing = the caller's project gallery. Memberships come straight from `me`
// (the frozen /v1/me contract, resolved in the layout); an estate admin instead sees ALL tenants
// via the /capi/v1/projects pass-through (estate-observer gated upstream), annotated with their
// own role where they hold one. Degrade, never 500: a failed estate listing falls back to the
// membership list.
export const load: PageServerLoad = async ({ parent, fetch }) => {
	const { me } = await parent();
	if (!me) return { signedIn: false, estateAdmin: false, projects: [] as GalleryProject[] };

	const memberships: GalleryProject[] = me.projects.map((p) => ({
		project: p.project,
		role: p.role,
		warehouses: null,
	}));

	if (me.estate_admin) {
		try {
			const res = await fetch('/capi/v1/projects');
			if (res.ok) {
				const roleOf = new Map(me.projects.map((p) => [p.project, p.role]));
				const projects = parse(EstateProjectsSchema, await res.json()).map(
					(p): GalleryProject => ({
						project: p.project,
						role: roleOf.get(p.project) ?? null,
						warehouses: p.warehouses.length,
					}),
				);
				return { signedIn: true, estateAdmin: true, projects };
			}
		} catch {
			// fall through to the membership-derived gallery
		}
	}
	return { signedIn: true, estateAdmin: me.estate_admin, projects: memberships };
};
