// LEGACY PATH — the labeling-task detail moved to /tasks/[id] when the reader-facing vocabulary
// unified (the campaign is a "labeling task" everywhere a person reads; "project" stays the WIRE
// name the annotator service speaks). Old links and bookmarks land here; send them on.
import { redirect } from '@sveltejs/kit';
import { base } from '$app/paths';
import type { PageLoad } from './$types';

export const load: PageLoad = ({ params, url }) => {
	redirect(308, `${base}/tasks/${encodeURIComponent(params.id)}${url.search}`);
};
