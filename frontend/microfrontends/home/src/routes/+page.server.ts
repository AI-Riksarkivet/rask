import { loadGallery } from '$lib/gallery';
import type { PageServerLoad } from './$types';

// The landing IS the project gallery. The load itself lives in `$lib/gallery` because `/projects`
// mounts the same surface (2026-08-03 ruling — a project is the top of the hierarchy, so the
// estate's project list is a main-menu page); two copies of this listing is the duplication the
// ruling exists to delete.
export const load: PageServerLoad = (event) => loadGallery(event);
