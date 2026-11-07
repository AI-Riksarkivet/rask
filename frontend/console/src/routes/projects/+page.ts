import { loadGallery } from '#lib/gallery.ts';
import type { PageLoad } from './$types';

// `/projects` — the estate's project list.
export const load: PageLoad = (event) => loadGallery(event);
