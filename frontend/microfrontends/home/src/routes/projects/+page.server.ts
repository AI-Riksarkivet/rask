import { loadGallery } from '$lib/gallery';
import type { PageServerLoad } from './$types';

// `/projects` — the navbar-addressable project surface. The SAME load as `/`: one implementation,
// two mounts (2026-08-03 ruling — a project is the top of the hierarchy, so the estate's project
// list belongs to the main menu; a second copy of the listing is what this move deletes).
export const load: PageServerLoad = (event) => loadGallery(event);
