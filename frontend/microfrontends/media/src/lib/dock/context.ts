/**
 * The context key the media workbench threads to its panels.
 *
 * Kept in a plain `.ts` module rather than beside the class in `workbench.svelte.ts` so a panel can
 * import the getter without pulling the rune-compiled module into its own graph.
 */
import { getContext } from 'svelte';
import type { MediaWorkbench } from '$lib/dock/workbench.svelte';

export const MEDIA_WORKBENCH = Symbol('rask.media-workbench');

export function getMediaWorkbench(): MediaWorkbench {
	const workbench = getContext<MediaWorkbench | undefined>(MEDIA_WORKBENCH);
	if (workbench === undefined) {
		throw new Error(
			'media panel mounted without a MediaWorkbench — pass it in the Dock `context` map under MEDIA_WORKBENCH',
		);
	}
	return workbench;
}
