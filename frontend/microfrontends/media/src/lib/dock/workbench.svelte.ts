/**
 * Shared state for the media workbench — loaded once, read by every panel.
 *
 * Panels are mounted imperatively by `@rask/dockview`, so they do NOT inherit the workbench page's
 * context tree; this is threaded through the dock's `context` prop and reached with
 * {@link getMediaWorkbench}. Two things follow from that, and both are the point of docking here:
 *
 *   * The topic hierarchy is fetched ONCE for the workbench rather than once per panel. The `/tree`
 *     route fetches it in its own page effect; a dock with a treemap panel and a results panel would
 *     otherwise fetch it twice.
 *   * `selectedTopic` is how the treemap panel talks to the results panel. They are siblings mounted
 *     into different DOM subtrees with no component relationship at all, so a prop cannot carry it —
 *     which is exactly the case the `/tree` page solves today by nesting both inside one
 *     `ResizableSplit`. Docking dissolves that nesting, so the state moves here.
 */
import { getTopics, type TopicNode } from '@rask/media-api';

export type TopicPhase = 'loading' | 'ready' | 'unavailable' | 'error';

export class MediaWorkbench {
	hierarchy = $state<TopicNode | null>(null);
	noiseLabel = $state('');
	phase = $state<TopicPhase>('loading');
	errorMsg = $state<string | null>(null);
	/** The topic whose chunks the results panel shows; `null` = nothing selected yet. */
	selectedTopic = $state<string | null>(null);

	/** The read in flight, if one is — two panels mounting in the same tick is ONE request, not two. */
	#loading: Promise<void> | null = null;

	load(): Promise<void> {
		this.#loading ??= this.#read().finally(() => {
			this.#loading = null;
		});
		return this.#loading;
	}

	async #read(): Promise<void> {
		try {
			const res = await getTopics();
			if (!res.built || !res.hierarchy) {
				this.phase = 'unavailable';
				return;
			}
			this.hierarchy = res.hierarchy;
			this.noiseLabel = res.noise_label ?? ''; // '' (no match) on an older backend
			this.phase = 'ready';
		} catch (e) {
			this.errorMsg = e instanceof Error ? e.message : String(e);
			this.phase = 'error';
		}
	}
}
