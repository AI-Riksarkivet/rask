import { loadGallery } from '$lib/gallery';
import { readProjectsView } from '$lib/remote/view-prefs.remote';
import type { PageServerLoad } from './$types';

// `/projects` — the estate's project list, and the ONLY mount of the gallery since `/` became Home
// (the two-level ruling: Home · Projects · Settings at the estate root). The membership empty state
// lives here with it, because "you are not a member of any project yet" answers "where are my
// projects", not "what is this product".
//
// The saved VIEW is resolved HERE rather than on mount, so the first paint is already the view the
// caller chose. Reading it client-side would render the gallery and then swap to the table a beat
// later, on every single load. `readProjectsView` answers null both for "never chosen" and for every
// failure, so the default lives in exactly one place: the component's `initialView` default.
export const load: PageServerLoad = async (event) => {
	const [gallery, view] = await Promise.all([loadGallery(event), readProjectsView()]);
	return { ...gallery, initialView: view ?? undefined };
};
