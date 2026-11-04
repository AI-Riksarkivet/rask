import { loadGallery } from '$lib/gallery';
import { readProjectsView } from '$lib/remote/view-prefs.api';
import type { PageLoad } from './$types';

// `/projects` — the estate's project list. The saved VIEW (gallery or table) is resolved in the load
// so the first paint is already the view the caller chose; `readProjectsView` answers null both for
// "never chosen" and for every failure, so the default lives in the component's `initialView`.
export const load: PageLoad = async (event) => {
	const [gallery, view] = await Promise.all([loadGallery(event), readProjectsView()]);
	return { ...gallery, initialView: view ?? undefined };
};
