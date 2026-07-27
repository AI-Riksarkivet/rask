/**
 * App-level descriptor store: loads the active dataset's {@link DatasetView}
 * once at startup and publishes it (both reactively and via the module-level
 * active-view singleton the pure helpers read).
 *
 * Which dataset: a `?dataset=<id>` query param selects a non-default dataset
 * (the acid test serves the same build at `/` and `/?dataset=smoke`); with no
 * param the backend's default DB is used, its id derived from the health
 * endpoint's db path.
 */

import { getDatasetView } from '@rask/media-api';
import { setActiveView, type DatasetView } from '@rask/media-api/descriptor';
import { serviceHealth } from '$lib/service-health.svelte';

class DescriptorStore {
	view = $state<DatasetView | null>(null);
	error = $state<string | null>(null);

	/** The dataset id from `?dataset=`, or null for the default DB. */
	private paramId(): string | null {
		if (typeof location === 'undefined') return null;
		return new URLSearchParams(location.search).get('dataset');
	}

	async load(): Promise<void> {
		try {
			const param = this.paramId();
			let id: string;
			let isDefault: boolean;
			if (param) {
				id = param;
				isDefault = false;
			} else {
				// Through the shared store, not a direct `getHealth()`: this is the zone's ONLY health
				// fetcher, so the sidebar badge, the search-bar mode selector and this derivation all
				// read one response. Fetching it here independently made a third request for the same
				// endpoint on every page load, and let the badge and the modes disagree about whether
				// search works — the exact drift `service-health.svelte.ts` documents as the reason it
				// is a single store.
				const health = await serviceHealth.ensure();
				if (health === null) throw new Error(serviceHealth.error ?? 'health unavailable');
				id =
					health.db.path
						.replace(/\.lance$/, '')
						.split('/')
						.pop() ?? '';
				isDefault = true;
			}
			const view = await getDatasetView(id, isDefault);
			setActiveView(view);
			this.view = view;
			// Test hook: expose the active dataset id + identity so an e2e check can
			// prove which dataset the same build is currently rendering.
			if (typeof window !== 'undefined') {
				(window as unknown as { __activeDataset?: unknown }).__activeDataset = {
					id: view.id,
					keyFields: view.keyFields,
					hasTime: view.hasTime,
				};
			}
		} catch (e) {
			this.error = e instanceof Error ? e.message : String(e);
		}
	}
}

export const descriptor = new DescriptorStore();
